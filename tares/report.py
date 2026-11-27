"""The factory's weekly figures per project (TR-1666).

`report_weeks` (the session store) holds one row per project and ISO week in UTC, so a report page
reads those rows and no raw ones. They are worked out from the factory's records (read through the
Store's factory methods) and from `session_usage`, so a rebuild can always write them again. A
background pass keeps them current (`run`).

A pass reads every source once, works out all the (project, week) rows in memory and writes only the
ones that differ. A late event changes an older week (an answer, a fix, a later review round, a long
stage that just ended), and working everything out again cannot miss one. A full pass also writes two
things that are no week's figure: what is stuck at that moment into the setting `report.stuck`
(`_stuck`), and the findings that recur into `report.recurring` (`_recurring`).

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
import re
import threading
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from itertools import groupby
from typing import Callable

from sqlalchemy import delete, select

from . import factory as factory_mod
from .envelope import now_utc
from .session_store.tables import report_weeks

EVERY = 5 * 60.0          # seconds between passes
FIRST_AFTER = 30.0        # after a start, so startup work goes first
BUILT_SETTING = "report.weeks_built"   # set once the cell's first full build has run
STAGES = ("doing", "review", "changes", "blocked")   # the stages whose time is counted
STUCK_SETTING = "report.stuck"   # what was stuck at the last full pass
STUCK_SHOWN = 5                  # how many of each kind "stuck now" lists
RECURRING_SETTING = "report.recurring"   # the findings that recurred at the last full pass
RECURRING_SHOWN = 5                      # how many recurring findings the page lists
NAMED_KINDS = frozenset(kind for kind, _ in factory_mod.FINDING_KINDS)
BUCKET_EDGES = (1, 4, 24, 72)    # start to merge, hours; a time on an edge is in the bucket above it
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
    """The same moment in UTC; a time with no zone is UTC."""
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


def _read(store) -> dict:
    """The records a pass works from that both the weekly rows and "stuck now" need."""
    projects = store.list_projects()
    ids = sorted(p["id"] for p in projects)
    return {"projects": projects, "desk": store.list_desk(None),
            "tickets": store.tickets_by_project(ids), "changes": store.all_stage_changes(),
            "handovers": store.handovers_by_project(ids)}


def _compute(store, week: date | None = None,
             read: dict | None = None) -> dict[tuple[str, date], dict]:
    """{(project, week): row} from one read of each source, no query per cell. With `week`, the
    replies are read for that week only, so only that week's rows are whole."""
    read = read or _read(store)
    live = {p["id"] for p in read["projects"]}
    cells: dict = defaultdict(_cell)
    items = read["desk"]
    waits = _ticket_waits(items, read["tickets"])
    _add_stages(cells, read["changes"], waits, live)
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


# ── stuck now ─────────────────────────────────────────────────────────────────
def _stamp(at: datetime) -> str:
    """A moment as the snapshot writes it: all one width, so the text sorts as the moments do."""
    return _utc(at).isoformat(timespec="seconds")


def _oldest(row: dict) -> tuple:
    return row["at"], row["number"] or 0, row["label"]


def _stage_since(changes: list[dict], stage: str) -> datetime | None:
    """When a ticket came into `stage`: the first change of the last run of that value in its
    history (every set_stage writes a change, and a repeated stage is no move). None when the
    history does not end in that stage."""
    since = None
    for change in reversed(changes):
        if change["value"] != stage:
            break
        since = change["at"]
    return since


