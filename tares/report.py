"""The factory's weekly figures per project (TR-1666).

`report_weeks` (the session store) holds one row per project and ISO week in UTC, so a report page
reads those rows and no raw ones. They are worked out from the factory's records (read through the
Store's factory methods) and from `session_usage`, so a rebuild can always write them again. A
background pass keeps them current (`run`).

A pass reads every source once, works out all the (project, week) rows in memory and writes only the
ones that differ. A late event changes an older week (an answer, a fix, a later review round, a long
stage that just ended), and working everything out again cannot miss one.

The columns of a row. Project "" holds what belongs to no project; every duration is in hours:

    merged               tickets whose first change to merged fell in the week
    start_to_merge       [h, ...] first doing to first merged, for those of them that have both
    stage_hours          {stage: {"total": h, "waiting": h}} for doing, review, changes and
                         blocked: finished intervals only, split at Monday 00:00; `waiting` is the
                         part during which an answered blocking question about the ticket was open
    waited_hours         how long tickets waited on a person: for each ticket, the time at least one
                         answered blocking question about it was open (two open at once count once;
                         a question naming no ticket adds nothing), added up over the tickets
    milestones_accepted  milestones whose latest acceptance fell in the week
    reviewed             tickets whose first review round fell in the week
    passed_first         those of them whose first round passed
    rounds               [n, ...] review rounds to date, one number for each of those tickets
    findings             [{kind, severity, blocking, opened, opened_open, opened_fixed, fixed}]:
                         `opened` were found in the week and `opened_open` and `opened_fixed` say
                         where they stand now; `fixed` were fixed by a review made in the week
    questions            {asked, blocking, answered, answer_hours: [h, ...]}: every desk item, in
                         the week it was asked
    cost_usd             API-equivalent cost of the week's priced replies
    unpriced             the week's replies that have no price
    cost_by_role         {role: {cost_usd, replies, unpriced}}; "person" is a person's own session
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from itertools import groupby

from sqlalchemy import delete, select

from . import factory as factory_mod
from .envelope import now_utc
from .session_store.tables import report_weeks

EVERY = 5 * 60.0          # seconds between passes
FIRST_AFTER = 30.0        # after a start, so startup work goes first
BUILT_SETTING = "report.weeks_built"   # set once the cell's first full build has run
STAGES = ("doing", "review", "changes", "blocked")   # the stages whose time is counted
NO_PROJECT = ""
PERSON = "person"         # the role of a session that is no station's
JSON_COLUMNS = ("start_to_merge", "stage_hours", "rounds", "findings", "questions", "cost_by_role")
_ZERO = timedelta()
_PASS = threading.Lock()  # one pass at a time: an older read must not overwrite a newer one


# ── the four decided rules (D50), each in one place ──────────────────────────
# Cost, the week, what counts as waiting and what counts as a round. With them: a stage that has
# not ended and a question not answered yet add no time to a week's totals.
def week_of(at: date | datetime) -> date:
    """The week rule: a figure belongs to the ISO week, in UTC, of the moment it happened, and a
    week is named by its Monday. (`usage_by_week` groups replies the same way in SQL.)"""
    day = _utc(at).date() if isinstance(at, datetime) else at
    return day - timedelta(days=day.weekday())


def split_weeks(start: datetime, end: datetime):
    """The week rule for a span of time: (week, from, to) for each week it touches, cut at
    Monday 00:00 UTC. Nothing for a span that does not move forward."""
    start, end = _utc(start), _utc(end)
    while start < end:
        week = week_of(start)
        cut = min(end, datetime.combine(week + timedelta(days=7), time(), timezone.utc))
        yield week, start, cut
        start = cut


def cost_project(project: str | None, live: set[str]) -> str:
    """Where a reply's cost goes. `project` is the one `usage_by_week` names, which is none for a
    crew station's unstamped reply and for a reply nothing claims. None, or a project that is no
    more, goes to the "" rows, so the weeks still add up to everything spent."""
    return project if project in live else NO_PROJECT


def waiting_span(item: dict) -> tuple[datetime, datetime] | None:
    """When a desk item held work up for a person: from asked to answered, for a blocking item
    that was answered. Open and withdrawn items, and ones that block nothing, never count. A merge
    that waits for approval is such an item already, so no merge queue time is added."""
    if not (item["blocking"] and item["state"] == "answered"
            and item["created_at"] and item["closed_at"]):
        return None
    return _utc(item["created_at"]), _utc(item["closed_at"])


def counted_rounds(reviews: list[dict]) -> list[dict]:
    """The reviews of a ticket that are rounds: all but the merge queue's automatic passes."""
    return [r for r in reviews if not factory_mod.automatic_pass(r)]


