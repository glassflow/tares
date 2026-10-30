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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

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
        for k in ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT", "FACTORY_CREW", "TARES_PROJECT"):
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
        ck("no crew in the environment: no crew stamp", "factory_crew" not in stamped[0]
           and "crew" not in st)
        os.environ["FACTORY_CREW"] = "ana-mbp"
        stamped = ship.stamp_station([{"type": "user"}])
        ck("the crew's name is stamped on every shipped line (TR-541)", stamped[0].get("factory_crew") == "ana-mbp", stamped)
        from tares.connectors.claude_code import ClaudeCodeConnector
        lab = ClaudeCodeConnector.label_context(ClaudeCodeConnector.__new__(ClaudeCodeConnector),
                                                {"sessionId": "s", "factory_station": "ana-mbp-reviewer",
                                                 "factory_crew": "ana-mbp"})
        ck("the transcript label crew comes from factory_crew", lab.get("crew") == "ana-mbp"
           and lab.get("station") == "ana-mbp-reviewer", lab)
        ck("crew is a declared label axis", any(p["name"] == "crew" for p in ClaudeCodeConnector.PROVIDES))
        sent.clear(); os.remove(ship.station_marker(cfg["data_dir"], "s-env"))
        ship.announce(cfg, hook, "")
        ck("the session_station line says the crew too",
           next(o for o in sent if o["type"] == "session_station").get("crew") == "ana-mbp"
           and all(o.get("factory_crew") == "ana-mbp" for o in sent), sent)
    finally:
        ship.ship_lines = real
        for k in ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT", "FACTORY_CREW", "TARES_PROJECT"):
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
            att = {n: (await cx.get(f"/api/crew/{n}")).json().get("attach")
                   for n in ("crew-orchestrator", "shop-build-m1")}
            ck("a station's attach command: a crew station by its role, a builder by its name",
               att == {"crew-orchestrator": "tares factory crew attach orchestrator",
                       "shop-build-m1": "tares factory crew attach shop-build-m1"}, att)
            more = [st_line("n1", "ana-mbp-reviewer", "reviewer", ago=3), say("n1", "ana-mbp-reviewer", "hi", 2),
                    state("n1", "working", 2),
                    st_line("n2", "reviewer-reviewer", "reviewer", ago=3), say("n2", "reviewer-reviewer", "hi", 2),
                    state("n2", "working", 2)]
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in more) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})
            ck("a named crew's station served by the API: attach by its role (TR-541)",
               (await cx.get("/api/crew/ana-mbp-reviewer")).json().get("attach")
               == "tares factory crew attach reviewer")
            ck("the role the station reported settles it when its name alone cannot (a crew named reviewer)",
               (await cx.get("/api/crew/reviewer-reviewer")).json().get("attach")
               == "tares factory crew attach reviewer")
        from tares.factory import attach_command
        ck("a helper of a crew station keeps its full name",
           attach_command("crew-reviewer-lint") == "tares factory crew attach crew-reviewer-lint",
           attach_command("crew-reviewer-lint"))
        from tares.factory import crew_station_role, on_crew_side
        ck("a named crew's own stations go by their role (TR-541)",
           [attach_command(f"ana-mbp-{r}") for r in ("orchestrator", "reviewer", "releaser")]
           == [f"tares factory crew attach {r}" for r in ("orchestrator", "reviewer", "releaser")],
           attach_command("ana-mbp-reviewer"))
        ck("the role the station reports settles it",
           attach_command("a-reviewer", "reviewer") == "tares factory crew attach reviewer"
           and attach_command("a-reviewer", "helper") == "tares factory crew attach a-reviewer"
           and attach_command("a-reviewer", "releaser") == "tares factory crew attach a-reviewer")
        ck("a named crew's helper and builder keep their full names",
           attach_command("a-reviewer-lint") == "tares factory crew attach a-reviewer-lint"
           and attach_command("a-shop-build-m1", "builder") == "tares factory crew attach a-shop-build-m1"
           and attach_command("crew-reviewer-releaser") == "tares factory crew attach crew-reviewer-releaser")
        ck("a project with -build- in its name is not mangled into a crew role",
           crew_station_role("crew-build-tools-build-reviewer") == ""
           and attach_command("crew-build-tools-build-m1") == "tares factory crew attach crew-build-tools-build-m1"
           and attach_command("x-build-y-build-releaser") == "tares factory crew attach x-build-y-build-releaser")
        rows = [{"name": "a-reviewer", "role": "reviewer", "parent": ""},
                {"name": "a-reviewer-lint", "role": "helper", "parent": "a-reviewer"},
                {"name": "a-shop-build-m1", "role": "builder", "parent": "a-orchestrator"},
                {"name": "a-shop-build-m1-e2e", "role": "helper", "parent": "a-shop-build-m1"}]
        ck("a named crew's station and its helper serve every project; a builder and its helper do not",
           on_crew_side("a-reviewer", rows) and on_crew_side("a-reviewer-lint", rows)
           and not on_crew_side("a-shop-build-m1", rows) and not on_crew_side("a-shop-build-m1-e2e", rows))
        ck("a station that reports a role outside the crew's is never read as a crew role by its name",
           crew_station_role("a-helper", "helper") == "" and crew_station_role("a-builder", "builder") == ""
           and attach_command("a-helper", "helper") == "tares factory crew attach a-helper")
        ck("a station no row reports is read by its name: a project named like a role is not the crew's",
           not on_crew_side("shop-reviewer-build-m1", []) and on_crew_side("a-releaser", [])
           and on_crew_side("a-reviewer-lint", []) and not on_crew_side("shop", []))
        orphans = [{"name": "a-reviewer-lint", "role": "helper", "parent": "a-reviewer"},
                   {"name": "a-shop-build-m1-e2e", "role": "helper", "parent": "a-shop-build-m1"}]
        ck("a helper whose parent no row reports follows the parent's name",
           on_crew_side("a-reviewer-lint", orphans) and not on_crew_side("a-shop-build-m1-e2e", orphans))

        print("== crew settings and grants ==")
        s = json.loads(await mf.get_crew_settings())
        ck("settings start unset", s == {"autonomy": None, "releaser": None, "release_profile": None,
                                         "prod_pattern": None, "challenger": None,
                                         "builders_max": None, "idle_wake": None, "pace": None,
                                         "kit": {"frozen": None}, "claude_update": None}, s)
        s = json.loads(await mf.set_crew_settings(claude_update=False))
        ck("the Claude Code update switch is a setting of its own", s["claude_update"] is False
           and s["autonomy"] is None, s)
        s = json.loads(await mf.set_crew_settings(claude_update=True))
        ck("and switches back on", s["claude_update"] is True, s)
        s = json.loads(await mf.set_crew_settings(autonomy="L4-ship", release_profile="kind",
                                                  prod_pattern="prod-*", challenger=True))
        ck("the person's session saves them", s["autonomy"] == "L4-ship" and s["challenger"] is True, s)
        bad = await mf.set_crew_settings(autonomy="L9")
        ck("an unknown autonomy is refused", "unknown autonomy" in bad, bad)
        bad = await mf.set_crew_settings(autonomy="L5-dark")
        ck("L5 is not offered: the factory stops at merged", "unknown autonomy" in bad, bad)
        s = json.loads(await mf.get_crew_settings())
        ck("no releaser until the person says so", s["releaser"] is None, s)
        s = json.loads(await mf.set_crew_settings(releaser=False))
        ck("the releaser is a setting of its own", s["releaser"] is False
           and s["autonomy"] == "L4-ship", s)
        s = json.loads(await mf.set_crew_settings(builders_max=2))
        ck("a later change keeps the rest", s["autonomy"] == "L4-ship" and s["builders_max"] == 2, s)
        s = json.loads(await mf.set_crew_settings(idle_wake=True))
        ck("idle_wake is a setting of its own, off until the person turns it on",
           s["idle_wake"] is True and s["builders_max"] == 2, s)
        s = json.loads(await mf.get_crew_settings())
        ck("the MCP tool reads idle_wake back", s["idle_wake"] is True, s)
        async with mk() as cx:
            for junk in ("maybe", "false", 1, [True]):
                r = await cx.put("/api/crew/settings", json={"idle_wake": junk})
                ck(f"idle_wake {junk!r} is refused: it is true or false", r.status_code == 400, r.text)
            r = await cx.put("/api/crew/settings", json={"builders_max": 3})
            ck("another change keeps idle_wake", r.json()["idle_wake"] is True, r.text)
            await cx.put("/api/crew/settings", json={"builders_max": 2})
        s = json.loads(await mf.set_crew_settings(pace=True))
        ck("pace is a setting of its own, off until the person turns it on",
           s["pace"] is True and s["idle_wake"] is True and s["builders_max"] == 2, s)
        s = json.loads(await mf.get_crew_settings())
        ck("the MCP tool reads pace back", s["pace"] is True, s)
        async with mk() as cx:
            for junk in ("maybe", "false", 1, [True]):
                r = await cx.put("/api/crew/settings", json={"pace": junk})
                ck(f"pace {junk!r} is refused: it is true or false", r.status_code == 400, r.text)
            r = await cx.put("/api/crew/settings", json={"builders_max": 3})
            ck("another change keeps pace", r.json()["pace"] is True, r.text)
            await cx.put("/api/crew/settings", json={"builders_max": 2})
            r = await cx.get("/api/crew/pace")
            ck("/api/crew/pace answers with the setting on and no plan reading yet",
               r.status_code == 200 and r.json()["on"] is True and r.json()["known"] is False
               and r.json()["level"] == 0, r.text)
            ck("/api/crew carries the same", (await cx.get("/api/crew")).json()["pace"]["on"] is True)
            await cx.put("/api/crew/settings", json={"pace": False})
            ck("turned off again, the state is off",
               (await cx.get("/api/crew/pace")).json()["state"] == "off")
        frozen = {"at": "2026-10-25T10:00:00Z", "by": "kiran", "version": "123456"}
        async with mk() as cx:
            r = await cx.put("/api/crew/settings", json={"kit": {"frozen": frozen}})
            ck("the kit can be frozen: when, by whom, at which version",
               r.status_code == 200 and r.json()["kit"] == {"frozen": frozen}, r.text)
            s = json.loads(await mf.get_crew_settings())
            ck("the MCP tool reads it, and the rest is kept", s["kit"] == {"frozen": frozen}
               and s["autonomy"] == "L4-ship" and s["builders_max"] == 2, s)
            r = await cx.put("/api/crew/settings", json={"builders_max": 3})
            ck("another change keeps the freeze", r.json()["kit"] == {"frozen": frozen}, r.text)
            await cx.put("/api/crew/settings", json={"builders_max": 2})
            for name, junk in (("a kit that is not an object", "frozen"),
                               ("a frozen kit that is not an object", {"frozen": "yes"}),
                               ("a frozen kit without a version", {"frozen": {"at": "x", "by": "y"}}),
                               ("a frozen kit with a number for a time", {"frozen": {"at": 5, "by": "y", "version": "1"}}),
                               ("a frozen kit with a blank who", {"frozen": {"at": "x", "by": " ", "version": "1"}}),
                               ("an unknown kit field", {"frozen": None, "pinned": "abc"}),
                               ("an empty kit", {}),
                               ("a frozen kit with a time that is no time", {"frozen": {"at": "yesterday", "by": "y", "version": "1"}})):
                r = await cx.put("/api/crew/settings", json={"kit": junk})
                ck(f"{name} is refused", r.status_code == 400, r.text)
            s = (await cx.get("/api/crew/settings")).json()
            ck("a refused kit changes nothing", s["kit"] == {"frozen": frozen}, s)
            r = await cx.put("/api/crew/settings", json={"kit": {"frozen": None}})
            ck("unfreezing clears it", r.status_code == 200 and r.json()["kit"] == {"frozen": None}, r.text)
        as_station("crew-reviewer", "reviewer")
        bad = await mf.set_crew_settings(autonomy="L3-review")
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
            await cx.post("/ingest/claude_code", content=json.dumps(state("o1", "idle", 0.5)) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})
        out = await mf.hand_over(project="Shop", repo="/w/shop")
        ck("an orchestrator between turns (idle) still counts as running",
           "crew-orchestrator" in out and "[TF:SHIP]" in out, out)
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

        print("== a station the watch is retrying onto a new Claude Code (TR-687) ==")
        async with mk() as cx:
            ck("no station is retrying until the factory CLI says so",
               all(r["claude_retry"] is None for r in (await cx.get("/api/crew")).json()["stations"]))
            r = await cx.put("/api/crew/claude-retry", json={"stations": {
                "crew-reviewer": {"target": "2.1.9", "fails": 2, "next_at": 1790000000}}})
            ck("the CLI reports a retrying station", r.status_code == 200, r.text)
            ck("/api/crew lists them too, for a client that has no row for the station",
               (await cx.get("/api/crew")).json()["claude_retry"]
               == {"crew-reviewer": {"target": "2.1.9", "fails": 2, "next_at": 1790000000}})
            rows = {x["name"]: x for x in (await cx.get("/api/crew")).json()["stations"]}
            ck("the Crew page row carries target, failed moves and the next try",
               rows["crew-reviewer"]["claude_retry"] == {"target": "2.1.9", "fails": 2, "next_at": 1790000000}
               and all(x["claude_retry"] is None for n, x in rows.items() if n != "crew-reviewer"), rows)
            for name, junk in (("a list", {"stations": []}), ("no stations", {}),
                               ("a row without a target", {"stations": {"crew-reviewer": {"fails": 1}}})):
                r = await cx.put("/api/crew/claude-retry", json=junk)
                ck(f"{name} is refused", r.status_code == 400, r.text)
            for name, junk in (("too many stations", {"stations": {f"s{i}": {"target": "2.1.9"} for i in range(201)}}),
                               ("a name of 129 characters", {"stations": {"n" * 129: {"target": "2.1.9"}}}),
                               ("an empty name", {"stations": {"": {"target": "2.1.9"}}})):
                r = await cx.put("/api/crew/claude-retry", json=junk)
                ck(f"{name} is refused", r.status_code == 400, r.text)
            rows = {x["name"]: x for x in (await cx.get("/api/crew")).json()["stations"]}
            ck("a refused report changes nothing", rows["crew-reviewer"]["claude_retry"]["fails"] == 2, rows)
            r = await cx.put("/api/crew/claude-retry", json={"stations": {
                "crew-reviewer": {"target": "2.1.9", "fails": True, "next_at": -5}}})
            ck("a boolean or out-of-range count falls back to the default, not stored as given",
               r.status_code == 200 and "true" not in r.text
               and r.json()["stations"]["crew-reviewer"] == {"target": "2.1.9", "fails": 1, "next_at": 0}, r.text)
            await cx.put("/api/crew/claude-retry", json={"stations": {}})
            ck("an empty list clears every mark",
               all(r["claude_retry"] is None for r in (await cx.get("/api/crew")).json()["stations"]))


        print("== ending a station ==")
        nd = {"Content-Type": "application/x-ndjson"}
        lines = [st_line("e-old", "end-build-m1", "builder", ago=300), say("e-old", "end-build-m1", "first", 299),
                 st_line("e1", "end-build-m1", "builder", ago=61), say("e1", "end-build-m1", "working", 60),
                 state("e1", "working", 60)]
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n", headers=nd)
            before = (await cx.get("/api/crew/end-build-m1")).json()
        ck("a working station silent for 60 min shows quiet", before["state"] == "quiet", before["state"])
        as_station("b1", "builder")
        async with mk() as cx:
            r = await cx.post("/api/crew/end-build-m1/end", json={"reason": "retired"})
        ck("a builder may not end a station", r.status_code == 403, r.text)
        as_station("crew-orchestrator", "orchestrator")
        async with mk() as cx:
            r = await cx.post("/api/crew/end-build-m1/end", json={"reason": "  retired  "})
        ck("the orchestrator may", r.status_code == 200, r.text)
        as_station("", "")
        row = r.json()
        ck("ended with the reason", (row["state"], row["state_reason"]) == ("ended", "retired"), row)
        ck("the earlier session is kept", [e["session"] for e in row["earlier"]] == ["e-old"], row["earlier"])
        async with mk() as cx:
            crew = (await cx.get("/api/crew")).json()
            listed = next(x for x in crew["stations"] if x["name"] == "end-build-m1")
            ck("/api/crew shows it ended", (listed["state"], listed["state_reason"]) == ("ended", "retired"), listed)
            r = await cx.post("/api/crew/end-build-m1/end", json={"reason": "again"})
            ck("ending an ended station keeps its reason",
               r.status_code == 200 and r.json()["state_reason"] == "retired", r.text)
            r = await cx.post("/api/crew/nobody/end")
            ck("an unknown station is 404", r.status_code == 404, r.text)
            r = await cx.post("/api/crew/end-build-m1/end")
            ck("no body is fine", r.status_code == 200, r.text)
            await cx.post("/ingest/claude_code", content=json.dumps(state("e1", "working", 5)) + "\n", headers=nd)
            st = (await cx.get("/api/crew/end-build-m1")).json()
            ck("a working line from before the end does not turn it back", st["state"] == "ended", st["state"])
            await cx.post("/ingest/claude_code", content=json.dumps(state("e1", "working", -1)) + "\n", headers=nd)
            st = (await cx.get("/api/crew/end-build-m1")).json()
            ck("a live session reporting after the end turns it back", st["state"] == "working", st["state"])

            lines = [st_line("n1", "noreason-build", "builder", ago=2), state("n1", "working", 2)]
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n", headers=nd)
            r = await cx.post("/api/crew/noreason-build/end", json={})
            ck("with no reason it says marked ended", r.json()["state_reason"] == "marked ended", r.text)

