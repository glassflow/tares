"""Reviving, restarting, retiring, stopping, halting and resuming in Python (tares/crew/lifecycle.py, TR-955).

Each command runs against the Python fake Claude Code (tests/factory/fake_claude.py) in a throwaway crew home,
with a small fake Tares that writes down what it is asked. The call to launch is replaced by a recorder that
registers a new session, so these checks need no bash and no real Claude Code. The same commands run on bash
and on Python side by side in the parity harness (`parity.py lifecycle_*`).

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_lifecycle.py
"""
import contextlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
FACTORY_DIR = HERE / "factory"

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-lifecycle-"))
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
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FACTORY_DIR))

import fake_claude  # noqa: E402
from tares.crew import claude, dispatch, lifecycle as LC, state  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
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
    """A Tares that answers by route and writes down each request: {"POST /x": (status, doc)}; /health is up."""

    def __init__(self, routes=None, health=True):
        self.routes = {"GET /api/crew/settings": (200, {"settings": {}})}
        self.routes.update(routes or {})
        self.health = health
        self.requests: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                key = f"{self.command} {self.path}"
                outer.requests.append({"route": key, "auth": self.headers.get("Authorization"), "body": body,
                                       "type": self.headers.get("Content-Type")})
                if key == "GET /health":
                    status, doc = (200 if outer.health else 503), {"ok": True}
                else:
                    status, doc = outer.routes.get(key, (404, {"detail": "not found"}))
                data = (doc if isinstance(doc, str) else json.dumps(doc)).encode("utf-8")
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

    def asked(self, route: str) -> list:
        return [r for r in self.requests if r["route"] == route]


def reset(stations: dict, agents: list | None = None, **files):
    """A fresh crew home with these stations, and the fake Claude Code listing these sessions."""
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    fake_claude.write_agents(STATE, agents or [])
    for name, text in files.items():
        (CREW / name).write_text(text, encoding="utf-8")
    for k in ("FACTORY_STOP_WAIT", "FAKE_CLAUDE_STOP_KEEPS_STATE", "FAKE_CLAUDE_AGENTS_FAIL"):
        os.environ.pop(k, None)


def station(**over) -> dict:
    row = {"role": "builder", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
           "cwd": str(SCRATCH / "work"), "model": "opus", "effort": "high", "id": "aaaa0001", "sessionId": "sess-1"}
    row.update(over)
    return row


def stations() -> dict:
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


def agent_ids() -> list:
    return [r["id"] for r in json.loads((STATE / "agents.json").read_text(encoding="utf-8"))]


@contextlib.contextmanager
def no_claude():
    """CLAUDE_BIN unset and nothing on PATH: Claude Code is not to be found."""
    old = {k: os.environ.pop(k, None) for k in ("CLAUDE_BIN",)}
    old_path, os.environ["PATH"] = os.environ.get("PATH"), ""
    try:
        yield
    finally:
        os.environ["CLAUDE_BIN"] = old["CLAUDE_BIN"]
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path


launched: list = []
real_launch = L.launch


def fake_launch(client, station_name, role, parent, project, repo, cwd, model, effort, prompt, resume="", **kw):
    """What revive hands to launch; the 'session' it starts is a new row in the fake Claude Code."""
    n = len(launched) + 2
    new_id = f"bbbb{n:04x}"
    launched.append({"station": station_name, "role": role, "parent": parent, "project": project, "cwd": cwd,
                     "model": model, "effort": effort, "prompt": prompt, "resume": resume})
    state.stations_put(station_name, {"id": new_id, "sessionId": f"sess-{n}", "status": "active"}, client.paths)
    rows = json.loads((STATE / "agents.json").read_text(encoding="utf-8"))
    rows.append(fake_claude.agent_row("working", new_id, station_name, cwd))
    fake_claude.write_agents(STATE, rows)


L.launch = fake_launch


