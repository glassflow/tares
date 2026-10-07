"""The ledger on Tares (M4: TR-427, TR-428, TR-410, TR-429), plus the fixes from M3's review.

Tickets hold who works them and where they stand; the orchestrator assigns, the station holding a
ticket moves it, the releaser ships it, anyone else is refused with the holder named, and an
unlabeled session moves anything. A ticket's pull request is read from GitHub (a fake GitHub
answers here): head, CI, the reviewer's factory/review status, merged; a merge seen on GitHub
marks the ticket merged; a PR on a factory/<ref> branch links itself; a closed PR disagrees with a
merged ticket. The crew's [TF:...] messages that name a ticket show on it.

M3 review fixes covered: crew stations never count as a project's build session (a pick-up does
not replace the orchestrator), the newest session is a station's current one, a silent waiting
station shows as quiet, grants and shared docs cannot be written around add_grant and the
orchestrator, and renaming a milestone keeps its goal and checks.

Run: .venv/bin/python tests/test_factory_ledger.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-ledger-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-ledger"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory_github
from tares.envelope import Envelope

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── a fake GitHub: acme/shop has PRs 7 (T1) and 8 (T2) ──────────────────────
GH = {
    7: {"head": "aaa111", "state": "open", "merged": False, "branch": "factory/T1",
        "statuses": [{"context": "ci/test", "state": "success"},
                     {"context": "factory/review", "state": "failure", "description": "2 findings"}],
        "runs": [{"status": "completed", "conclusion": "success"}]},
    8: {"head": "bbb222", "state": "open", "merged": False, "branch": "factory/T2",
        "statuses": [], "runs": [{"status": "in_progress", "conclusion": None}]},
}
CALLS = []


def fake_github(request: httpx.Request) -> httpx.Response:
    CALLS.append(request.url.path)
    if request.headers.get("authorization") != "Bearer ghp_good_token_for_tests":
        return httpx.Response(401, json={})
    parts = request.url.path.strip("/").split("/")
    if parts[:3] != ["repos", "acme", "shop"]:
        return httpx.Response(404, json={})
    if parts[3] == "pulls":
        n = int(parts[4])
        pr = GH.get(n)
        if not pr:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={
            "number": n, "title": f"PR {n}", "state": pr["state"], "merged": pr["merged"],
            "merge_commit_sha": "mmm999" if pr["merged"] else None,
            "merged_at": "2026-10-07T12:00:00Z" if pr["merged"] else None,
            "html_url": f"https://github.com/acme/shop/pull/{n}",
            "head": {"sha": pr["head"], "ref": pr["branch"]}})
    if parts[3] == "commits":
        sha = parts[4]
        pr = next((p for p in GH.values() if p["head"] == sha), {"statuses": [], "runs": []})
        if parts[5] == "status":
            return httpx.Response(200, json={"statuses": pr["statuses"]})
        return httpx.Response(200, json={"check_runs": pr["runs"]})
    return httpx.Response(404, json={})


factory_github._transport = httpx.MockTransport(fake_github)


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def main():
    from tares.daemon import make_app
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
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
        return r

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        await mf.write_milestone("Store", "A shop", [{"check": "curl /", "expect": "200"}], project="Shop")
        for t in ("Catalog", "Cart", "Checkout"):
            await m.write_ticket(t, milestone="Store", project="Shop")
        store.upsert_github_credential("gh", "ghp_good_token_for_tests")

        print("== who may move a ticket (TR-428) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.set_stage("T1", "doing", project="Shop")
        ck("an unassigned ticket cannot be moved by a station", "no holder yet" in bad, bad)
        bad = await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
        ck("a builder cannot assign", "only the orchestrator" in bad, bad)
        as_station("crew-orchestrator", "orchestrator")
        out = json.loads(await mf.assign_ticket("T1", "shop-build-m1", project="Shop"))
        ck("the orchestrator assigns; the ticket starts at todo",
           out["holder"] == "shop-build-m1" and out["stage"] == "todo", out)
        await mf.assign_ticket("T2", "shop-build-m1", project="Shop")
        await mf.assign_ticket("T3", "shop-build-m2", project="Shop")
        bad = await mf.set_stage("T1", "doing", project="Shop")
        ck("the orchestrator does not move a stage", "held by shop-build-m1" in bad, bad)
        as_station("crew-reviewer", "reviewer")
        bad = await mf.set_stage("T1", "review", project="Shop")
        ck("a reviewer is refused, the holder named", "held by shop-build-m1" in bad
           and "reassign" in bad, bad)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        out = json.loads(await mf.set_stage("T1", "doing", project="Shop"))
        ck("the holder moves its ticket; a Tares ticket's status follows",
           out["stage"] == "doing" and out["status"] == "in_progress", out)
        bad = await mf.set_stage("T3", "doing", project="Shop")
        ck("a builder cannot move another builder's ticket", "held by shop-build-m2" in bad, bad)
        as_station("shop-build-m1-test", "helper", "shop-build-m1")
        out = json.loads(await mf.set_stage("T2", "doing", project="Shop"))
        ck("a helper started by the holder counts as the holder", out["stage"] == "doing", out)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.set_stage("T1", "shipped", project="Shop")
        ck("only the releaser ships", "only the releaser" in bad, bad)
        bad = await mf.set_stage("T1", "blocked", project="Shop")
        ck("blocked needs a reason", "say why" in bad, bad)
        bad = await mf.set_stage("T1", "done", project="Shop")
        ck("an unknown stage is refused", "unknown stage" in bad, bad)

        print("== the pull request from GitHub (TR-410) ==")
        out = json.loads(await mf.set_stage("T1", "review", pr="https://github.com/acme/shop/pull/7",
                                            project="Shop"))
        ck("linking the PR reads it: head, CI green, the reviewer asked for changes",
           out["pr"]["head"] == "aaa111" and out["pr"]["ci"] == "success"
           and out["pr"]["verdict"] == "changes" and not out["pr"]["merged"], out)
        bad = await mf.set_stage("T2", "review", pr="not a pr", project="Shop")
        ck("a PR that is not one is refused", "pull request's URL" in bad, bad)
        as_station()
        # T2's PR appears on GitHub on its factory branch; Tares links it on its own
        store.append([Envelope(source="github_shop", source_type="event_stream", key_value="acme/shop#8",
                               event_type="pull_request", text="PR #8 opened",
                               event_time=datetime.now(timezone.utc), payload={},
                               labels={"repo": "acme/shop", "number": "8", "branch": "factory/T2",
                                       "action": "opened"})])
        lt = {t["label"]: t for t in json.loads(await m.list_tickets(project="Shop"))["tickets"]}
        ck("a PR on factory/T2 in the project's repo links itself, CI running",
           lt["T2"]["pr"] and lt["T2"]["pr"].get("head") == "bbb222"
           and lt["T2"]["pr"].get("ci") == "pending", lt["T2"])
        n_calls = len(CALLS)
        await m.list_tickets(project="Shop")
        ck("a fresh read is not repeated within the minute", len(CALLS) == n_calls, CALLS[n_calls:])
        GH[7].update(merged=True, state="closed")
        t1 = store.get_ticket(shop, "T1")
        store.update_ticket(t1["id"], pr={**t1["pr"], "checked_at": None})
        g = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("a merge seen on GitHub marks the ticket merged, status done",
           g["stage"] == "merged" and g["status"] == "done" and g["pr"]["merged"], g)
        ck("and the history says GitHub did it",
           g["history"][-1]["by"] == "github" and g["history"][-1]["value"] == "merged", g["history"])
        ck("the history keeps every move with who made it",
           [(h["field"], h["value"], h["by"]) for h in g["history"]][:3]
           == [("holder", "shop-build-m1", "crew-orchestrator"), ("stage", "doing", "shop-build-m1"),
               ("stage", "review", "shop-build-m1")], g["history"])
        GH[8].update(state="closed")
        t2 = store.get_ticket(shop, "T2")
        store.update_ticket(t2["id"], stage="merged", pr={**t2["pr"], "checked_at": None})
        lt = {t["label"]: t for t in json.loads(await m.list_tickets(project="Shop"))["tickets"]}
        ck("merged on Tares but closed unmerged on GitHub: the ticket says so",
           "closed without merging" in (lt["T2"].get("pr_note") or ""), lt["T2"])
        as_station("crew-releaser", "releaser")
        out = json.loads(await mf.set_stage("T1", "shipped", project="Shop"))
        ck("the releaser ships", out["stage"] == "shipped", out)
        as_station()
        out = json.loads(await mf.set_stage("T3", "blocked", reason="needs a payments key", project="Shop"))
        ck("an unlabeled session moves anything", out["stage"] == "blocked"
           and out["stage_reason"] == "needs a payments key", out)

        print("== the crew's messages on the ticket (TR-429) ==")
        def send(sid, to, text, minutes):
            return {"type": "assistant", "sessionId": sid, "cwd": "/w", "timestamp": ago(minutes),
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": f"x{minutes}", "name": "SendMessage",
                         "input": {"to": to, "message": text}}]}}
        await ingest([
            {"type": "session_station", "sessionId": "o1", "cwd": "/w", "timestamp": ago(30),
             "station": "crew-orchestrator", "role": "orchestrator"},
            {"type": "session_station", "sessionId": "b1", "cwd": "/w", "timestamp": ago(30),
             "station": "shop-build-m1", "role": "builder", "parent": "crew-orchestrator",
             "tares_project": "Shop"},
            {"type": "session_station", "sessionId": "r1", "cwd": "/w", "timestamp": ago(30),
             "station": "crew-reviewer", "role": "reviewer"},
            send("o1", "shop-build-m1", "[TF:TASK] T1 Catalog\nticket: T1", 20),
            send("b1", "crew-reviewer", "[TF:REVIEW] T1 PR #7\npr: https://github.com/acme/shop/pull/7", 15),
            send("r1", "shop-build-m1", "[TF:VERDICT] changes on T1\nfindings: 2", 10),
            send("b1", "crew-orchestrator", "[TF:DONE] T2 merged", 5),
            send("o1", "someone", "plain message about T1 without a type", 4),
        ])
        g = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("T1 shows task, review, verdict in order, nothing else",
           [x["type"] for x in g.get("messages", [])] == ["TASK", "REVIEW", "VERDICT"], g.get("messages"))
        ck("each with sender and receiver stations",
           (g["messages"][1]["from"], g["messages"][1]["to"]) == ("shop-build-m1", "crew-reviewer"),
           g["messages"])

        print("== M3 review fixes ==")
        await ingest([
            {"type": "session_project", "sessionId": "o1", "cwd": "/w", "timestamp": ago(3),
             "tares_project": "Shop"},
            {"type": "session_state", "sessionId": "o1", "cwd": "/w", "timestamp": ago(2),
             "state": "waiting", "reason": "the turn ended"}])
        async with mk() as cx:
            ss = (await cx.get(f"/api/projects/{shop}/sessions")).json()["sessions"]
            ck("the orchestrator naming the project is not one of its build sessions",
               "o1" not in [s["session"] for s in ss], ss)
            pk = (await cx.get(f"/api/projects/{shop}/pickup")).json()
            ck("so a pick-up never offers to replace it",
               (pk.get("previous_session") or {}).get("session") != "o1", pk.get("previous_session"))
        await ingest([
            {"type": "session_station", "sessionId": "k1", "cwd": "/w", "timestamp": ago(60),
             "station": "crew-releaser", "role": "releaser"},
            {"type": "session_state", "sessionId": "k1", "cwd": "/w", "timestamp": ago(59), "state": "working"},
            {"type": "session_station", "sessionId": "k2", "cwd": "/w", "timestamp": ago(20),
             "station": "crew-releaser", "role": "releaser"},
            {"type": "session_state", "sessionId": "k2", "cwd": "/w", "timestamp": ago(19), "state": "ended"},
            {"type": "session_station", "sessionId": "w1", "cwd": "/w", "timestamp": ago(200),
             "station": "crew-idle", "role": "reviewer"},
            {"type": "session_state", "sessionId": "w1", "cwd": "/w", "timestamp": ago(199), "state": "waiting"}])
        async with mk() as cx:
            crew = {s["name"]: s for s in (await cx.get("/api/crew")).json()["stations"]}
        ck("the newest session is the station's: a killed older one does not outrank it",
           crew["crew-releaser"]["session"] == "k2" and crew["crew-releaser"]["state"] == "ended", crew["crew-releaser"])
        ck("a station waiting in silence for over 90 min shows quiet", crew["crew-idle"]["state"] == "quiet",
           crew["crew-idle"])
        as_station()
        await m.write_doc("spec", "Spec", "A shop.", project="Shop")   # brings the shared docs
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await m.write_doc("grants", "Grants", "anything goes", project="Shop")
        ck("grants cannot be written as a doc", "add_grant" in bad, bad)
        docs = json.loads(await m.list_docs(project="Shop"))
        agents = next(d for d in docs if d["kind"] == "agents" and d["global"])
        bad = await m.write_doc("agents", "AGENTS.md (all projects)", "# taken over", id=agents["id"], project="Shop")
        ck("a builder cannot rewrite a shared doc", "only the orchestrator" in bad, bad)
        as_station()
        out = json.loads(await mf.write_milestone("Storefront", id="Store", project="Shop"))
        ms = json.loads(await mf.list_milestones(project="Shop"))
        ck("renaming a milestone keeps its goal and checks",
           out["name"] == "Storefront" and ms[0]["goal"] == "A shop" and ms[0]["checks"], ms)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
