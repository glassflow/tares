"""Moving the stations onto a newer Claude Code in Python (tares/crew/cc_update.py, TR-963).

Each check runs against the Python fake Claude Code (tests/factory/fake_claude.py) in a throwaway crew home, with a
small fake Tares. The call to launch is replaced by a recorder that registers a new session (and gives it the Claude
Code version the check wants), so these checks need no bash and no real Claude Code. The bash and the Python run side
by side in the bash suite tests/factory/claude_update_test.sh and the parity harness.

The spare sessions of Claude Code's pool are faked too: the process list prune_spares reads is replaced by a list of
sleeping child processes of this script, and one check reads the real process list with a child of its own whose
command line says it is a spare of version 0.0.1 (no real Claude Code spare is older than that).

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_cc_update.py
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
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
FACTORY_DIR = HERE / "factory"

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-ccupdate-"))
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
from tares.crew import cc_update as CU, claude, state, views, watch  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0
OLD, NEW = "2.1.100", "2.1.200"
CP = CrewPaths.at(CREW)
WORK = SCRATCH / "work"
WORK.mkdir()


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


BIN = os.environ["CLAUDE_BIN"] = make_launcher(SCRATCH / "bin", FACTORY_DIR / "fake_claude.py")

_real_attached = claude.attached


def _attached(station, paths=None):
    """claude.attached, which scans every process: psutil on macOS now and then raises SystemError for one that is
    just ending (a flake of claude.py, not of what is tested here). Not attached then."""
    try:
        return _real_attached(station, paths)
    except SystemError:
        return False


claude.attached = _attached


class FakeTares:
    """A Tares that answers by route; /health is up unless health=False."""

    def __init__(self, settings=None, health=True):
        self.routes = {"GET /api/crew/settings": (200, {"settings": settings or {}})}
        self.health = health
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                key = f"{self.command} {self.path}"
                if key == "GET /health":
                    status, doc = (200 if outer.health else 503), {"ok": True}
                else:
                    status, doc = outer.routes.get(key, (404, {"detail": "not found"}))
                data = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET

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


def station(**over) -> dict:
    row = {"role": "builder", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
           "cwd": str(WORK), "model": "opus", "effort": "high", "id": "aaaa0001", "sessionId": "sess-1"}
    row.update(over)
    return row


def row(st: str, id: str, name: str, version=OLD, **extra) -> dict:
    """A session row; no version means the version has to be told from the process, which is not there."""
    if version:
        extra["version"] = version
    else:
        extra["pid"] = 3999999
    return fake_claude.agent_row(st, id, name, str(WORK), **extra)


def world(stations: dict, rows, installed=NEW):
    """A fresh crew home with these stations, the fake Claude Code listing these sessions and installed as `installed`."""
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    fake_claude.write_agents(STATE, rows)
    (STATE / "version").write_text(installed + "\n", encoding="utf-8")
    for k in ("FACTORY_CC_TRIES", "FACTORY_CC_BACKOFF", "FAKE_CLAUDE_AGENTS_FAIL"):
        os.environ.pop(k, None)
    CU.reset()
    del launched[:]
    SPAWN[:] = [NEW]
    ON_LAUNCH.clear()


def stations_now() -> dict:
    return json.loads((CREW / "stations.json").read_text(encoding="utf-8"))


def events() -> list:
    try:
        lines = (CREW / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines]


def kinds() -> list:
    return [(e["event"], e["station"], e["detail"]) for e in events() if e["event"] not in ("revived", "restarted-fresh")]


def calls() -> list:
    try:
        lines = (STATE / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x)["args"] for x in lines if x.strip()]


launched: list = []
SPAWN: list = [NEW]      # the version each new session runs, in turn; the last one stays
ON_LAUNCH: list = []     # called with each launch


def fake_launch(client, station_name, role, parent, project, repo, cwd, model, effort, prompt, resume="", **kw):
    """What a restart hands to launch; the 'session' it starts is a new row in the fake Claude Code."""
    n = len(launched) + 2
    new_id = f"bbbb{n:04x}"
    launched.append({"station": station_name, "prompt": prompt, "resume": resume})
    for hook in ON_LAUNCH:
        hook(len(launched))
    state.stations_put(station_name, {"id": new_id, "sessionId": f"sess-{n}", "status": "active"}, client.paths)
    version = SPAWN.pop(0) if len(SPAWN) > 1 else SPAWN[0]
    rows = json.loads((STATE / "agents.json").read_text(encoding="utf-8"))
    rows.append(row("idle", new_id, station_name, version))
    fake_claude.write_agents(STATE, rows)


L.launch = fake_launch
REAL_LISTING = CU._listing
CU._listing = lambda: []   # the real process list holds real spares of Claude Code: never prune those


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


class Said:
    """A client whose messages are kept; the Tares behind it is a FakeTares you hold open yourself."""

    def __init__(self, **kw):
        self.msgs: list = []
        self.client = TaresClient(CP, os.environ, self.msgs.append)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.client.close()


def round_says(fn):
    """Run fn() with stderr kept: (its result, the lines the factory said)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = fn()
    return res, [x[len("tares factory: "):] for x in err.getvalue().splitlines() if x.startswith("tares factory: ")]


def move(target=NEW, s="shop-build-m1", settings=None, health=True):
    """claude_move for s: (exit code, what it said)."""
    with FakeTares(settings, health), Said() as sd:
        rc = CU.claude_move(CP, sd.client, s, target, claude_bin=BIN)
        return rc, sd.msgs, sd


def after_move_prompt(skill="builder") -> str:
    with FakeTares(), Said() as sd:
        from tares.crew import lifecycle
        rearm = lifecycle.rearm_text(sd.client)
    return (f"Claude Code was updated to {NEW}, so the factory restarted you on it, in this same conversation, with "
            f"fresh settings. Reload the skills factory-protocol and station-{skill}, re-read your report, {rearm}, "
            "then carry on where you were.")


