"""Every status word a person reads about factory work, in one place.

A screen sends a state key and the words for it; the console keeps only the tone
(ui/src/components/edition/statusTones.ts). Before this file the same tables lived in
factory.py, factory_home.py and three screens, and the same fact got a different word on each.
Moving them here changed no word: the tables below are the ones those files held, under the
name of the surface that shows them. Where two tables say one fact differently, both stay
until the words are settled (the status-words spec, decision 6).

tests/test_status_words.py walks every state the code can produce and fails on one with no
words, so a new stage or state cannot reach a screen as a raw key.
"""
from __future__ import annotations

# ── tickets ───────────────────────────────────────────────────────────────────

# a ticket's stage in words; factory.state_of returns these, and screens key on them
TICKET_STAGE = {"todo": "to do", "doing": "in progress", "review": "in review",
                "changes": "changes asked", "blocked": "blocked", "merged": "merged",
                "shipped": "merged", "canceled": "canceled"}

# the reviewer's verdict, as the project and ticket pages write it ("round 2: changes asked").
# A block is "stopped it", so "blocked" names nothing a reviewer did (the spec, decision 6)
VERDICT = {"pass": "passed", "changes": "changes asked", "block": "reviewer stopped it"}


def reviewer_stopped(reason: str | None = None, who: str | None = None) -> str:
    """"Reviewer stopped it: the migration drops a column" on a screen that has the reason (the
    first blocking finding), "Reviewer stopped it" on one that has not."""
    who = (who or "").strip()
    head = f"{who[:1].upper()}{who[1:]} stopped it" if who else "Reviewer stopped it"
    reason = " ".join(str(reason or "").split()).rstrip(".")
    return f"{head}: {reason}" if reason else head


# the reviewer's verdict at the head of the comment the crew posts on GitHub
VERDICT_ON_GITHUB = {"pass": "Pass", "changes": "Changes requested", "block": "Blocked"}

# ── pull requests and CI ──────────────────────────────────────────────────────

PR_STATE = {"open": "Open", "draft": "Draft", "merged": "Merged",
            "closed": "Closed without merging"}

# CI on every screen ("open, checks passed"); the turn words "Checks running" and "Checks
# failing" say the same. `none` (no checks on the repo) reads NO_CI
CI = {"success": "Checks passed", "failure": "Checks failing", "pending": "Checks running"}
NO_CI = "No checks"

# ── projects ──────────────────────────────────────────────────────────────────

# factory_home.project_state, then the two states the Projects list and a project's page add.
# The key stays `waiting_on_you` (the API's), the words are "for you" like every other surface
PROJECT_STATE = {"building": "Building", "waiting_on_you": "Waiting for you",
                 "not_started": "Not started", "ready_to_close": "Ready to close"}
PROJECT_LATER = "Later"
PROJECT_DONE = "Done"

# the waiting item that asks a person to close a project; the state keeps "Ready to close"
SIGNOFF = "Close the project?"

# ── a station (a crew member's session) ───────────────────────────────────────

# The spec's decision 3: one table for `tares factory crew status` and the console's crew cards.
# "Waiting for you" only with an open item that names the station (decision 5); a station that
# waits with none says so instead, and one between turns with background work running is not idle
STATION = {"working": "Working", "waiting": "Waiting for you",
           "waiting_unasked": "Waiting, nothing asked yet", "background": "Waiting on background work",
           "idle": "Idle", "not_heard": "Not heard from", "ended": "Ended"}


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def plain_wait(minutes: int | None) -> str:
    """A span in plain words: "just now", "5 minutes", "1 hour", "3 hours", "1 day", "6 days",
    "3 weeks"."""
    if minutes is None or minutes < 1:
        return "just now"
    if minutes < 60:
        return _count(minutes, "minute")
    if minutes < 1440:
        return _count(minutes // 60, "hour")
    if minutes < 10080:
        return _count(minutes // 1440, "day")
    return _count(minutes // 10080, "week")


def background_work(tasks: int) -> str:
    """"Waiting on a background task", "Waiting on 2 background tasks": Claude Code's job file counts
    what runs, it does not name it or say when it ends, so neither does this."""
    return "Waiting on a background task" if tasks == 1 else f"Waiting on {tasks} background tasks"


def station_state(turn: str, background: int = 0, asked: bool = False) -> str:
    """A station's words from where its session is (`turn`: Claude Code's working, done between
    turns, blocked on a person; anything else is over), the background tasks it still runs and
    whether an open item for a person names it."""
    if turn == "working":
        return STATION["working"]
    if turn == "blocked":
        return STATION["waiting"] if asked else STATION["waiting_unasked"]
    if turn == "done":
        return background_work(background) if background > 0 else STATION["idle"]
    return STATION["ended"]


# ── a project's ticket counts (factory.progress) ──────────────────────────────

# the groups the count line names, in its order; to do and work under way are what is left of
# the total, so the line does not list them
COUNT_WORD = {"merged": "merged", "closed": "closed", "queued": "queued",
              "stuck": "stuck"}


def count_line(c: dict) -> str:
    """"5 merged, 2 queued, 1 stuck of 8": a project's or milestone's tickets in one line. Merged
    always shows; the other groups only when there are some."""
    if not c.get("total"):
        return "No tickets yet"
    parts = [f"{c.get(k) or 0} {w}" for k, w in COUNT_WORD.items() if k == "merged" or c.get(k)]
    return ", ".join(parts) + f" of {c['total']}"


# ── whose turn a ticket is (factory.turn_of) ──────────────────────────────────

# what a ticket waits for, in words; the screen adds the tickets a queued one waits on
# ("Queued after T3") and the reason a stuck one gives
TURN = {"to_do": "To do", "queued": "Queued", "building": "Building", "reviewing": "Reviewing",
        "changes_asked": "Changes asked", "checks_running": "Checks running",
        "checks_failing": "Checks failing", "ready_for_you": "Ready for you",
        "next_to_merge": "Next to merge", "merging": "Merging", "stuck": "Stuck", "merged": "Merged", "closed": "Closed",
        "canceled": "Canceled"}

# who moves it next: the crew, a person, GitHub (its checks), or nobody (it waits or is over)
TURN_WHOSE = {"to_do": "crew", "queued": "nobody", "building": "crew", "reviewing": "crew",
              "changes_asked": "crew", "checks_running": "github", "checks_failing": "crew",
              "ready_for_you": "person", "next_to_merge": "crew", "merging": "crew", "stuck": "person",
              "merged": "nobody", "closed": "nobody", "canceled": "nobody"}
