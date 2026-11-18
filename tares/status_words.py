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

# the reviewer's verdict, as the project page writes it ("round 2: changes asked")
VERDICT = {"pass": "passed", "changes": "changes asked", "block": "blocked"}

# the reviewer's verdict at the head of the comment the crew posts on GitHub
VERDICT_ON_GITHUB = {"pass": "Pass", "changes": "Changes requested", "block": "Blocked"}

# ── pull requests and CI ──────────────────────────────────────────────────────

PR_STATE = {"open": "Open", "draft": "Draft", "merged": "Merged",
            "closed": "Closed without merging"}

# CI on the project and ticket pages ("open, CI green")
CI_ON_PROJECT = {"success": "CI green", "failure": "CI failing", "pending": "CI running"}

# CI on the Reviews screen; `none` (no checks on the repo) reads NO_CI there
CI_ON_REVIEWS = {"success": "CI passed", "failure": "CI failing", "pending": "CI in progress"}
NO_CI = "No CI"

# ── projects ──────────────────────────────────────────────────────────────────

# factory_home.project_state, then the two states the Projects list adds and the one the
# project page adds for a closed project
PROJECT_STATE = {"building": "Building", "waiting_on_you": "Waiting on you",
                 "not_started": "Not started", "ready_to_close": "Ready to close"}
PROJECT_LATER = "Later"
PROJECT_DONE_ON_LIST = "Done"
PROJECT_DONE_ON_PAGE = "Finished"

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