def fresh_text(skill="builder") -> str:
    with FakeTares(), Said() as sd:
        from tares.crew import lifecycle
        rearm = lifecycle.rearm_text(sd.client)
    return (f"Claude Code was updated to {NEW}, but your old conversation kept coming back on an older Claude Code, "
            "so the factory started you again under the same name, in a new conversation, on "
            f"{NEW}. Nothing from the old one carries over except your report and what is on Tares. Reload the skills "
            f"factory-protocol and station-{skill}, re-read your report and your work on Tares, {rearm}, then carry "
            "on where your report left off.")


# ---------------------------------------------------------------------------------------------
print("station_cc and cc_behind")
world({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", sessionId="s2"),
       "shop-build-m3": station(id="aaaa0003", sessionId="s3"), "crew-reviewer": station(role="reviewer", id="aaaa0004"),
       "shop-build-m4": station(id="aaaa0005", status="retired")},
      [row("idle", "aaaa0001", "shop-build-m1", OLD), row("idle", "aaaa0002", "shop-build-m2", NEW),
       row("idle", "aaaa0003", "shop-build-m3", None), row("idle", "aaaa0004", "crew-reviewer", "2.1.50"),
       row("idle", "aaaa0005", "shop-build-m4", OLD)])
agents = claude.active_json(BIN)
ck("station_cc reads the version of the session row", CU.station_cc(CP, "shop-build-m1", agents) == OLD)
ck("...also when the list is not handed in", CU.station_cc(CP, "shop-build-m2") == NEW)
ck("a session whose version cannot be told gives empty", CU.station_cc(CP, "shop-build-m3", agents) == "")
ck("a station that has no session gives empty", CU.station_cc(CP, "nobody", agents) == "")
ck("cc_behind counts the older active stations (a retired one and an unknown version do not count)",
   CU.cc_behind(CP, NEW, agents) == 2, str(CU.cc_behind(CP, NEW, agents)))
ck("...also when the list is not handed in", CU.cc_behind(CP, NEW) == 2)
(CREW / "stations" / "crew-reviewer.done").write_text("", encoding="utf-8")
ck("a station with a .done file does not count", CU.cc_behind(CP, NEW, agents) == 1, str(CU.cc_behind(CP, NEW, agents)))
os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
ck("an unreadable session list counts none", CU.cc_behind(CP, NEW) == 0)
os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
ck("views uses the same functions", views.station_cc is CU.station_cc and views.cc_behind is CU.cc_behind)

# the roster, the other reader of station_cc
world({"shop-build-m1": station(cc_failed=NEW, cc_retry_at=int(time.time()) + 3000),
       "shop-build-m2": station(id="aaaa0002", sessionId="s2")},
      [row("idle", "aaaa0001", "shop-build-m1", OLD), row("idle", "aaaa0002", "shop-build-m2", NEW)])
