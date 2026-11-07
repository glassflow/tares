"""Core with no licence: one crew per name, whatever machine (TR-924).

A cell of one person (auth on, the root token is the owner). Station lines of one crew name from two
machines land in one crew with no machine; a crew named `ana-laptop` keeps its name, so
`POST /api/crew/ana-laptop-orchestrator/end?crew=ana-laptop` finds its station. The line is never
refused. The team decides the crew a line joins (`resolve_crew`), so a team that keeps machines apart
makes two crews of the same lines. Crews kept apart by machine with a licence are ee/tests/test_crews.py.

Run: .venv/bin/python tests/test_crews_single.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-crews-single-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-crews-single"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares.daemon import make_app

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", crew=None, machine=None, ago=30):
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


def nd(lines):
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class MachineTeam(team_mod.SingleTeam):
    """A stand-in team that keeps crews apart by machine, as the ee team does."""
    seen: list = []

    def resolve_crew(self, name, person_id, machine):
        self.seen.append((name, person_id, machine))
        return (name or "").strip() or "crew", (machine or "").strip()


async def main():
    app = make_app()
    H = {"Authorization": f"Bearer {AUTH}"}
    mk = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=dict(H))
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
        ck("only local is on the cell", app.state.store.only_local_person())

        async def push(lines):
            async with mk() as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines),
                                  headers={"Content-Type": "application/x-ndjson"})
                ck("the lines are taken in", r.status_code < 300, r.text)

        async def crews():
            async with mk() as cx:
                r = await cx.get("/api/crew")
                ck("/api/crew answers", r.status_code == 200, r.text)
                return r.json()

        print("== one crew name on two machines is one crew ==")
        await push([st_line("o-1", "crew-orchestrator", "orchestrator", crew="crew", machine="mbp", ago=40),
                    say("o-1", "crew-orchestrator", "ready", 5),
                    st_line("r-1", "crew-reviewer", "reviewer", "crew-orchestrator", crew="crew", machine="desk", ago=30),
                    say("r-1", "crew-reviewer", "reviewing", 5)])
        out = await crews()
        ck("one crew, no machine", [(c["name"], c["machine"], c["person"]["id"]) for c in out["crews"]]
           == [("crew", "", "local")], out["crews"])
        c0 = out["crews"][0]
        ck("both stations are in it", sorted(s["name"] for s in c0["stations"])
           == ["crew-orchestrator", "crew-reviewer"], c0["stations"])
        ck("the orchestrator's children come from the one crew", next(
            s for s in c0["stations"] if s["name"] == "crew-orchestrator")["children"] == ["crew-reviewer"])

        print("== a new session on another machine replaces the station, not a second crew ==")
        await push([st_line("o-2", "crew-orchestrator", "orchestrator", crew="crew", machine="desk", ago=3),
                    say("o-2", "crew-orchestrator", "back", 1)])
        out = await crews()
        orch = next(s for c in out["crews"] for s in c["stations"] if s["name"] == "crew-orchestrator")
        ck("still one crew", len(out["crews"]) == 1, out["crews"])
        ck("the newest session is the station, the old one earlier", orch["session"] == "o-2"
           and [e["session"] for e in orch["earlier"]] == ["o-1"], orch)

        print("== a line with no crew name joins crew `crew` ==")
        await push([st_line("n-1", "crew-releaser", "releaser", ago=2), say("n-1", "crew-releaser", "hi", 1)])
        out = await crews()
        ck("still one crew, with three stations", len(out["crews"]) == 1
           and len(out["crews"][0]["stations"]) == 3, out["crews"])

        print("== a named crew keeps its name ==")
        await push([st_line("a-1", "ana-laptop-orchestrator", "orchestrator", crew="ana-laptop", machine="mbp", ago=4),
                    say("a-1", "ana-laptop-orchestrator", "ready", 2)])
        out = await crews()
        ck("a second crew, named ana-laptop, with no machine", sorted((c["name"], c["machine"]) for c in out["crews"])
           == [("ana-laptop", ""), ("crew", "")], out["crews"])
        async with mk() as cx:
            r = await cx.post("/api/crew/ana-laptop-orchestrator/end", params={"crew": "ana-laptop"},
                              json={"reason": "retired"})
            ck("ending the station with ?crew=ana-laptop finds it", r.status_code == 200
               and r.json()["session"] == "a-1", f"{r.status_code} {r.text[:160]}")
            r = await cx.post("/api/crew/ana-laptop-orchestrator/end", params={"crew": "nope"},
                              json={"reason": "retired"})
            ck("a crew name that has no such station is 404", r.status_code == 404, f"{r.status_code} {r.text[:100]}")

        print("== the team decides the crew a line joins ==")
        real = team_mod.get
        team = MachineTeam()
        team_mod.get = lambda: team
        try:
            await push([st_line("m-1", "b-orchestrator", "orchestrator", crew="b", machine="m1", ago=2),
                        say("m-1", "b-orchestrator", "one", 1),
                        st_line("m-2", "b-reviewer", "reviewer", "b-orchestrator", crew="b", machine="m2", ago=2),
                        say("m-2", "b-reviewer", "two", 1)])
        finally:
            team_mod.get = real
        out = await crews()
        ck("the team was asked with the line's crew name and machine",
           [(n, m) for n, _, m in team.seen] == [("b", "m1"), ("b", "m2")], team.seen)
        ck("a team that keeps machines apart makes two crews of them", sorted(c["machine"] for c in out["crews"]
           if c["name"] == "b") == ["m1", "m2"], out["crews"])
        ck("the single team's crews are untouched", sum(1 for c in out["crews"] if c["name"] == "crew") == 1)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
