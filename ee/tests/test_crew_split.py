# Tares enterprise licence, see ee/LICENSE
"""One crew stays one crew when a cell gets its licence (TR-1223 follow-up).

Without a licence the cell keeps no machine for a crew, so a person's crew is (person, name, ''). Once a
licence is on, the next session names the machine, and before this fix that made a second crew: the
Crew page showed the same person twice, the old one empty and ended. Now:

  - a session that names a machine joins the person's crew of that name that has none, which takes it;
  - a session with no machine (the licence gone again) joins the person's only crew of that name;
  - with two crews of that name on two machines, a session with no machine can't be placed: a new crew;
  - a cell that already split merges them when it starts: the sessions move, the bare record goes;
  - the Crew page puts a crew whose members all ended over a day ago last, marked ended, and gives
    each crew its own name.

Run: .venv/bin/python ee/tests/test_crew_split.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-crew-split-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-split"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, machine, crew="crew", ago=30):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": "", "factory_station": station,
         "factory_crew": crew, "crew": crew}
    if machine:
        o["factory_machine"] = o["machine"] = machine
    return o


def state(sid, s, ago):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago), "state": s, "reason": ""}


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    mk = lambda h=None: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                          headers=h or root)
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
            keys, pid = {}, {}
            for who in ("ashish", "adam", "kim"):
                r = await cx.post("/api/keys", json={"name": f"{who}-laptop", "scopes": ["admin", "ingest", "read"],
                                                     "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]

        async def push(who, lines):
            async with mk(keys[who]) as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines), headers={"Content-Type": "application/x-ndjson"})
                assert r.status_code < 300, r.text

        def crews_of(person):
            return [c for c in store.list_crews() if c["person_id"] == person]

        print("== a crew from before the licence takes the machine ==")
        # as a cell without a licence records it: no machine
        with store._swrite:
            bare = store._crew_id("crew", pid["ashish"], "", datetime.now(timezone.utc) - timedelta(days=2))
        await push("ashish", [st_line("a-o2", "crew-orchestrator", "orchestrator", "ashish-mbp", ago=5),
                              state("a-o2", "working", 1)])
        mine = crews_of(pid["ashish"])
        ck("the session joined the crew that had no machine, and it took the machine",
           len(mine) == 1 and mine[0]["id"] == bare and mine[0]["machine"] == "ashish-mbp", mine)
        await push("ashish", [st_line("a-o3", "crew-reviewer", "reviewer", "", ago=3)])
        ck("a session with no machine (the licence gone again) joins the person's only crew of that name",
           len(crews_of(pid["ashish"])) == 1, crews_of(pid["ashish"]))

        print("== two machines can't be told apart ==")
        await push("kim", [st_line("k1", "crew-orchestrator", "orchestrator", "kim-a", ago=10),
                           st_line("k2", "crew-orchestrator", "orchestrator", "kim-b", ago=9)])
        await push("kim", [st_line("k3", "crew-reviewer", "reviewer", "", ago=8)])
        ck("with crews on two machines, a session with no machine gets a crew of its own",
           sorted(c["machine"] for c in crews_of(pid["kim"])) == ["", "kim-a", "kim-b"], crews_of(pid["kim"]))

        print("== a cell that already split merges them when it starts ==")
        with store._swrite:
            old = store._crew_id("crew", pid["adam"], "", datetime.now(timezone.utc) - timedelta(days=3))
            new = "c_adamnew001"
            store.sc.execute("INSERT INTO crews (id, name, person_id, machine, first_seen, last_seen) VALUES "
                             "(?, 'crew', ?, 'adam-mbp', ?, ?)", [new, pid["adam"], datetime.now(timezone.utc),
                                                                 datetime.now(timezone.utc)])
        store.set_session_station("d-old", "crew-orchestrator", "orchestrator", "", None,
                                  datetime.now(timezone.utc) - timedelta(days=3), old)
        store.set_session_station("d-new", "crew-orchestrator", "orchestrator", "", None, datetime.now(timezone.utc), new)
        ck("before the merge Adam has two crews", len(crews_of(pid["adam"])) == 2)
        with store._swrite:
            store._merge_split_crews()
        after = crews_of(pid["adam"])
        moved = store.sc.execute("SELECT crew_id FROM session_stations WHERE session = 'd-old'").fetchone()[0]
        ck("after it: one crew, on its machine, with the old sessions in it, from when the old one began",
           len(after) == 1 and after[0]["id"] == new and moved == new
           and after[0]["first_seen"] < datetime.now(timezone.utc) - timedelta(days=2), after)
        with store._swrite:
            store._merge_split_crews()
        ck("Kim's crews on two machines are left as they are",
           len(crews_of(pid["kim"])) == 3, crews_of(pid["kim"]))

        print("== the Crew page ==")
        await push("adam", [st_line("d-x", "crew-orchestrator", "orchestrator", "adam-mbp", crew="adam-crew", ago=60 * 50),
                            state("d-x", "ended", 60 * 49)])
        async with mk(keys["ashish"]) as cx:
            page = (await cx.get("/api/factory/crew")).json()
        rows = [(c["person"]["name"], c["name"], c["ended"]) for c in page["crews"]]
        ck("a crew whose members all ended over a day ago is marked ended and comes after the live ones",
           rows[-1] == ("Adam", "adam-crew", True) and not any(e for _, _, e in rows[:-1]), rows)
        ck("it says when it was last heard from", (page["crews"][-1]["last_heard"] or "").endswith("ago"), page["crews"][-1])
        ck("each crew carries its own name", {n for _, n, _ in rows} >= {"crew", "adam-crew"}, rows)
        ck("the caller's live crew is first", page["crews"][0]["mine"] and not page["crews"][0]["ended"], page["crews"][0])

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