def _stuck(read: dict, at: datetime) -> dict:
    """What is stuck at `at`, kept per project so page() can merge the ones a viewer reads:
    {at, projects: {id: {questions, tickets}}, unfiled}. Each of the two is {count, top}, `top`
    the STUCK_SHOWN oldest. `unfiled` holds every question of no project with whom it is for,
    because who reads one is decided per viewer. A question is stuck when it is open and marked
    blocking; a ticket, when it sits in one of STAGES. Neither is, in a project that is finished
    or kept for later. Text is worded as the project page words it (no folder of the crew's
    machine)."""
    from .screens.factory_project import cleaner   # the screens import much of the daemon
    live = {p["id"]: p for p in read["projects"]}
    clean = {pid: cleaner(read["handovers"].get(pid)) for pid in live}
    resting = {pid for pid, p in live.items()
               if p["status"] in ("done", "later")
               or (read["handovers"].get(pid) or {}).get("finished")}
    questions: dict[str, list[dict]] = defaultdict(list)
    unfiled = []
    for d in read["desk"]:
        if d["state"] != "open" or not d["blocking"] or not d["created_at"]:
            continue
        pid = d["project"]
        row = {"number": d["number"], "label": d["label"], "at": _stamp(d["created_at"])}
        if not pid:
            unfiled.append({**row, "text": d["question"], "project": None,
                            "to_person_id": d.get("to_person_id")})
        elif pid in live and pid not in resting:
            questions[pid].append({**row, "text": clean[pid](d["question"])})
    history = {ticket: list(rows)
               for ticket, rows in groupby(read["changes"], key=lambda c: c["ticket"])}
    tickets: dict[str, list[dict]] = defaultdict(list)
    for pid, rows in read["tickets"].items():
        if pid in resting:
            continue
        for t in rows:
            if t["stage"] not in STAGES or factory_mod.state_of(t) in ("merged", "canceled"):
                continue
            since = _stage_since(history.get(t["id"], []), t["stage"])
            if since:
                tickets[pid].append({"number": t.get("number"), "label": factory_mod.label(t),
                                     "title": clean[pid](t["title"]), "stage": t["stage"],
                                     "at": _stamp(since)})

    def top(rows: list[dict]) -> dict:
        return {"count": len(rows), "top": sorted(rows, key=_oldest)[:STUCK_SHOWN]}

    return {"at": _stamp(at), "unfiled": unfiled,
            "projects": {pid: {"questions": top(questions.get(pid, [])),
                               "tickets": top(tickets.get(pid, []))}
                         for pid in sorted(set(questions) | set(tickets))}}


def _stuck_now(store, visible: dict[str, dict], ids: list[str],
               asks_me: Callable[[dict], bool] | None) -> dict | None:
    """The last pass's "stuck now" as this viewer reads it: only the projects in `ids` (all of
    them in `visible`), and the questions of no project that `asks_me` lets through (None: the
    scope holds none). Counted and cut after that, so nothing else is named or counted. None
    before the first pass."""
    saved = store.get_setting(STUCK_SETTING)
    if not saved:
        return None
    snapshot = json.loads(saved)
    out: dict = {"at": snapshot["at"]}
    for kind in ("questions", "tickets"):
        count, rows = 0, []
        for pid in ids:
            part = snapshot["projects"].get(pid)
            if part:
                count += part[kind]["count"]
                rows += [{**row, "project": {"id": pid, "name": visible[pid]["name"]}}
                         for row in part[kind]["top"]]
        if kind == "questions" and asks_me:
            for row in snapshot["unfiled"]:
                if asks_me(row):
                    count += 1
                    rows.append({k: v for k, v in row.items() if k != "to_person_id"})
        out[kind] = {"count": count, "rows": sorted(rows, key=_oldest)[:STUCK_SHOWN]}
    return out


# ── recurring findings ────────────────────────────────────────────────────────
def _recurring(store, read: dict, at: datetime) -> dict:
    """What a full pass saves of the recurring findings: for each repo a ticket's PR names, each
    kind found on RECUR_MIN or more of its tickets in the RECUR_DAYS before `at`, with the
    (project id, ticket label) pairs it was found on, so a read can count its own projects."""
    tickets = {(pid, t["id"]): t for pid, rows in read["tickets"].items() for t in rows}
    since = at - timedelta(days=factory_mod.RECUR_DAYS)
    out = []
    for repo in sorted({(t["pr"] or {}).get("repo") or "" for t in tickets.values()} - {""}):
        found = factory_mod.recurring_refs(store.repo_findings(repo, since),
                                           lambda *key: tickets.get(key))
        out += [{"repo": repo, "kind": kind, "refs": sorted(k["refs"])}
                for kind, k in found.items() if len(k["refs"]) >= factory_mod.RECUR_MIN]
    return {"at": _stamp(at), "rows": out}