def pace():
    print("== pace_now: the plan's limits against the thresholds (TR-674) ==")
    from tares.factory import pace_now
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    later, soon = "2026-10-25T15:00:00+00:00", "2026-10-30T00:00:00+00:00"
    past = "2026-10-25T11:00:00+00:00"

    def plan(session=0, weekly=0, sr=later, wr=soon, **extra):
        return {"limits": [{"kind": "session", "percent": session, "resets_at": sr, **extra},
                           {"kind": "weekly_all", "percent": weekly, "resets_at": wr}]}

    on = {"pace": True}
    table = [("84% of the window", plan(84), 0, "ok", False, None),
             ("85% of the window", plan(85), 1, "slow", False, "2026-10-25T15:00:00Z"),
             ("94% of the window", plan(94), 1, "slow", False, "2026-10-25T15:00:00Z"),
             ("95% of the window", plan(95), 2, "hold", False, "2026-10-25T15:00:00Z"),
             ("100% of the window", plan(100), 2, "limit", True, "2026-10-25T15:00:00Z"),
             ("89% of the week", plan(10, 89), 0, "ok", False, None),
             ("90% of the week", plan(10, 90), 1, "slow", False, "2026-10-30T00:00:00Z"),
             ("100% of the week", plan(10, 100), 1, "limit", True, "2026-10-30T00:00:00Z"),
             ("both limits high: until the later reset", plan(96, 91), 2, "hold", False, "2026-10-30T00:00:00Z"),
             ("a window that reset (flagged by Tares)", plan(99, reset=True), 0, "ok", False, None),
             ("a window whose time has passed", plan(99, sr=past), 0, "ok", False, None),
             ("a full window that reset, a weekly limit that did not", plan(100, 91, sr=past), 1, "slow", False, "2026-10-30T00:00:00Z"),
             ("a per-model week at 100% stops that model, not the crew",
              {"limits": [{"kind": "weekly_scoped", "percent": 100, "resets_at": later}]}, 0, "ok", False, None),
             ("a per-model week at 100% beside a window at 86%",
              {"limits": [{"kind": "session", "percent": 86, "resets_at": later},
                          {"kind": "weekly_scoped", "percent": 100, "resets_at": soon}]}, 1, "slow", False, "2026-10-25T15:00:00Z")]
    for name, pl, level, state, at_limit, until in table:
        r = pace_now(on, pl, now)
        ck(f"{name}: level {level}, {state}", (r["level"], r["state"], r["at_limit"], r["until"]) == (level, state, at_limit, until), r)
    r = pace_now(on, plan(87, 12), now)
    ck("the percents and a plain note are there",
       (r["session_pct"], r["weekly_pct"]) == (87, 12) and r["note"].startswith("Pacing: the 5-hour window is at 87%")
       and "2026-10-25 15:00 UTC" in r["note"] and "\u2014" not in r["note"], r)
    r = pace_now(on, plan(99, reset=True), now)
    ck("a reset limit counts as 0", r["session_pct"] == 0, r)
    for name, settings in (("unset", {}), ("false", {"pace": False}), ("null", {"pace": None})):
        r = pace_now(settings, plan(99, 99), now)
        ck(f"the setting {name}: state off, level 0, whatever the plan says",
           (r["on"], r["state"], r["level"], r["at_limit"], r["until"]) == (False, "off", 0, False, None), r)
    r = pace_now(on, None, now)
    ck("on with no plan reading: nothing known, level 0", (r["known"], r["level"], r["state"]) == (False, 0, "ok"), r)


pace()
plugin()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