def run(fn, args, routes=None, health=True, **env):
    """Run lifecycle.cmd_<fn> as the dispatcher does; (exit code, stdout, stderr, the fake Tares)."""
    out, err = io.StringIO(), io.StringIO()
    old = {k: os.environ.get(k) for k in env}
    try:
        with FakeTares(routes, health) as t, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            os.environ.update({k: str(v) for k, v in env.items()})
            argv = [fn, *args] if fn not in ("crew down", "crew halt", "crew resume") else [*fn.split(), *args]
            code = getattr(LC, "cmd_" + fn.split()[-1])(argv, dict(os.environ))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue(), err.getvalue(), t


def said(err: str) -> list:
    return [x[len("tares factory: "):] for x in err.splitlines() if x.startswith("tares factory: ")]


# ---------------------------------------------------------------------------------------------
print("the dispatcher")
for argv in (["revive", "x"], ["restart", "x"], ["retire", "x"], ["down"], ["halt", "why"], ["resume"],
             ["crew", "down"], ["crew", "halt"], ["crew", "resume"]):
    ck("dispatch.find " + " ".join(argv), dispatch.find(argv) is not None)
ck("crew update and crew restart are here (TR-964, tares/crew/cc_update.py)",
   dispatch.find(["crew", "update"]) is not None and dispatch.find(["crew", "restart"]) is not None)
ck("crew revive is not a command", dispatch.find(["crew", "revive"]) is None)

# ---------------------------------------------------------------------------------------------
print("halt and resume")
reset({})
code, out, err, _ = run("halt", [])
halt = json.loads((CREW / "HALT").read_text(encoding="utf-8"))
ck("halt without a reason writes the default", code == 0 and halt["reason"] == "halted by the human" and "at" in halt, str(halt))
ck("HALT is compact JSON", (CREW / "HALT").read_text(encoding="utf-8").startswith('{"at":"'))
ck("halt prints both lines", said(err) == [
    "crew halted: stations finish their current step and start nothing risky (merges, pushes, deploys, tags, new sessions). "
    "tares factory crew resume lifts it.",
    "tares factory crew attach orchestrator".join(["tell the orchestrator too (", ") so it sends HALT to every station"])], str(said(err)))
ck("halt writes the event with an empty detail", events()[-1]["event"] == "halt" and events()[-1]["detail"] == "", str(events()))
code, out, err, _ = run("crew halt", ["out of money"])
ck("a custom reason is kept", json.loads((CREW / "HALT").read_text(encoding="utf-8"))["reason"] == "out of money")
ck("...and is the event's detail", events()[-1]["detail"] == "out of money", str(events()[-1]))
ck("halt says nothing on stdout", out == "")
code, out, err, _ = run("crew resume", [])
ck("resume removes HALT and says so", code == 0 and not (CREW / "HALT").exists() and said(err) == ["crew resumed"], str(said(err)))
ck("resume writes its event", events()[-1]["event"] == "resume", str(events()[-1]))
code, out, err, _ = run("resume", [])
ck("resume with no HALT is fine", code == 0 and said(err) == ["crew resumed"])
shutil.rmtree(CREW)
code, out, err, _ = run("halt", [])
ck("halt without a crew refuses", code == 1 and "no crew yet in" in err and "run: tares factory crew up" in err, err)
reset({})
with no_claude():
    code, out, err, _ = run("halt", [])
ck("bash looks for claude first: halt dies without it", code == 1 and "claude not found on PATH (set CLAUDE_BIN to its path)" in err
   and not (CREW / "HALT").exists(), err)

# ---------------------------------------------------------------------------------------------
print("retire")
work = SCRATCH / "work"
work.mkdir(exist_ok=True)
reset({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", sessionId="sess-2")},
      [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work)),
       fake_claude.agent_row("working", "aaaa0002", "shop-build-m2", str(work))])