out = io.StringIO()
with FakeTares(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
    views.cmd_roster(["roster"], dict(os.environ))
ck("roster shows the version, flags the older one and its retry",
   f"shop-build-m1\tbuilder\tshop\tdone\t{OLD} (older than {NEW}, retrying in 50 min)\n" in out.getvalue()
   and f"shop-build-m2\tbuilder\tshop\tdone\t{NEW}\n" in out.getvalue(), out.getvalue())
out = io.StringIO()
with FakeTares(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
    views.cmd_status(["status"], dict(os.environ))
ck("status counts the stations behind and the ones retrying",
   f"claude code: 1 station(s) run an older Claude Code than {NEW}" in out.getvalue(), out.getvalue())

# ---------------------------------------------------------------------------------------------
print("duplicate_sessions")
world({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002")},
      [row("idle", "aaaa0001", "shop-build-m1"), row("idle", "aaaa0009", "shop-build-m1"),
       row("idle", "aaaa0002", "shop-build-m2"), row("idle", "aaaa0003", "stranger"), row("idle", "aaaa0004", "stranger")])
ck("two live sessions under a recorded name are named, with their ids",
   CU.duplicate_sessions(CP, BIN) == ["shop-build-m1: aaaa0001, aaaa0009"], str(CU.duplicate_sessions(CP, BIN)))
world({"shop-build-m1": station()}, [row("idle", "aaaa0001", "shop-build-m1"), row("stopped", "aaaa0009", "shop-build-m1")])
ck("a stopped session does not count", CU.duplicate_sessions(CP, BIN) == [])
no_process = row("working", "aaaa0009", "shop-build-m1")   # still listed (a schedule is armed), but no process holds it
no_process.pop("pid", None)
fake_claude.write_agents(STATE, [row("idle", "aaaa0001", "shop-build-m1"), no_process])
ck("a listed session no process holds does not count", CU.duplicate_sessions(CP, BIN) == [],
   str(CU.duplicate_sessions(CP, BIN)))
os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
ck("an unreadable list gives none", CU.duplicate_sessions(CP, BIN) == [])
os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
(CREW / "stations.json").unlink()
ck("no stations file gives none", CU.duplicate_sessions(CP, BIN) == [])

# ---------------------------------------------------------------------------------------------
print("claude_auto")
for label, settings, health, want in (
        ("on when the setting is not there", {}, True, True),
        ("on when it is true", {"claude_update": True}, True, True),
        ("off when it is false", {"claude_update": False}, True, False)):
    with FakeTares(settings, health), Said() as sd:
        got = CU.claude_auto(sd.client)
        ck("claude_auto: " + label, got is want, str(got))
os.environ["TARESD_URL"] = "http://127.0.0.1:9"   # nothing listens there
with Said() as sd:
    ck("claude_auto: off when Tares does not answer", CU.claude_auto(sd.client) is False)
os.environ.pop("TARESD_URL")
with FakeTares({}), Said() as sd:
    sd.client.routes_unused = None
    ck("claude_auto leaves the client's own settings alone", CU.claude_auto(sd.client) and sd.client._settings is None)
with FakeTares() as t, Said() as sd:
    t.routes["GET /api/crew/settings"] = (500, {"detail": "boom"})
    ck("off when the settings cannot be read", CU.claude_auto(sd.client) is False)

# ---------------------------------------------------------------------------------------------
print("tell_newer_claude")
world({"shop-build-m1": station()}, [row("idle", "aaaa0001", "shop-build-m1", OLD)])
with FakeTares(), Said() as sd:
    CU.tell_newer_claude(CP, sd.client, NEW)
    ck("it says so, naming the stations behind and the unfreeze command", sd.msgs == [
        f"Claude Code {NEW} is newer than the one 1 station(s) run; not moving stations while the kit is frozen "
        "(tares factory crew unfreeze lets them follow it)"], str(sd.msgs))
    ck("...writes the version where it remembers it and records an event",
       (CREW / "claude-newer").read_text() == NEW + "\n" and kinds() == [("claude-newer", "", NEW)], str(kinds()))
    CU.tell_newer_claude(CP, sd.client, NEW)
    ck("the same version is said once", len(sd.msgs) == 1 and len(events()) == 1)
    world({"shop-build-m1": station()}, [row("idle", "aaaa0001", "shop-build-m1", OLD)])
    (CREW / "claude-newer").write_text("2.1.150\n")
    CU.tell_newer_claude(CP, sd.client, NEW)
    ck("a newer version is said again", len(sd.msgs) == 2 and (CREW / "claude-newer").read_text() == NEW + "\n")
    world({"shop-build-m1": station()}, [row("idle", "aaaa0001", "shop-build-m1", NEW)])
    (CREW / "claude-newer").write_text(NEW + "\n")
    CU.tell_newer_claude(CP, sd.client, NEW)
    ck("with nothing behind it says nothing and forgets the file", len(sd.msgs) == 2 and not (CREW / "claude-newer").exists())
    CU.tell_newer_claude(CP, sd.client, NEW)
    ck("...and a missing file is fine", len(sd.msgs) == 2)

# ---------------------------------------------------------------------------------------------
print("cc_movable")
world({"shop-build-m1": station(), "shop-build-m2": station(id="aaaa0002", sessionId=""),
       "shop-build-m3": station(id="aaaa0003")},
      [row("idle", "aaaa0001", "shop-build-m1"), row("idle", "aaaa0002", "shop-build-m2"),
       row("working", "aaaa0003", "shop-build-m3")])
ck("an idle station with a conversation, nobody attached, can be moved", CU.cc_movable(CP, "shop-build-m1", NEW, BIN))
ck("one with no conversation to resume cannot", not CU.cc_movable(CP, "shop-build-m2", NEW, BIN))
ck("one working cannot", not CU.cc_movable(CP, "shop-build-m3", NEW, BIN))
with patched(claude, "attached", lambda s, paths=None: True):
    ck("one somebody has open cannot", not CU.cc_movable(CP, "shop-build-m1", NEW, BIN))
with patched(CU, "cc_backoff", lambda *a: True):
    ck("one backing off cannot", not CU.cc_movable(CP, "shop-build-m1", NEW, BIN))
ck("the backoff stub never waits", CU.cc_backoff(CP, "shop-build-m1", NEW) is False)
ck("the delay stub is FACTORY_CC_BACKOFF, 600 without it, with no doubling",
   CU.cc_delay(1, {}) == 600 and CU.cc_delay(5, {"FACTORY_CC_BACKOFF": "45"}) == 45 and CU.cc_delay(2, {"FACTORY_CC_BACKOFF": "x"}) == 600)

# ---------------------------------------------------------------------------------------------
print("cc_pick")
crew = {"crew-orchestrator": station(role="orchestrator", id="aaaa0001", parent=""),
        "shop-build-m1": station(id="aaaa0002"), "crew-reviewer": station(role="reviewer", id="aaaa0003"),
        "crew-reviewer-h1": station(role="helper", id="aaaa0004", parent="crew-reviewer")}
rows_all = [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "shop-build-m1"),
            row("idle", "aaaa0003", "crew-reviewer"), row("idle", "aaaa0004", "crew-reviewer-h1")]
world(crew, rows_all)
ck("a builder or reviewer goes first, in file order", CU.cc_pick(CP, NEW, BIN) == "shop-build-m1", CU.cc_pick(CP, NEW, BIN))
(CREW / "stations" / "shop-build-m1.done").write_text("")
ck("a finished builder is skipped", CU.cc_pick(CP, NEW, BIN) == "crew-reviewer")
fake_claude.write_agents(STATE, [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "shop-build-m1"),
                                 row("working", "aaaa0003", "crew-reviewer"), row("idle", "aaaa0004", "crew-reviewer-h1")])
ck("a station that is busy is passed over; the helper comes before the orchestrator",
   CU.cc_pick(CP, NEW, BIN) == "crew-reviewer-h1", CU.cc_pick(CP, NEW, BIN))
fake_claude.write_agents(STATE, [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "shop-build-m1"),
                                 row("working", "aaaa0003", "crew-reviewer"), row("idle", "aaaa0004", "crew-reviewer-h1", NEW)])
ck("the orchestrator waits while a busy station is still behind", CU.cc_pick(CP, NEW, BIN) == "")
fake_claude.write_agents(STATE, [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "shop-build-m1"),
                                 row("idle", "aaaa0003", "crew-reviewer", NEW), row("idle", "aaaa0004", "crew-reviewer-h1", NEW)])
ck("a finished builder does not hold the orchestrator back: it goes last", CU.cc_pick(CP, NEW, BIN) == "crew-orchestrator")
with patched(CU, "cc_backoff", lambda p, s, t: s == "crew-reviewer"):
    fake_claude.write_agents(STATE, rows_all)
    (CREW / "stations" / "shop-build-m1.done").unlink()
    ck("a station backing off is skipped", CU.cc_pick(CP, NEW, BIN) == "shop-build-m1")
    (CREW / "stations" / "shop-build-m1.done").write_text("")
    ck("...and does not hold the orchestrator back either (the helper still goes first)",
       CU.cc_pick(CP, NEW, BIN) == "crew-reviewer-h1")
    fake_claude.write_agents(STATE, [row("idle", "aaaa0001", "crew-orchestrator"), row("idle", "aaaa0002", "shop-build-m1"),
                                     row("idle", "aaaa0003", "crew-reviewer"), row("idle", "aaaa0004", "crew-reviewer-h1", NEW)])
    ck("...so with only it behind the orchestrator is picked", CU.cc_pick(CP, NEW, BIN) == "crew-orchestrator")
helper_first = {"crew-reviewer-h1": station(role="helper", id="aaaa0004", parent="crew-reviewer"),
                "shop-build-m1": station(id="aaaa0002"), "crew-reviewer": station(role="reviewer", id="aaaa0003")}
