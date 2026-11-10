"""The watch's pace step in Python (tares/crew/pace.py, TR-962).

The pace reading, the pace files, what each station is told and the revive step's questions, run against the
Python fake Claude Code (tests/factory/fake_claude.py) in a throwaway crew home, with a small fake Tares that
answers GET /api/crew/pace and the crew settings. The restart that tells a station is replaced by a recorder
(launch), so nothing here needs bash or a real Claude Code, and no real station or watch is touched. The same
rounds run on bash and on Python side by side in the parity harness (`parity.py pace_*`).

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_pace.py
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
FACTORY_DIR = HERE / "factory"

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-pace-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
CREW = SCRATCH / "crew"
STATE = SCRATCH / "claude"
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1", FACTORY_CREW_HOME=str(CREW), FAKE_CLAUDE_STATE=str(STATE),
)
(SCRATCH / "home").mkdir()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FACTORY_DIR))

import fake_claude  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew import pace, state, watch  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def make_launcher(folder: Path, script: Path) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        f = folder / "claude.cmd"
        f.write_bytes(f'@"{sys.executable}" "{script}" %*\r\n'.encode("ascii", "replace"))
    else:
        f = folder / "claude"
        f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)
    return str(f)


CLAUDE = make_launcher(SCRATCH / "bin", FACTORY_DIR / "fake_claude.py")
os.environ["CLAUDE_BIN"] = CLAUDE


class FakeTares:
    """A Tares that answers by route: {"GET /x": (status, doc)}; /health is up. `pace` and `idle_wake` are the
    two things these checks change from one to the next."""

    def __init__(self, doc=None, idle_wake=False, health=True):
        self.doc = doc
        self.idle_wake = idle_wake
        self.health = health
        self.requests: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                key = f"GET {self.path}"
                outer.requests.append(key)
                if key == "GET /health":
                    status, doc = (200 if outer.health else 503), {"ok": True}
                elif key == "GET /api/crew/pace":
                    status, doc = (200, outer.doc) if outer.doc is not None else (404, {"detail": "none"})
                elif key == "GET /api/crew/settings":
                    status, doc = 200, {"settings": {"idle_wake": outer.idle_wake}}
                else:
                    status, doc = 404, {"detail": "not found"}
                data = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        os.environ["TARESD_URL"] = self.url
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("TARESD_URL", None)


def doc(session=0, weekly=0, *, level=None, at_limit=False, on=True, known=True, until="2033-05-18T05:00:00Z"):
    """What GET /api/crew/pace answers for a window at `session`% and a week at `weekly`%."""
    if level is None:
        level = 2 if session >= 95 else 1 if session >= 85 or weekly >= 90 else 0
    return {"on": on, "known": known, "level": level, "at_limit": at_limit, "until": until,
            "note": f"Pacing: the 5-hour window is at {session}%.", "session_pct": session, "weekly_pct": weekly}


work = SCRATCH / "work"
work.mkdir()
paths = CrewPaths.from_env()
NAMES = {"crew-reviewer": "reviewer", "shop-build-m1": "builder", "crew-helper": "helper"}


def station(role, **over) -> dict:
    row = {"role": role, "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
           "cwd": str(work), "model": "opus", "effort": "high", "id": "", "sessionId": ""}
    row.update(over)
    return row


def reset(rows: dict | None = None, states: dict | None = None):
    """A fresh crew home. `rows` are the stations ({name: role}); each has a session, idle unless `states` says."""
    pace.forget()
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    rows = NAMES if rows is None else rows
    stations, agents = {}, []
    for i, (name, role) in enumerate(rows.items(), 1):
        job = f"aaaa{i:04x}"
        stations[name] = station(role, id=job, sessionId=f"sess-{i}")
        agents.append(fake_claude.agent_row((states or {}).get(name, "idle"), job, name, str(work)))
    (CREW / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    fake_claude.write_agents(STATE, agents)
    for k in ("FAKE_CLAUDE_FAIL_RESUME", "FACTORY_STOP_WAIT"):
        os.environ.pop(k, None)


def events() -> list:
    try:
        lines = (CREW / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines]


def told() -> list:
    """(station, what) for each pace event."""
    return [(e["station"], e["detail"]) for e in events() if e["event"] == "pace"]


def pfile(s: str):
    try:
        return json.loads(paths.pace_station_file(s).read_text(encoding="utf-8"))
    except OSError:
        return None


def pace_json():
    try:
        return json.loads(paths.pace_file.read_text(encoding="utf-8"))
    except OSError:
        return None


launched: list = []
fail_restart = [False]
real_restart = None


def fake_restart(client, paths_, args, *, claude_bin):
    """What pace.send hands to lifecycle.restart: the station and the prompt."""
    from tares.crew.errors import CrewError
    if fail_restart[0]:
        raise CrewError(f"could not start {args[0]}")
    launched.append({"station": args[0], "prompt": args[1]})


from tares.crew import lifecycle  # noqa: E402

real_restart = lifecycle.restart
lifecycle.restart = fake_restart


def capture(fn, *a, **kw):
    """(result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        r = fn(*a, **kw)
    return r, out.getvalue(), err.getvalue()


def round_(tares: FakeTares) -> str:
    """One run of the pace step against this Tares; what it said."""
    with contextlib.redirect_stdout(io.StringIO()):
        _, _, err = capture(pace.step, paths, env=dict(os.environ), claude_bin=CLAUDE)
    return err


def reading(d, **kw):
    """The Reading `read` makes of a Tares that answers d."""
    reset({})
    with FakeTares(d, **kw):
        c = TaresClient(paths, dict(os.environ))
        try:
            return pace.read(c)
        finally:
            c.close()


# ---------------------------------------------------------------------------------------------
print("the reading")
r = reading(doc(85, 10))
ck("a reading: active, level 1, not at the limit", r.state == "active" and r.level == 1 and not r.at_limit and r.paced
   and not r.closed, vars(r))
ck("...with the note and the reset time", r.note.startswith("Pacing: the 5-hour window is at 85%") and r.until == "2033-05-18T05:00:00Z")
r = reading(doc(100, 10, level=2, at_limit=True))
ck("at the limit: closed", r.closed and r.level == 2)
r = reading(doc(10, 10, level=0))
ck("level 0: active, not paced", r.state == "active" and not r.paced)
ck("on false: off", reading(doc(99, 0, on=False)).state == "off")
ck("known false: unknown", reading(doc(99, 0, known=False)).state == "unknown")
ck("no answer from the route: unknown", reading(None).state == "unknown")
ck("Tares down: unknown", reading(doc(99, 0), health=False).state == "unknown")
ck("a string true counts, like jq's (on, known, at_limit)",
   pace._read_doc({"on": "true", "known": "true", "level": "2", "at_limit": "true"}).closed)
ck("a document that is not an object: unknown", pace._read_doc([1]).state == "unknown")
ck("a level that is text and not a number: 0", pace._read_doc({"on": True, "known": True, "level": "high"}).level == 0)

print("what a process remembers")
pace.forget()
ck("nothing read: last is unknown", pace.last(paths).state == "unknown")
reset({})
with FakeTares(doc(85, 10)) as t:
    c = TaresClient(paths, dict(os.environ))
    pace.read(c)
    c.close()
    ck("after a read, last is that reading", pace.last(paths).level == 1)
    n = len([x for x in t.requests if x == "GET /api/crew/pace"])
    ck("current inside the ttl does not ask Tares again", pace.current(paths, dict(os.environ)).level == 1
       and len([x for x in t.requests if x == "GET /api/crew/pace"]) == n, str(t.requests))
    t.doc = doc(10, 10)
    pace._LAST[str(paths.home)] = (pace._LAST[str(paths.home)][0] - pace.READING_TTL - 1, pace.last(paths))
    ck("current past the ttl reads again", pace.current(paths, dict(os.environ)).level == 0)

# ---------------------------------------------------------------------------------------------
print("pace.json: the stopped ones")
reset()
R_OPEN = pace.Reading("active", 2, True, "2033-05-18T05:00:00Z")
R_FREE = pace.Reading("active", 0)
_, _, err = capture(pace.hold_down, paths, "crew-reviewer", R_OPEN)
ck("hold_down says so once, with the reset time and the first station",
   err.count("the plan's limit is full until 2033-05-18T05:00:00Z, so a stopped station is not revived now "
             "(crew-reviewer is the first)") == 1, err)
d = pace_json()
ck("...and writes the station, since when and the reset time",
   d["stopped"] == ["crew-reviewer"] and d["said"] is True and d["until"] == "2033-05-18T05:00:00Z"
   and isinstance(d["limit_since"], int), str(d))
_, _, err = capture(pace.hold_down, paths, "shop-build-m1", R_OPEN)
ck("a second station in the same episode is not said again", err == "", err)
ck("...but is remembered, in order", pace_json()["stopped"] == ["crew-reviewer", "shop-build-m1"], str(pace_json()))
ck("stopped: yes for a remembered station", pace.stopped(paths, "crew-reviewer", R_OPEN))
ck("stopped: no for another", not pace.stopped(paths, "crew-helper", R_OPEN))
ck("stopped: no while the reading is unknown", not pace.stopped(paths, "crew-reviewer", pace.Reading()))
pace.mark_tried(paths, "crew-reviewer")
ck("one failed resume is not tried", not pace.tried(paths, "crew-reviewer"))
pace.mark_tried(paths, "crew-reviewer")
ck("two failed resumes in a row are", pace.tried(paths, "crew-reviewer"))
capture(pace.hold_down, paths, "crew-reviewer", R_OPEN)
ck("the limit filling again forgets the failed resumes", not pace.tried(paths, "crew-reviewer") and pace_json()["tried"] == [])
since = pace_json()["limit_since"]
capture(pace.hold_down, paths, "crew-reviewer", R_OPEN)
ck("limit_since keeps the first time", pace_json()["limit_since"] == since)
pace.revived(paths, "crew-reviewer", R_OPEN)
ck("revived forgets the station and keeps the file while others wait", pace_json()["stopped"] == ["shop-build-m1"])
pace.revived(paths, "shop-build-m1", R_OPEN)
ck("...and while the limit is still full", pace_json() is not None and pace_json()["stopped"] == [])
pace.revived(paths, "shop-build-m1", R_FREE)
ck("...and drops the file when nothing is left and the limit is gone", pace_json() is None)
pace.revived(paths, "shop-build-m1", R_FREE)
ck("revived with no file does nothing", pace_json() is None)

print("tidy")
reset({"crew-reviewer": "reviewer", "shop-build-m1": "builder", "crew-helper": "helper"})
CREW.joinpath("pace.json").write_text(json.dumps({"limit_since": 5, "until": "x", "said": True,
                                                  "stopped": ["crew-reviewer", "shop-build-m1", "crew-helper", "gone"],
                                                  "tried": ["gone", "crew-reviewer"]}), encoding="utf-8")
pace.tidy(paths, pace.Reading())
ck("an unknown reading changes nothing", len(pace_json()["stopped"]) == 4)
pace.tidy(paths, R_OPEN)
ck("a station that is gone or a helper is forgotten, the others kept",
   pace_json()["stopped"] == ["crew-reviewer", "shop-build-m1"] and pace_json()["tried"] == ["crew-reviewer"], str(pace_json()))
ck("...and the limit still full, said stays", pace_json()["said"] is True)
paths.station_file("shop-build-m1", "done").write_text("", encoding="utf-8")
pace.tidy(paths, R_OPEN)
ck("a builder that finished is not waited for", pace_json()["stopped"] == ["crew-reviewer"], str(pace_json()))
pace.tidy(paths, R_FREE)
ck("the limit gone: the episode is over", pace_json()["said"] is False and pace_json()["stopped"] == ["crew-reviewer"])
state.stations_put("crew-reviewer", {"status": "retired"}, paths)
pace.tidy(paths, R_FREE)
ck("a retired station was the last: the file goes", pace_json() is None)
CREW.joinpath("pace.json").write_text(json.dumps({"stopped": ["crew-reviewer"]}), encoding="utf-8")
pace.tidy(paths, pace.Reading("off"))
ck("the pace off drops the file", pace_json() is None)

print("a pace file that is damaged")
CREW.joinpath("pace.json").write_text("{not json", encoding="utf-8")
ck("reads as the defaults", pace.json_read(paths)["stopped"] == [] and pace.json_read(paths)["said"] is False)
CREW.joinpath("pace.json").write_text(json.dumps({"stopped": "x", "tried": 3}), encoding="utf-8")
ck("a list that is not a list reads as empty", pace.json_read(paths)["stopped"] == [] and pace.json_read(paths)["tried"] == [])

# ---------------------------------------------------------------------------------------------
print("what plan asks of a station")
reset()
R1, R2 = pace.Reading("active", 1, False, "", "N1"), pace.Reading("active", 2, False, "", "N2")
with FakeTares(doc()) as t:
    c = TaresClient(paths, dict(os.environ))

    def pl(s, rd):
        return pace.plan(c, paths, s, rd, claude_bin=CLAUDE)

    ck("an unknown reading: nothing", pl("crew-reviewer", pace.Reading()) is None)
    p = pl("crew-reviewer", R1)
    ck("level 1, not stretched: stretch, told to ride at 45 minutes",
       p["want"] == "stretch" and p["stretched"] and not p["held"] and p["prompt"].startswith("N1 Stretch your tick to 45 minutes")
       and p["prompt"].endswith("Carry on from your report."), str(p))
    ck("...with CronList then CronDelete", "CronList, then CronDelete" in p["prompt"])
    p = pl("shop-build-m1", R2)
    ck("level 2, a builder: stretch and hold in one note", p["want"] == "stretch" and p["held"] and pace.HOLD_TEXT in p["prompt"], str(p))
    p = pl("crew-reviewer", R2)
    ck("level 2, a reviewer: stretch only", p["want"] == "stretch" and not p["held"] and pace.HOLD_TEXT not in p["prompt"])
    ck("a helper is not told", pl("crew-helper", R1) is None)
    ck("a role outside the crew is not told", (state.stations_put("crew-x", station("spectator", sessionId="s"), paths),
                                              pl("crew-x", R1))[1] is None)
    ck("the pace off, nothing told: nothing", pl("crew-reviewer", pace.Reading("off")) is None)
    ck("level 0, nothing told: nothing", pl("crew-reviewer", R_FREE) is None)

    state.pace_station_put("crew-reviewer", True, False, paths)
    state.pace_station_put("shop-build-m1", True, False, paths)
    ck("stretched, still level 1: nothing", pl("crew-reviewer", R1) is None)
    ck("stretched, and the reading is missing: nothing, not a release", pl("crew-reviewer", pace.Reading()) is None)
    p = pl("shop-build-m1", R2)
    ck("stretched builder, level 2: hold", p["want"] == "hold" and p["held"] and p["stretched"], str(p))
    ck("stretched reviewer, level 2: nothing (only builders hold)", pl("crew-reviewer", R2) is None)
    state.pace_station_put("shop-build-m1", True, True, paths)
    ck("held builder, level 2: nothing", pl("shop-build-m1", R2) is None)
    p = pl("shop-build-m1", R1)
    ck("held builder, level 1: unhold, still stretched", p["want"] == "unhold" and p["stretched"] and not p["held"]
       and "room for builders again" in p["prompt"], str(p))
    p = pl("shop-build-m1", R_FREE)
    ck("held builder, level 0: release, back to the normal tick", p["want"] == "release" and not p["stretched"]
       and not p["held"] and "stopped pacing the crew" in p["prompt"], str(p))
    p = pl("shop-build-m1", pace.Reading("off"))
    ck("the pace turned off while stretched: release", p["want"] == "release")

    ck("a station with no session is not told", (state.stations_put("crew-reviewer", {"sessionId": ""}, paths),
                                                pl("crew-reviewer", pace.Reading("off")))[1] is None)
    state.stations_put("crew-reviewer", {"sessionId": "sess-1"}, paths)
    paths.halt_file.write_text('{"at": 1, "reason": "x"}\n', encoding="utf-8")
    ck("a halted crew tells no one", pl("shop-build-m1", R_FREE) is None)
    paths.halt_file.unlink()
    rows = json.loads((STATE / "agents.json").read_text(encoding="utf-8"))
    for row in rows:
        if row["name"] == "shop-build-m1":
            row["status"] = "busy"
    fake_claude.write_agents(STATE, rows)
    ck("a station in the middle of a turn is not told", pl("shop-build-m1", R_FREE) is None)

    t.idle_wake = True
    c.close()
    c = TaresClient(paths, dict(os.environ))   # the settings are read once per client: a new round, a new client
    ck("idle_wake on: nothing is told", pl("crew-reviewer", R1) is None)
    ck("...and a pace file is removed (a released tick is not stretched)", pfile("crew-reviewer") is None)
    t.idle_wake = False
    c.close()

# ---------------------------------------------------------------------------------------------
print("telling a station")
reset()
del launched[:]
with FakeTares(doc(85, 10)) as t:
    c = TaresClient(paths, dict(os.environ))
    p = pace.plan(c, paths, "crew-reviewer", pace.read(c), claude_bin=CLAUDE)
    _, out, err = capture(pace.send, c, paths, p, claude_bin=CLAUDE)
    ck("send restarts the station in its conversation with the note",
       launched == [{"station": "crew-reviewer", "prompt": p["prompt"]}], str(launched))
    ck("...says so", "pacing: telling crew-reviewer (stretch) in the same conversation" in err, err)
    ck("...writes the event and the pace file", told() == [("crew-reviewer", "stretch")] and pfile("crew-reviewer") == {"stretched": True, "held": False},
       str(told()))

    fail_restart[0] = True
    p = pace.plan(c, paths, "shop-build-m1", pace.read(c), claude_bin=CLAUDE)
    _, out, err = capture(pace.send, c, paths, p, claude_bin=CLAUDE)
    ck("a restart that fails says so and will try again", "could not tell shop-build-m1 about the pace; will try again next round" in err, err)
    ck("...sends no event and writes no pace file", told() == [("crew-reviewer", "stretch")] and pfile("shop-build-m1") is None)
    fail_restart[0] = False

    t.idle_wake = True
    n = len(launched)
    capture(pace.send, c, paths, p, claude_bin=CLAUDE)
    ck("send checks the setting again: idle_wake went on, no restart", len(launched) == n)
    t.idle_wake = False
    paths.halt_file.write_text('{"at": 1, "reason": "x"}\n', encoding="utf-8")
    capture(pace.send, c, paths, p, claude_bin=CLAUDE)
    ck("send checks the halt again: no restart", len(launched) == n)
    paths.halt_file.unlink()
    c.close()

# ---------------------------------------------------------------------------------------------
print("the step")
reset()
del launched[:]
with FakeTares(doc(85, 10)) as t:
    err = round_(t)
    ck("a round tells every station of a crew role once, and not the helper",
       sorted(told()) == [("crew-reviewer", "stretch"), ("shop-build-m1", "stretch")], str(told()))
    ck("...the launches match the events", sorted(x["station"] for x in launched) == ["crew-reviewer", "shop-build-m1"])
    n = len(launched)
    round_(t)
    ck("the next round tells no one again", len(launched) == n and len(told()) == 2)
    t.doc = doc(96, 10, level=2)
    round_(t)
    ck("the plan at 96%: the builder is told to hold, the reviewer nothing", told()[2:] == [("shop-build-m1", "hold")], str(told()))
    t.doc = doc(10, 10, level=0)
    round_(t)
    ck("back to normal: each is told once to go back", sorted(told()[3:]) == [("crew-reviewer", "release"), ("shop-build-m1", "release")]
       and pfile("crew-reviewer") is None and pfile("shop-build-m1") is None, str(told()))

print("the step with a builder that is done, a station mid-turn, and a reading that is missing")
reset(states={"crew-reviewer": "working"})
del launched[:]
paths.station_file("shop-build-m1", "done").write_text("", encoding="utf-8")
with FakeTares(doc(85, 10)) as t:
    round_(t)
    ck("a done builder and a busy station are left alone", launched == [], str(launched))
    t.doc = doc(85, 10, known=False)
    reset(); del launched[:]
    err = round_(t)
    ck("a reading that is missing changes nothing", launched == [] and told() == [] and pfile("crew-reviewer") is None)
    t.doc = None
    round_(t)
    ck("...and a Tares that does not answer neither", launched == [])
    t.doc = doc(85, 10)
    t.health = False
    round_(t)
    ck("...and a Tares that is down neither", launched == [])

print("the pace off writes no file")
reset()
del launched[:]
with FakeTares(doc(99, 0, on=False)) as t:
    for _ in range(3):
        round_(t)
    ck("on false at 99%: no note, no restart, no file", launched == [] and told() == [] and pfile("crew-reviewer") is None
       and pace_json() is None)

print("idle_wake on: the step drops the pace file and tells no one")
reset()
state.pace_station_put("crew-reviewer", True, False, paths)
with FakeTares(doc(95, 10, level=2), idle_wake=True) as t:
    round_(t)
    ck("the file goes, nothing is sent", pfile("crew-reviewer") is None and launched == [] and told() == [])

# ---------------------------------------------------------------------------------------------
print("a start while the crew is paced")
reset()
with FakeTares(doc(85, 10)) as t:
    c = TaresClient(paths, dict(os.environ))
    pace.forget()
    pace.launch_note(c, paths, "crew-reviewer")
    ck("a process that has not read the pace writes no pace file", pfile("crew-reviewer") is None)
    pace.read(c)
    pace.launch_note(c, paths, "crew-reviewer")
    ck("a start while paced writes the station's pace file, already stretched", pfile("crew-reviewer") == {"stretched": True, "held": False})
    pace.launch_note(c, paths, "crew-helper")
    ck("a helper writes none", pfile("crew-helper") is None)
    t.idle_wake = True
    c.close()
    c = TaresClient(paths, dict(os.environ))
    pace.launch_note(c, paths, "shop-build-m1")
    ck("idle_wake on writes none", pfile("shop-build-m1") is None)
    t.idle_wake = False
    c.close()
    c = TaresClient(paths, dict(os.environ))
    t.doc = doc(10, 10, level=0)
    pace.read(c)
    pace.launch_note(c, paths, "shop-build-m1")
    ck("the plan with room writes none", pfile("shop-build-m1") is None)
    c.close()

print("what revive asks of the pace (Seam)")
reset()
with FakeTares(doc(100, 10, level=2, at_limit=True)) as t:
    seam = pace.Seam(dict(os.environ))
    ck("closed while a limit is full", seam.closed(paths))
    _, _, err = capture(seam.hold_down, paths, "crew-reviewer")
    ck("hold_down says so with the reset time", "the plan's limit is full until 2033-05-18T05:00:00Z" in err, err)
    ck("stopped is yes for it", seam.stopped(paths, "crew-reviewer"))
    seam.mark_tried(paths, "crew-reviewer")
    ck("tried counts", not seam.tried(paths, "crew-reviewer"))
    seam.mark_tried(paths, "crew-reviewer")
    ck("...two make it tried", seam.tried(paths, "crew-reviewer"))
    ck("the resume prompt is short and says the limit was reset",
       seam.resume_prompt(paths).startswith("The plan's limit was reset. Carry on from where you stopped")
       and "Reload the skills" not in seam.resume_prompt(paths))
    fp = seam.fresh_prompt(paths, "crew-reviewer")
    ck("the fresh prompt says why and to reload the skills", "could not be resumed" in fp
       and "Reload the skills factory-protocol and station-reviewer" in fp, fp)
    t.doc = doc(10, 10, level=0)
    pace._LAST[str(paths.home)] = (pace._LAST[str(paths.home)][0] - pace.READING_TTL - 1, pace.last(paths))
    ck("a new round's reading: no longer closed", not seam.closed(paths))
    seam.revived(paths, "crew-reviewer")
    ck("revived with the limit gone: the file goes", pace_json() is None)

print("revive after the pace was turned off")
reset()
paths.pace_file.write_text(json.dumps({"limit_since": "", "until": "", "said": True, "stopped": ["crew-reviewer"], "tried": []}),
                           encoding="utf-8")
with FakeTares(doc(99, 0, on=False)) as t:
    seam = pace.Seam(dict(os.environ))
    ck("a stopped station of a pace that is now off is not stopped (bash tidied at the top of the round)",
       not seam.stopped(paths, "crew-reviewer"))
    ck("...and the pace file is gone", pace_json() is None)

print("the watch runs it as a step")
ck("pace is a step of the watch", "pace" in watch.STEP_NAMES)
ck("and the step is this one", watch._STEPS.get("pace") is pace.step)

lifecycle.restart = real_restart
L.launch  # keep the import used
print(f"{P} passed, {F} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if F else 0)