(CREW / "stations" / "shop-build-m1.pace").write_text("{}\n", encoding="utf-8")
code, out, err, t = run("retire", ["shop-build-m1"], {"POST /api/crew/shop-build-m1/end?crew=crew": (200, {"ok": True})})
ck("retire succeeds and says so", code == 0 and said(err) == ["retired shop-build-m1"], str(said(err)))
ck("status is retired, the rest kept", stations()["shop-build-m1"]["status"] == "retired" and stations()["shop-build-m1"]["id"] == "aaaa0001")
ck("the other station is untouched", stations()["shop-build-m2"]["status"] == "active")
ck("the pace file is removed", not (CREW / "stations" / "shop-build-m1.pace").exists())
ck("the session is stopped and removed", ["stop", "aaaa0001"] in calls() and ["rm", "aaaa0001"] in calls() and "aaaa0001" not in agent_ids(), str(calls()))
ck("the event names the station and the id", events()[-1] == {**events()[-1], "event": "retired", "station": "shop-build-m1", "detail": "aaaa0001"}, str(events()))
ask = t.asked("POST /api/crew/shop-build-m1/end?crew=crew")
ck("it tells Tares: reason retired, JSON", len(ask) == 1 and json.loads(ask[0]["body"]) == {"reason": "retired"}
   and ask[0]["type"] == "application/json", str(ask))
ck("...without a key when there is none", ask and ask[0]["auth"] is None, str(ask))
code, out, err, t = run("retire", ["shop-build-m2"], {"POST /api/crew/shop-build-m2/end?crew=crew": (404, {"detail": "unknown"})})
ck("a 404 from Tares is fine and silent", code == 0 and said(err) == ["retired shop-build-m2"], str(said(err)))
reset({"shop-build-m1": station()}, [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work))])
code, out, err, t = run("retire", ["shop-build-m1"], {"POST /api/crew/shop-build-m1/end?crew=crew": (500, "boom")})
ck("any other code says Tares was not told, and retire still succeeds",
   code == 0 and said(err)[0].startswith("could not tell Tares at http://127.0.0.1:") and said(err)[0].endswith(
       " that shop-build-m1 ended; mark it ended on the Crew page") and said(err)[1] == "retired shop-build-m1", str(said(err)))
reset({"shop-build-m1": station()}, [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work))])
code, out, err, t = run("retire", ["shop-build-m1"], {}, TARES_AUTH_TOKEN="secret-key")
ck("a key is sent as a Bearer token", t.asked("POST /api/crew/shop-build-m1/end?crew=crew")[0]["auth"] == "Bearer secret-key",
   str(t.requests))
reset({"shop-build-m1": station()}, [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work))])
code, out, err, t = run("retire", ["shop-build-m1"], {}, health=False, TARESD_URL="http://127.0.0.1:9")
ck("with no Tares at all, retire still finishes", code == 0 and stations()["shop-build-m1"]["status"] == "retired"
   and any("could not tell Tares" in x for x in said(err)), str(said(err)))
code, out, err, _ = run("retire", ["nobody"])
ck("an unknown station is refused", code == 1 and "nobody is not a recorded station" in err, err)
code, out, err, _ = run("retire", [])
ck("no station: the usage line", code == 1 and "usage: tares factory retire <station>" in err, err)
code, out, err, _ = run("retire", [""])
ck("an empty station: the usage line", code == 1 and "usage: tares factory retire <station>" in err, err)
reset({"crew-reviewer": station(role="reviewer", id="cccc0001")}, [fake_claude.agent_row("working", "cccc0001", "crew-reviewer", str(work))])
code, out, err, _ = run("retire", ["reviewer"])
ck("a role word is this crew's station", code == 0 and stations()["crew-reviewer"]["status"] == "retired", err)