def _recurring_now(store, visible: dict[str, dict], ids: list[str],
                   accepted_rules: Callable[[], set[tuple]]) -> dict | None:
    """The last pass's recurring findings over the projects in `ids` (all of them in `visible`): a
    row per repo and kind, counted over those projects and only then held to RECUR_MIN; the
    RECURRING_SHOWN highest counts. A row is `rule_accepted` when `accepted_rules` has its repo
    and kind, whatever the scope. None before the first pass."""
    saved = store.get_setting(RECURRING_SETTING)
    if not saved:
        return None
    snapshot = json.loads(saved)
    names = {i: visible[i]["name"] for i in ids}
    rows = []
    for part in snapshot["rows"]:
        refs = [(pid, ticket) for pid, ticket in part["refs"] if pid in names]
        row = factory_mod.recurring_row(part["kind"], refs, names)
        if row:
            rows.append({"repo": part["repo"], **row})
    rows.sort(key=lambda r: (-r["count"], r["repo"], r["kind"] or ""))
    rows = rows[:RECURRING_SHOWN]
    ruled = accepted_rules() if rows else set()
    return {"at": snapshot["at"], "days": factory_mod.RECUR_DAYS,
            "rows": [{**r, "rule_accepted": (r["repo"], r["kind"]) in ruled} for r in rows]}


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
    """Work out every week of every project again, what is stuck now and the findings that recur.
    Returns {rows, changed, removed} of the weekly rows."""
    with _PASS:
        at = now_utc()
        read = _read(store)
        out = _write(store, _compute(store, read=read))
        for key, value in ((STUCK_SETTING, _stuck(read, at)),
                           (RECURRING_SETTING, _recurring(store, read, at))):
            store.set_setting(key, json.dumps(value, sort_keys=True, separators=(",", ":")))
        return out


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
    with store.sessions_db.read() as conn:
        rows = [dict(r._mapping) for r in conn.execute(q)]
    for row in rows:
        for column in JSON_COLUMNS:
            row[column] = json.loads(row[column])
    return rows


# ── the Report page's one read ────────────────────────────────────────────────
DEFAULT_WEEKS = 8
MAX_YEARS = 2         # between the dates as written; widened to whole weeks it can be 106 columns
FIRST_YEAR = 2000     # an earlier date is a typo, and the week arithmetic stays in range
CANNOT_SEE = (
    "Work done outside Tares.",
    "Pushes that skipped the merge queue.",
    "Your bill: cost is API-equivalent (list prices), not what a subscription charges.",
)


def _review_figures(rows: list[dict]) -> dict:
    """The pass rate and rounds of the tickets first reviewed in the weekly rows `rows`, as the
    Delivered and Review sections both say them."""
    rounds = [n for r in rows for n in r["rounds"]]
    return {
        "first_pass": {"passed": sum(r["passed_first"] for r in rows),
                       "reviewed": sum(r["reviewed"] for r in rows)},
        "rounds_per_ticket": sum(rounds) / len(rounds) if rounds else None,
    }


def _delivered_figures(rows: list[dict]) -> dict:
    """What was delivered in the weekly rows `rows`. Durations are in hours."""
    hours = [h for r in rows for h in r["start_to_merge"]]
    return {
        "merged": sum(r["merged"] for r in rows),
        "milestones_accepted": sum(r["milestones_accepted"] for r in rows),
        "projects_merged": len({r["project"] for r in rows if r["merged"]}),
        "start_to_merge": {"p50": percentile(hours, 0.5), "p90": percentile(hours, 0.9),
                           "tickets": len(hours)},      # of `merged`: those with a start
        **_review_figures(rows),
    }