# ── percentiles over the stored lists ─────────────────────────────────────────
def percentile(values: list[float], q: float) -> float | None:
    """The `q` (0 to 1) percentile as SQL's percentile_cont gives it: linear interpolation between
    the two nearest of the sorted values. None for no values."""
    xs = sorted(values)
    if not xs:
        return None
    at = q * (len(xs) - 1)
    low = int(at)
    high = min(low + 1, len(xs) - 1)
    return xs[low] + (xs[high] - xs[low]) * (at - low)


def percentiles(values: list[float]) -> dict[str, float | None]:
    """{p50, p75, p90} of the values, for a list joined from any number of rows."""
    return {f"p{n}": percentile(values, n / 100) for n in (50, 75, 90)}


# ── working the figures out ───────────────────────────────────────────────────
def _utc(at: datetime) -> datetime:
    """DuckDB hands times back in the local zone; a time with no zone is UTC."""
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def _hours(span: timedelta) -> float:
    return round(span.total_seconds() / 3600, 4)


def _cell() -> dict:
    return {"merged": 0, "start_to_merge": [], "stages": {s: [_ZERO, _ZERO] for s in STAGES},
            "waited": _ZERO, "milestones": 0, "reviewed": 0, "passed_first": 0, "rounds": [],
            "findings": {}, "asked": 0, "blocking": 0, "answered": 0, "answer_hours": [],
            "cost": {}}


def _union(spans: list[tuple]) -> list[tuple]:
    """The spans with the overlapping ones joined, so no moment is counted twice."""
    out: list[tuple] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _ticket_waits(items: list[dict], tickets: dict[str, list[dict]]) -> dict[tuple, list[tuple]]:
    """{(project, ticket id): the joined spans it waited on a person}. A desk item names tickets by
    their label (T3, ENG-12) within its project; a deleted ticket's label names nothing any more."""
    ids: dict[tuple[str, str], str] = {}
    for project, rows in tickets.items():
        for t in rows:
            for ref in (t["id"], t.get("identifier")):
                if ref:
                    ids[project, ref.upper()] = t["id"]
        for t in rows:      # after, so a number never takes an identifier's place
            if t.get("number"):
                ids.setdefault((project, f"T{t['number']}"), t["id"])
    waits: dict[tuple, list[tuple]] = defaultdict(list)
    for item in items:
        span = waiting_span(item)
        if span:
            for ref in item["tickets"]:
                tid = ids.get((item["project"], str(ref).upper()))
                if tid:
                    waits[item["project"], tid].append(span)
    return {key: _union(spans) for key, spans in waits.items()}


def _add_waited(cells, waits: dict[tuple, list[tuple]]) -> None:
    """The time tickets waited on a person, from the same spans the stage split uses."""
    for (project, _), spans in waits.items():
        for span in spans:
            for week, start, end in split_weeks(*span):
                cells[project, week]["waited"] += end - start


def _add_stages(cells, changes: list[dict], waits: dict[tuple, list[tuple]], live: set[str]) -> None:
    """Tickets merged, start to merge, and the time spent in each stage."""
    for ticket, rows in groupby(changes, key=lambda c: c["ticket"]):
        moves: list[dict] = []
        for row in rows:        # every set_stage writes a row: a repeated stage is no move
            if not moves or moves[-1]["value"] != row["value"]:
                moves.append(row)
        first = {}
        for move in moves:
            first.setdefault(move["value"], move)
        merged, doing = first.get("merged"), first.get("doing")
        if merged and merged["project"] in live:
            cell = cells[merged["project"], week_of(merged["at"])]
            cell["merged"] += 1
            if doing and _utc(doing["at"]) < _utc(merged["at"]):
                cell["start_to_merge"].append(_hours(_utc(merged["at"]) - _utc(doing["at"])))
        for move, following in zip(moves, moves[1:]):
            if move["value"] not in STAGES or move["project"] not in live:
                continue
            spans = waits.get((move["project"], ticket), [])
            for week, start, end in split_weeks(move["at"], following["at"]):
                spent = cells[move["project"], week]["stages"][move["value"]]
                spent[0] += end - start
                spent[1] += sum((max(min(end, b) - max(start, a), _ZERO) for a, b in spans), _ZERO)