world(helper_first, [row("idle", "aaaa0004", "crew-reviewer-h1"), row("idle", "aaaa0002", "shop-build-m1"),
                     row("idle", "aaaa0003", "crew-reviewer", NEW)])
ck("a helper listed first still comes after a builder that is behind", CU.cc_pick(CP, NEW, BIN) == "shop-build-m1",
   CU.cc_pick(CP, NEW, BIN))
(CREW / "stations" / "crew-reviewer-h1.done").write_text("")
(CREW / "stations" / "shop-build-m1.done").write_text("")
ck("a helper with a .done file is still moved (a .done file means finished only for the others)",
   CU.cc_pick(CP, NEW, BIN) == "crew-reviewer-h1", CU.cc_pick(CP, NEW, BIN))
world(crew, [row("idle", "aaaa0001", "crew-orchestrator", NEW), row("idle", "aaaa0002", "shop-build-m1", NEW),
             row("idle", "aaaa0003", "crew-reviewer", NEW), row("idle", "aaaa0004", "crew-reviewer-h1", NEW)])
ck("nothing behind: nothing picked", CU.cc_pick(CP, NEW, BIN) == "")
world(crew, [row("idle", "aaaa0001", "crew-orchestrator")])
ck("a station with no session shows no version, so is not behind", CU.cc_pick(CP, NEW, BIN) == "crew-orchestrator")
world(crew, rows_all)
os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
ck("an unreadable session list picks nothing", CU.cc_pick(CP, NEW, BIN) == "")
os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")

# ---------------------------------------------------------------------------------------------
print("prune_spares")


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def spare_cmd(version: str, sock: Path) -> str:
    return f"/opt/claude/versions/{version}/claude bg-pty-host --x --bg-spare {sock}"


def gone_soon(proc: subprocess.Popen) -> bool:
    try:
        proc.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        return False


procs: list = []
try:
    world({}, [])
    old_sock, claimed_sock, new_sock, race_sock = (SCRATCH / f"{n}.claim.sock" for n in ("old", "claimed", "new", "race"))
    for s in (old_sock, new_sock, race_sock):
        s.write_text("")
    p_old, p_claimed, p_new, p_race, p_other = (sleeper() for _ in range(5))
    procs += [p_old, p_claimed, p_new, p_race, p_other]
    listing = [(p_old.pid, spare_cmd("2.1.100", old_sock)), (p_claimed.pid, spare_cmd("2.1.100", claimed_sock)),
               (p_new.pid, spare_cmd("2.1.200", new_sock)), (p_other.pid, "/usr/bin/vim notes.txt")]
    with patched(CU, "_listing", lambda: listing):
        CU.prune_spares(CP, NEW)
    ck("an older spare nobody has claimed is stopped", gone_soon(p_old),
       str({"socket": old_sock.exists(), "listed": [c for _, c in listing][:1], "events": kinds()}))
    ck("a claimed one (no claim socket any more) is left", p_claimed.poll() is None)
    ck("a spare of the target version is left", p_new.poll() is None)
    ck("another process is left", p_other.poll() is None)
    ck("each pruned spare is an event: no station, the spare's version", kinds() == [("spare-pruned", "", "2.1.100")], str(kinds()))
    # the spare is claimed between the listing and the kill
    real_older = claude.older_than

    def older_then_claimed(v, t):
        res = real_older(v, t)
        if os.path.exists(race_sock):
            os.unlink(race_sock)
        return res

    with patched(CU, "_listing", lambda: [(p_race.pid, spare_cmd("2.1.100", race_sock))]), \
            patched(claude, "older_than", older_then_claimed):
        CU.prune_spares(CP, NEW)
    ck("a spare claimed just before the kill is left", p_race.poll() is None and len(events()) == 1, str(kinds()))
    with patched(CU, "_listing", lambda: [(2 ** 22 + 12345, spare_cmd("2.1.100", old_sock))]):
        CU.prune_spares(CP, NEW)
    ck("a spare that is gone by the kill is skipped, with no event", len(events()) == 1, str(kinds()))
    # a socket path with a space in it (a Windows profile name such as "Jane Doe") is still found
    spaced = SCRATCH / "a b" / "spaced.claim.sock"
    spaced.parent.mkdir(exist_ok=True)
    spaced.write_text("")
    p_spaced = sleeper()
    procs.append(p_spaced)
    with patched(CU, "_listing", lambda: [(p_spaced.pid, spare_cmd("2.1.100", spaced))]):
        CU.prune_spares(CP, NEW)
    ck("an older spare whose socket path holds a space is stopped", gone_soon(p_spaced), str(spare_cmd("2.1.100", spaced)))

    # the real process list, with a child whose command line says it is a spare of version 0.0.1
    shim = SCRATCH / "py"
    try:
        shim.symlink_to(sys.executable)
    except (OSError, NotImplementedError):
        shim = None
    if shim is None:
        print("  skip the real process list: no symlink to run python as a spare")
    else:
        real_sock = SCRATCH / "real.claim.sock"
        real_sock.write_text("")
        p_real = subprocess.Popen([str(shim), "-c", "import time; time.sleep(120)", "bg-pty-host",
                                   "/x/versions/0.0.1/claude", "--bg-spare", str(real_sock)])
        procs.append(p_real)
        time.sleep(0.3)
        if p_real.poll() is not None:   # Windows cannot run a linked python.exe: nothing to find
            print("  skip the real process list: the python shim did not start")
        else:
            with patched(CU, "_listing", REAL_LISTING):
                CU.prune_spares(CP, "0.0.2")
            ck("the real process list: the older spare is found and stopped",
               gone_soon(p_real) and kinds()[-1:] == [("spare-pruned", "", "0.0.1")], str(kinds()))
finally:
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()

# ---------------------------------------------------------------------------------------------
print("claude_move")
one = {"shop-build-m1": station()}
old_row = [row("idle", "aaaa0001", "shop-build-m1", OLD)]
world(one, old_row)
rc, msgs, sd = move()
ck("0: the station runs the new version afterwards", rc == 0, str(rc))
ck("...restarted once, in its conversation, with the prompt", len(launched) == 1 and launched[0]["resume"] == "sess-1"
   and launched[0]["prompt"] == after_move_prompt(), str(launched))