# ---------------------------------------------------------------------------------------------
print("down")
DESK = {"desk": [
    {"id": "d1", "question": "Ship it?", "project_finished": True},
    {"id": "d2", "question": "Should we run crew down now?"},
    {"id": "d3", "question": "Please STOP THE CREW"},
    {"id": "d4", "question": "Which colour?"},
    {"id": "d5", "question": None, "project_finished": False},
    {"id": "d6", "question": "Please Crew Down"},
]}
reset({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", status="retired"),
       "crew-reviewer": station(role="reviewer", id="cccc0001")},
      [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work)),
       fake_claude.agent_row("working", "cccc0001", "crew-reviewer", str(work))])
routes = {"GET /api/desk?state=open": (200, DESK)}
code, out, err, t = run("crew down", [], routes)
ck("down retires each active station, in file order",
   code == 0 and [e["station"] for e in events() if e["event"] == "retired"] == ["shop-build-m1", "crew-reviewer"], str(events()))
ck("...but not a retired one", stations()["shop-build-m2"]["status"] == "retired" and ["stop", "aaaa0002"] not in calls())
ck("it ends with the closing line", said(err)[-1] == "all stations stopped. Tares keeps the projects, tickets and the desk; "
   "tares factory crew up starts fresh sessions.", str(said(err)))
w = {r["route"]: json.loads(r["body"])["reason"] for r in t.requests if r["route"].startswith("POST /api/desk/")}
ck("a finished project's question is withdrawn with its reason",
   w.get("POST /api/desk/d1/withdraw") == "the crew stopped and the project is finished", str(w))
ck("'crew down' (case-sensitive) and 'stop the crew' (any case) are withdrawn as the person's",
   w.get("POST /api/desk/d2/withdraw") == "the person stopped the crew" and w.get("POST /api/desk/d3/withdraw") == "the person stopped the crew", str(w))
ck("other questions stay, and so does 'Crew Down' in another case", not {"d4", "d5", "d6"} & {k.split("/")[3] for k in w}, str(w))
ck("the retired stations were told to Tares", len(t.asked("POST /api/crew/shop-build-m1/end?crew=crew")) == 1)
reset({"shop-build-m1": station()}, [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work))])
(CREW / "stations.json").unlink()
code, out, err, t = run("down", [], routes)
ck("no stations.json: says so and withdraws nothing", code == 0 and said(err) == ["no stations recorded"]
   and not [r for r in t.requests if r["route"].startswith("POST /api/desk")], str(said(err)))
