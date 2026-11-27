"""The `joiner` block of GET /api/home (TR-1719): is the caller joining a factory someone else
set up?

{joining, team_settings, team_crews, own_crew_up}. A person other than the owner is joining when
the cell's own crew settings name an autonomy and another person's crew has reported. The owner and
a key with no person get nothing about a team. Everything goes through the real GET /api/home with
real keys, but for two station shapes no recording makes, which call joiner_block directly.

Run: .venv/bin/python tests/test_factory_home_joiner.py
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-factory-joiner-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-joiner"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory_home as fh

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station(crew, role, machine, ago=2):
    """The two lines one station of `crew` records: who it is, and that it is idle."""
    sid, name = f"s-{crew}-{role}", f"{crew}-{role}"
    return [{"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
             "station": name, "role": role, "parent": "", "factory_station": name,
             "factory_crew": crew, "crew": crew, "factory_machine": machine, "machine": machine},
            {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
             "state": "idle", "reason": ""}]


NOTHING = {"joining": False, "team_settings": None, "team_crews": 0}
TEAM = {"autonomy": "L3-review", "releaser": False, "builders_max": 3, "challenger": True}


def about_team(j: dict) -> dict:
    return {k: j.get(k) for k in NOTHING}


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        store = app.state.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     timeout=30) as cx:
            async def home(who=root, fresh=True):
                if fresh:
                    fh.clear_cache()
                r = await cx.get("/api/home", headers=who)
                assert r.status_code == 200, r.text
                return r.json()

            async def joiner(who=root):
                return (await home(who)).get("joiner") or {}

            async def push(who, *stations):
                body = "\n".join(json.dumps(x) for lines in stations for x in lines) + "\n"
                r = await cx.post("/ingest/claude_code", content=body,
                                  headers={**who, "Content-Type": "application/x-ndjson"})
                assert r.status_code < 300, r.text

            async def settings(who, body):
                r = await cx.put("/api/crew/settings", json=body, headers=who)
                assert r.status_code == 200, r.text
                return r.json()

            def key(kid, person=None, scopes=("read", "ingest")):
                token = f"nvf_{kid}_{kid}-secret-for-the-test"
                store.insert_api_key(kid, kid, f"nvf_{kid}", hashlib.sha256(token.encode()).hexdigest(),
                                     list(scopes), person_id=person)
                return {"Authorization": f"Bearer {token}"}

            r = await cx.post("/api/sources", headers=root, json={
                "name": "claude_code", "connector": "claude_code", "poll": "10s", "config": {"push": True}})
            assert r.status_code < 300, r.text

            print("== a cell of one person ==")
            h = await home()
            ck("a cell of one: the block is there, with its four keys",
               set(h.get("joiner") or {}) == {"joining", "team_settings", "team_crews", "own_crew_up"},
               h.get("joiner"))
            ck("a cell of one: nobody is joining",
               h.get("joiner") == {**NOTHING, "own_crew_up": False}, h.get("joiner"))
            alone = fh.home(store, None, datetime.now(timezone.utc)).get("joiner")
            ck("a call with no request: the block is there, with nothing about a team",
               alone == {**NOTHING, "own_crew_up": False}, alone)

            print("== a second person, and their own crew only ==")
            ana = store.create_person("Ana Example", "ana@example.test")["id"]
            ben = store.create_person("Ben Example", "ben@example.test")["id"]
            ana_key, ben_key = key("kana", ana), key("kben", ben)
            owner_key, no_person = key("kowner", "local"), key("knobody")
            await settings(root, TEAM)
            ck("own_crew_up: false before the member's crew reports",
               (await joiner(ana_key)).get("own_crew_up") is False, await joiner(ana_key))
            await push(ana_key, station("ana", "orchestrator", "m-ana"))
            ck("own_crew_up: false with the orchestrator alone",
               (await joiner(ana_key)).get("own_crew_up") is False, await joiner(ana_key))
            await push(ana_key, station("ana", "reviewer", "m-ana"))
            j = await joiner(ana_key)
            ck("own_crew_up: true once the orchestrator and the reviewer have reported",
               j.get("own_crew_up") is True, j)
            ck("a member's own crew is not another's: nobody to join",
               j.get("joining") is False and j.get("team_crews") == 0, j)
            ck("the team's settings are there before anyone else's crew is",
               j.get("team_settings") == TEAM, j)

            print("== stations no recording makes ==")
            fx = SimpleNamespace(caller_person=lambda r: {"id": ana}, is_system=lambda r: False,
                                 is_admin=lambda r: False)
            seen = datetime.now(timezone.utc)

            def of(person, last_at):
                row = {"role": "builder", "state": "idle", "person": person, "last_at": last_at}
                return fh.joiner_block(store, fx, object(), [row], {})

            got = of({"id": ben}, seen)
            ck("(the direct call sees another person's station that has reported)",
               got["team_crews"] == 1 and got["joining"] is True, got)
            got = of({"id": ben}, None)
            ck("another person's station that never reported does not count",
               got["team_crews"] == 0 and got["joining"] is False, got)
            got = of(None, seen)
            ck("a station of no person does not count",
               got["team_crews"] == 0 and got["joining"] is False, got)
            fx.own_stations = lambda r, rows: [s for s in rows if s["person"]["id"] == ana]

            def up_for(person):
                rows = [{"role": role, "state": "idle", "person": {"id": person}, "last_at": seen}
                        for role in ("orchestrator", "reviewer")]
                return fh.joiner_block(store, fx, object(), rows, {})["own_crew_up"]

            ck("called alone, it cuts the stations to the caller's own",
               (up_for(ana), up_for(ben)) == (True, False), (up_for(ana), up_for(ben)))

            print("== the owner has two crews ==")
            await push(root, station("first", "orchestrator", "m-1"), station("first", "reviewer", "m-1"),
                       station("second", "orchestrator", "m-2"))
            owners = [c for c in store.list_crews() if c["person_id"] == "local"]
            ck("(the owner's two crews are two crews)", len(owners) == 2, owners)
            j = await joiner(ana_key)
            ck("the second person is joining", j.get("joining") is True, j)
            ck("with the four values the team stored", j.get("team_settings") == TEAM, j)
            ck("two crews of one other person count once", j.get("team_crews") == 1, j)
            j = await joiner(ben_key)
            ck("a third person counts both of the others", j.get("team_crews") == 2, j)
            ck("another person's crew is never the member's own", j.get("own_crew_up") is False, j)

            for who, headers in (("the cell's token", root), ("the owner's key", owner_key)):
                j = await joiner(headers)
                ck(f"{who}: not joining, and nothing about a team", about_team(j) == NOTHING, j)
                ck(f"{who}: their own crew is up", j.get("own_crew_up") is True, j)
            j = await joiner(no_person)
            ck("a key with no person: not joining, and nothing about a team", about_team(j) == NOTHING, j)
            ck("a key with no person: no other person's crew is its own", j.get("own_crew_up") is False, j)

            print("== one kept home per caller ==")
            fh.clear_cache()
            first = await home(ana_key, fresh=False)
            other = await home(ben_key, fresh=False)
            again = await home(ana_key, fresh=False)
            ck("(the second read of one caller is the kept one)", again["now"] == first["now"],
               (first["now"], again["now"]))
            ck("two people reading inside the cache window get their own block",
               (first["joiner"]["team_crews"], first["joiner"]["own_crew_up"]) == (1, True)
               and (other["joiner"]["team_crews"], other["joiner"]["own_crew_up"]) == (2, False),
               (first["joiner"], other["joiner"]))

            print("== no stored team settings ==")
            await settings(root, {"clear": list(TEAM)})
            j = await joiner(ana_key)
            ck("no team settings: not joining", j.get("joining") is False, j)
            ck("no team settings: none are given", "team_settings" in j and j["team_settings"] is None, j)
            ck("no team settings: the other crews are still counted", j.get("team_crews") == 1, j)
            own = await settings(ana_key, {"autonomy": "L3-review", "releaser": True})
            ck("(the member's own settings are saved)",
               own["autonomy"] == "L3-review" and own["releaser"] is True, own)
            j = await joiner(ana_key)
            ck("a member's own settings are never the team's",
               j.get("joining") is False and "team_settings" in j and j["team_settings"] is None, j)
            await settings(root, {"autonomy": "L4-ship"})
            j = await joiner(ana_key)
            ck("a value the team never set stays null, whatever the member saved",
               j.get("team_settings") == {"autonomy": "L4-ship", "releaser": None, "builders_max": None,
                                          "challenger": None} and j.get("joining") is True, j)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
