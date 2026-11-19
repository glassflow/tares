"""Status words: every state the code can produce has words in tares/status_words.py.

Run: python tests/test_status_words.py

Walks the enums and the deriving functions themselves, never a hand-written list of states, so a
stage, verdict or project state added later fails here until it has words.
"""
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares import factory, factory_home, words
from tares import status_words as sw
from tares.screens import factory_project, factory_projects, factory_reviews

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# tickets: every stage, and every answer state_of can give
stages = set(factory.STAGES) | {"canceled"}
ck("every ticket stage has words", stages <= set(sw.TICKET_STAGE), stages - set(sw.TICKET_STAGE))
shown = set(sw.TICKET_STAGE.values())
outs = set()
for stage, status, blocked in itertools.product(
        [None, *factory.STAGES], [None, "todo", "in_progress", "in_review", "done", "canceled"],
        [None, ["T1"]]):
    outs.add(factory.state_of({"stage": stage, "status": status}, blocked))
ck("every state_of answer is a ticket word", outs <= shown, outs - shown)
ck("factory.STAGE_WORD is the shared table", factory.STAGE_WORD is sw.TICKET_STAGE)
ck("every turn has words and an owner", set(factory.TURNS) == set(sw.TURN) == set(sw.TURN_WHOSE))

# verdicts, on the project page and on GitHub
ck("every verdict has words on the project page", set(factory.VERDICTS) <= set(sw.VERDICT))
ck("every verdict has words on GitHub", set(factory.VERDICTS) <= set(sw.VERDICT_ON_GITHUB))

# pull requests: every answer pr_state gives
prs = set()
for pr, stage in itertools.product(
        [{}, {"url": "u"}, {"url": "u", "merged": True}, {"url": "u", "state": "closed"},
         {"url": "u", "draft": True}, {"url": "u", "state": "open"}], [None, "merged", "doing"]):
    prs.add(factory_project.pr_state({"pr": pr, "stage": stage}))
prs.discard(None)
ck("every pull request state has words", prs <= set(sw.PR_STATE), prs - set(sw.PR_STATE))

# CI: success, failure and pending have words on both surfaces; none reads NO_CI on Reviews
ci = {"success", "failure", "pending"}
ck("CI has words, one table for every screen", ci <= set(sw.CI))
ck("CI has words on Reviews", ci <= set(factory_reviews.CI))
ck("no CI reads the shared words", factory_reviews.NO_CI[0] == sw.NO_CI)

# projects: the home states, the list's and the page's
home = set(factory_home._STATE_ORDER)
ck("every home project state has words", home <= set(sw.PROJECT_STATE), home - set(sw.PROJECT_STATE))
missing = [s for s in factory_projects.STATES
           if s not in sw.PROJECT_STATE and s not in ("later", "done")]
ck("every Projects list state has words", not missing, missing)
page = home | {"finished", "later"}
ck("every project page state has words", page <= set(factory_project.STATE_LABEL),
   page - set(factory_project.STATE_LABEL))

# the spec's decision 6, one line each
vals = [v for t in (sw.TICKET_STAGE, sw.VERDICT, sw.PR_STATE, sw.CI, sw.PROJECT_STATE, sw.TURN)
        for v in t.values()] + list(sw.STATION.values()) + [sw.NO_CI, sw.PROJECT_LATER, sw.PROJECT_DONE, sw.SIGNOFF]
ck("waiting is \"for you\", never \"on you\"", not [v for v in vals if "on you" in v.lower()])
ck("a closed project reads the same on the list and its page",
   factory_projects.DONE_LABEL == factory_project.STATE_LABEL["finished"] == sw.PROJECT_DONE == "Done")
ck("CI reads as checks on every screen, the same words as the turns",
   all(v.startswith("Checks ") for v in sw.CI.values()) and sw.CI["pending"] == sw.TURN["checks_running"]
   and sw.CI["failure"] == sw.TURN["checks_failing"] and factory_reviews.CI["success"][0] == sw.CI["success"])
ck("a reviewer's block never reads as blocked",
   "block" not in sw.VERDICT["block"] and "block" not in factory_reviews.review_words({"verdict": "block"}).lower()
   and sw.reviewer_stopped("Drops a column.") == "Reviewer stopped it: Drops a column"
   and sw.reviewer_stopped(" ", "the crew reviewer") == "The crew reviewer stopped it")
ck("the close-the-project item is a question, not the state's words",
   sw.SIGNOFF == "Close the project?" and sw.SIGNOFF != sw.PROJECT_STATE["ready_to_close"])
ck("someone who cannot close it reads who can",
   sw.signoff_for("Kiran") == "Kiran can close it" and sw.signoff_for("an admin") == "An admin can close it")

# a station (decisions 3 and 5): every turn the crew engine reads has words, and only an item makes it a wait for you
from tares.crew import claude as crew_claude
turns = set(crew_claude.TURN_OF_STATUS.values()) | set(crew_claude.DEAD_STATES) | {"", "null"}
said = {sw.station_state(t, bg, a) for t in turns for bg in (0, 1, 3) for a in (False, True)}
ck("every station turn has words, none of them raw", not said & turns and all(w[:1].isupper() for w in said), said)
ck("waiting is for you only with an item that names the station",
   sw.station_state("blocked", 0, True) == sw.STATION["waiting"] == "Waiting for you"
   and sw.station_state("blocked", 0, False) == "Waiting, nothing asked yet"
   and "for you" not in sw.station_state("done", 0, True))
ck("between turns with background work is not idle",
   sw.station_state("done", 1) == "Waiting on a background task" and sw.station_state("done", 2) == "Waiting on 2 background tasks"
   and sw.station_state("done", 0) == "Idle")
ck("the console's crew card and the CLI read one table",
   factory_home._state_text("working", None, None) == sw.STATION["working"]
   and factory_home._state_text("waiting", None, None) == sw.STATION["waiting"]
   and factory_home._state_text("idle", None, None) == sw.STATION["idle"]
   and factory_home._state_text("ended", None, None) == sw.STATION["ended"])

# the words themselves pass the words list
tables = {k: v for k, v in vars(sw).items() if k.isupper()}
bad = words.offenders_in(tables)
ck("no status word is on the forbidden list", not bad, bad)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