# the watch is stopped first
reset({})
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "crew watch"])
(CREW / "watch.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
(CREW / "stations.json").unlink()
code, out, err, t = run("down", [], routes)
try:
    proc.wait(timeout=10)
    gone = True
except subprocess.TimeoutExpired:
    proc.kill()
    gone = False
ck("down stops the watch, removes its pid file and says so", gone and not (CREW / "watch.pid").exists()
   and said(err)[0] == "stopped the watch", str(said(err)))
ck("...before it looks at the stations", said(err)[:2] == ["stopped the watch", "no stations recorded"], str(said(err)))

print("the watch pid")
reset({})
paths = CrewPaths.from_env()
ck("no pid file: no watch", LC.watch_pid(paths) is None)
(CREW / "watch.pid").write_text("not a number\n", encoding="utf-8")
ck("a pid file that is not a number: no watch", LC.watch_pid(paths) is None)
(CREW / "watch.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
ck("a live process that is not a watch: no watch", LC.watch_pid(paths) is None)
lines = []
LC.stop_watch(paths, lines.append)
ck("stop_watch on that: removes the file, stops nothing, says nothing", not (CREW / "watch.pid").exists() and lines == [])
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "crew watch", "--once"])
(CREW / "watch.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
ck("a process whose command line has 'crew watch' is the watch", LC.watch_pid(paths) == proc.pid)
proc.kill()
proc.wait()
(CREW / "watch.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
lines = []
LC.stop_watch(paths, lines.append)
ck("a watch that is already gone is treated as not running", not (CREW / "watch.pid").exists() and lines == [])

# ---------------------------------------------------------------------------------------------
print("what a restart tells a station about its schedule")
reset({})
paths = CrewPaths.from_env()
PLAIN = "re-arm your schedule if you have one"
with FakeTares({"GET /api/crew/settings": (200, {"settings": {"idle_wake": True}})}):
    c = TaresClient(paths)
    c.prime()
    ck("idle_wake on: arm no tick", LC.rearm_text(c).startswith("arm no tick of your own (idle_wake is on: the factory wakes you "
                                                               "when there is work and holds your tick; if you hold one, delete it "
                                                               "with CronList and CronDelete)"), LC.rearm_text(c))
    c.close()
with FakeTares({"GET /api/crew/settings": (200, {"settings": {}})}):
    c = TaresClient(paths)
    c.prime()
    ck("not paced: the plain advice", LC.rearm_text(c) == PLAIN, LC.rearm_text(c))
    real_pace = LC.pace.read
    LC.pace.read = lambda client: LC.pace._read_doc({"on": True, "known": True, "level": 1})
    ck("paced at level 1: 45 minutes", LC.rearm_text(c) == "re-arm your schedule if you have one, at 45 minutes while the factory "
       "paces the crew before the plan's limit", LC.rearm_text(c))
    LC.pace.read = lambda client: LC.pace._read_doc({"on": True, "known": True, "level": 0})
    ck("paced at level 0: the plain advice", LC.rearm_text(c) == PLAIN)
    LC.pace.read = real_pace
    c.close()
with FakeTares({"GET /api/crew/settings": (500, "no")}):
    c = TaresClient(paths)
    ck("settings Tares cannot give mean idle_wake off", LC.rearm_text(c) == PLAIN, LC.rearm_text(c))
    c.close()

# ---------------------------------------------------------------------------------------------
print("revive")
STOPPED = [fake_claude.agent_row("stopped", "aaaa0001", "shop-build-m1", str(work))]
AGAIN = ("Reload the skills factory-protocol and station-builder, re-read your report and your work on Tares, "
         "re-arm your schedule if you have one, then carry on where your report left off.")
reset({"shop-build-m1": station()}, STOPPED)
del launched[:]
code, out, err, t = run("revive", ["shop-build-m1"])
ck("revive of a stopped station launches it again", code == 0 and len(launched) == 1, err)
a = launched[0] if launched else {}
ck("...with its recorded role, parent, project, cwd, model and effort",
   (a.get("role"), a.get("parent"), a.get("project"), a.get("cwd"), a.get("model"), a.get("effort"))
   == ("builder", "crew-orchestrator", "shop", str(work), "opus", "high"), str(a))
ck("...resuming its conversation", a.get("resume") == "sess-1", str(a))
ck("...with the revived prompt", a.get("prompt") == f"You were revived by the factory under the same name. {AGAIN}", a.get("prompt"))
ck("the old session is removed from Claude Code", ["rm", "aaaa0001"] in calls() and "aaaa0001" not in agent_ids(), str(calls()))
earlier = stations()["shop-build-m1"].get("earlier")
ck("the old one is kept as an earlier session", earlier and earlier[0]["id"] == "aaaa0001" and earlier[0]["sessionId"] == "sess-1"
   and earlier[0]["replacedAt"].endswith("Z"), str(earlier))
ck("the event is revived, with the new id", events()[-1]["event"] == "revived" and events()[-1]["detail"] == stations()["shop-build-m1"]["id"]
   and events()[-1]["detail"] != "aaaa0001", str(events()))
ck("revive prints nothing itself", out == "" and said(err) == [], err)

reset({"shop-build-m1": station()}, STOPPED)
del launched[:]
code, out, err, t = run("revive", ["shop-build-m1", "--fresh"])
ck("--fresh starts a new conversation", code == 0 and launched[0]["resume"] == "", str(launched))
ck("...with the fresh prompt", launched[0]["prompt"] == "You were started again under the same name, in a new conversation: the last "
   "one kept refilling its context right after each compaction, so Claude Code stopped it. Nothing from it carries over except your "
   f"report and what is on Tares. {AGAIN} Read big files, logs and diffs in parts.", launched[0]["prompt"])
ck("...and the event is restarted-fresh", events()[-1]["event"] == "restarted-fresh", str(events()))
ck("...the old session is still replaced and kept", ["rm", "aaaa0001"] in calls() and stations()["shop-build-m1"]["earlier"][0]["sessionId"] == "sess-1")

reset({"shop-build-m1": station()}, STOPPED)
del launched[:]
code, out, err, t = run("revive", ["shop-build-m1", "carry on with T2"])
ck("a prompt of its own is used as it is", launched[0]["prompt"] == "carry on with T2" and launched[0]["resume"] == "sess-1", str(launched))
reset({"shop-build-m1": station()}, STOPPED)
del launched[:]
run("revive", ["shop-build-m1", "--fresh", "only this"])
ck("--fresh with a prompt keeps the prompt", launched[0]["prompt"] == "only this" and launched[0]["resume"] == "", str(launched))

reset({"shop-build-m1": station(earlier=[{"id": "old", "sessionId": "s0", "replacedAt": "2026-01-01T00:00:00Z"}])}, STOPPED)
run("revive", ["shop-build-m1"])
ck("a second revive adds to the earlier list", [e["id"] for e in stations()["shop-build-m1"]["earlier"]] == ["old", "aaaa0001"],
   str(stations()["shop-build-m1"].get("earlier")))

reset({"shop-build-m1": station(), "shop-build-m1-fix": station(role="helper", parent="shop-build-m1", id="aaaa0009")},
      [fake_claude.agent_row("stopped", "aaaa0009", "shop-build-m1-fix", str(work))])
del launched[:]
run("revive", ["shop-build-m1-fix"])
ck("a helper reads its parent's manual", "station-builder," in launched[0]["prompt"] and "station-helper" not in launched[0]["prompt"],
   launched[0]["prompt"][:120])

reset({"crew-reviewer": station(role="reviewer", id="cccc0001")}, [fake_claude.agent_row("stopped", "cccc0001", "crew-reviewer", str(work))])
del launched[:]
code, out, err, _ = run("revive", ["reviewer"])
ck("a role word is this crew's station", code == 0 and launched and launched[0]["station"] == "crew-reviewer", err)

print("revive refuses")
reset({"shop-build-m1": station(status="retired")}, STOPPED)
del launched[:]
code, out, err, _ = run("revive", ["shop-build-m1"])
ck("a retired station: spawn starts it again", code == 1 and said(err) == ["shop-build-m1 was retired; tares factory spawn starts it again"]
   and not launched, str(said(err)))
reset({"shop-build-m1": station()}, [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work))])
code, out, err, _ = run("revive", ["shop-build-m1"])
ck("a running station: message it instead", code == 1 and said(err) == ["shop-build-m1 is running; message it instead"] and not launched, str(said(err)))
code, out, err, _ = run("revive", ["nobody"])
ck("an unknown station", code == 1 and said(err) == ["nobody is not a recorded station"], str(said(err)))
code, out, err, _ = run("revive", [])
ck("no station: the usage line", code == 1 and said(err) == ["usage: tares factory revive <station> [--fresh] [prompt]"], str(said(err)))
code, out, err, _ = run("revive", [""])
ck("an empty station: the usage line", code == 1 and said(err) == ["usage: tares factory revive <station> [--fresh] [prompt]"], str(said(err)))
os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
code, out, err, _ = run("revive", ["shop-build-m1"])
os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
ck("a session list that cannot be read: not guessing", code == 1 and "not guessing whether shop-build-m1 runs" in err and not launched, err)
reset({"shop-build-m1": station()}, STOPPED)
with no_claude():
    code, out, err, _ = run("revive", ["shop-build-m1"])