ck("...records claude-updated with the version", kinds()[-1] == ("claude-updated", "shop-build-m1", NEW), str(kinds()))
ck("...says nothing itself", msgs == [], str(msgs))
ck("...and leaves the caller's client alone (it uses its own)", sd.client._settings is None)

world({"shop-build-m1": station(cc_failed=NEW, cc_fails=2, cc_retry_at=99, cc_fresh=NEW)}, old_row)
rc, msgs, _ = move()
st = stations_now()["shop-build-m1"]
ck("a success clears the failure marks", rc == 0 and all(st.get(k) is None for k in ("cc_failed", "cc_fails", "cc_retry_at", "cc_fresh")), str(st))

world(one, old_row)
SPAWN[:] = [None]
rc, msgs, _ = move()
ck("0: a version that cannot be told is taken for the target, and said",
   rc == 0 and msgs == [f"could not tell which Claude Code shop-build-m1 runs; taking it for {NEW}"]
   and kinds()[-1] == ("claude-updated", "shop-build-m1", NEW), str((rc, msgs, kinds())))

world(one, old_row)
SPAWN[:] = [OLD, NEW]
rc, msgs, _ = move()
ck("an old spare comes back once: it is said, an event is recorded, and the second try succeeds",
   rc == 0 and len(launched) == 2 and kinds() == [
       ("stale-spare", "shop-build-m1", OLD), ("claude-updated", "shop-build-m1", NEW)]
   and msgs == [f"shop-build-m1 came back on Claude Code {OLD}, an older spare from Claude Code's pool, not {NEW}; "
                "stopping it and starting it again"], str((rc, msgs, kinds())))

world(one, old_row)
os.environ["FACTORY_CC_TRIES"] = "2"
SPAWN[:] = [OLD]
rc, msgs, _ = move()
st = stations_now()["shop-build-m1"]
ck("5: an old spare every time. Two tries, then one in a new conversation", rc == 5 and len(launched) == 3
   and [x["resume"] for x in launched] == ["sess-1", "sess-2", ""], str((rc, launched)))
ck("...the first tries use the prompt and the last the fresh prompt", launched[0]["prompt"] == after_move_prompt()
   and launched[2]["prompt"] == fresh_text(), str([x["prompt"][:50] for x in launched]))
ck("...saying so before the new conversation", f"shop-build-m1 still comes back on an older Claude Code after 2 tries; "
   f"starting it again in a new conversation on {NEW}" in msgs, str(msgs))
ck("...the station is marked failed for the version, with a count and a retry time 600 s away",
   st["cc_failed"] == NEW and st["cc_fails"] == 1 and st["cc_fresh"] == NEW
   and abs(st["cc_retry_at"] - (time.time() + 600)) < 30, str(st))
ck("...and the events say so", kinds()[-1] == ("claude-retry", "shop-build-m1", NEW)
   and sum(1 for k in kinds() if k[0] == "stale-spare") == 3, str(kinds()))
del launched[:]
os.environ["FACTORY_CC_BACKOFF"] = "30"
rc, msgs, _ = move()
st = stations_now()["shop-build-m1"]
ck("the next round for the same version: no new conversation again, the count goes up, FACTORY_CC_BACKOFF is the delay",
   rc == 5 and len(launched) == 2 and all(x["resume"] != "" for x in launched) and st["cc_fails"] == 2
   and abs(st["cc_retry_at"] - (time.time() + 30)) < 30, str((rc, launched, st)))
os.environ.pop("FACTORY_CC_BACKOFF")

world({"crew-reviewer": station(role="reviewer", id="aaaa0003"),
       "crew-reviewer-h1": station(role="helper", id="aaaa0001", parent="crew-reviewer")},
      [row("idle", "aaaa0001", "crew-reviewer-h1", OLD)])
SPAWN[:] = [OLD]
os.environ["FACTORY_CC_TRIES"] = "2"
rc, msgs, _ = move(s="crew-reviewer-h1")
ck("a helper is never started in a new conversation, and the prompt names its parent's skill",
   rc == 5 and len(launched) == 2 and all(x["resume"] != "" for x in launched)
   and launched[0]["prompt"] == after_move_prompt("reviewer"), str((rc, [x["resume"] for x in launched])))
os.environ.pop("FACTORY_CC_TRIES")

world(one, old_row)
rc, msgs, _ = move(health=False)
ck("3: Tares does not answer", rc == 3 and launched == [], str((rc, launched)))

world(one, old_row)
(CREW / "HALT").write_text("{}")
rc, _, _ = move()
ck("4: the crew is halted", rc == 4 and launched == [])
world(one, [row("working", "aaaa0001", "shop-build-m1", OLD)])
rc, _, _ = move()
ck("4: the station is not idle", rc == 4 and launched == [])
world(one, old_row)
with patched(claude, "attached", lambda s, paths=None: True):
    rc, _, _ = move()
ck("4: someone has it open", rc == 4 and launched == [])
world(one, old_row)
SPAWN[:] = [OLD]
ON_LAUNCH.append(lambda n: (CREW / "HALT").write_text("{}"))
rc, _, _ = move()
ck("4: a halt between the tries stops the retries", rc == 4 and len(launched) == 1, str((rc, len(launched))))
world(one, old_row)
SPAWN[:] = [OLD]
with patched(claude, "attached", lambda s, paths=None: bool(launched)):
    rc, _, _ = move()
ck("4: someone opening it between the tries stops the retries", rc == 4 and len(launched) == 1, str((rc, len(launched))))

world(one, old_row)
SPAWN[:] = [OLD, NEW]
seen = []
with patched(CU, "prune_spares", lambda p, t: seen.append((t, len(launched)))):
    rc, _, _ = move()
ck("older spares are pruned before each restart", rc == 0 and seen == [(NEW, 0), (NEW, 1)], str((rc, seen)))

world(one, old_row)
with patched(CU, "_pace_closed", lambda c, **k: True):
    rc, _, _ = move()
ck("6: the plan's limit is full", rc == 6 and launched == [])
world(one, old_row)
SPAWN[:] = [OLD]
closed = []
ON_LAUNCH.append(lambda n: closed.append(1))
with patched(CU, "_pace_closed", lambda c, **k: bool(closed)):
    rc, _, _ = move()