def _add_desk(cells, items: list[dict], live: set[str]) -> None:
    """Questions asked and answered, in the week they were asked."""
    for item in items:
        project = item["project"] or NO_PROJECT
        if project != NO_PROJECT and project not in live:
            continue
        if item["created_at"]:
            cell = cells[project, week_of(item["created_at"])]
            cell["asked"] += 1
            cell["blocking"] += int(item["blocking"])
            if item["state"] == "answered":
                cell["answered"] += 1
                if item["closed_at"]:
                    cell["answer_hours"].append(
                        _hours(_utc(item["closed_at"]) - _utc(item["created_at"])))


def _finding(cell: dict, key: tuple) -> dict:
    return cell["findings"].setdefault(key, {"opened": 0, "opened_open": 0, "opened_fixed": 0,
                                             "fixed": 0})


def _add_reviews(cells, reviews: list[dict], findings: list[dict], live: set[str]) -> None:
    """Review rounds, in the week of a ticket's first, and findings opened and fixed."""
    for _, rows in groupby(reviews, key=lambda r: r["ticket"]):
        rounds = counted_rounds(list(rows))
        if not rounds or rounds[0]["project"] not in live:
            continue
        first = rounds[0]
        cell = cells[first["project"], week_of(first["at"])]
        cell["reviewed"] += 1
        cell["passed_first"] += int(first["verdict"] == "pass")
        cell["rounds"].append(len(rounds))
    reviewed_at = {r["id"]: r["at"] for r in reviews}
    for f in findings:
        if f["project"] not in live:
            continue
        key = (f["kind"] or "", f["severity"] or "", f["blocking"])
        if f["found_at"]:
            group = _finding(cells[f["project"], week_of(f["found_at"])], key)
            group["opened"] += 1
            group["opened_fixed" if f["state"] == "fixed" else "opened_open"] += 1
        if f["state"] == "fixed" and f["fixed_in"] in reviewed_at:
            week = week_of(reviewed_at[f["fixed_in"]])
            _finding(cells[f["project"], week], key)["fixed"] += 1


def _add_milestones(cells, store, live: set[str]) -> None:
    project_of = {m["id"]: project
                  for project, rows in store.milestones_by_project(sorted(live)).items()
                  for m in rows}
    for mid, accepted in store.milestone_acceptances(list(project_of)).items():
        if accepted["at"]:
            cells[project_of[mid], week_of(accepted["at"])]["milestones"] += 1


def _add_cost(cells, usage: list[dict], live: set[str]) -> None:
    for u in usage:
        cell = cells[cost_project(u["project"], live), u["week"]]
        role = cell["cost"].setdefault(u["role"] or PERSON, [0.0, 0, 0])
        role[0] += u["cost_usd"]
        role[1] += u["replies"]
        role[2] += u["unpriced"]


def _row(cell: dict) -> dict:
    """A cell as the table's columns (all but the key and updated_at), the lists sorted so the
    same records always give the same text."""
    values = {
        "merged": cell["merged"],
        "start_to_merge": sorted(cell["start_to_merge"]),
        "stage_hours": {s: {"total": _hours(total), "waiting": _hours(waiting)}
                        for s, (total, waiting) in cell["stages"].items()},
        "waited_hours": _hours(cell["waited"]),
        "milestones_accepted": cell["milestones"],
        "reviewed": cell["reviewed"],
        "passed_first": cell["passed_first"],
        "rounds": sorted(cell["rounds"]),
        "findings": [{"kind": kind, "severity": severity, "blocking": blocking, **counts}
                     for (kind, severity, blocking), counts in sorted(cell["findings"].items())],
        "questions": {"asked": cell["asked"], "blocking": cell["blocking"],
                      "answered": cell["answered"],
                      "answer_hours": sorted(cell["answer_hours"])},
        "cost_usd": round(sum(v[0] for v in cell["cost"].values()), 6),
        "unpriced": sum(v[2] for v in cell["cost"].values()),
        "cost_by_role": {role: {"cost_usd": round(v[0], 6), "replies": v[1], "unpriced": v[2]}
                         for role, v in cell["cost"].items()},
    }
    for column in JSON_COLUMNS:
        values[column] = json.dumps(values[column], sort_keys=True, separators=(",", ":"))
    return values


def _live(store) -> set[str]:
    return {p["id"] for p in store.list_projects()}