ck("no Claude Code: the plain message", code == 1 and "claude not found on PATH (set CLAUDE_BIN to its path)" in err, err)

# ---------------------------------------------------------------------------------------------
print("restart")
RUNNING = [fake_claude.agent_row("working", "aaaa0001", "shop-build-m1", str(work))]
reset({"shop-build-m1": station()}, RUNNING)
del launched[:]
code, out, err, _ = run("restart", ["shop-build-m1"])
ck("restart stops the session and revives it", code == 0 and ["stop", "aaaa0001"] in calls() and len(launched) == 1, err)
ck("...in the same conversation", launched[0]["resume"] == "sess-1" and launched[0]["prompt"].startswith("You were revived"), str(launched))
ck("...and the event says revived", events()[-1]["event"] == "revived", str(events()))
reset({"shop-build-m1": station()}, RUNNING)
del launched[:]
code, out, err, _ = run("restart", ["shop-build-m1", "--fresh", "start over"])
ck("restart passes --fresh and the prompt on", code == 0 and launched[0]["resume"] == "" and launched[0]["prompt"] == "start over"
   and events()[-1]["event"] == "restarted-fresh", str(launched))
reset({"shop-build-m1": station()}, RUNNING)
del launched[:]
real_time, real_stop = LC.time, LC._stop_session
slept = []
# Swap lifecycle's own `time` name, not time.sleep: subprocess polls with the shared time.sleep too
# (1ms, 2ms ... 50ms), and those calls must not land in the list.
LC.time = types.SimpleNamespace(sleep=slept.append)
LC._stop_session = lambda job_id, binary: None   # a session that ignores the stop
try:
    code, out, err, _ = run("restart", ["shop-build-m1"], FACTORY_STOP_WAIT="2")