ck("6: the limit fills between the tries", rc == 6 and len(launched) == 1, str((rc, len(launched))))
from tares.crew import pace as PACE   # noqa: E402
_who = types.SimpleNamespace(paths=CP, env={})
PACE.remember(CP, PACE.Reading("active", 2, True))
ck("_pace_closed: a full limit that has not reset is closed", CU._pace_closed(_who) is True)
PACE.remember(CP, PACE.Reading("active", 1, False))
ck("_pace_closed: a slow-down is not closed", CU._pace_closed(_who) is False)
PACE.remember(CP, PACE.Reading("off"))
ck("_pace_closed: pace off is not closed", CU._pace_closed(_who) is False)
PACE.forget()

world(one, old_row)
ON_LAUNCH.append(lambda n: (_ for _ in ()).throw(CrewError("launch failed (fake)")))
rc, msgs, _ = move()
ck("1: a restart that fails is said and gives 1", rc == 1 and msgs == ["launch failed (fake)"], str((rc, msgs)))
world(one, old_row)
ON_LAUNCH.append(lambda n: (_ for _ in ()).throw(OSError("disk gone")))
rc, msgs, _ = move()
ck("1: so does an OS error", rc == 1 and msgs and "disk gone" in msgs[0], str((rc, msgs)))
world({}, [])
rc, msgs, _ = move()
ck("4: a station that is not recorded is not idle", rc == 4 and launched == [] and not msgs, str((rc, msgs)))

# ---------------------------------------------------------------------------------------------
print("watch_claude_move")


def watch_move(rc, s="shop-build-m1", settings=None, closed=False, dup_rows=None):
    """watch_claude_move with claude_move answering rc: (what it said, whether claude_move ran)."""
    ran = []
    with FakeTares(settings), Said() as sd, patched(CU, "claude_move", lambda *a, **k: ran.append(1) or rc), \
            patched(CU, "_pace_closed", lambda c, **k: closed):
        CU.watch_claude_move(CP, sd.client, s, NEW, claude_bin=BIN)
    return sd.msgs, bool(ran)


START = f"shop-build-m1 runs an older Claude Code than {NEW}; restarting it on {NEW} now that it's idle, in the same conversation"
world(one, old_row)
msgs, ran = watch_move(0)
ck("0: it says what it does and that the station runs the version", ran and msgs == [START, f"shop-build-m1 runs Claude Code {NEW}"], str(msgs))
world(one, [row("idle", "aaaa0001", "shop-build-m1", NEW), row("idle", "aaaa0009", "shop-build-m1", NEW)])
msgs, ran = watch_move(0)
ck("0: two sessions under the name are said and recorded", msgs[-1] == "TWO SESSIONS named shop-build-m1: aaaa0001, aaaa0009; "
   "stop the one stations.json does not list (factory roster)" and kinds() == [
       ("two-sessions", "shop-build-m1", "shop-build-m1: aaaa0001, aaaa0009")], str((msgs, kinds())))
world(one, old_row)
msgs, ran = watch_move(3)
ck("3: Tares does not answer", msgs == [START, f"Tares doesn't answer; shop-build-m1 moves onto Claude Code {NEW} once it does"], str(msgs))
for code in (4, 6):
    msgs, ran = watch_move(code)
    ck(f"{code}: nothing more is said", msgs == [START], str(msgs))
world({"shop-build-m1": station(cc_failed=NEW, cc_retry_at=int(time.time()) + 3000),
       "crew-reviewer-h1": station(role="helper", id="aaaa0002", parent="crew-reviewer", cc_failed=NEW, cc_retry_at=int(time.time()) + 3000)}, old_row)
msgs, ran = watch_move(5)
ck("5: a builder is told when the watch tries again", msgs[-1] == f"shop-build-m1 keeps coming back on an older Claude Code than {NEW} "
   "(an old pre-warmed spare in Claude Code's pool), even in a new conversation; the watch tries again in 50 min, and "
   "tares factory crew update tries again now", msgs[-1])
msgs, ran = watch_move(5, s="crew-reviewer-h1")
ck("5: a helper keeps its conversation", msgs[-1] == f"crew-reviewer-h1 keeps coming back on an older Claude Code than {NEW} "
   "(an old pre-warmed spare in Claude Code's pool); a helper keeps its conversation, since its task is only there; "
   "the watch tries again in 50 min, and tares factory crew update tries again now", msgs[-1])
msgs, ran = watch_move(1)
ck("1: a builder is revived as before", msgs[-1] == f"could not restart shop-build-m1 on Claude Code {NEW}; the watch revives it as "
   "before, and no other station moves until it is up", msgs[-1])
msgs, ran = watch_move(1, s="crew-reviewer-h1")
ck("1: a helper's parent is asked to spawn it again", msgs[-1] == f"could not restart crew-reviewer-h1 on Claude Code {NEW}; the watch does "
   "not revive helpers, so ask crew-reviewer to spawn it again if it is down", msgs[-1])
msgs, ran = watch_move(0, settings={"kit": {"frozen": {"at": "x", "by": "y"}}})
ck("frozen since the round began: nothing is said or moved", msgs == [] and not ran, str((msgs, ran)))
msgs, ran = watch_move(0, closed=True)
ck("the plan's limit filled since the round began: nothing is said or moved", msgs == [] and not ran)

# ---------------------------------------------------------------------------------------------
print("the watch's step")
TWO = {"crew-orchestrator": station(role="orchestrator", id="aaaa0001", parent=""), "shop-build-m1": station(id="aaaa0002")}
TWO_ROWS = [row("idle", "aaaa0001", "crew-orchestrator", OLD), row("idle", "aaaa0002", "shop-build-m1", OLD)]


def step(settings=None, health=True, closed=False):
    """cc_update.step once: what it said, and the stations it handed to watch_claude_move."""
    moved = []
    if not health:
        os.environ["TARESD_URL"] = "http://127.0.0.1:9"   # nothing listens there
    with (FakeTares(settings) if health else contextlib.nullcontext()), patched(CU, "watch_claude_move", lambda p, c, s, t, **k: moved.append((s, t))), \
            patched(CU, "_pace_closed", lambda c, **k: closed):
        _, said = round_says(lambda: CU.step(CP))
    os.environ.pop("TARESD_URL", None)
    return said, moved


