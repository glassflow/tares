"""You in the loop (M7: TR-449..453).

One desk for every project (D1, D2... for the cell, blockers first), written only by the
orchestrator. The person answers in the orchestrator's session; the answer is kept word for word
and matched against what they typed there (a paraphrase is "not found"). A standing answer that
matches becomes a grant; one that does not, never. A batch of assumptions is kept or overturned in
one answer, an overturned one naming the tickets it touched. Decisions reach the ticket, the
pick-up and the reviewer's brief.

Run: .venv/bin/python tests/test_factory_desk.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-desk-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-desk"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def unit():
    print("== words as typed ==")
    turns = [{"session": "o1", "text": "Hmm. Use Stripe test mode, and keep it simple!"},
             {"session": "o1", "text": "I don't want to ship it until Friday\nyes"}]
    ck("a whole sentence of theirs matches, case and spacing aside",
       factory.words_match("use  stripe test mode, and keep it simple", turns) is not None)
    ck("a whole line matches", factory.words_match("Yes", turns) is not None)
    ck("a fragment does not", factory.words_match("use stripe test mode", turns) is None
       and factory.words_match("ship it", turns) is None)
    ck("nor a word hidden in another", factory.words_match("ok", [{"text": "token"}]) is None)
    ck("a paraphrase is not", factory.words_match("Go with Stripe", turns) is None)
    ck("one character is not an answer", factory.words_match("y", turns) is None)
    ck("every project only when they say so", factory.says_every_project("fine for every project")
       and not factory.says_every_project("fine for staging"))


async def main():
    from tares.daemon import make_app
    app = make_app()
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
        blog = json.loads(await m.create_project("Blog", "Write"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        for t in ("Catalog", "Payments", "Checkout"):
            await m.write_ticket(t, project="Shop")
        await m.write_ticket("Post", project="Blog")

        print("== one desk (TR-449) ==")
        as_station("crew-orchestrator", "orchestrator")
        d1 = json.loads(await mf.desk_add("Payments: Stripe test mode or Paddle?", "Stripe",
                                          "the spec mentions EU cards and it has a test mode",
                                          ["Stripe", "Paddle"], blocks="shop-build-m1 on T2",
                                          tickets=["T2"], blocking=True, project="Shop"))
        d2 = json.loads(await mf.desk_add("Blog: comments on or off?", "off", "less moderation",
                                          project="Blog"))
        d3 = json.loads(await mf.desk_add("Staging deploys: ask each time?", "no, standing yes",
                                          "they are cheap to undo", project="Shop"))
        ck("one numbering for the cell", [d1["label"], d2["label"], d3["label"]] == ["D1", "D2", "D3"],
           (d1, d2, d3))
        lst = json.loads(await mf.desk_list())
        ck("blockers first, then oldest", [d["label"] for d in lst] == ["D1", "D2", "D3"], lst)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.desk_add("x?", "y", "z", project="Shop")
        ck("a builder cannot write the desk", "only the orchestrator" in bad, bad)
        bad = await mf.desk_answer("D1", "Stripe")
        ck("nor answer it", "only the orchestrator" in bad, bad)

        print("== answers word for word (TR-450) ==")
        def now_plus(seconds):
            t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        await ingest([
            {"type": "session_station", "sessionId": "o1", "cwd": "/w", "timestamp": ago(10),
             "station": "crew-orchestrator", "role": "orchestrator"},
            {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": ago(30),
             "message": {"role": "user", "content": "Paddle, obviously"}},
            {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(1),
             "message": {"role": "user", "content": "Use Stripe test mode for now"}},
            {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(2),
             "message": {"role": "user", "content": "Always fine to deploy to staging"}},
            {"type": "user", "sessionId": "x1", "cwd": "/w", "timestamp": now_plus(2),
             "message": {"role": "user", "content": "Comments off please"}},
        ])
        as_station("crew-orchestrator", "orchestrator")
        out = json.loads(await mf.desk_answer("D1", "Paddle, obviously"))
        ck("words typed before the question was asked do not answer it", out["recorded"] is False, out)
        out = json.loads(await mf.desk_answer("D1", "Use Stripe test mode for now", choice="Stripe"))
        ck("an answer copied from the session matches", out["matches_what_they_typed"], out)
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("the decision reaches its ticket, first", g["decisions"] and g["decisions"][0]["words"]
           == "Use Stripe test mode for now", g.get("decisions"))
        out = json.loads(await mf.desk_answer("D2", "Comments off please"))
        ck("words typed in another session, not the orchestrator's: nothing recorded, still open",
           out["recorded"] is False and out["still_open"]
           and "D2" in [d["label"] for d in json.loads(await mf.desk_list())], out)
        bad = await mf.desk_answer("D1", "again")
        ck("an answered item is closed", "already answered" in bad, bad)
        bad = await mf.desk_answer("D3", "Always fine to deploy to staging", standing=True, scope="all")
        ck("a project's question does not become a grant for every project unless they say so",
           "only when the person says so" in bad, bad)

        print("== standing answers become grants (TR-451) ==")
        out = json.loads(await mf.desk_answer("D3", "Always fine to deploy to staging", standing=True))
        ck("a standing answer that matches becomes a grant", out["became_a_grant"], out)
        txt = await mf.read_grants(project="Shop")
        ck("in the project's grants, quoted and dated",
           '"Always fine to deploy to staging" (20' in txt, txt)
        d4 = json.loads(await mf.desk_add("Prod deploys?", "ask me", "risky", project="Shop"))
        out = json.loads(await mf.desk_answer(d4["label"], "You may always deploy to prod", standing=True))
        ck("a standing answer the person did not type never becomes a grant",
           out["recorded"] is False, out)
        ck("and is not in the grants", "deploy to prod" not in await mf.read_grants(project="Shop"))

        print("== assumption batches (TR-452) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        for q, c, t in (("Currency?", "EUR", "T1"), ("Page size?", "20", "T1"),
                        ("Date format?", "ISO", "T3"), ("Rounding?", "half up", "T2")):
            await mf.assume(q, c, "default", [t], project="Shop")
        as_station("crew-orchestrator", "orchestrator")
        d5 = json.loads(await mf.desk_add("Keep these assumptions?", "keep all four", "they are defaults",
                                          assumptions=["A1", "A2", "A3", "A4"], project="Shop"))
        ck("a batch carries its assumptions", d5["assumptions"] == ["A1", "A2", "A3", "A4"], d5)
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(3),
                       "message": {"role": "user", "content": "keep 1, 2, 4; overturn 3, use the other date format"}}])
        bad = await mf.desk_answer(d5["label"], "keep 1, 2, 4; overturn 3, use the other date format",
                                   kept=["A1", "A2"], overturned={"A3": "use the other date format"})
        ck("a batch answer decides every listed assumption", "left out: A4" in bad, bad)
        out = json.loads(await mf.desk_answer(d5["label"], "keep 1, 2, 4; overturn 3, use the other date format",
                                              kept=["A1", "A2", "A4"],
                                              overturned={"A3": "use the other date format"}))
        ck("three kept, one overturned", sorted((a["label"], a["state"]) for a in out["assumptions"])
           == [("A1", "kept"), ("A2", "kept"), ("A3", "overturned"), ("A4", "kept")], out)
        ck("the overturned one names the tickets it touched", out["follow_up_tickets_needed"]
           == [{"assumption": "A3", "tickets": ["T3"]}], out)
        asm = json.loads(await mf.list_assumptions(project="Shop"))
        a3 = next(a for a in asm if a["label"] == "A3")
        ck("with the person's words", a3["words"] == "use the other date format" and a3["state"] == "overturned", a3)

        print("== decisions travel (TR-451) ==")
        async with mk() as cx:
            pk = (await cx.get(f"/api/projects/{shop}/pickup")).json()
            br = (await cx.get(f"/api/projects/{shop}/tickets/T2/brief")).json()["brief"]
        ck("the pick-up has them", any(d["words"] == "Use Stripe test mode for now" for d in pk["decisions"]), pk["decisions"])
        ck("so does the reviewer's brief", "Use Stripe test mode for now" in br, br[-600:])
        dl = json.loads(await mf.list_decisions(project="Shop"))
        ck("list_decisions, newest first, only what the person typed",
           [d["label"] for d in dl] == ["D5", "D3", "D1"] and all(d["matched"] for d in dl), dl)

        print("== withdrawing ==")
        d6 = json.loads(await mf.desk_add("Logo colour?", "blue", "brand", project="Blog"))
        out = json.loads(await mf.desk_withdraw(d6["label"], "the designer decided"))
        ck("a withdrawn item leaves the open desk", out["state"] == "withdrawn"
           and d6["label"] not in [d["label"] for d in json.loads(await mf.desk_list())], out)
        as_station()
        out = json.loads(await mf.desk_add("A person's own question?", "yes", "testing", project="Blog"))
        ck("a person's own session may use the desk", out["label"] == "D7", out)


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
