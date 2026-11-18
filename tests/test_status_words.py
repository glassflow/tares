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
ck("CI has words on the project page", ci <= set(sw.CI_ON_PROJECT))
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

# the words themselves pass the words list
tables = {k: v for k, v in vars(sw).items() if k.isupper()}
bad = words.offenders_in(tables)
ck("no status word is on the forbidden list", not bad, bad)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