def delivered(rows: list[dict], weeks: list[date], before: list[dict] | tuple = ()) -> dict:
    """The Delivered section (TR-1668): the figures of the range, the same figures for the rows
    `before` it (None when no project has a row there: cost nothing claims is no figure of what
    was delivered), and what merged week by week."""
    by_week = defaultdict(list)
    for r in rows:
        by_week[r["week"]].append(r)
    return {
        **_delivered_figures(rows),
        "previous": _delivered_figures(before)
                    if any(r["project"] != NO_PROJECT for r in before) else None,
        "weeks": [{"week": w.isoformat(),
                   "merged": sum(r["merged"] for r in by_week[w]),
                   "p50": percentile([h for r in by_week[w] for h in r["start_to_merge"]], 0.5)}
                  for w in weeks],
    }


def _finding_split(rows: list[dict]) -> list[dict]:
    """The findings first raised in the weekly rows `rows`, by kind and severity as they stand
    today, fixed and still open, most open first. The kinds FINDING_KINDS does not name are one
    "other" row per severity."""
    counts = defaultdict(lambda: [0, 0])
    for r in rows:
        for f in r["findings"]:
            kind = f["kind"] if f["kind"] in NAMED_KINDS else "other"
            count = counts[kind, f["severity"]]
            count[0] += f["opened_fixed"]
            count[1] += f["opened_open"]
    out = [{"kind": kind, "severity": severity, "fixed": fixed, "open": still}
           for (kind, severity), (fixed, still) in counts.items() if fixed or still]
    return sorted(out, key=lambda f: (-f["open"], f["severity"], f["kind"]))


def review(rows: list[dict], weeks: list[date], before: list[dict] | tuple = ()) -> dict:
    """The Review section (TR-1670): the pass rate and rounds of the range and week by week, its
    findings, and the same two figures for each project with a ticket first reviewed in it, with
    those of the rows `before` it (None when none was reviewed there). page() adds the projects'
    names, puts them in order and adds the recurring findings."""
    by_week, of_range, earlier = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_week[r["week"]].append(r)
        of_range[r["project"]].append(r)
    for r in before:
        earlier[r["project"]].append(r)
    projects = []
    for pid, its in of_range.items():
        figures = _review_figures(its)
        if pid == NO_PROJECT or not figures["first_pass"]["reviewed"]:
            continue
        previous = _review_figures(earlier.get(pid, []))
        projects.append({"project": pid, **figures,
                         "previous": previous if previous["first_pass"]["reviewed"] else None})
    return {
        **_review_figures(rows),
        "weeks": [{"week": w.isoformat(), **_review_figures(by_week[w])} for w in weeks],
        "findings": _finding_split(rows),
        "projects": projects,
    }