world(TWO, TWO_ROWS)
said, moved = step()
ck("a version that has stayed the same moves the station that is picked", moved == [("shop-build-m1", NEW)] and said == [], str((moved, said)))
world(TWO, TWO_ROWS, installed=OLD)
step()
(STATE / "version").write_text(NEW + "\n")
said, moved = step()
ck("a version that changed since the last round waits a round", moved == [], str(moved))
said, moved = step()
ck("...and is moved once it has stayed", moved == [("shop-build-m1", NEW)], str(moved))
world(TWO, TWO_ROWS, installed=OLD)
step()
(STATE / "version").write_text("\n")
said, moved = step()
ck("a version that cannot be read moves nothing", moved == [], str(moved))
(STATE / "version").write_text(OLD + "\n")
said, moved = step()
ck("...and the version that comes back is not new", moved == [])
ck("reset forgets the version seen", (CU.reset(), CU._cc_seen)[1] is None)

world(TWO, TWO_ROWS)
said, moved = step(settings={"claude_update": False})
ck("claude_update off: nothing moves", moved == [])
world(TWO, TWO_ROWS)
said, moved = step(health=False)
ck("Tares not answering counts as off", moved == [])
world(TWO, TWO_ROWS)
(CREW / "HALT").write_text("{}")
said, moved = step()
ck("halted: nothing moves", moved == [])
world(TWO, TWO_ROWS[:1])
said, moved = step()
ck("a station that is down holds the move back", moved == [])
world({**TWO, "crew-reviewer-h1": station(role="helper", id="aaaa0003", parent="crew-reviewer")}, TWO_ROWS)
said, moved = step()
ck("...but a helper that is down does not", moved == [("shop-build-m1", NEW)], str(moved))
world(TWO, TWO_ROWS[:1])
(CREW / "stations" / "shop-build-m1.done").write_text("")
said, moved = step()
ck("...nor a builder that is finished: the orchestrator goes", moved == [("crew-orchestrator", NEW)], str(moved))
world(TWO, TWO_ROWS[:1] + [row("idle", "aaaa0002", "shop-build-m1", NEW)])
(CREW / "stations" / "shop-build-m1.done").write_text("")
said, moved = step()
ck("...the orchestrator goes when the finished builder is the only other station", moved == [("crew-orchestrator", NEW)], str(moved))
world(TWO, TWO_ROWS)
os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
said, moved = step()
os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
ck("an unreadable session list moves nothing", moved == [])

# the Crew page's retry flags follow the marks every settled round with every station up
synced = []
with patched(CU, "cc_retry_sync", lambda p, c: synced.append(1)):
    world(TWO, TWO_ROWS)
    step()
    ck("a settled round with every station up syncs the retry flags", len(synced) == 1, str(synced))
    world(TWO, TWO_ROWS, installed=OLD)
    step()
    (STATE / "version").write_text(NEW + "\n")
    del synced[:]
    step()
    ck("...a round whose version just changed does not", synced == [], str(synced))
    world(TWO, TWO_ROWS[:1])
    step()
    ck("...nor does one with a station down", synced == [], str(synced))
    world(TWO, TWO_ROWS)
    os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
    step()
    os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
    ck("...nor one that cannot read the session list", synced == [], str(synced))
    world({}, [])   # no station to be down: only the unreadable list itself holds the round back
    os.environ["FAKE_CLAUDE_AGENTS_FAIL"] = "1"
    step()
    os.environ.pop("FAKE_CLAUDE_AGENTS_FAIL")
    ck("...even with no station to be down", synced == [], str(synced))
    world({}, [])
    step()
    ck("...while an empty crew with a readable list does", len(synced) == 1, str(synced))

frozen = {"kit": {"frozen": {"at": "2026-01-01", "by": "me"}}}
world(TWO, TWO_ROWS)
said, moved = step(settings=frozen)
ck("frozen: nothing moves, and it says there is a newer Claude Code", moved == [] and said == [
    f"Claude Code {NEW} is newer than the one 2 station(s) run; not moving stations while the kit is frozen "
    "(tares factory crew unfreeze lets them follow it)"], str((moved, said)))
said, moved = step(settings=frozen)
ck("...once for the version", said == [] and moved == [], str(said))
(STATE / "version").write_text("2.1.300\n")
CU.reset()
said, moved = step(settings=frozen)
ck("...and again for a newer one", len(said) == 1 and "Claude Code 2.1.300 is newer" in said[0], str(said))
world(TWO, TWO_ROWS)
(CREW / "claude-newer").write_text("old\n")
said, moved = step()
ck("not frozen: the file that remembers is removed and the station moves", not (CREW / "claude-newer").exists() and len(moved) == 1)
world(TWO, TWO_ROWS)
(CREW / "claude-newer").write_text("old\n")
said, moved = step(closed=True)
ck("the plan's limit full: nothing moves, nothing is said, the file stays", moved == [] and said == []
   and (CREW / "claude-newer").exists())

# the whole step with the move itself: the builder, then the orchestrator
world(TWO, TWO_ROWS)
with FakeTares():
    _, said = round_says(lambda: CU.step(CP))
ck("a round moves one station, the builder", [x["station"] for x in launched] == ["shop-build-m1"]
   and ("claude-updated", "shop-build-m1", NEW) in kinds() and said[0] == START
   and said[-1] == f"shop-build-m1 runs Claude Code {NEW}", str((launched, said)))
launched.clear()
with FakeTares():
    _, said = round_says(lambda: CU.step(CP))
ck("the next round moves the orchestrator", [x["station"] for x in launched] == ["crew-orchestrator"]
   and ("claude-updated", "crew-orchestrator", NEW) in kinds(), str((launched, kinds())))
launched.clear()
with FakeTares():
    _, said = round_says(lambda: CU.step(CP))
ck("then nothing is behind", launched == [] and said == [])

