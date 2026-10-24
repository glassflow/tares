"""Builders start knowing the repo (TR-683).

One builder brief call returns, for a ticket: where it stands, the ticket's working doc, its
milestone and acceptance checks, the state of what it depends on, the project's and the shared
AGENTS.md, the assumptions and desk questions open on it, and where the builder left off. Starting
a ticket is that one call. A builder hands over its checks with the stage change or in one
add_check call instead of one call each, and a non-holder records nothing. Crew stats count the
calls that only coordinate (Tares and Linear tools, messages, skills, the schedule) per station and
per merged PR, so a regression shows. The builder manual's start steps make one Tares read.

Run: .venv/bin/python tests/test_factory_builder.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-builder-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-builder"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

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
            "head": {"sha": HEAD["value"], "ref": "factory/T2"}})
    if parts[5] == "status":
        return httpx.Response(200, json={"statuses": []})
    return httpx.Response(200, json={"check_runs": []})


factory_github._transport = httpx.MockTransport(fake_github)


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def unit():
    print("== which calls only coordinate (TR-683) ==")
    yes = ["mcp__tares__get_ticket", "mcp__plugin_tares_tares__set_stage", "mcp__linear-personal__get_issue",
           "SendMessage", "ListAgents", "Skill", "CronCreate", "CronList", "CronDelete"]
    no = ["Bash", "Read", "Edit", "Agent", "Write", "mcp__claude-in-chrome__navigate", "", None]
    ck("Tares, Linear, messages, skills and the schedule count", all(factory.is_coordination(t) for t in yes))
    ck("work does not", not any(factory.is_coordination(t) for t in no))


def tool_line(sid, name, inp, minutes_ago=0):
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/shop", "timestamp": ago(minutes_ago),
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"{sid}-{name}-{minutes_ago}", "name": name, "input": inp}]}}


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
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        as_station("crew-orchestrator", "orchestrator")
        await mf.write_milestone("Store", "A shop that takes orders",
                                 [{"check": "curl /cart", "expect": "an empty cart as JSON"}], project="Shop")
        await m.write_ticket("Catalog", milestone="Store", project="Shop")
        await m.write_ticket("Cart", milestone="Store", depends_on=["T1"], project="Shop")
        await m.write_ticket("Checkout", milestone="Store", project="Shop")
        wd = json.loads(await m.write_doc("working", "Working: Cart",
                                          "## Goal\nAdd and remove cart lines.\n\n## Progress\nNot started.\n",
                                          project="Shop"))
        await m.attach_working_doc("T2", wd["id"], project="Shop")
        await m.write_doc("agents", "AGENTS.md", "Run tests with `pytest -q`. Branch factory/<ref>.",
                          project="Shop")
        await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
        await mf.assign_ticket("T2", "shop-build-m1", project="Shop")
        await mf.assign_ticket("T3", "shop-build-m2", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.assume("Cart lines per user?", "50", "a common cap", ["T2"], "T2", project="Shop")
        await mf.assume("Currency?", "EUR", "Berlin", ["T3"], project="Shop")
        as_station("crew-orchestrator", "orchestrator")
        await mf.desk_add("Cart: guest carts allowed?", "yes", "less friction", ["yes", "no"],
                          tickets=["T2"], blocking=True, project="Shop")
        await mf.desk_add("Logo colour?", "blue", "brand", tickets=["T3"], project="Shop")

        print("== one brief call (TR-683) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        brief = await mf.builder_brief("T2", project="Shop")
        ck("the ticket and where it stands", brief.startswith("# Builder brief: T2 Cart")
           and "Held by shop-build-m1" in brief, brief[:200])
        ck("its working doc", "Add and remove cart lines." in brief, brief)
        ck("its milestone with the acceptance checks",
           "Milestone: Store" in brief and "A shop that takes orders" in brief
           and "`curl /cart` should show: an empty cart as JSON" in brief, brief)
        ck("what it depends on, and that it is not done", "T1 Catalog" in brief and "NOT DONE" in brief, brief)
        ck("the project's AGENTS.md", "Run tests with `pytest -q`" in brief, brief)
        ck("the open assumption on it, not the one on another ticket",
           "Cart lines per user?" in brief and "Currency?" not in brief, brief)
        ck("the desk question on it, not the one on another ticket",
           "guest carts allowed?" in brief and "(blocking)" in brief and "Logo colour" not in brief, brief)
        ck("pointers to the rest, not the rest", "## Read as needed" in brief, brief)
        ck("no em dashes", "—" not in brief, brief)

        print("== starting the ticket is the same call (TR-683) ==")
        out = await mf.builder_brief("T2", start=True, project="Shop")
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("start moves it to doing and still returns the brief", g["stage"] == "doing"
           and out.startswith("# Builder brief: T2"), (g["stage"], out[:80]))
        as_station("shop-build-m2", "builder", "crew-orchestrator")
        out = await mf.builder_brief("T1", start=True, project="Shop")
        ck("a builder that does not hold it gets the refusal and the brief",
           out.startswith("Could not move the ticket to doing") and "# Builder brief: T1" in out, out[:300])
        g = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("and the ticket did not move", g["holder"] == "shop-build-m1" and g["stage"] in (None, "todo"),
           (g["holder"], g["stage"]))
        await m.write_ticket("Old", status="done", project="Shop")
        as_station("crew-orchestrator", "orchestrator")
        await mf.assign_ticket("T4", "shop-build-m1", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.builder_brief("T4", start=True, project="Shop")
        ck("start leaves a ticket that is already done alone",
           json.loads(await m.get_ticket("T4", project="Shop"))["stage"] in (None, "todo", "merged"))
        as_station("crew-orchestrator", "orchestrator")
        await m.write_ticket("Dropped", status="canceled", project="Shop")
        await m.write_ticket("After dropped", depends_on=["T5"], project="Shop")
        out = await mf.builder_brief("T6", project="Shop")
        ck("a canceled dependency is named, not counted as done",
           "T5 Dropped: " in out and "CANCELED" in out and "NOT DONE" not in out, out[:400])
        as_station("shop-build-m2", "builder", "crew-orchestrator")
        bad = await mf.builder_brief("T99", project="Shop")
        ck("an unknown ticket is an error, not a crash", "brief" not in bad.lower() or "T99" in bad, bad)

        print("== checks ride with the call that already happens (TR-683) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        out = json.loads(await mf.add_check(
            "T2", "pytest -q", "14 passed", "1a2b3c4", "test_cart",
            more=[{"command": "ruff check .", "result": "clean", "commit": "1a2b3c4"}], project="Shop"))
        ck("add_check with more records both and returns both lines", len(out["lines"]) == 2
           and out["line"] == out["lines"][0], out)
        bad = await mf.add_check("T2", "make", "ok", more=[{"command": "x"}], project="Shop")
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("a half-written check in the list refuses the whole call",
           "what it showed" in bad and [c["command"] for c in g["checks"]] == ["pytest -q", "ruff check ."], (bad, g["checks"]))
        out = json.loads(await mf.set_stage(
            "T2", "review", pr="https://github.com/acme/shop/pull/7",
            checks=[{"command": "python tests/test_cart.py", "result": "3 passed", "commit": "1a2b3c4"}],
            project="Shop"))
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("set_stage review with checks records them and moves the ticket", out["stage"] == "review"
           and [c["command"] for c in g["checks"]][-1] == "python tests/test_cart.py"
           and "python tests/test_cart.py" in g["working_doc_body"], (out, g["checks"]))
        as_station("shop-build-m2", "builder", "crew-orchestrator")
        bad = await mf.set_stage("T2", "changes", checks=[{"command": "c", "result": "r"}], project="Shop")
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("a non-holder's stage change records no checks", len(g["checks"]) == 3 and g["stage"] == "review", (bad, g["checks"]))
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.set_stage("T2", "review", checks=[{"command": "c"}], project="Shop")
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("a bad check stops the stage change", "what it showed" in bad and len(g["checks"]) == 3, (bad, g["checks"]))
        out = await mf.builder_brief("T2", start=True, project="Shop")
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("start on a ticket already in review leaves it there", g["stage"] == "review"
           and "Could not move" not in out, (g["stage"], out[:120]))
        store.upsert_github_credential("gh", "ghp_x")
        t2 = store.get_ticket(shop, "T2")
        store.update_ticket(t2["id"], pr={**t2["pr"], "checked_at": ago(600)})
        HEAD["value"] = "bbbbbbb2222"
        out = await mf.builder_brief("T2", project="Shop")
        ck("the brief reads the pull request again, so a revive sees its new head", "head bbbbbbb2222" in out, store.get_ticket(shop, "T2")["pr"])
        again = await mf.builder_brief("T2", project="Shop")
        ck("after a revive the brief says where it left off",
           "## Where you left off" in again and "`pytest -q` -> 14 passed (broke test_cart)" in again
           and "pull/7" in again, again)

        print("== coordination calls, per station and per merged PR (TR-683) ==")
        await ingest([
            {"type": "session_station", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ago(30),
             "station": "shop-build-m1", "role": "builder", "parent": "crew-orchestrator", "tares_project": "Shop"},
            tool_line("b1", "mcp__tares__get_ticket", {"ticket": "T2"}, 20),
            tool_line("b1", "mcp__plugin_tares_tares__set_stage", {"ticket": "T2"}, 19),
            tool_line("b1", "SendMessage", {"to": "crew-reviewer", "message": "[TF:REVIEW] T2"}, 18),
            tool_line("b1", "Skill", {"skill": "station-builder"}, 17),
            tool_line("b1", "CronCreate", {"cron": "*/30 * * * *"}, 16),
            tool_line("b1", "Bash", {"command": "pytest -q"}, 15),
            tool_line("b1", "Read", {"file_path": "/w/shop/a.py"}, 14),
        ])
        async with mk() as cx:
            crew = (await cx.get("/api/crew")).json()
            row = next(s for s in crew["stations"] if s["name"] == "shop-build-m1")
            ck("the station row counts coordination calls, not Bash or Read", row["coordination_calls"] == 5, row)
            pc = (await cx.get(f"/api/projects/{shop}/crew")).json()["coordination"]
            ck("no merged PR yet: calls, no ratio", pc["calls"] == 5 and pc["merged_prs"] == 0
               and pc["per_merged_pr"] is None, pc)
        await mf.set_stage("T2", "merged", project="Shop")
        async with mk() as cx:
            pc = (await cx.get(f"/api/projects/{shop}/crew")).json()["coordination"]
            ck("one merged PR: calls per merged PR", pc["merged_prs"] == 1 and pc["per_merged_pr"] == 5.0
               and pc["stations"] == {"shop-build-m1": 5}, pc)
        as_station("crew-orchestrator", "orchestrator")
        listed = json.loads(await mf.list_crew())
        ck("list_crew shows it", next(s for s in listed if s["name"] == "shop-build-m1")["coordination_calls"] == 5, listed)

    print("== the builder manual: start is one Tares read (TR-683) ==")
    manual = open(os.path.join(ROOT, "claude-plugin", "skills", "station-builder", "SKILL.md")).read()
    start = re.search(r"\n1\. \*\*Start\.\*\*(.*?)\n2\. ", manual, re.S).group(1)
    boot = re.search(r"\n3\. Read your `TASK`(.*?)\n4\. ", manual, re.S).group(1)
    ck("start makes the one brief call", "builder_brief(ticket, start=True)" in start, start[:200])
    ck("boot reads each ticket with the brief, not get_ticket and list_docs", "builder_brief(ticket)" in boot
       and "get_ticket" not in boot and "list_docs" not in boot, boot[:300])
    ck("a revive reads the brief too", "revive" in boot, boot)
    ck("the review request carries the checks", "checks=[...]" in manual)


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
