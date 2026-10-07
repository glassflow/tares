"""One crew on Tares (M3: TR-418, TR-419, TR-420, TR-422, TR-425).

The plugin side: a station's environment (FACTORY_STATION, FACTORY_ROLE, FACTORY_PARENT,
TARES_PROJECT) becomes one session_station line and one session_project line per session, and a
station stamp on every line. The Tares side, through the claude_code ingest: sessions grouped into
stations (a revived reviewer is one station with an earlier session replaced, a builder sits
under the orchestrator), quiet stations, a TARES_PROJECT that names no project, the crew's
settings and grants (who may write them), the hand-over, and a project delete that keeps the
crew's sessions.

Run: .venv/bin/python tests/test_factory_crew.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-factory-crew-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import ship
import tares.mcp_server as m
import tares.mcp_factory as mf

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def plugin():
    print("== the plugin reads the station from its environment ==")
    sent = []
    real = ship.ship_lines
    ship.ship_lines = lambda cfg, objs: sent.extend(objs) or True
    cfg = {"base": "http://x", "headers": {}, "data_dir": os.path.join(_TMP, "plugin")}
    os.makedirs(cfg["data_dir"], exist_ok=True)
    hook = {"session_id": "s-env", "cwd": "/w/app"}
    try:
        for k in ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT", "TARES_PROJECT"):
            os.environ.pop(k, None)
        ship.announce(cfg, hook, "")
        ck("no station in the environment: nothing sent", sent == [], sent)
        os.environ.update(FACTORY_STATION="shop-build-m1", FACTORY_ROLE="builder",
                          FACTORY_PARENT="crew-orchestrator", TARES_PROJECT="Shop")
        ship.announce(cfg, hook, "")
        kinds = [o["type"] for o in sent]
        st = next((o for o in sent if o["type"] == "session_station"), {})
        pj = next((o for o in sent if o["type"] == "session_project"), {})
        ck("one session_station and one session_project line", kinds == ["session_station", "session_project"], kinds)
        ck("the station line says station, role, parent and project",
           (st.get("station"), st.get("role"), st.get("parent"), st.get("tares_project"))
           == ("shop-build-m1", "builder", "crew-orchestrator", "Shop"), st)
        ck("the project line comes from the environment", pj.get("from_env") is True
           and pj.get("tares_project") == "Shop", pj)
        ck("both carry the station stamp", all(o.get("factory_station") == "shop-build-m1" for o in sent))
        sent.clear()
        ship.announce(cfg, hook, "")
        ck("sent once per session", sent == [], sent)
        ck("the session's project is the environment's from its first line",
           ship.read_project(cfg["data_dir"], "s-env") == "Shop")
        stamped = ship.stamp_station([{"type": "user"}])
        ck("every shipped line carries the station", stamped[0].get("factory_station") == "shop-build-m1")
    finally:
        ship.ship_lines = real
        for k in ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT", "TARES_PROJECT"):
            os.environ.pop(k, None)


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", project="", ago=30):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def say(sid, station, text, ago):
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "factory_station": station, "message": {"role": "assistant", "content": text}}


def state(sid, s, ago, reason=""):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "state": s, "reason": reason}


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station, role):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True},
                                                    "project": shop})
            ck("the claude_code source is made", r.status_code < 300, r.text)

        print("== stations from the sessions Tares receives ==")
        lines = [
            st_line("o1", "crew-orchestrator", "orchestrator", ago=60), say("o1", "crew-orchestrator", "ready", 1),
            state("o1", "waiting", 1, "the turn ended; the session waits for input"),
            st_line("r1", "crew-reviewer", "reviewer", ago=50), say("r1", "crew-reviewer", "reviewing", 40),
            state("r1", "ended", 39, "exit"),
            st_line("r2", "crew-reviewer", "reviewer", ago=20), say("r2", "crew-reviewer", "back", 2),
            state("r2", "working", 2),
            st_line("x1", "crew-releaser", "releaser", ago=40), say("x1", "crew-releaser", "idle", 30),
            state("x1", "working", 30),
            st_line("b1", "shop-build-m1", "builder", "crew-orchestrator", "Shop", ago=10),
            say("b1", "shop-build-m1", "on T1", 1), state("b1", "working", 1),
            {"type": "session_project", "sessionId": "b1", "cwd": "/w/shop", "timestamp": ts(10),
             "tares_project": "Shop", "from_env": True},
            st_line("g1", "ghost-build-m1", "builder", "crew-orchestrator", "Ghost", ago=5),
            {"type": "session_project", "sessionId": "g1", "cwd": "/w/ghost", "timestamp": ts(5),
             "tares_project": "Ghost", "from_env": True},
        ]
        async with mk() as cx:
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
            ck("the crew's lines are taken in", r.status_code < 300, r.text)
            crew = (await cx.get("/api/crew")).json()
        by = {s["name"]: s for s in crew["stations"]}
        ck("one row per station", sorted(by) == ["crew-orchestrator", "crew-releaser", "crew-reviewer",
                                                  "ghost-build-m1", "shop-build-m1"], sorted(by))
        ck("stations come in role order, orchestrator first", crew["stations"][0]["name"] == "crew-orchestrator")
        rv = by["crew-reviewer"]
        ck("a revived reviewer is one station: current r2, r1 replaced",
           rv["session"] == "r2" and [e["session"] for e in rv["earlier"]] == ["r1"]
           and rv["earlier"][0]["state"] == "replaced", rv)
        ck("the orchestrator waits", by["crew-orchestrator"]["state"] == "waiting", by["crew-orchestrator"])
        ck("a working station silent for 30 min is quiet", by["crew-releaser"]["state"] == "quiet"
           and by["crew-releaser"]["quiet_minutes"] >= 29, by["crew-releaser"])
        ck("the builder sits under the orchestrator", "shop-build-m1" in by["crew-orchestrator"]["children"]
           and by["shop-build-m1"]["parent"] == "crew-orchestrator", by["crew-orchestrator"])
        ck("the builder is on its project", by["shop-build-m1"]["project_name"] == "Shop", by["shop-build-m1"])
        async with mk() as cx:
            ev = (await cx.get("/api/sources/claude_code/events", params={"limit": 200})).json()
        warn = [e for e in ev if e.get("event_type") == "session_warning"]
        ck("a TARES_PROJECT naming no project is a warning on the session, no project made",
           warn and "Ghost" in warn[0]["text"] and not any(
               p["name"] == "Ghost" for p in json.loads(await m.list_projects())), warn)
        async with mk() as cx:
            rd = (await cx.post("/read", json={"selector": {"station": "shop-build-m1"},
                                               "window": "2h", "include_payload": True})).json()
        ck("lines carry the station label: a read by station finds the builder's lines",
           rd.get("count") == 2 and "session=b1" in rd.get("payload", "")
           and "station=shop-build-m1" in rd.get("payload", ""), rd)
        out = json.loads(await mf.list_crew())
        ck("list_crew: stations with state and earlier sessions",
           next(s for s in out if s["name"] == "crew-reviewer")["earlier_sessions"] == 1, out)
        async with mk() as cx:
            pc = (await cx.get(f"/api/projects/{shop}/crew")).json()
            ck("the project's crew: the builder that worked on it",
               [s["name"] for s in pc["stations"]] == ["shop-build-m1"], pc)
            r = await cx.get("/api/crew/nobody")
            ck("an unknown station is 404", r.status_code == 404)

        print("== crew settings and grants ==")
        s = json.loads(await mf.get_crew_settings())
        ck("settings start unset", s == {"autonomy": None, "release_profile": None, "prod_pattern": None,
                                         "challenger": None, "builders_max": None}, s)
        s = json.loads(await mf.set_crew_settings(autonomy="L4-ship", release_profile="kind",
                                                  prod_pattern="prod-*", challenger=True))
        ck("the person's session saves them", s["autonomy"] == "L4-ship" and s["challenger"] is True, s)
        bad = await mf.set_crew_settings(autonomy="L9")
        ck("an unknown autonomy is refused", "unknown autonomy" in bad, bad)
        s = json.loads(await mf.set_crew_settings(builders_max=2))
        ck("a later change keeps the rest", s["autonomy"] == "L4-ship" and s["builders_max"] == 2, s)
        as_station("crew-reviewer", "reviewer")
        bad = await mf.set_crew_settings(autonomy="L5-dark")
        ck("a reviewer may not change them", "only the orchestrator" in bad, bad)
        bad = await mf.add_grant("deploy anything", "all")
        ck("a reviewer may not add a grant", "only the orchestrator" in bad, bad)
        bad = await m.add_to_global("agents", "reviewers merge")
        ck("a reviewer may not add to the shared docs", "only the orchestrator" in bad or "403" in bad, bad)
        as_station("crew-orchestrator", "orchestrator")
        g = json.loads(await mf.add_grant('"Merging after review is fine"', "all"))
        ck("the orchestrator relays a grant, quoted and dated",
           g["line"].startswith('"Merging after review is fine" (20') and g["added"], g)
        as_station("", "")
        g = json.loads(await mf.add_grant("Deploy to staging without asking", "project", project="Shop"))
        ck("a project grant", g["added"], g)
        bad = await m.add_to_global("grants", "anything")
        ck("grants do not go through add_to_global", "add_grant" in bad, bad)
        txt = await mf.read_grants(project="Shop")
        ck("read_grants returns both, with dates",
           "Merging after review is fine" in txt and "Deploy to staging without asking" in txt, txt)
        async with mk() as cx:
            crew = (await cx.get("/api/crew")).json()
        ck("the crew page carries settings and grants",
           crew["settings"]["release_profile"] == "kind" and "Merging after review" in crew["grants"], crew["settings"])

        print("== the hand-over ==")
        out = await mf.hand_over(project="Shop", repo="/w/shop")
        ck("hand_over names the running orchestrator and the message to send",
           "crew-orchestrator" in out and "[TF:SHIP]" in out and "/w/shop" in out, out)
        async with mk() as cx:
            pc = (await cx.get(f"/api/projects/{shop}/crew")).json()
        ck("the project says when it was handed to the crew", pc["handover"]["repo"] == "/w/shop", pc)
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content=json.dumps(state("o1", "ended", 0)) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})
        out = await mf.hand_over(project="Shop", repo="/w/shop")
        ck("with no orchestrator running, it says to run factory crew up", "factory crew up" in out, out)

        print("== a project delete keeps the crew ==")
        other = json.loads(await m.create_project("Other", ""))["id"]
        async with mk() as cx:
            await cx.delete(f"/api/projects/{other}")
            crew = (await cx.get("/api/crew")).json()
        ck("the crew's session states survive another project's delete",
           {s["name"]: s["state"] for s in crew["stations"]}.get("crew-reviewer") == "working", crew["stations"])


plugin()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
