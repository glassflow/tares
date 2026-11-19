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
eq("passed and green but its dependency is open: queued, not ready for you",
   turn("review", ci="success", verdict="pass", blocked_by=["T1"]), "queued")
eq("the same at L4: queued, not merging",
   turn("review", ci="success", verdict="pass", blocked_by=["T1"], autonomy="L4-ship"), "queued")
eq("a person said yes at L3: next to merge, not ready for you",
   turn("review", ci="success", verdict="pass", queue="approved"), "next_to_merge")
eq("said yes but CI fails since: checks failing",
   turn("review", ci="failure", verdict="pass", queue="approved"), "checks_failing")
eq("a queued row approved for its head reads approved", factory.queue_state_of(
   {"state": "queued", "head": "h2", "approved_head": "h2"}), "approved")
eq("approved for an older head: just queued", factory.queue_state_of(
   {"state": "queued", "head": "h2", "approved_head": "h1"}), "queued")
eq("a new push after the yes: just queued, though the row still names the old head",
   factory.queue_state_of({"state": "queued", "head": "h1", "approved_head": "h1"}, "h2"), "queued")
eq("the ticket's head matches the yes: approved", factory.queue_state_of(
   {"state": "queued", "head": "h1", "approved_head": "H1"}, "h1"), "approved")
eq("no row: none", factory.queue_state_of(None), None)
eq("queue sent it back at L4: changes asked, not merging",
   turn("review", ci="success", verdict="pass", autonomy="L4-ship", queue="sent_back"), "changes_asked")
eq("queue cannot merge it at L4: stuck, not merging",
   turn("review", ci="success", verdict="pass", autonomy="L4-ship", queue="blocked"), "stuck")
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
        [None, ["T1"]], ["L3-review", "L4-ship"], [None, "queued", "waiting_ci", "waiting_approval", "merging", "blocked", "sent_back", "approved"]):
    seen.add(turn(stage, status, ci, False, verdict, cur, blocked, auto, q))
ck("every answer has words", seen <= set(sw.TURN), seen - set(sw.TURN))
ck("every answer has an owner", seen <= set(sw.TURN_WHOSE), seen - set(sw.TURN_WHOSE))
ck("every turn is reachable", set(sw.TURN) <= seen, set(sw.TURN) - seen)
ck("a person's turn is only ready for you or stuck",
   {k for k, w in sw.TURN_WHOSE.items() if w == "person"} == {"ready_for_you", "stuck"})

# counts (factory.progress): the spec's decision 2, the groups read off the turn
def tk(i, **k):
    return {"id": i, "stage": None, "status": "todo", "depends_on": [], "pr": None, **k}


eq("a ticket closed with no merge counts as closed, not merged",
   factory.count_of(tk("a", status="done")), "closed")
eq("a merged pull request counts as merged whatever the stage",
   factory.count_of(tk("a", stage="review", pr={"merged": True})), "merged")
eq("a stage of merged counts as merged", factory.count_of(tk("a", stage="merged")), "merged")
eq("a wait on another ticket counts as queued", factory.count_of(tk("a"), ["T1"]), "queued")
eq("a station's stop counts as stuck", factory.count_of(tk("a", stage="blocked")), "stuck")
eq("work under way counts as doing", factory.count_of(tk("a", stage="changes")), "doing")
seen_counts = {factory.COUNT_OF_TURN.get(k, "doing") for k in sw.TURN}
ck("every turn lands in a count group", seen_counts <= set(factory.COUNTS), seen_counts - set(factory.COUNTS))

ts = [tk("m", stage="merged"), tk("c", status="done"), tk("s", stage="blocked"),
      tk("q", depends_on=["o"]), tk("o"), tk("x", status="canceled"), tk("d", stage="doing")]
eq("a project's counts: merged, closed, queued and stuck apart, canceled out of the total",
   factory.progress(ts), {"merged": 1, "closed": 1, "doing": 1, "stuck": 1, "queued": 1, "todo": 1,
                          "canceled": 1, "total": 6})
eq("the count line names merged always, the rest when there are some",
   sw.count_line(factory.progress(ts)), "1 merged, 1 closed, 1 queued, 1 stuck of 6")
eq("the count line of a project with only merges", sw.count_line({"merged": 2, "total": 3}), "2 merged of 3")
eq("the count line of an empty project", sw.count_line({"merged": 0, "total": 0}), "No tickets yet")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
