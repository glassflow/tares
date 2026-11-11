# Tares enterprise licence, see ee/LICENSE
"""The Crew page shows every crew on the cell, under the person it belongs to (TR-1223).

Two people, each with a crew whose stations are called the same (crew-orchestrator, crew-reviewer, a
builder): Ashish's orchestrator is working, Adam's ended an hour ago. Before TR-1223 the page took the
first orchestrator of any crew, so Ashish saw "Not running". Now:

  - `crews` has one entry per crew, the caller's own first, then by person name;
  - each crew has its own summary, orchestrator, reviewer and builders, none from the other crew;
  - the top-level summary and members are the caller's own crew's;
  - each member names its crew, and the detail call with that crew opens that crew's station;
  - a person with two machines has two crews, and only then is the machine shown.

Run: .venv/bin/python ee/tests/test_crew_page_by_person.py
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

_TMP = tempfile.mkdtemp(prefix="tares-crew-page-by-person-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-page-by-person"
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


def st_line(sid, station, role, machine, parent="", ago=30):
    return {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "station": station, "role": role, "parent": parent, "factory_station": station,
            "factory_crew": "crew", "crew": "crew", "factory_machine": machine, "machine": machine}


def say(sid, station, text, ago):
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "factory_station": station, "message": {"role": "assistant", "content": text}}


def state(sid, s, ago):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago), "state": s,
            "reason": ""}


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    mk = lambda h=None: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                          headers=h or root)
    nd_h = {"Content-Type": "application/x-ndjson"}
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            keys, pid = {}, {}
            for who, name in (("ashish", "Ashish"), ("adam", "Adam")):
                r = await cx.post("/api/keys", json={"name": f"{who}-laptop", "scopes": ["admin", "ingest", "read"],
                                                     "person": {"email": f"{who}@example.com", "name": name}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]

        async def push(who, lines):
            async with mk(keys[who]) as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines), headers=nd_h)
                ck(f"{who}'s lines are taken in", r.status_code < 300, r.text)

        await push("ashish", [st_line("a-o", "crew-orchestrator", "orchestrator", "ashish-mbp", ago=50),
                              say("a-o", "crew-orchestrator", "handing out work", 1), state("a-o", "working", 1),
                              st_line("a-r", "crew-reviewer", "reviewer", "ashish-mbp", ago=50),
                              say("a-r", "crew-reviewer", "reviewing", 3), state("a-r", "idle", 3),
                              st_line("a-b", "proj-build-m1", "builder", "ashish-mbp", "crew-orchestrator", ago=20),
                              say("a-b", "proj-build-m1", "building", 2), state("a-b", "working", 2)])
        await push("adam", [st_line("d-o", "crew-orchestrator", "orchestrator", "adam-mbp", ago=120),
                            say("d-o", "crew-orchestrator", "done for today", 60), state("d-o", "ended", 60),
                            st_line("d-r", "crew-reviewer", "reviewer", "adam-mbp", ago=120),
                            say("d-r", "crew-reviewer", "done", 60), state("d-r", "ended", 60)])

        async with mk(keys["ashish"]) as cx:
            page = (await cx.get("/api/factory/crew")).json()
        crews = page.get("crews") or []
        ck("one crew per person, the caller's own first",
           [c["person"]["name"] for c in crews] == ["Ashish", "Adam"] and crews[0]["mine"] and not crews[1]["mine"],
           [(c["person"]["name"], c["mine"]) for c in crews])
        mine, theirs = crews[0], crews[1]
        ck("Ashish's crew runs: its own orchestrator is working",
           mine["summary"].startswith("Working") and mine["talk_to"]["state"] == "working", mine["summary"])
        ck("Adam's crew is not running: its own orchestrator ended",
           theirs["summary"] == "Not running" and theirs["talk_to"]["state"] == "ended", theirs["summary"])
        ck("the top of the page is the caller's crew, not the first one found",
           page["summary"] == mine["summary"] and page["talk_to"]["crew"] == mine["id"], page["summary"])
        ck("each crew has only its own members",
           mine["reviewer"]["crew"] == mine["id"] and theirs["reviewer"]["crew"] == theirs["id"]
           and [b["station"] for g in mine["builders"] for b in g["members"]] == ["proj-build-m1"]
           and theirs["builders"] == [], (mine["builders"], theirs["builders"]))
        ck("the machine is not shown while each person has one crew",
           not mine["machine_shown"] and not theirs["machine_shown"] and mine["machine"] == "ashish-mbp")

        async with mk(keys["adam"]) as cx:
            as_adam = (await cx.get("/api/factory/crew")).json()
        ck("Adam sees the same crews, his own first",
           [c["person"]["name"] for c in as_adam["crews"]] == ["Adam", "Ashish"]
           and as_adam["summary"] == "Not running", [c["person"]["name"] for c in as_adam["crews"]])

        async with mk(keys["ashish"]) as cx:
            d = (await cx.get("/api/crew/crew-orchestrator", params={"crew": theirs["id"]})).json()
        ck("the detail call with a member's crew opens that crew's station",
           d["crew_id"] == theirs["id"] and d["session"] == "d-o", (d.get("crew_id"), d.get("session")))

        await push("ashish", [st_line("a2-o", "crew-orchestrator", "orchestrator", "ashish-studio", ago=10),
                              say("a2-o", "crew-orchestrator", "second machine", 1), state("a2-o", "working", 1)])
        async with mk(keys["ashish"]) as cx:
            page = (await cx.get("/api/factory/crew")).json()
        rows = [(c["person"]["name"], c["machine"], c["machine_shown"]) for c in page["crews"]]
        ck("a second machine is a second crew of the same person, and then the machine is shown",
           rows[:2] == [("Ashish", "ashish-mbp", True), ("Ashish", "ashish-studio", True)]
           and rows[2] == ("Adam", "adam-mbp", False), rows)

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