def _compute(store, week: date | None = None) -> dict[tuple[str, date], dict]:
    """{(project, week): row} from one read of each source, no query per cell. With `week`, the
    replies are read for that week only, so only that week's rows are whole."""
    live = _live(store)
    cells: dict = defaultdict(_cell)
    items = store.list_desk(None)
    waits = _ticket_waits(items, store.tickets_by_project(sorted(live)))
    _add_stages(cells, store.all_stage_changes(), waits, live)
    _add_waited(cells, waits)
    _add_desk(cells, items, live)
    _add_reviews(cells, store.all_reviews(), store.all_review_findings(), live)
    _add_milestones(cells, store, live)
    since = until = None
    if week is not None:
        since = datetime.combine(week, time(), timezone.utc)
        until = since + timedelta(days=7)
    _add_cost(cells, store.usage_by_week(since, until), live)
    return {key: _row(cell) for key, cell in cells.items()}


# ── the table ─────────────────────────────────────────────────────────────────
def _write(store, rows: dict[tuple[str, date], dict], only: tuple[str, date] | None = None) -> dict:
    """Make the table (or the one row `only` names) hold exactly `rows`: a row that differs is
    written, a row with nothing to say any more goes. Returns {rows, changed, removed}."""
    t = report_weeks
    scope = [] if only is None else [t.c.project == only[0], t.c.week == only[1]]
    at = now_utc()
    with store.sessions_db.begin() as conn:
        have = {(r.project, r.week): r._mapping for r in conn.execute(select(t).where(*scope))}
        changed = [{"project": key[0], "week": key[1], "updated_at": at, **row}
                   for key, row in rows.items()
                   if key not in have or any(have[key][c] != v for c, v in row.items())]
        gone = [key for key in have if key not in rows]
        for project, week in gone:
            conn.execute(delete(t).where(t.c.project == project, t.c.week == week))
        if changed:
            insert = store.sessions_db.upsert(t)
            conn.execute(insert.on_conflict_do_update(
                index_elements=[t.c.project, t.c.week],
                set_={c.name: insert.excluded[c.name] for c in t.columns if not c.primary_key}),
                changed)
    if changed:
        # a project deleted since its records were read must not come back: delete_project takes
        # the project away first and its rows after, so one of the two sees the other
        keep = sorted(_live(store) | {NO_PROJECT})
        with store.sessions_db.begin() as conn:
            conn.execute(delete(t).where(t.c.project.not_in(keep)))
    return {"rows": len(rows), "changed": len(changed), "removed": len(gone)}


def rebuild_all(store) -> dict:
    """Work out every week of every project again. Returns {rows, changed, removed}."""
    with _PASS:
        return _write(store, _compute(store))


def rebuild_week(store, project: str, week: date | datetime) -> dict:
    """Work out one project's one week again (`week` is any day or moment in it; project "" is
    what belongs to none). Returns {rows, changed, removed}."""
    key = (project or NO_PROJECT, week_of(week))
    with _PASS:
        rows = _compute(store, key[1])
        return _write(store, {key: rows[key]} if key in rows else {}, only=key)


def refresh_recent(store) -> dict:
    """Bring the table up to date with what happened lately, in whichever week it counts. A late
    event can change any older week and working them all out is cheap, so this is a full pass
    that writes only what differs."""
    return rebuild_all(store)


def read_weeks(store, projects: list[str] | None = None, from_: date | datetime | None = None,
               to: date | datetime | None = None) -> list[dict]:
    """The rows of `projects` (None: all of them; "" asks for what belongs to no project) from
    the week `from_` is in to the week `to` is in, both included (None: no limit), ordered by week
    and project, with the JSON columns parsed and `week` its Monday as a date."""
    if projects is not None and not projects:
        return []
    t = report_weeks
    q = select(t).order_by(t.c.week, t.c.project)
    if projects is not None:
        q = q.where(t.c.project.in_(list(projects)))
    if from_ is not None:
        q = q.where(t.c.week >= week_of(from_))
    if to is not None:
        q = q.where(t.c.week <= week_of(to))
    with store.sessions_db.begin() as conn:
        rows = [dict(r._mapping) for r in conn.execute(q)]
    for row in rows:
        for column in JSON_COLUMNS:
            row[column] = json.loads(row[column])
    return rows


# ── the background pass ───────────────────────────────────────────────────────
def refresh(store) -> dict:
    """One background pass. The first one on a cell says so, once, and marks it in a setting."""
    out = refresh_recent(store)
    if not store.get_setting(BUILT_SETTING):
        store.set_setting(BUILT_SETTING, "1")
        print(f"taresd: report weeks built ({out['rows']} rows)", flush=True)
    return out


async def run(store, stop: asyncio.Event, every: float = EVERY, first: float = FIRST_AFTER) -> None:
    delay = first
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        delay = every
        try:
            await asyncio.to_thread(refresh, store)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"taresd: report pass failed ({type(e).__name__}: {e})", flush=True)
