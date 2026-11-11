"""Review and merge (M6: TR-440..446).

Two layers. The builder's Codex challenger lines (recorded on its factory/<ref> branch) become the
ticket's first layer: open, fixed, waived with a reason (a crew station must give one). The
reviewer records rounds on Tares with its findings, re-reviews mark them fixed or open, a pass
leaves nothing blocking open, only the reviewer records one. A verdict counts only for its commit:
new commits after "changes" put the ticket back in review, and an old pass says it is for an older
commit. The queue orders reviews across projects. The brief stops on a merged PR or a moved head
and lists the builder's checks against the recording (matched, not found, differs; a break check
needs the test failing then passing). Findings that keep coming back in a repo become a rule the
person accepts (into AGENTS.md) or rejects (never offered again). When the project's review gate is
an outside tool (TR-857) the crew reviewer is skipped: the queue leaves the ticket out, Tares reads
the tool's verdict once per commit and keeps it as a review record.

Run: .venv/bin/python tests/test_factory_review.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-review-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-review"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory, factory_github

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


HEAD = {"value": "aaaaaaa1111"}


def fake_github(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    if parts[3] == "pulls":
        return httpx.Response(200, json={
            "number": int(parts[4]), "title": "PR", "state": "open", "merged": False,
            "html_url": f"https://github.com/acme/shop/pull/{parts[4]}",
            "head": {"sha": HEAD["value"], "ref": "factory/T1"}})
    if parts[5] == "status":
        return httpx.Response(200, json={"statuses": []})
    return httpx.Response(200, json={"check_runs": []})


factory_github._transport = httpx.MockTransport(fake_github)

from tares import factory_api, factory_waiting
from tares.integrations import registry
from tares.integrations.review import ReviewGate
from tares.integrations.types import Verdict


class FakeGate(ReviewGate):
    """An outside review tool: `answer` is what it says next (a Verdict, or an Exception to raise)."""
    name = "fakegate"
    title = "FakeBot"
    answer = None
    calls = []

    def __init__(self, store):
        pass

    async def check_connection(self, settings):
        return True, "ok"

    async def verdict(self, pr):
        FakeGate.calls.append(pr)
        if isinstance(FakeGate.answer, Exception):
            raise FakeGate.answer
        return FakeGate.answer


FX = {}
_register = factory_api.register


def _register_keeping_fx(app, store, h):
    FX["fx"] = _register(app, store, h)
    return FX["fx"]


factory_api.register = _register_keeping_fx   # the pages' queue is on the daemon's own fx

_real_get = registry.get
registry.get = lambda slot, name: FakeGate if (slot, name) == ("review", "fakegate") else _real_get(slot, name)


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def unit():
    print("== a claimed check against the recording (TR-442) ==")
    t0 = datetime.now(timezone.utc)
    cmds = [
        {"at": t0 - timedelta(minutes=9), "command": "cd app && pytest -q", "output": "14 passed in 1.2s", "is_error": False},
        {"at": t0 - timedelta(minutes=8), "command": "pytest tests/test_parse.py", "output": "1 failed, test_parse", "is_error": True},
        {"at": t0 - timedelta(minutes=7), "command": "pytest tests/test_parse.py", "output": "1 passed test_parse", "is_error": False},
        {"at": t0 - timedelta(minutes=6), "command": "npm run lint", "output": "3 errors", "is_error": True},
    ]
    def check(cmd, result, broke=""):
        return {"command": cmd, "result": result, "broke_test": broke, "at": t0}
    ck("an honest check matches", factory.match_check(check("pytest -q", "14 passed"), cmds)[0] == "matched")
    ck("a check never run is not found", factory.match_check(check("make e2e", "all green"), cmds)[0] == "not_found")
    st, rec = factory.match_check(check("npm run lint", "clean"), cmds)
    ck("a misreported result differs, with what the recording shows", st == "differs" and "3 errors" in rec, rec)
    ck("a wrong count differs", factory.match_check(check("pytest -q", "20 passed"), cmds)[0] == "differs")
    ck("a break check matches when the test failed then passed",
       factory.match_check(check("pytest -q", "14 passed", "test_parse"), cmds)[0] == "matched")
    st, rec = factory.match_check(check("pytest -q", "14 passed", "test_cart"), cmds)
    ck("a break check with no failing run differs", st == "differs" and "test_cart" in rec, rec)
    late = {"command": "pytest -q", "result": "14 passed", "broke_test": "", "at": t0 - timedelta(minutes=20)}
    ck("a run after the claim does not count", factory.match_check(late, cmds)[0] == "not_found")
    short = [{"at": t0 - timedelta(minutes=3), "command": "pytest", "output": "1 passed", "is_error": False}]
    ck("a shorter command that ran does not stand for the claimed one",
       factory.match_check(check("pytest -q tests/", "1 passed"), short)[0] == "not_found")
    twelve = [{"at": t0 - timedelta(minutes=3), "command": "pytest -q", "output": "12 passed in 1s", "is_error": False}]
    ck("2 passed is not 12 passed", factory.match_check(check("pytest -q", "2 passed"), twelve)[0] == "differs")
    ck("a zero count in the claim is not required in the output",
       factory.match_check(check("pytest -q", "12 passed, 0 failed"), twelve)[0] == "matched")
    hidden = [{"at": t0 - timedelta(minutes=3), "command": "pytest -q",
               "output": "FAILED tests/test_x.py::test_a\n12 passed 0 failed", "is_error": False}]
    ck("a FAILED line is a failure even next to 0 failed",
       factory.match_check(check("pytest -q", "12 passed"), hidden)[0] == "differs")
    unittest = [{"at": t0 - timedelta(minutes=3), "command": "python t.py", "output": "ran 4, errors=0", "is_error": False}]
    ck("errors=0 is not a failure", factory.match_check(check("python t.py", "4 ran"), unittest)[0] == "matched")
    early = [{"at": t0 - timedelta(hours=3), "command": "pytest -q", "output": "14 passed", "is_error": False}]
    ck("a run from before the ticket went into the work does not count",
       factory.match_check(check("pytest -q", "14 passed"), early, since=t0 - timedelta(hours=1))[0] == "not_found")
    ck("finding kinds use whole words: blocking is not a lock",
       factory.finding_kind("this is a blocking issue") != "concurrency"
       and factory.finding_kind("the author field") != "security")

    print("== the Codex layer from its recorded reviews (TR-440) ==")
    ev = [{"type": "challenge_commit", "challenge": {"verdict": "FAIL", "sha": "1", "findings": [
              {"priority": "P2", "title": "missing null check"}, {"priority": "P1", "title": "sql injection"}]}},
          {"type": "challenge_waived", "challenge": {"findings": [
              {"priority": "P1", "title": "sql injection", "waived": True, "reason": "internal only"}]}},
          {"type": "challenge_commit", "challenge": {"verdict": "PASS", "sha": "2", "findings": []}}]
    layer = factory.challenger_layer(ev)
    ck("fixed and waived are told apart, the layer is clean",
       [f["title"] for f in layer["fixed"]] == ["missing null check"]
       and layer["waived"][0]["reason"] == "internal only" and layer["clean"] and layer["rounds"] == 2, layer)

    print("== a waiver in a crew needs its reason ==")
    env = {**clean_env(), "FACTORY_STATION": "shop-build-m1", "CLAUDE_PLUGIN_DATA": os.path.join(_TMP, "pd")}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "claude-plugin", "scripts", "challenger.py"),
                        "waive", "1"], capture_output=True, text=True, env=env, cwd=_TMP)
    ck("refused without a reason", r.returncode == 1 and "needs its reason" in r.stdout, r.stdout + r.stderr)


async def main():
    from tares.daemon import make_app
    from tares.envelope import Envelope
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station="", role="", parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    async def ingest(lines):
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        await mf.write_milestone("Store", "", [], project="Shop")
        for t in ("Catalog", "Cart", "Checkout"):
            await m.write_ticket(t, milestone="Store", project="Shop")
        store.upsert_github_credential("gh", "ghp_x")
        as_station("crew-orchestrator", "orchestrator")
        await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.set_stage("T1", "doing", project="Shop")
        await ingest([   # the builder runs its tests after it started the ticket
            {"type": "session_station", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ago(30),
             "station": "shop-build-m1", "role": "builder", "parent": "crew-orchestrator", "tares_project": "Shop"},
            {"type": "assistant", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ago(0), "message": {
                "role": "assistant", "content": [{"type": "tool_use", "id": "u1", "name": "Bash",
                                                  "input": {"command": "pytest -q"}}]}},
            {"type": "user", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ago(0), "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "14 passed"}]}}])
        await mf.add_check("T1", "pytest -q", "14 passed", project="Shop")
        await mf.add_check("T1", "make e2e", "all green", project="Shop")
        await mf.set_stage("T1", "review", pr="https://github.com/acme/shop/pull/7", project="Shop")
        await ingest([
            {"type": "challenge_commit", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ago(18),
             "gitBranch": "factory/T1", "tares_project": "Shop", "flow": "challenger",
             "challenge": {"verdict": "FAIL", "sha": "abc", "findings": [{"priority": "P2", "title": "no tests for empty cart"}]}},
            {"type": "challenge_commit", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ago(17),
             "gitBranch": "factory/T1", "tares_project": "Shop", "flow": "challenger",
             "challenge": {"verdict": "PASS", "sha": "abd", "findings": []}},
        ])

        print("== the review brief (TR-445) ==")
        as_station("crew-reviewer", "reviewer")
        brief = await mf.review_brief("T1", project="Shop")
        ck("GitHub's facts come first", brief.index("## The pull request") < brief.index("## The ticket"), brief[:300])
        ck("an invented check is flagged, the honest one matched",
           "[NOT IN THE RECORDING] `make e2e`" in brief and "[matched] `pytest -q`" in brief, brief)
        ck("unmatched checks come first",
           brief.index("NOT IN THE RECORDING") < brief.index("[matched]"), brief)
        ck("the Codex layer shows what it found and that it is fixed",
           "fixed [P2] no tests for empty cart" in brief, brief)
        brief = await mf.review_brief("T1", head="bbbbbbb9999", project="Shop")
        ck("a moved head stops the review", "STOP: the head moved" in brief, brief[-300:])

        print("== review rounds (TR-441) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.record_review("T1", "aaaaaaa1111", "pass", "ran it", project="Shop")
        ck("a builder cannot record a review", "only the reviewer" in bad, bad)
        as_station("crew-reviewer", "reviewer")
        bad = await mf.record_review("T1", "aaaaaaa1111", "pass", "", project="Shop")
        ck("a review says what was verified", "verified" in bad, bad)
        out = json.loads(await mf.record_review(
            "T1", "aaaaaaa1111", "changes", "pytest -q: 14 passed; broke parse, test failed",
            "no load test",
            [{"severity": "P2", "file": "db/migrations/001.sql", "line": 3, "text": "the migration is missing for the new column"},
             {"severity": "P3", "text": "naming", "blocking": False}], project="Shop"))
        ck("round 1: two findings F1, F2", out["round"] == 1 and out["findings"] == ["F1", "F2"]
           and out["still_open"] == ["F1", "F2"], out)
        bad = await mf.record_review("T1", "aaaaaaa1111", "pass", "again", project="Shop")
        ck("a pass with a blocking finding still open is refused", "no blocking finding open" in bad, bad)
        g = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("the verdict counts for its commit", g["verdict"]["current"] and g["verdict"]["verdict"] == "changes", g["verdict"])

        print("== the verdict and its commit (TR-444) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.set_stage("T1", "changes", project="Shop")
        HEAD["value"] = "ccccccc2222"
        t1 = store.get_ticket(shop, "T1")
        store.update_ticket(t1["id"], pr={**t1["pr"], "checked_at": None})
        g = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("new commits after the verdict: back in review, the verdict is for an older commit",
           g["stage"] == "review" and not g["verdict"]["current"]
           and "older commit" in g["verdict"]["note"], (g["stage"], g["verdict"]))
        as_station("crew-reviewer", "reviewer")
        out = json.loads(await mf.record_review("T1", "ccccccc2222", "pass", "pytest -q: 15 passed",
                                                resolved={"F1": "fixed", "F2": "fixed"}, project="Shop"))
        ck("round 2 marks both fixed and passes", out["round"] == 2 and sorted(out["fixed"]) == ["F1", "F2"]
           and out["still_open"] == [], out)

        print("== the queue (TR-444) ==")
        b = json.loads(await m.create_project("Blog", ""))["id"]
        await m.write_ticket("Post", project="Blog")
        as_station()
        await mf.set_stage("T1", "review", project="Blog")
        await mf.set_stage("T2", "review", project="Shop")
        q = json.loads(await mf.review_queue())
        ck("tickets blocking a milestone come first",
           [(x["project_name"], x["ticket"]) for x in q][:2] in ([("Shop", "T1"), ("Shop", "T2")],
                                                                 [("Shop", "T2"), ("Shop", "T1")])
           and q[-1]["project_name"] == "Blog", q)

        print("== recurring findings and the rules offered (TR-443) ==")
        as_station("crew-reviewer", "reviewer")
        for ref in ("T2", "T3"):
            as_station("crew-orchestrator", "orchestrator")
            await mf.assign_ticket(ref, "shop-build-m1", project="Shop")
            as_station()
            await mf.set_stage(ref, "review", pr="acme/shop#8", project="Shop")
            as_station("crew-reviewer", "reviewer")
            await mf.record_review(ref, HEAD["value"], "changes", "ran tests",
                                   findings=[{"severity": "P2", "text": "a migration is missing"}],
                                   project="Shop")
        rec = json.loads(await mf.recurring_findings("acme/shop"))
        ck("three tickets with a missing migration is recurring",
           rec["recurring"] and rec["recurring"][0]["kind"] == "migration" and rec["recurring"][0]["count"] == 3, rec)
        async with mk() as cx:
            ps = (await cx.get("/api/rule-proposals")).json()["proposals"]
            ck("a rule is offered, for the project's AGENTS.md", len(ps) == 1 and ps[0]["scope"] == "project"
               and "migration" in ps[0]["text"], ps)
            r = await cx.post(f"/api/rule-proposals/{ps[0]['id']}/accept", json={"text": "Every schema change ships with its migration."})
            ck("a crew station cannot decide a rule", r.status_code == 403, r.text)
            as_station()
        async with mk() as cx:
            r = await cx.post(f"/api/rule-proposals/{ps[0]['id']}/accept", json={"text": "Every schema change ships with its migration."})
            ck("accepting adds it to the project's AGENTS.md", r.status_code == 200, r.text)
            doc = (await cx.get(f"/api/projects/{shop}/docs/{r.json()['doc']}")).json()
            ck("the line is there", "- Every schema change ships with its migration." in doc["body"], doc["body"])
            await store_reject_and_reoffer(cx, store, mf, as_station)
        await outside_gate_cases(store, mf, m, as_station, shop)


async def outside_gate_cases(store, mf, m, as_station, shop):
    print("== an outside review tool is the gate (TR-857) ==")
    as_station()
    uid = json.loads(await m.create_project("Gated", ""))["id"]
    for title in ("Alpha", "Beta"):
        await m.write_ticket(title, project="Gated")
    await mf.set_stage("T1", "review", pr="acme/shop#21", project="Gated")
    gated = store.get_ticket(uid, "T1")
    HEAD["value"] = "d0d0d0d0001"

    async def refresh():
        t = store.get_ticket(uid, "T1")
        store.update_ticket(t["id"], pr={**t["pr"], "checked_at": None})
        await factory_api._refresh(store, uid)
        return store.get_ticket(uid, "T1")

    def records():
        return [r for r in store.ticket_reviews(gated["id"]) if r["by"] == "fakegate"]

    q = json.loads(await mf.review_queue())
    ck("the Tares gate (the default) still has the ticket in the reviewer's queue",
       any(x["project_name"] == "Gated" for x in q), q)
    ck("outside_gate is None without a choice", factory_api.outside_gate(store, uid) is None)
    FakeGate.answer = Verdict(state="approved", head="d0d0d0d0001", by="FakeBot", summary="ok")
    await refresh()
    ck("the Tares gate never asks an outside tool", FakeGate.calls == [] and records() == [], FakeGate.calls)

    store.set_project_choice(uid, "review", "fakegate", {}, by="test")
    ck("an outside gate is named", factory_api.outside_gate(store, uid) == ("fakegate", "FakeBot"),
       factory_api.outside_gate(store, uid))
    q = json.loads(await mf.review_queue())
    ck("the crew reviewer's queue leaves the ticket out", not any(x["project_name"] == "Gated" for x in q), q)
    rows = [x for x in factory_api_queue_all(store) if x["project_name"] == "Gated"]
    ck("the pages' queue has it, naming the gate", rows and rows[0]["outside_gate"] == "FakeBot", rows)
    ck("another project's rows carry no gate",
       all(x["outside_gate"] is None for x in factory_api_queue_all(store) if x["project_name"] != "Gated"))
    ck("the wait is for the tool, by name",
       factory_waiting._crew_has({"outside_gate": "FakeBot", "verdict": {}}, "L3-review") == "Waiting for FakeBot")
    ck("changes asked are the tool's",
       "FakeBot asked for" in factory_waiting._crew_has(
           {"outside_gate": "FakeBot", "verdict": {"verdict": "changes", "current": True}}, "L3-review"))
    ck("without a gate the words are the reviewer's",
       factory_waiting._crew_has({"verdict": {}}, "L3-review") == "The reviewer reviews it")

    print("  -- nothing to act on --")
    for state in ("pending", "stale", "none"):
        FakeGate.answer = Verdict(state=state, head="d0d0d0d0001", by="FakeBot", summary="x")
        t = await refresh()
        ck(f"{state}: nothing recorded, still in review", t["stage"] == "review" and records() == [], t["stage"])
    FakeGate.answer = RuntimeError("the tool is down")
    t = await refresh()
    ck("a gate that raises changes nothing", t["stage"] == "review" and records() == [], t["stage"])
    try:     # directly, so the refresh's own catch-all cannot hide a raise
        cur = store.get_ticket(uid, "T1")
        await factory_api._outside_verdict(store, uid, cur, {**cur["pr"], "head": "d0d0d0d0001"},
                                           ("fakegate", "FakeBot"))
        raised = False
    except Exception:
        raised = True
    ck("and the error stays inside the gate's read", not raised)
    FakeGate.answer = Verdict(state="approved", head="e1e1e1e1002", by="FakeBot", summary="old")
    t = await refresh()
    ck("a verdict for another commit is ignored", t["stage"] == "review" and records() == [], records())
    HEAD["value"] = "not-a-commit"
    FakeGate.calls.clear()
    FakeGate.answer = Verdict(state="approved", head="", by="FakeBot", summary="")
    t = await refresh()
    ck("a head that is not a commit is never sent to the tool", FakeGate.calls == [] and records() == [], FakeGate.calls)
    HEAD["value"] = "d0d0d0d0001"

    print("  -- approved --")
    FakeGate.answer = Verdict(state="approved", head="d0d0d0d0001", by="FakeBot", summary="Looks good")
    t = await refresh()
    rec = records()
    ck("approved: stays in review, one pass on record for the commit",
       t["stage"] == "review" and len(rec) == 1 and rec[0]["verdict"] == "pass"
       and rec[0]["head"] == "d0d0d0d0001" and "FakeBot approved" in rec[0]["verified"], rec)
    await refresh()
    ck("a second read of the same commit adds none", len(records()) == 1, records())
    q = factory_api_queue_all(store)
    row = [x for x in q if x["project_name"] == "Gated"][0]
    ck("the row's verdict is the pass", row["verdict"] and row["verdict"].get("verdict") == "pass", row["verdict"])

    print("  -- changes --")
    HEAD["value"] = "d0d0d0d0003"
    FakeGate.answer = Verdict(state="changes", head="d0d0d0d0003", by="FakeBot", summary="Two problems",
                              findings=["the cart total is wrong", "no test for an empty cart"])
    t = await refresh()
    rec = [r for r in records() if r["head"] == "d0d0d0d0003"]
    ck("changes: one record with the tool's findings", len(rec) == 1 and rec[0]["verdict"] == "changes"
       and len(store.open_findings(gated["id"])) == 2, rec)
    ck("the ticket moves to changes with the tool's words", t["stage"] == "changes" and t["stage_reason"] == "Two problems",
       (t["stage"], t.get("stage_reason")))
    ck("the findings are the tool's, blocking",
       {f["text"] for f in store.open_findings(gated["id"])} >= {"the cart total is wrong", "no test for an empty cart"},
       store.open_findings(gated["id"]))
    t = await refresh()
    ck("a ticket in changes is not asked again for the same commit", len(records()) == 2 and t["stage"] == "changes",
       (len(records()), t["stage"]))
    HEAD["value"] = "d0d0d0d0004"
    FakeGate.answer = Verdict(state="pending", head="d0d0d0d0004", by="FakeBot", summary="")
    t = await refresh()
    ck("new commits put it back in review", t["stage"] == "review", t["stage"])
    FakeGate.answer = Verdict(state="changes", head="d0d0d0d0004", by="FakeBot", summary="", findings=[])
    t = await refresh()
    ck("changes with no findings and no words still moves it, with the tool's name",
       t["stage"] == "changes" and "FakeBot" in (t.get("stage_reason") or ""), (t["stage"], t.get("stage_reason")))


def factory_api_queue_all(store):
    """The pages' queue, read through the daemon's own `fx` (the SimpleNamespace `register` builds)."""
    return FX["fx"].queue()


async def store_reject_and_reoffer(cx, store, mf, as_station):
    from tares.envelope import now_utc
    p = store.upsert_rule_proposal("acme/other", "config key", None, "all", "Check config keys.", ["X T1"], now_utc())
    r = await cx.post(f"/api/rule-proposals/{p['id']}/reject")
    ck("rejecting keeps it", r.json()["state"] == "rejected", r.text)
    again = store.upsert_rule_proposal("acme/other", "config key", None, "all", "Check config keys.", ["X T1"], now_utc())
    ck("a rejected rule is never offered again", again is None, again)


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
