"""The watch's revive step in Python (tares/crew/watch_revive.py, TR-959): a station that stopped is revived under
the same name, one Claude Code stopped for thrashing on autocompact is started fresh, and the rest is left alone.

Each round runs against the Python fake Claude Code (tests/factory/fake_claude.py) in a throwaway crew home, with a
small fake Tares. The call to launch is replaced by a recorder that registers a new session, so these checks need
no bash and no real Claude Code. The same rounds run on bash and on Python side by side in the parity harness
(`parity.py 'watch_revive_*'`). Nothing here signals a real station or the real watch.

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_watch_revive.py
"""
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

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-watch-revive-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
CREW = SCRATCH / "crew"
STATE = SCRATCH / "claude"
HOME = SCRATCH / "home"
os.environ.update(
    HOME=str(HOME), USERPROFILE=str(HOME),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1", FACTORY_CREW_HOME=str(CREW), FAKE_CLAUDE_STATE=str(STATE),
    FACTORY_STOP_WAIT="2",
)
HOME.mkdir()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FACTORY_DIR))

import fake_claude  # noqa: E402
from tares.crew import launch as L, state, watch, watch_revive as WR  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def make_launcher(folder: Path, script: Path) -> str:
    """A `claude` that runs the Python fake: a sh script, or a .cmd on Windows. Returns its path."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        f = folder / "claude.cmd"
        f.write_bytes(f'@"{sys.executable}" "{script}" %*\r\n'.encode("ascii", "replace"))
    else:
        f = folder / "claude"
        f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)
    return str(f)


os.environ["CLAUDE_BIN"] = make_launcher(SCRATCH / "bin", FACTORY_DIR / "fake_claude.py")


class FakeTares:
    """A Tares that answers /health and the crew settings, and writes down each request."""

    def __init__(self):
        self.requests: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                key = f"{self.command} {self.path}"
                outer.requests.append(key)
                if key == "GET /health":
                    status, doc = 200, {"ok": True}
                elif key == "GET /api/crew/settings":
                    status, doc = 200, {"settings": {}}
                else:
                    status, doc = 404, {"detail": "not found"}
                data = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

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


work = SCRATCH / "work"
work.mkdir()


def station(**over) -> dict:
    row = {"role": "builder", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
           "cwd": str(work), "model": "opus", "effort": "high", "id": "aaaa0001", "sessionId": "sess-1"}
    row.update(over)
    return row


def reset(stations: dict, agents: list):
    """A fresh crew home with these stations, and the fake Claude Code listing these sessions."""
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    shutil.rmtree(HOME / ".claude", ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    fake_claude.write_agents(STATE, agents)
    for k in ("FAKE_CLAUDE_AGENTS_FAIL", "FAKE_CLAUDE_AGENTS_FAIL_AFTER"):
        os.environ.pop(k, None)
    del launched[:]
    fail_on.clear()


def stations_now() -> dict:
    return json.loads((CREW / "stations.json").read_text(encoding="utf-8"))


def events() -> list:
    try:
        lines = (CREW / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines]


def calls() -> list:
    try:
        lines = (STATE / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x)["args"] for x in lines if x.strip()]


def row(kind: str, sid: str, name: str, **extra) -> dict:
    return fake_claude.agent_row(kind, sid, name, str(work), **extra)


def transcript(sess: str, *lines: str) -> None:
    folder = HOME / ".claude" / "projects" / "-crew"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{sess}.jsonl", "a", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line + "\n")


ORDINARY = '{"type":"assistant","message":{"model":"claude-opus-5-5","content":[{"type":"text","text":"tick done"}]}}'
THRASH = ('{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Autocompact is '
          'thrashing: the context refilled to the limit within 3 turns of the previous compact, 3 times in a row."}]}}')
WOKEN = '{"type":"user","message":{"role":"user","content":"carry on with T4"}}'

launched: list = []
fail_on: set = set()


def fake_launch(client, station_name, role, parent, project, repo, cwd, model, effort, prompt, resume="", **kw):
    """What revive hands to launch; the 'session' it starts is a new row in the fake Claude Code."""
    if station_name in fail_on:
        raise CrewError(f"could not start {station_name} (fake)")
    n = len(launched) + 2
    new_id = f"bbbb{n:04x}"
    launched.append({"station": station_name, "prompt": prompt, "resume": resume})
    state.stations_put(station_name, {"id": new_id, "sessionId": f"sess-new-{n}", "status": "active"}, client.paths)
    rows = json.loads((STATE / "agents.json").read_text(encoding="utf-8"))
    rows.append(fake_claude.agent_row("working", new_id, station_name, cwd))
    fake_claude.write_agents(STATE, rows)


real_launch = L.launch
L.launch = fake_launch


def go(**env) -> str:
    """One round of the revive step; what it said (stderr)."""
    err = io.StringIO()
    old = {k: os.environ.get(k) for k in env}
    try:
        with FakeTares(), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            os.environ.update({k: str(v) for k, v in env.items()})
            WR.step(CrewPaths.from_env())
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return err.getvalue()


def said(err: str) -> list:
    return [x[len("tares factory: "):] for x in err.splitlines() if x.startswith("tares factory: ")]


AGAIN = ("Reload the skills factory-protocol and station-builder, re-read your report and your work on Tares, "
         "re-arm your schedule if you have one, then carry on where your report left off.")

# ---------------------------------------------------------------------------------------------
print("the step is the watch's revive step")
ck("watch.py registers it", watch._STEPS.get("revive") is WR.step)
reset({"shop-build-m1": station()}, [row("stopped", "aaaa0001", "shop-build-m1")])
with FakeTares(), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
    watch.run_steps(CrewPaths.from_env())
ck("...a stopped station is revived by watch.run_steps", len(launched) == 1, str(launched))

# ---------------------------------------------------------------------------------------------
print("revive")
reset({"shop-build-m1": station(), "crew-reviewer": station(role="reviewer", id="aaaa0002", sessionId="sess-2")},
      [row("stopped", "aaaa0001", "shop-build-m1"), row("idle", "aaaa0002", "crew-reviewer")])
err = go()
ck("a stopped station is revived under the same name, resuming its conversation",
   len(launched) == 1 and launched[0]["station"] == "shop-build-m1" and launched[0]["resume"] == "sess-1", str(launched))
ck("...with the revived prompt", launched and launched[0]["prompt"] == f"You were revived by the factory under the same name. {AGAIN}",
   str(launched))
ck("...and says so", said(err) == ["shop-build-m1 stopped; reviving it under the same name"], str(said(err)))
ck("a running station is left alone", "crew-reviewer" not in [x["station"] for x in launched])
ck("the event is revived", [e["event"] for e in events()] == ["revived"], str(events()))
ck("the old session is kept as an earlier one",
   [e["id"] for e in stations_now()["shop-build-m1"].get("earlier", [])] == ["aaaa0001"], str(stations_now()["shop-build-m1"]))
ck("last.started names it, last.down is set", WR.last.started == {"shop-build-m1"} and WR.last.down and not WR.last.unread,
   repr(WR.last))
go()
ck("the next round finds it running and starts nothing", len(launched) == 1, str(launched))
ck("...last.down is clear", not WR.last.down and WR.last.started == set(), repr(WR.last))

print("who is not revived")
reset({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", sessionId="sess-2"),
       "shop-build-m1-probe": station(role="helper", id="aaaa0003", sessionId="sess-3"),
       "old-one": station(status="retired", id="aaaa0004", sessionId="sess-4"),
       "crew-releaser": station(role="releaser", id="aaaa0005", sessionId="sess-5")},
      [row("stopped", f"aaaa000{i}", n) for i, n in ((1, "shop-build-m1"), (2, "shop-build-m2"), (3, "shop-build-m1-probe"),
                                                       (4, "old-one"), (5, "crew-releaser"))])
(CREW / "stations" / "shop-build-m1.done").write_text("", encoding="utf-8")
go()
ck("a finished builder, a helper and a retired station are left; the rest come back",
   sorted(x["station"] for x in launched) == ["crew-releaser", "shop-build-m2"], str(launched))
reset({"crew-releaser": station(role="releaser", id="aaaa0005", sessionId="sess-5")}, [row("stopped", "aaaa0005", "crew-releaser")])
(CREW / "stations" / "crew-releaser.done").write_text("", encoding="utf-8")
go()
ck("...a releaser with a .done file is still revived", [x["station"] for x in launched] == ["crew-releaser"], str(launched))

print("a session list that cannot be read")
reset({"shop-build-m1": station()}, [row("stopped", "aaaa0001", "shop-build-m1")])
err = go(FAKE_CLAUDE_AGENTS_FAIL="1")
ck("nothing is revived, and it says so",
   launched == [] and said(err) == ["could not read Claude Code's session list (claude agents --json); "
                                    "nothing is revived or restarted this round"], str(said(err)))
ck("...last.unread is set", WR.last.unread and not WR.last.started, repr(WR.last))
err = go()
ck("readable again: the next round revives it", len(launched) == 1 and not WR.last.unread, str(launched))

reset({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", sessionId="sess-2")},
      [row("stopped", "aaaa0001", "shop-build-m1"), row("stopped", "aaaa0002", "shop-build-m2")])
err = go(FAKE_CLAUDE_AGENTS_FAIL_AFTER="1")
ck("a list that fails while one station is checked skips it and goes on",
   launched == [] and "skipping shop-build-m1 this round" in said(err) and "skipping shop-build-m2 this round" in said(err)
   and any("not guessing whether shop-build-m1 runs" in x for x in said(err)) and WR.last.down, str(said(err)))
ck("...last.handled names both, so a later step leaves them alone",
   WR.last.handled == {"shop-build-m1", "shop-build-m2"} and WR.last.started == set(), repr(WR.last))

print("a revive that fails")
reset({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", sessionId="sess-2")},
      [row("stopped", "aaaa0001", "shop-build-m1"), row("stopped", "aaaa0002", "shop-build-m2")])
fail_on.add("shop-build-m1")
err = go()
ck("it is reported, and the next station is still revived",
   "could not revive shop-build-m1; will try again next round" in said(err)
   and [x["station"] for x in launched] == ["shop-build-m2"], str(said(err)))
ck("...the refusal itself is printed too", "could not start shop-build-m1 (fake)" in said(err), str(said(err)))
ck("...it is not counted as started", WR.last.started == {"shop-build-m2"}, repr(WR.last))
ck("...but it is handled, like the one that came back", WR.last.handled == {"shop-build-m1", "shop-build-m2"}, repr(WR.last))
fail_on.clear()
go()
ck("the next round revives it", [x["station"] for x in launched] == ["shop-build-m2", "shop-build-m1"], str(launched))

# ---------------------------------------------------------------------------------------------
print("stuck on autocompact")
if True:
    reset({"crew-orchestrator": station(role="orchestrator", parent="", project="", id="aaaa0001", sessionId="sess-1"),
           "crew-reviewer": station(role="reviewer", parent="", project="", id="aaaa0002", sessionId="sess-2")},
          [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "crew-reviewer")])
    transcript("sess-1", ORDINARY, '{"type":"system","subtype":"compact_boundary"}', THRASH)
    transcript("sess-2", THRASH, ORDINARY)
    err = go()
    ck("the stuck station is stopped and started again in a new conversation",
       [x["station"] for x in launched] == ["crew-orchestrator"] and launched[0]["resume"] == "" and ["stop", "aaaa0001"] in calls(),
       f"{launched} {calls()}")
    ck("...it says why", said(err)[:1] == ["crew-orchestrator is stuck: Claude Code stopped it for thrashing on autocompact; "
                                          "starting it fresh under the same name"], str(said(err)))
    ck("...the new conversation's prompt says so", "in a new conversation" in launched[0]["prompt"], launched[0]["prompt"])
    ck("...thrashing is recorded, then restarted-fresh", [e["event"] for e in events()] == ["thrashing", "restarted-fresh"], str(events()))
    ck("...a station whose last reply is ordinary is left alone", all(x["station"] != "crew-reviewer" for x in launched))
    ck("...it counts as started and nothing was down", WR.last.started == {"crew-orchestrator"} and not WR.last.down, repr(WR.last))
    ck("...it is handled, and the one left alone is not", WR.last.handled == {"crew-orchestrator"}, repr(WR.last))
    go()
    ck("the new conversation is not restarted again", len(launched) == 1, str(launched))

print("...but not when it is working, was woken, or is a helper")
reset({"crew-releaser": station(role="releaser", parent="", project="", id="aaaa0001", sessionId="sess-1")},
      [row("working", "aaaa0001", "crew-releaser")])
transcript("sess-1", THRASH)
go()
ck("busy by the list: left alone", launched == [] and ["stop", "aaaa0001"] not in calls(), str(launched))
fake_claude.write_agents(STATE, [row("idle", "aaaa0001", "crew-releaser")])
transcript("sess-1", WOKEN)
go()
ck("woken by a message after the notice: left alone", launched == [], str(launched))
reset({"crew-orchestrator": station(role="orchestrator", parent="", project="", id="aaaa0001", sessionId="sess-1"),
       "crew-reviewer-probe": station(role="helper", parent="crew-reviewer", id="aaaa0002", sessionId="sess-2")},
      [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "crew-reviewer-probe")])
transcript("sess-2", THRASH)
go()
ck("a stuck helper is left to its parent", launched == [], str(launched))
reset({"crew-releaser": station(role="releaser", parent="", project="", id="aaaa0001", sessionId="sess-1")},
      [row("idle", "aaaa0001", "crew-releaser")])
go()
ck("a station with no transcript is left alone", launched == [], str(launched))
(CREW / "stations" / "crew-releaser.done").write_text("", encoding="utf-8")

print("...a stuck station that stopped before the watch saw it")
reset({"crew-releaser": station(role="releaser", parent="", project="", id="aaaa0001", sessionId="sess-1")},
      [row("stopped", "aaaa0001", "crew-releaser")])
transcript("sess-1", THRASH)
err = go()
ck("comes back in a new conversation, not resumed",
   len(launched) == 1 and launched[0]["resume"] == "" and "in a new conversation" in launched[0]["prompt"], str(launched))
ck("...and says so", said(err) == ["crew-releaser stopped; reviving it under the same name, in a new conversation "
                                   "since its last one thrashed"], str(said(err)))
ck("...with the restarted-fresh event, and no thrashing event", [e["event"] for e in events()] == ["restarted-fresh"], str(events()))

print("a restart that fails")
reset({"crew-orchestrator": station(role="orchestrator", parent="", project="", id="aaaa0001", sessionId="sess-1")},
      [row("idle", "aaaa0001", "crew-orchestrator")])
transcript("sess-1", THRASH)
fail_on.add("crew-orchestrator")
err = go()
ck("is reported and tried again next round", "could not restart crew-orchestrator; will try again next round" in said(err)
   and WR.last.started == set(), str(said(err)))
ck("...and it is handled all the same: a later step does not restart it a second time",
   WR.last.handled == {"crew-orchestrator"}, repr(WR.last))

# ---------------------------------------------------------------------------------------------
print("the pace seam")


class Spy(WR.PaceSeam):
    def __init__(self, **ans):
        self.ans = ans
        self.log = []

    def _ask(self, name, *a):
        self.log.append((name, *a[1:]))
        return self.ans.get(name, "" if name.endswith("prompt") else False)

    def revived(self, paths, s):
        self.log.append(("revived", s))

    def closed(self, paths):
        return self._ask("closed", paths)

    def hold_down(self, paths, s):
        self.log.append(("hold_down", s))

    def stopped(self, paths, s):
        return self._ask("stopped", paths, s)

    def tried(self, paths, s):
        return self._ask("tried", paths, s)

    def resume_prompt(self, paths):
        return self._ask("resume_prompt", paths)

    def fresh_prompt(self, paths, s):
        return self._ask("fresh_prompt", paths, s)

    def mark_tried(self, paths, s):
        self.log.append(("mark_tried", s))


real_pace = WR.pace
two = lambda: reset({"shop-build-m1": station()}, [row("stopped", "aaaa0001", "shop-build-m1")])  # noqa: E731

two()
WR.pace = Spy(closed=True)
err = go()
ck("a closed limit holds a down station: no revive", launched == [] and ("hold_down", "shop-build-m1") in WR.pace.log
   and said(err) == [], str(WR.pace.log))
ck("...and the round still knows a station is down", WR.last.down)

two()
WR.pace = Spy(stopped=True, resume_prompt="PACE RESUME")
err = go()
ck("a station the limit stopped is revived in its conversation with the resume prompt",
   len(launched) == 1 and launched[0]["resume"] == "sess-1" and launched[0]["prompt"] == "PACE RESUME", str(launched))
ck("...and says so", said(err) == ["shop-build-m1 stopped while the plan's limit was full, which has reset; "
                                   "reviving it under the same name"], str(said(err)))
ck("...pace is told it runs again", ("revived", "shop-build-m1") in WR.pace.log, str(WR.pace.log))

two()
WR.pace = Spy(stopped=True, tried=True, fresh_prompt="PACE FRESH")
err = go()
ck("one whose own conversation could not be resumed starts a new one",
   len(launched) == 1 and launched[0]["resume"] == "" and launched[0]["prompt"] == "PACE FRESH", str(launched))
ck("...and says so", said(err) == ["shop-build-m1 stopped while the plan's limit was full, which has reset; reviving it "
                                   "under the same name, in a new conversation since its own could not be resumed"], str(said(err)))

two()
WR.pace = Spy(stopped=True, resume_prompt="PACE RESUME")
fail_on.add("shop-build-m1")
go()
ck("a failed pace resume is marked tried", ("mark_tried", "shop-build-m1") in WR.pace.log, str(WR.pace.log))
two()
WR.pace = Spy()
fail_on.add("shop-build-m1")
go()
ck("a failed plain revive is not", ("mark_tried", "shop-build-m1") not in WR.pace.log, str(WR.pace.log))

two()
transcript("sess-1", THRASH)
WR.pace = Spy(stopped=True, resume_prompt="PACE RESUME")
go()
ck("a stuck station is started fresh whatever the limit did", launched and launched[0]["resume"] == "", str(launched))

reset({"shop-build-m1": station()}, [row("idle", "aaaa0001", "shop-build-m1")])
WR.pace = Spy()
go()
ck("a running station is not told it runs again while there is no pace file", WR.pace.log == [], str(WR.pace.log))
(CREW / "pace.json").write_text("{}\n", encoding="utf-8")
go()
ck("...with one, it is", ("revived", "shop-build-m1") in WR.pace.log, str(WR.pace.log))
WR.pace = real_pace
inert = WR.PaceSeam()   # watch.py hands the revive loop the real pace (TR-962); the stand-in it replaced stays inert
ck("the seam is inert by default", not inert.closed(None) and not inert.stopped(None, "x") and not inert.tried(None, "x")
   and inert.resume_prompt(None) == "" and inert.fresh_prompt(None, "x") == "")

L.launch = real_launch
print(f"{P} passed, {F} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if F else 0)