# a round in which the revive step found a station down moves nothing, even when that station is up again
from tares.crew import watch_revive as WR   # noqa: E402
world(TWO, TWO_ROWS)
WR.last = WR.Round()
WR.last.down = True
synced = []
with patched(CU, "cc_retry_sync", lambda p, c: synced.append(1)):
    said, moved = step()
ck("a station revived this round holds every move, and the retry flags", moved == [] and synced == [], str((moved, synced)))
WR.last = WR.Round()
said, moved = step()
ck("...while a round the revive step found nothing down moves one", moved == [("shop-build-m1", NEW)], str(moved))

# claude_auto: a null setting is on
for label, settings in (("null", {"claude_update": None}),):
    with FakeTares(settings), Said() as sd:
        ck(f"claude_auto: a {label} setting is on", CU.claude_auto(sd.client) is True)

# the binary the step resolved reaches the version reads
world(TWO, TWO_ROWS)
ck("cc_behind reads the session list with the binary it is given", CU.cc_behind(CP, NEW, binary=BIN) == 2
   and CU.cc_behind(CP, NEW, binary="/nonexistent/claude") == 0)
ck("station_cc reads the session list with the binary it is given", CU.station_cc(CP, "shop-build-m1", binary=BIN) == OLD
   and CU.station_cc(CP, "shop-build-m1", binary="/nonexistent/claude") == "")

# any error from a restart ends the move with 1, not only the usual ones
world(one, old_row)
ON_LAUNCH.append(lambda n: (_ for _ in ()).throw(ValueError("bad json (fake)")))
rc, msgs, _ = move()
ck("1: any other error from a restart is said and gives 1", rc == 1 and msgs == ["bad json (fake)"], str((rc, msgs)))

# the failure count carries over for the same version only
world({"shop-build-m1": station(cc_failed="2.1.50", cc_fails=3, cc_retry_at=99)}, old_row)
SPAWN[:] = [OLD]
rc, msgs, _ = move()
st = stations_now()["shop-build-m1"]
ck("5: a count left by an older target starts again at 1", rc == 5 and st["cc_failed"] == NEW and st["cc_fails"] == 1, str((rc, st)))

# the version poll goes on past an empty read
world(one, old_row)
SPAWN[:] = [None]
naps = []


def nap(_secs):
    naps.append(1)
    if len(naps) == 2:   # the version shows up on the third look
        rows = json.loads((STATE / "agents.json").read_text(encoding="utf-8"))
        for r in rows:
            if r.get("name") == "shop-build-m1" and "pid" in r:
                r.pop("pid")
                r["version"] = NEW
        fake_claude.write_agents(STATE, rows)


with patched(CU.time, "sleep", nap):
    rc, msgs, _ = move()
ck("0: a version that appears on a later look is the one taken, and nothing is said", rc == 0 and msgs == [] and len(naps) == 2,
   str((rc, msgs, len(naps))))

# watch_claude_move reads the freeze and the pace afresh, not from what the round read
world(one, old_row)
ran, asked = [], []
with FakeTares() as t, Said() as sd, patched(CU, "claude_move", lambda *a, **k: ran.append(1) or 4), \
        patched(CU, "_pace_closed", lambda c, **k: asked.append(k) or False):
    sd.client.crew_settings()   # the round's own copy: not frozen
    t.routes["GET /api/crew/settings"] = (200, {"settings": {"kit": {"frozen": {"at": "x", "by": "y"}}}})
    CU.watch_claude_move(CP, sd.client, "shop-build-m1", NEW, claude_bin=BIN)
ck("watch_claude_move: a freeze since the round read the settings wins", ran == [] and sd.msgs == [], str((ran, sd.msgs)))
world(one, old_row)
with FakeTares(), Said() as sd, patched(CU, "claude_move", lambda *a, **k: 4), patched(CU, "_pace_closed", lambda c, **k: asked.append(k) or False):
    CU.watch_claude_move(CP, sd.client, "shop-build-m1", NEW, claude_bin=BIN)
ck("watch_claude_move: the pace is read again right before the restart", {"fresh": True} in asked, str(asked))

# _pace_closed(fresh=True) reads the pace again; without fresh it takes the round's reading
world(one, old_row)
reads = []
with FakeTares(), Said() as sd, patched(PACE, "read", lambda c: reads.append("read") or PACE.Reading("active", 2, True)), \
        patched(PACE, "current", lambda p, e: reads.append("current") or PACE.Reading("active", 1, False)):
    fresh_closed = CU._pace_closed(sd.client, fresh=True)
    round_closed = CU._pace_closed(sd.client)
ck("_pace_closed: fresh reads the pace again, the default takes the round's reading",
   reads == ["read", "current"] and fresh_closed is True and round_closed is False, str((reads, fresh_closed, round_closed)))

# the binary the step resolved reaches every read after it
world(one, old_row)
SPAWN[:] = [OLD, NEW]
got = []
real_station_cc = CU.station_cc
with patched(CU, "station_cc", lambda paths, s, agents=None, binary=None: got.append(binary) or real_station_cc(paths, s, agents, binary)):
    rc, _, _ = move()
ck("the version poll of a move reads with the binary the move was given", rc == 0 and got and set(got) == {BIN}, str((rc, got)))
world(TWO, TWO_ROWS)
told = []
with patched(CU, "tell_newer_claude", lambda paths, c, v, binary=None: told.append(binary)):
    step(settings=frozen)
ck("step hands the binary it resolved to the frozen-crew notice", told == [BIN], str(told))
world(TWO, TWO_ROWS)
behind = []
with FakeTares(frozen), Said() as sd, patched(CU, "cc_behind", lambda paths, v, agents=None, binary=None: behind.append(binary) or 0):
    CU.tell_newer_claude(CP, sd.client, NEW, BIN)
ck("tell_newer_claude counts the stations behind with the binary it is given", behind == [BIN], str(behind))

# the watch runs the step
ran = []
with patched(CU, "step", lambda paths: ran.append(paths)):
    watch._STEPS["updates"](CP)
ck("the updates step of the watch runs cc_update.step", ran == [CP] and watch._STEPS["updates"] is watch._updates_step)
ck("the stubs are small and named", all(callable(getattr(CU, n)) for n in ("cc_backoff", "cc_delay", "cc_retry_sync", "_pace_closed",
                                                                          "upgrades_frozen")))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
