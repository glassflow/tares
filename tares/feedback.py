"""Was this worth raising? (TR-746)

A person answers yes or no on anything the AI SRE watcher raised: the watcher's own finding, or a
note an investigator wrote after it handed the finding on. The answer is kept per result and per
person and credited to the watcher at the root of the chain, because it is about the watcher's
decision to raise. It becomes a hit rate per watcher, a short list of recent "no" cases in the
watcher's prompt (AI SRE cells only, see prompt_section) and an export.

The Slack buttons for the same question call answer() with via="slack" (TR-762).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from . import goal

MAX_ROOT_STEPS = 10
NOTE_MAX = 300
MIN_ANSWERS = 5          # a hit rate under this many answers is not a rate yet
PROMPT_DAYS = 14
PROMPT_LINES = 5
PROMPT_LINE_MAX = 200
EXPORT_COLS = ("at", "run_id", "watcher", "watcher_run", "dispatch_id", "project", "worth",
               "person_name", "note", "via")


class NotRaised(Exception):
    """The result is not something a watcher raised, so it has no such question."""


def scheduled_triggers(store) -> set:
    """The names of the schedule triggers, from the catalog the store holds."""
    return {t["name"] for t in store.list_catalog_triggers()
            if isinstance(t.get("condition"), dict) and t["condition"].get("every")}


def raised_root(store, run_id: str, scheduled: set | None = None,
                as_run: dict | None = None) -> dict | None:
    """The watcher run that raised what `run_id` holds (its agent, id, dispatch_id and project
    are what an answer is credited to), found by walking up the parents at most MAX_ROOT_STEPS;
    None when no watcher raised it. KeyError when there is no such run. `as_run` overrides fields
    of the run (the outcome and verdict of a run that is still open when its note is posted)."""
    run = store.get_agent_run(run_id)
    if run is None:
        raise KeyError(f"no run {run_id!r}")
    if as_run:
        run = {**run, **as_run}
    scheduled = scheduled_triggers(store) if scheduled is None else scheduled
    path, cur = [run], run
    while len(path) <= MAX_ROOT_STEPS and cur.get("parent_run_id"):
        cur = store.get_agent_run(cur["parent_run_id"])
        if cur is None:
            break
        path.append(cur)
    path.reverse()
    outcome = goal.outcome_of(run, goal.is_watcher_run(run, scheduled))
    return goal.raised_by(path, outcome, any(r.get("practice") for r in path), scheduled)


def answerable(store, run_id: str, as_run: dict | None = None) -> bool:
    """Whether a person can answer "Was this worth raising?" on this result (the Slack buttons
    ask before they show). False for a run that is gone or that nothing raised."""
    try:
        return raised_root(store, run_id, as_run=as_run) is not None
    except Exception:
        return False


def counts(rows: list[dict]) -> dict:
    yes = sum(1 for r in rows if r["worth"])
    return {"yes": yes, "no": len(rows) - yes}


def answer(store, run_id: str, worth: bool, person: str, person_name: str | None, via: str,
           note: str | None = None) -> dict:
    """Record `person`'s answer on a result and return {"yes", "no"}, the counts on it now.
    KeyError for an unknown run, NotRaised when no watcher raised it."""
    root = raised_root(store, run_id)
    if root is None:
        raise NotRaised("Only what the watcher raised can be answered")
    store.set_feedback(run_id, person, person_name or person, bool(worth),
                       (note or "").strip()[:NOTE_MAX] or None, via, root["agent"], root["id"],
                       root.get("dispatch_id"), root.get("project"))
    return counts(store.feedback_for([run_id])[run_id])


def with_answers(store, results: list[dict], person: str | None) -> None:
    """Add "worth" ({"yes", "no", "mine"}: true, false or None for the caller's own answer) to
    each result. One read for the whole page."""
    got = store.feedback_for([r["id"] for r in results])
    for r in results:
        rows = got[r["id"]]
        mine = next((x["worth"] for x in rows if x["person"] == person), None)
        r["worth"] = {**counts(rows), "mine": mine}


def hit_rate_words(row: dict, days: int = 30) -> str:
    """"7 of 9 worth raising in the last 30 days", or "2 answers so far" under MIN_ANSWERS."""
    n = row["answered"]
    if n < MIN_ANSWERS:
        return f"{n} answer{'' if n == 1 else 's'} so far"
    return f"{row['yes']} of {n} worth raising in the last {days} days"


def hit_rates(store, days: int = 30, now=None) -> list[dict]:
    """Per watcher and project: answered, yes, no, hit_rate (None under MIN_ANSWERS), words."""
    now = now or datetime.now(timezone.utc)
    out = []
    for row in store.watcher_hit_rates(now - timedelta(days=days)):
        rate = round(row["yes"] / row["answered"], 2) if row["answered"] >= MIN_ANSWERS else None
        out.append({"agent": row["watcher"], "project": row["project"],
                    "answered": row["answered"], "yes": row["yes"], "no": row["no"],
                    "hit_rate": rate, "words": hit_rate_words(row, days)})
    return out


def export_rows(store, since=None) -> list[dict]:
    rows = store.feedback_export(since)
    return [{**{c: r[c] for c in EXPORT_COLS if c != "at"}, "at": goal._iso(r["answered_at"])}
            for r in rows]


def export_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=EXPORT_COLS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def prompt_section(store, agent_name: str, now=None) -> str:
    """What the watcher is told at the start of a run: the cases it raised in the last PROMPT_DAYS
    days that a person said were not worth raising, newest first, at most PROMPT_LINES. Empty
    when there are none. The caller decides whether this cell and run get it at all."""
    now = now or datetime.now(timezone.utc)
    rows = store.recent_not_worth(agent_name, now - timedelta(days=PROMPT_DAYS), PROMPT_LINES)
    if not rows:
        return ""
    settings = store.service_settings()
    lines = []
    for r in rows:
        run = store.get_agent_run(r["run_id"])
        what = (goal.title(goal.title_parts(run, run["started_at"], settings), now) if run
                else "an earlier finding")
        line = f"- {what} ({goal._since_words(r['answered_at'], now)}): {r['note'] or 'no reason given'}"
        lines.append(goal._no_dash(" ".join(line.split()))[:PROMPT_LINE_MAX])
    return ("\n\nRecently you raised these and a person said they were not worth raising:\n"
            + "\n".join(lines) + "\nRaise something like these only if it is clearly worse.")
