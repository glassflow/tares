"""The Tares reviewer as the built-in review gate (TR-852).

Covered: `same_commit` (short and long hashes, any case, empty and missing); `verdict_now` still
answers as before; the gate is installed as review/tares; check_connection; every state comes from
fixtures: approved (pass), changes (from changes and from block), pending, stale and none; a
record for the current head wins over a disagreeing review status; the review status stands in
when there is no record, only on the current head; the latest round is the one that counts; the
summary is cut to 200 characters; findings come back as text; null and empty input (no store,
no ticket, no project, a head of None or "", a ticket the project does not have, a ticket with
no review and no snapshot, a snapshot with no verdict) never raise and say `none`.

Run: python tests/test_review_gate.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


from tares import factory as factory_mod
from tares import factory_api
from tares.integrations import registry
from tares.integrations.builtin.review_tares import TaresReview
from tares.integrations.review import ReviewGate
from tares.integrations.types import PRRef, VERDICT_STATES, Verdict
from tares.store import Store

run = asyncio.run
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
OLD = "abc1234def5678abc1234def5678abc1234def56"
NEW = "def5678abc1234def5678abc1234def5678abc12"

print("== same_commit ==")
ck("equal hashes", factory_mod.same_commit(OLD, OLD))
ck("a short hash starts the long one", factory_mod.same_commit("abc1234", OLD))
ck("a long hash is started by the short one", factory_mod.same_commit(OLD, "abc1234"))
ck("case does not matter", factory_mod.same_commit("ABC1234", OLD))
ck("different commits", not factory_mod.same_commit(OLD, NEW))
ck("an empty hash is no commit", not factory_mod.same_commit("", OLD) and not factory_mod.same_commit(OLD, ""))
ck("None is no commit", not factory_mod.same_commit(None, OLD) and not factory_mod.same_commit(OLD, None))
ck("two empty hashes are not the same commit", not factory_mod.same_commit("", "") and not factory_mod.same_commit(None, None))
ck("surrounding spaces are ignored", factory_mod.same_commit(f" {OLD} ", "abc1234"))

print("== verdict_now keeps its answers ==")
last = {"head": OLD[:7], "verdict": "pass", "round": 1, "at": NOW}
v = factory_api.verdict_now({"pr": {"head": OLD}}, last)
ck("current when the PR head is the reviewed commit", v["current"] is True and v["note"] is None, v)
v = factory_api.verdict_now({"pr": {"head": NEW}}, last)
ck("not current once the head moved, with its note",
   v["current"] is False and "older commit (abc1234)" in v["note"] and "def5678" in v["note"], v)
ck("current when the PR has no head yet", factory_api.verdict_now({"pr": {}}, last)["current"] is True)
ck("current when the ticket has no PR", factory_api.verdict_now({}, last)["current"] is True)
ck("no review, no answer", factory_api.verdict_now({"pr": {"head": NEW}}, None) is None)
ck("a review with no head counts as current",
   factory_api.verdict_now({"pr": {"head": NEW}}, {**last, "head": ""})["current"] is True)

print("== installed ==")
ck("review/tares is the class", registry.get("review", "tares") is TaresReview)
ck("it is a built-in", "tares.integrations.builtin.review_tares:TaresReview" in registry.BUILTINS)
ck("it is a review gate with its names",
   issubclass(TaresReview, ReviewGate) and TaresReview.slot == "review" and TaresReview.name == "tares"
   and TaresReview.title == "Tares reviewer" and TaresReview.ee is False and TaresReview.settings_schema == [])
ok, msg = run(TaresReview().check_connection({}))
ck("check_connection says built in, even with no store", ok is True and msg == "Built in", (ok, msg))

tmp = tempfile.mkdtemp(prefix="tares-review-gate-")
s = Store(os.path.join(tmp, "t.duckdb"))
s.create_project("p1", "blank", "One", {})
s.create_project("p2", "blank", "Two", {})
gate = TaresReview(s)


def ticket(title, pr=None, uid="p1"):
    tid = s.create_ticket(uid, "tares", title, "todo", None)
    if pr is not None:
        s.update_ticket(tid, pr=pr)
    return tid


def review(tid, head, verdict, verified="ran the tests", findings=None, by="crew-reviewer", uid="p1"):
    return s.add_review(uid, tid, "https://github.com/o/r/pull/1", head, verdict, verified, "", by,
                        findings or [], {}, factory_mod.finding_kind, NOW)


def ref(tid, head, uid="p1", **kw):
    return PRRef("o/r", 1, head, "factory-modular/m3", "https://github.com/o/r/pull/1", tid, uid)

def verdict(tid, head, uid="p1"):
    return run(gate.verdict(ref(tid, head, uid)))


print("== a review record on the current head ==")
t = ticket("approved")
review(t, OLD[:7], "pass", "ran tests/test_x.py: 9 passed")
v = verdict(t, OLD)
ck("pass gives approved", v.state == "approved" and v.head == OLD[:7], v)
ck("by is who recorded it, summary the verified text",
   v.by == "crew-reviewer" and v.summary == "ran tests/test_x.py: 9 passed", v)
ck("a Verdict of a known state", isinstance(v, Verdict) and v.state in VERDICT_STATES)
ck("the PR link comes along", v.url == "https://github.com/o/r/pull/1", v)
ck("a short head on the PR matches the long one on the record",
   verdict(t, OLD[:7]).state == "approved" and verdict(t, OLD.upper()).state == "approved")

t = ticket("changes")
review(t, OLD, "changes", "ran it", [{"severity": "P2", "file": "tares/a.py", "line": 12,
                                      "blocking": True, "text": "No test for the empty case"}])
v = verdict(t, OLD)
ck("changes gives changes", v.state == "changes", v)
ck("the findings come as text", v.findings == ["No test for the empty case (P2, tares/a.py:12)"], v.findings)
ck("findings() is the same text", run(gate.findings(ref(t, OLD))) == v.findings)

t = ticket("block")
review(t, OLD, "block", "it deletes data")
ck("block gives changes", verdict(t, OLD).state == "changes")

t = ticket("long summary")
review(t, OLD, "pass", "x" * 500)
ck("the summary is cut to 200 characters", len(verdict(t, OLD).summary) == 200)

t = ticket("finding with no file")
review(t, OLD, "changes", "ran it", [{"severity": "P3", "file": "", "line": 0, "blocking": False,
                                      "text": "Name it better"}])
ck("a finding with no file keeps its text and severity", verdict(t, OLD).findings == ["Name it better (P3)"])

print("== stale ==")
t = ticket("stale")
review(t, OLD[:7], "pass")
v = verdict(t, NEW)
ck("a record for an older commit is stale", v.state == "stale", v)
ck("the summary names both commits",
   v.summary == "The review is for an older commit (abc1234); the pull request is now at def5678", v.summary)
ck("a stale verdict keeps the head it was for", v.head == OLD[:7], v.head)

print("== the latest round counts ==")
t = ticket("two rounds")
review(t, OLD, "changes", "round one")
review(t, NEW, "pass", "round two")
ck("the new round on the new head approves", verdict(t, NEW).state == "approved")
ck("the old head is now stale", verdict(t, OLD).state == "stale")
t = ticket("two rounds, regression")
review(t, OLD, "pass", "round one")
review(t, OLD, "block", "round two")
ck("a later block on the same head wins over the earlier pass", verdict(t, OLD).state == "changes")

print("== the review status on the PR, with no record ==")
snap = lambda verdict_, head, text="": {"repo": "o/r", "number": 1, "head": head,
                                         "verdict": verdict_, "verdict_head": head, "verdict_text": text}
t = ticket("status pass", snap("pass", NEW, "Reviewer passed"))
v = verdict(t, NEW)
ck("status pass gives approved", v.state == "approved" and v.summary == "Reviewer passed", v)
t = ticket("status changes", snap("changes", NEW))
ck("status changes gives changes", verdict(t, NEW).state == "changes")
t = ticket("status pending", snap("pending", NEW))
ck("status pending gives pending", verdict(t, NEW).state == "pending")
t = ticket("status old head", {**snap("pass", OLD), "head": NEW})
ck("a status set on an older head gives none", verdict(t, NEW).state == "none")
t = ticket("status short head", snap("pass", OLD[:7]))
ck("a short status head matches", verdict(t, OLD).state == "approved")
t = ticket("status none", {"repo": "o/r", "number": 1, "head": NEW, "verdict": None, "verdict_head": None})
ck("a snapshot with no review status gives none", verdict(t, NEW).state == "none")
t = ticket("status error", {"repo": "o/r", "number": 1, "head": NEW, "verdict": "unknown", "verdict_head": NEW})
ck("a status word it does not know gives none", verdict(t, NEW).state == "none")
t = ticket("status with no head", {"repo": "o/r", "number": 1, "verdict": "pass", "verdict_head": None})
ck("a status that names no head gives none", verdict(t, NEW).state == "none")

print("== a record wins over a disagreeing status ==")
t = ticket("record changes, status pass", snap("pass", NEW))
review(t, NEW, "changes", "found a bug")
ck("record changes beats status pass", verdict(t, NEW).state == "changes")
t = ticket("record pass, status changes", snap("changes", NEW))
review(t, NEW, "pass", "all good")
ck("record pass beats status changes", verdict(t, NEW).state == "approved")
t = ticket("record pass, status pending", snap("pending", NEW))
review(t, NEW, "pass", "all good")
ck("record pass beats status pending", verdict(t, NEW).state == "approved")
t = ticket("old record, status on the new head", snap("pass", NEW))
review(t, OLD, "pass", "old")
ck("an older record is stale even when the status passes the new head", verdict(t, NEW).state == "stale")

print("== no record, no status ==")
t = ticket("nothing yet")
ck("a ticket with no review and no PR is none", verdict(t, NEW).state == "none")
ck("findings are an empty list", run(gate.findings(ref(t, NEW))) == [])
t = ticket("clean pass")
review(t, NEW, "pass", "ok")
ck("a pass with no findings has an empty findings list", verdict(t, NEW).findings == [])

print("== null and empty input ==")
t = ticket("for null checks")
review(t, OLD, "pass", "ok")
for label, head in (("None", None), ("empty", ""), ("spaces", "   ")):
    v = verdict(t, head)
    ck(f"a head of {label} is none, not an error", v.state == "none", v)
ck("a PR with no ticket is none", run(gate.verdict(PRRef("o/r", 1, NEW, project_uid="p1"))).state == "none")
ck("a PR with no project is none", run(gate.verdict(PRRef("o/r", 1, NEW, ticket=t))).state == "none")
ck("a bare PRRef is none", run(gate.verdict(PRRef("o/r", 1, NEW))).state == "none")
ck("a ticket the project does not have is none", verdict("T999", NEW).state == "none")
ck("a ticket of another project is none", verdict(t, NEW, uid="p2").state == "none")
ck("a project that does not exist is none", verdict(t, NEW, uid="nope").state == "none")
ck("findings of a PR with no ticket are []", run(gate.findings(PRRef("o/r", 1, NEW))) == [])
ck("findings of an unknown ticket are []", run(gate.findings(ref("T999", NEW))) == [])
ck("the ticket can be named by its number", verdict("T" + str(s.get_ticket("p1", t)["number"]), OLD).state == "approved")
ck("the ticket can be named by its id", verdict(s.get_ticket("p1", t)["id"], OLD).state == "approved")
try:
    run(TaresReview().verdict(ref(t, NEW)))
    ck("a bare gate (no store) refuses to answer", False, "no error")
except RuntimeError as e:
    ck("a bare gate (no store) refuses to answer, saying why", "store" in str(e), e)

print()
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
