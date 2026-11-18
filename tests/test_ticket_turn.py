"""Whose turn a ticket is: factory.turn_of, the spec's table row by row.

Run: python tests/test_ticket_turn.py
"""
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares import factory
from tares import status_words as sw

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def turn(stage, status=None, ci=None, merged=False, verdict=None, current=True,
         blocked_by=None, autonomy="L3-review", queue=None):
    t = {"stage": stage, "status": status, "pr": {"ci": ci, "merged": merged}}
    v = {"verdict": verdict, "current": current} if verdict else None
    return factory.turn_of(t, blocked_by, v, autonomy, queue)


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


# the spec's table, one row each
eq("todo, nothing open before it: to do", turn("todo"), "to_do")
eq("todo, waits on an open ticket: queued, not stuck", turn("todo", blocked_by=["T1"]), "queued")
eq("doing: building", turn("doing"), "building")
eq("review, no verdict yet: reviewing", turn("review", ci="success"), "reviewing")
eq("review, a verdict on an older head: reviewing",
   turn("review", ci="success", verdict="pass", current=False), "reviewing")
eq("changes stage: changes asked", turn("changes"), "changes_asked")
eq("changes verdict on the head: changes asked", turn("review", verdict="changes"), "changes_asked")
eq("passed, CI pending: checks running", turn("review", ci="pending", verdict="pass"), "checks_running")
eq("passed, CI failing: checks failing", turn("review", ci="failure", verdict="pass"), "checks_failing")
eq("passed, CI passed, L3: ready for you", turn("review", ci="success", verdict="pass"), "ready_for_you")
eq("passed, no CI on the repo, L3: ready for you", turn("review", ci="none", verdict="pass"), "ready_for_you")
eq("passed, CI passed, L4: merging",
   turn("review", ci="success", verdict="pass", autonomy="L4-ship"), "merging")
eq("queue row merging: merging", turn("review", ci="success", queue="merging"), "merging")
eq("explicit blocked: stuck", turn("blocked"), "stuck")
eq("reviewer's block on the head: stuck", turn("review", verdict="block"), "stuck")
eq("pr merged: merged", turn("review", merged=True), "merged")
eq("merged stage: merged", turn("merged"), "merged")
eq("done without a merge: closed, not merged", turn("doing", status="done"), "closed")
eq("canceled: canceled", turn("todo", status="canceled"), "canceled")

# whatever the inputs, the answer is a key with words and an owner
seen = set()
for stage, status, ci, verdict, cur, blocked, auto, q in itertools.product(
        [None, *factory.STAGES], [None, "todo", "in_progress", "in_review", "done", "canceled"],
        [None, "success", "failure", "pending", "none"], [None, *factory.VERDICTS], [True, False],
        [None, ["T1"]], ["L3-review", "L4-ship"], [None, "queued", "merging"]):
    seen.add(turn(stage, status, ci, False, verdict, cur, blocked, auto, q))
ck("every answer has words", seen <= set(sw.TURN), seen - set(sw.TURN))
ck("every answer has an owner", seen <= set(sw.TURN_WHOSE), seen - set(sw.TURN_WHOSE))
ck("every turn is reachable", set(sw.TURN) <= seen, set(sw.TURN) - seen)
ck("a person's turn is only ready for you or stuck",
   {k for k, w in sw.TURN_WHOSE.items() if w == "person"} == {"ready_for_you", "stuck"})

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