finally:
    LC.time, LC._stop_session = real_time, real_stop
ck("a station that will not stop: says how long it waited", code == 1 and said(err) == ["shop-build-m1 did not stop within 2s; try again"]
   and not launched, str(said(err)))
ck("...it looked FACTORY_STOP_WAIT times, a second apart", slept == [1, 1], str(slept))
reset({"shop-build-m1": station()}, RUNNING)
os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
code, out, err, _ = run("restart", ["shop-build-m1"])
os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
ck("a session list that cannot be read: nothing is stopped", code == 1 and said(err) == [
    "could not read Claude Code's session list (claude agents --json); not stopping shop-build-m1 without being able to see it stop"]
   and ["stop", "aaaa0001"] not in calls(), str(said(err)))
code, out, err, _ = run("restart", ["nobody"])
ck("an unknown station", code == 1 and said(err) == ["nobody is not a recorded station"], str(said(err)))
code, out, err, _ = run("restart", [])
ck("no station: the usage line", code == 1 and said(err) == ["usage: tares factory restart <station>"], str(said(err)))
code, out, err, _ = run("restart", [""])
ck("an empty station: the usage line", code == 1 and said(err) == ["usage: tares factory restart <station>"], str(said(err)))
ck("a wait of inf or text looks zero times, as seq does", LC._stop_wait({"FACTORY_STOP_WAIT": "inf"}) == 0
   and LC._stop_wait({"FACTORY_STOP_WAIT": "soon"}) == 0 and LC._stop_wait({}) == 10, "stop wait")
reset({"shop-build-m1": station()}, RUNNING)
real_write = LC.state.write_json
def _no_write(*a, **k):
    raise OSError("disk full")
LC.state.write_json = _no_write
try:
    LC._keep_earlier(CrewPaths.from_env(), "shop-build-m1", "aaaa0001", "sess")
    kept = True
except OSError:
    kept = False
LC.state.write_json = real_write
ck("an unwritable stations.json while keeping the earlier session does not crash", kept, "OSError escaped")
reset({"shop-build-m1": station(status="retired")}, RUNNING)
code, out, err, _ = run("restart", ["shop-build-m1"])
ck("a retired station is stopped, then revive refuses it", code == 1 and "was retired; tares factory spawn starts it again" in err, err)

L.launch = real_launch
print(f"{P} passed, {F} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if F else 0)
