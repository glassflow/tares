# Tares enterprise licence, see ee/LICENSE
"""One crew on Tares, the part that needs a second person (TR-1046, split from tests/test_factory_crew.py).

Two people's crews with the same station names keep their own retry marks: each person reports a
mark for their own crew and sees only theirs, another person's report leaves it alone, an empty
report clears the reporter's marks only, a person's crew-less mark covers a crew of theirs that has
none of its own, and a mark stored the old way (one flat list) still reads as the person local's.
Runs in process with a licence; the single-person part is tests/test_factory_crew.py.

Run: .venv/bin/python ee/tests/test_factory_crew_team.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-factory-crew-team-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-team"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", project="", ago=30):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True},
                                                "project": shop})
            # the cell's own credential has a crew-reviewer too (the person local's)
            await cx.post("/ingest/claude_code", content=json.dumps(st_line("rv1", "crew-reviewer", "reviewer")) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})

        print("== two people's crews with the same station names keep their own retry marks ==")
        keys, who = {}, {}
        async with mk() as cx:
            for n, mail in (("A", "ann@example.com"), ("B", "bob@example.com")):
                r = await cx.post("/api/keys", json={"name": f"{n}-laptop", "scopes": ["admin"],
                                                     "person": {"email": mail, "name": n}})
                keys[n] = {"Authorization": f"Bearer {r.json()['secret']}"}
                who[n] = r.json()["person"]["id"]
            for n, crewname in (("A", "alpha"), ("B", "beta")):
                line = {**st_line(f"rt-{n}", "crew-reviewer", "reviewer"), "factory_crew": crewname,
                        "crew": crewname}
                r = await cx.post("/ingest/claude_code", content=json.dumps(line) + "\n",
                                  headers={"Content-Type": "application/x-ndjson", **keys[n]})
                ck(f"set up: {n}'s crew {crewname} has a crew-reviewer", r.status_code < 300, r.text)

            def row_a(target, fails=1):
                return {"target": target, "fails": fails, "next_at": 1790000000}

            async def put(n, rows, crew=None):
                return await cx.put("/api/crew/claude-retry", headers=keys[n], params={"crew": crew} if crew else {},
                                    json={"stations": {"crew-reviewer": rows} if rows else {}})

            async def mine(n):
                r = await cx.get("/api/crew", headers=keys[n], params={"mine": 1})
                j = r.json()
                return ([x["claude_retry"] for x in j["stations"] if x["name"] == "crew-reviewer"],
                        j["claude_retry"])

            ck("A reports a mark for crew alpha", (await put("A", row_a("3.0.0", 2), "alpha")).status_code == 200)
            ck("B reports a mark for crew beta, which does not remove A's",
               (await put("B", row_a("4.0.0"), "beta")).status_code == 200)
            rows_a, flat_a = await mine("A")
            rows_b, flat_b = await mine("B")
            ck("A sees A's mark on A's station row, and only there",
               rows_a == [row_a("3.0.0", 2)] and flat_a == {"crew-reviewer": row_a("3.0.0", 2)}, f"{rows_a} {flat_a}")
            ck("B sees B's mark on B's station row, and only there",
               rows_b == [row_a("4.0.0")] and flat_b == {"crew-reviewer": row_a("4.0.0")}, f"{rows_b} {flat_b}")
            await put("A", row_a("3.0.0", 3), "alpha")
            rows_b, _ = await mine("B")
            ck("A reporting again leaves B's mark as it was", rows_b == [row_a("4.0.0")], str(rows_b))
            await put("A", None, "alpha")
            rows_a, flat_a = await mine("A")
            rows_b, flat_b = await mine("B")
            ck("A's empty report clears A's marks only",
               rows_a == [None] and flat_a == {} and rows_b == [row_a("4.0.0")]
               and flat_b == {"crew-reviewer": row_a("4.0.0")}, f"{rows_a} {flat_a} {rows_b} {flat_b}")
            await put("B", row_a("5.0.0"))
            await put("B", None, "beta")
            rows_b, _ = await mine("B")
            ck("a person's crew-less mark covers a crew of theirs that has none of its own",
               rows_b == [row_a("5.0.0")], str(rows_b))
            await put("B", None)
            await cx.put("/api/crew/claude-retry", json={"stations": {}})

            store = app.state.store
            store.set_setting("crew_claude_retry", json.dumps({"crew-reviewer": row_a("6.0.0", 4)}))
            r = (await cx.get("/api/crew")).json()
            ck("a mark stored the old way (one flat list) still reads, as the person local's",
               r["claude_retry"] == {"crew-reviewer": row_a("6.0.0", 4)}
               and all(x["claude_retry"] == row_a("6.0.0", 4) for x in r["stations"] if x["name"] == "crew-reviewer"
                       and (x["person"] or {}).get("id") in (None, "local")), str(r["claude_retry"]))
            await cx.put("/api/crew/claude-retry", json={"stations": {}})

asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