def _percents(parts: list[float]) -> list[int | None]:
    """The parts (hours, as the rows hold them) as whole percents that add to 100: each rounded
    down, the points left over going to the largest remainders, a tie to the earlier part. All
    None when the parts add to nothing."""
    units = [round(p * 10_000) for p in parts]
    total = sum(units)
    if not total:
        return [None] * len(units)
    out = [u * 100 // total for u in units]
    for i in sorted(range(len(units)), key=lambda i: (-(units[i] * 100 % total), i))[:100 - sum(out)]:
        out[i] += 1
    return out


def _stage_sums(rows: list[dict]) -> dict[str, tuple[float, float]]:
    """{stage: (total, waiting)} hours over the rows."""
    return {s: tuple(round(sum(r["stage_hours"][s][part] for r in rows), 4)
                     for part in ("total", "waiting")) for s in STAGES}


def time_spent(rows: list[dict], weeks: list[date]) -> dict:
    """The Time section (TR-1669): where the time of the four stages went, over the range and week
    by week, each stage split into waiting on a person and agent time (all the rest), and how long
    the merged tickets took from start to merge. Durations are in hours, percents whole; one with
    nothing to divide is None. page() adds what is stuck now."""
    sums = _stage_sums(rows)
    stages = []
    for stage, share in zip(STAGES, _percents([sums[s][0] for s in STAGES])):
        total, waiting = sums[stage]
        agent = round(total - waiting, 4)
        waiting_percent, agent_percent = _percents([waiting, agent])
        stages.append({"stage": stage, "total": total, "waiting": waiting, "agent": agent,
                       "share": share, "waiting_percent": waiting_percent,
                       "agent_percent": agent_percent})
    by_week = defaultdict(list)
    for r in rows:
        by_week[r["week"]].append(r)
    each = []
    for w in weeks:
        of = _stage_sums(by_week[w])
        each.append({"week": w.isoformat(),
                     "share": dict(zip(STAGES, _percents([of[s][0] for s in STAGES]))),
                     "waiting": {s: _percents([of[s][1], of[s][0] - of[s][1]])[0]
                                 for s in STAGES}})
    total = round(sum(t for t, _ in sums.values()), 4)
    waiting = sum(w for _, w in sums.values())
    hours = [h for r in rows for h in r["start_to_merge"]]
    buckets = [0] * (len(BUCKET_EDGES) + 1)
    for h in hours:
        buckets[bisect_right(BUCKET_EDGES, h)] += 1
    return {"total": total, "waiting_percent": _percents([waiting, total - waiting])[0],
            "stages": stages, "weeks": each,
            "start_to_merge": {"merged": sum(r["merged"] for r in rows),
                               "tickets": len(hours),     # of `merged`: those with a start
                               "buckets": buckets}}


# The sections of the page, in its order. A section's ticket puts its builder in its line:
# f(rows, weeks) -> what `sections[key]` answers, from the weekly rows of the scope and range
# (read_weeks) and the Mondays of the range. A builder named in COMPARED is also handed the rows
# of as many weeks just before the range, in the same scope: f(rows, weeks, before). A builder
# reads nothing else; page() itself adds what is not a week's figure: the last pass's "stuck now"
# to the time section and its recurring findings to the review section.
COMPARED = {"delivered", "review"}
SECTIONS: dict[str, Callable[..., dict] | None] = {
    "delivered": delivered,

    "time": time_spent,

    "review": review,

    "cost": None,

    "merges": None,

    "architecture": None,
}


class UnknownScope(LookupError):
    """A scope that names nothing the caller can read: an unknown id and a hidden one are alike."""


def _day(value: str) -> date:
    try:
        day = date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    except ValueError:
        day = None
    if day is None or day.year < FIRST_YEAR:
        raise ValueError("A date is written as year-month-day, for example 2026-11-25.")
    return day


def _years_before(day: date, years: int) -> date:
    """The same day `years` earlier; 28 February when that year has no such day."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def range_weeks(from_: str | None, to: str | None, today: date) -> list[date]:
    """The Mondays of a range, oldest first: every week that has a day in `from_` to `to`, up to
    the week of `today`. With no `to` it ends today; with no `from_` it is the last
    DEFAULT_WEEKS weeks up to `to`. Raises ValueError in words for a range there is no report of."""
    first = _day(from_) if from_ else None
    last = _day(to) if to else None
    if first and last and first > last:
        raise ValueError("The first date is after the last date.")
    if first and first > today:
        raise ValueError("The range starts in the future.")
    until = min(last or today, today)
    if first and first < _years_before(until, MAX_YEARS):
        raise ValueError("Pick a range of 2 years or less.")
    end = week_of(until)
    start = week_of(first) if first else end - timedelta(weeks=DEFAULT_WEEKS - 1)
    return [start + timedelta(weeks=i) for i in range((end - start).days // 7 + 1)]


def page(store, projects: list[dict], mine: Callable[[], set[str] | None], owners,
         scope: str = "factory", from_: str | None = None, to: str | None = None,
         today: date | None = None, asks_me: Callable[[dict], bool] | None = None,
         accepted_rules: Callable[[], set[tuple]] = set) -> dict:
    """What GET /api/factory/report answers. `projects` are the ones the caller can read, and no
    other is ever named or counted. `mine` gives the ids that are the caller's own (None: nothing
    narrows them) and is asked for the "mine" scope only. `owners` (factory_owners.Owners) says
    who is here to lead a team. `asks_me` says whether the caller reads a desk question that
    belongs to no project (None: none). `accepted_rules` gives the (repo, kind) of each accepted
    rule the caller sees (none by default), which marks the recurring findings.

    A team is the projects one person leads, named by that person's id; a cell of one person has
    none. The "" rows (cost no project claims) belong to the whole factory only."""
    today = today or now_utc().date()
    weeks = range_weeks(from_, to, today)
    visible = {p["id"]: p for p in projects}
    factory_projects = sorted((p for p in projects if p["kind"] == "software_factory"),
                              key=lambda p: (p["name"].lower(), p["id"]))
    lead_of = {} if owners.single else {p["id"]: p["lead_person_id"] for p in projects
                                        if p.get("lead_person_id") in owners.alive}
    leads = {lead_of[p["id"]] for p in factory_projects if p["id"] in lead_of}
    teams = sorted(({"id": i, "name": owners.names.get(i) or i} for i in leads),
                   key=lambda t: (t["name"].lower(), t["id"]))
    name = None
    kind, _, key = scope.partition(":")
    if scope == "factory":
        ids, rest = list(visible), True
    elif scope == "mine":
        own = mine()
        ids, rest = [i for i in visible if own is None or i in own], False
    elif kind == "team" and key:
        if key not in leads:
            raise UnknownScope(f"unknown team {key!r}")
        ids, rest = [i for i in visible if lead_of.get(i) == key], False
        name = owners.names.get(key) or key
    elif kind == "project" and key:
        if key not in visible:
            raise UnknownScope(f"unknown project {key!r}")
        ids, rest, name = [key], False, visible[key]["name"]
    else:
        raise ValueError("A scope is mine, factory, team:<id> or project:<id>.")
    # one read for the range and the same number of weeks before it, for the sections that compare
    read = read_weeks(store, ids + ([NO_PROJECT] if rest else []),
                      weeks[0] - timedelta(weeks=len(weeks)), weeks[-1])
    rows = [r for r in read if r["week"] >= weeks[0]]
    before = [r for r in read if r["week"] < weeks[0]]
    running = week_of(today)
    sections = {k: build(rows, weeks, before) if k in COMPARED else build(rows, weeks)
                for k, build in SECTIONS.items() if build}
    sections["time"]["stuck"] = _stuck_now(store, visible, ids, asks_me if rest else None)
    sections["review"]["projects"] = sorted(
        ({**p, "name": visible[p["project"]]["name"]} for p in sections["review"]["projects"]),
        key=lambda p: (p["first_pass"]["passed"] / p["first_pass"]["reviewed"],
                       p["name"].lower(), p["project"]))     # lowest pass rate first
    sections["review"]["recurring"] = _recurring_now(store, visible, ids, accepted_rules)
    return {
        "range": {"from": weeks[0].isoformat(),
                  "to": (weeks[-1] + timedelta(days=6)).isoformat(),
                  "weeks": [w.isoformat() for w in weeks],
                  "running_week": running.isoformat() if running in weeks else None,
                  "merged": sum(r["merged"] for r in rows)},
        "scope": {"value": scope, "name": name, "count": len(ids), "teams": teams,
                  "projects": [{"id": p["id"], "name": p["name"]} for p in factory_projects]},
        "cannot_see": list(CANNOT_SEE),
        "sections": sections,
    }


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
