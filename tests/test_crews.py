"""The cell keeps a record of each crew and lists stations by crew (TR-628).

A crew is one person's crew on one machine, named by the person: (person, crew name, machine). It is
made from the `session_station` line of a recording, with the person of the credential that shipped
it (never what the line says), the line's `factory_crew` and its `factory_machine`. Covers: two
people with the same crew name on two machines give two crews; each one's `crew-orchestrator` stays
its own (a new session replaces only that crew's); the same person on a second machine is a third
crew; a recording with no person (the root token) and no crew name joins crew `crew` of `local`;
`mine=1` keeps the caller's crews; `/api/crew/{name}` takes `crew` and otherwise returns the
caller's own; a station's children come from its own crew; the flat `stations` key is still there;
`list_crew(mine)`; the plugin stamping the machine; and sessions recorded before crews existed
joining the crew of `local` when the cell starts.

Run: .venv/bin/python tests/test_crews.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-crews-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crews"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import ship
import tares.mcp_server as m
import tares.mcp_factory as mf

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", crew=None, machine=None, ago=30):
    """The session_station line the plugin ships; crew and machine are stamped like the plugin
    does (factory_crew, factory_machine), and left off when None."""
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if crew:
        o["factory_crew"] = o["crew"] = crew
    if machine:
        o["factory_machine"] = o["machine"] = machine
    return o


def say(sid, station, text, ago):
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "factory_station": station, "message": {"role": "assistant", "content": text}}


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def plugin():
    print("== the plugin stamps the machine next to the crew ==")
    keep = {k: os.environ.pop(k, None) for k in ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT",
                                                  "FACTORY_CREW", "FACTORY_MACHINE")}
    try:
        ck("no station: nothing stamped", ship.stamp_station([{"type": "user"}]) == [{"type": "user"}])
        os.environ.update(FACTORY_STATION="a-orchestrator", FACTORY_ROLE="orchestrator",
                          FACTORY_CREW="a", FACTORY_MACHINE="m1")
        got = ship.stamp_station([{"type": "user"}])[0]
        ck("every shipped line carries the machine", got.get("factory_machine") == "m1"
           and got.get("factory_crew") == "a", got)
        del os.environ["FACTORY_MACHINE"]
        host = ship.machine_name()
        ck("without FACTORY_MACHINE it is the short host name", host and "." not in host
           and ship.stamp_station([{"type": "user"}])[0].get("factory_machine") == host, host)
        from tares.connectors.claude_code import ClaudeCodeConnector as C
        lab = C.label_context(C.__new__(C), {"sessionId": "s", "factory_machine": "m1"})
        ck("the transcript label machine comes from factory_machine", lab.get("machine") == "m1", lab)
        ck("machine is a declared label axis", any(p["name"] == "machine" for p in C.PROVIDES))
    finally:
        for k in ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT", "FACTORY_CREW", "FACTORY_MACHINE"):
            os.environ.pop(k, None)
            if keep[k] is not None:
                os.environ[k] = keep[k]


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk
    nd_h = {"Content-Type": "application/x-ndjson"}

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            keys, pid = {}, {}
            for who, mail in (("P", "pat@example.com"), ("Q", "quinn@example.com")):
                r = await cx.post("/api/keys", json={
                    "name": f"{who}-laptop", "scopes": ["ingest", "read"],
                    "person": {"email": mail, "name": who}})
                keys[who], pid[who] = {"Authorization": f"Bearer {r.json()['secret']}"}, r.json()["person"]["id"]
            ck("two people with bound keys", pid["P"] != pid["Q"], pid)

        async def push(who, lines):
            async with mk() as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines),
                                  headers={**nd_h, **(keys[who] if who else root)})
                ck(f"{who or 'root'}'s lines are taken in", r.status_code < 300, r.text)

        async def crew(who=None, **params):
            async with mk() as cx:
                r = await cx.get("/api/crew", params=params, headers=keys[who] if who else root)
                ck("/api/crew answers", r.status_code == 200, r.text)
                return r.json()

        print("== two people, one crew name, two machines ==")
        await push("P", [st_line("p-o1", "a-orchestrator", "orchestrator", crew="a", machine="m1", ago=60),
                         say("p-o1", "a-orchestrator", "ready", 5),
                         st_line("p-r1", "a-reviewer", "reviewer", "a-orchestrator", crew="a", machine="m1", ago=50),
                         say("p-r1", "a-reviewer", "reviewing", 5)])
        await push("Q", [st_line("q-o1", "a-orchestrator", "orchestrator", crew="a", machine="m2", ago=40),
                         say("q-o1", "a-orchestrator", "ready", 4)])
        out = await crew()
        by = {(c["person"]["id"], c["name"], c["machine"]): c for c in out["crews"]}
        ck("P's crew a on m1 and Q's crew a on m2 are two crews", len(out["crews"]) == 2
           and set(by) == {(pid["P"], "a", "m1"), (pid["Q"], "a", "m2")}, list(by))
        cp, cq = by[(pid["P"], "a", "m1")], by[(pid["Q"], "a", "m2")]
        ck("a crew carries its id, person name, machine and last_seen",
           cp["id"].startswith("c_") and cp["person"]["name"] == "P" and cq["person"]["name"] == "Q"
           and cp["machine"] == "m1" and cp["last_seen"], cp)
        ck("each crew lists its own stations", [s["name"] for s in cp["stations"]] == ["a-orchestrator", "a-reviewer"]
           and [s["name"] for s in cq["stations"]] == ["a-orchestrator"], (cp["stations"], cq["stations"]))
        ck("the same station name in two crews stays two stations",
           cp["stations"][0]["session"] == "p-o1" and cq["stations"][0]["session"] == "q-o1"
           and cp["stations"][0]["crew_id"] == cp["id"] and cq["stations"][0]["crew_id"] == cq["id"])
        ck("the old flat stations key lists them all", sorted((s["name"], s["session"]) for s in out["stations"])
           == [("a-orchestrator", "p-o1"), ("a-orchestrator", "q-o1"), ("a-reviewer", "p-r1")], out["stations"])
        ck("a station's children come from its own crew",
           cp["stations"][0]["children"] == ["a-reviewer"] and cq["stations"][0]["children"] == [],
           (cp["stations"][0]["children"], cq["stations"][0]["children"]))

        print("== a new session replaces only its own crew's station ==")
        await push("P", [st_line("p-o2", "a-orchestrator", "orchestrator", crew="a", machine="m1", ago=3),
                         say("p-o2", "a-orchestrator", "back", 1)])
        out = await crew()
        by = {c["id"]: c for c in out["crews"]}
        po = next(s for s in by[cp["id"]]["stations"] if s["name"] == "a-orchestrator")
        qo = next(s for s in by[cq["id"]]["stations"] if s["name"] == "a-orchestrator")
        ck("P's orchestrator is the newest session, the old one replaced", po["session"] == "p-o2"
           and [e["session"] for e in po["earlier"]] == ["p-o1"], po)
        ck("Q's orchestrator is untouched", qo["session"] == "q-o1" and qo["earlier"] == [], qo)

        print("== the same person on another machine, and a recording with no person ==")
        await push("P", [st_line("p-o3", "a-orchestrator", "orchestrator", crew="a", machine="m3", ago=2),
                         say("p-o3", "a-orchestrator", "elsewhere", 1)])
        await push(None, [st_line("l-o1", "crew-orchestrator", "orchestrator", ago=2),
                          say("l-o1", "crew-orchestrator", "hi", 1)])
        out = await crew()
        keyset = {(c["person"]["id"], c["name"], c["machine"]) for c in out["crews"]}
        ck("P on a second machine is a third crew; the root token's recording is a fourth",
           keyset == {(pid["P"], "a", "m1"), (pid["P"], "a", "m3"), (pid["Q"], "a", "m2"), ("local", "crew", "")},
           keyset)
        ck("a recording with no person and no crew name joins crew `crew` of local, no machine",
           any(c["person"]["id"] == "local" and c["machine"] == "" and c["name"] == "crew"
               for c in out["crews"]))

        print("== mine=1 ==")
        mine = await crew("P", mine=1)
        ck("P sees only P's crews", {c["machine"] for c in mine["crews"]} == {"m1", "m3"}
           and all(c["person"]["id"] == pid["P"] for c in mine["crews"]), mine["crews"])
        ck("the flat key is narrowed too", {s["session"] for s in mine["stations"]} == {"p-o2", "p-r1", "p-o3"},
           [s["session"] for s in mine["stations"]])
        mine = await crew("Q", mine=1)
        ck("Q sees one crew, on m2", [c["machine"] for c in mine["crews"]] == ["m2"], mine["crews"])
        mine = await crew(None, mine=1)
        ck("the root token's own are the crews of local", [c["person"]["id"] for c in mine["crews"]] == ["local"],
           mine["crews"])
        ck("without mine, nobody is filtered", len((await crew("Q"))["crews"]) == 4)

        print("== one station of a name that two crews share ==")
        async with mk() as cx:
            as_p = (await cx.get("/api/crew/a-orchestrator", headers=keys["P"])).json()
            as_q = (await cx.get("/api/crew/a-orchestrator", headers=keys["Q"])).json()
            by_id = (await cx.get("/api/crew/a-orchestrator", params={"crew": cq["id"]},
                                  headers=keys["P"])).json()
            by_name = await cx.get("/api/crew/a-orchestrator", params={"crew": "a"}, headers=keys["Q"])
            none = await cx.get("/api/crew/a-orchestrator", params={"crew": "nope"}, headers=keys["P"])
            r404 = await cx.get("/api/crew/nobody", headers=keys["P"])
        ck("with several matches, the caller's own crew's station comes back",
           as_p["person"]["id"] == pid["P"] and as_p["session"] in ("p-o2", "p-o3")
           and as_q["session"] == "q-o1" and as_q["crew_id"] == cq["id"], (as_p["session"], as_q["session"]))
        ck("`crew` picks a crew, whoever asks", by_id["session"] == "q-o1", by_id["session"])
        ck("`crew` takes the crew's name too, and the caller's wins among those", by_name.json()["session"] == "q-o1")
        ck("a crew that has no such station is 404", none.status_code == 404 and r404.status_code == 404)

        print("== list_crew ==")
        headers.clear(); headers.update(keys["P"])
        every = json.loads(await mf.list_crew())
        ck("list_crew lists the stations of every crew, with crew, machine and person",
           {(s["name"], s["crew"], s["machine"], s["person"]) for s in every}
           >= {("a-orchestrator", "a", "m1", "P"), ("a-orchestrator", "a", "m2", "Q"),
               ("crew-orchestrator", "crew", "", "You")}, every)
        mine = json.loads(await mf.list_crew(mine=True))
        ck("list_crew(mine=True) lists only P's", {s["person"] for s in mine} == {"P"}
           and {s["machine"] for s in mine} == {"m1", "m3"}, mine)
        headers.clear(); headers.update(root)

        print("== the crew of a project ==")
        uid = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            await push("Q", [st_line("q-b1", "a-shop-build-m1", "builder", "a-orchestrator", crew="a",
                                     machine="m2", ago=2) | {"tares_project": "Shop"},
                             say("q-b1", "a-shop-build-m1", "on T1", 1)])
            pc = (await cx.get(f"/api/projects/{uid}/crew")).json()
        ck("a project's crew names the crew of its builder",
           [c["id"] for c in pc["crews"]] == [cq["id"]] and [s["name"] for s in pc["stations"]] == ["a-shop-build-m1"], pc)

        print("== sessions from before crews ==")
        from tares.store import Store
        path = os.path.join(_TMP, "old.duckdb")
        s0 = Store(path)
        s0.con.execute("INSERT INTO session_stations (session, station, role, parent, project, seen_at) "
                       "VALUES ('old-1', 'crew-reviewer', 'reviewer', NULL, NULL, now())")
        s0.con.close()
        s1 = Store(path)
        rows = s1.station_sessions("claude_code")
        crews = s1.list_crews()
        ck("a session recorded before crews joins crew `crew` of local on start",
           len(rows) == 1 and rows[0]["crew_id"] == crews[0]["id"] and len(crews) == 1
           and (crews[0]["name"], crews[0]["person_id"], crews[0]["machine"]) == ("crew", "local", ""), (rows, crews))
        s1.con.close()
        s2 = Store(path)
        ck("starting again changes nothing", len(s2.list_crews()) == 1
           and s2.station_sessions("claude_code")[0]["crew_id"] == crews[0]["id"])
        s2.con.close()


if __name__ == "__main__":
    plugin()
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
