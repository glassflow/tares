"""Starting a station on Codex (tares/crew/codex.py and the Codex branch of tares/crew/launch.py, TR-1592).

Each case runs `launch.launch` in a throwaway crew home against a fake Tares whose launch route answers with
the real Codex runtime's spec (tests/factory/fake_launch.py), and a fake `codex` (tests/factory/fake_codex.py)
that opens the socket and answers as Codex 0.171.0 does. The real one is run by hand:
tests/factory/codex_probe/probe_launch.py.

 a. the version rule: the tested version and newer ones start, an older one, a pre-release of the tested one
    and a text with no version are refused with the reason;
 b. a start with no `claude` anywhere: the app-server runs in a session of its own, its socket is the owner's
    alone and answers `initialize`; what it was given (arguments, folder, environment), its first prompt handed
    over as a turn, and what is left on disk (the station's record, its settings, the launch copy, the event);
 c. the person's own Codex setup stays out: the station's Codex folder holds a link to their login and nothing
    else of theirs, a config left there is removed, and a station that comes up with something the launch did
    not name (an MCP server, a notify program, another Codex folder, an open socket) is stopped;
 d. the socket: the path is a link to the socket (as Codex makes it on macOS) or the socket itself; a stale one
    does not block a start, a live one does, a file in the way does, a name too long for a socket gets a short
    one, and a crew home too long is refused;
 e. what stops a start before anything runs: an older Codex, no `codex`, no login, a login file where the link
    belongs; and an app-server that ends at once or never opens its socket;
 f. a crew stays on its runtime: a Claude Code crew refuses a Codex launch and a Codex crew a Claude Code one,
    with Tares up and from the kept copy, and the kept copy is not replaced by the refused launch;
 g. Tares down: a Codex station starts again from the kept launch, with the model asked for, in the
    conversation it had; one that cannot be loaded is replaced by a new one, and said;
 h. the plan holds nothing of Claude Code's, and an unreadable ~/.claude/settings.json does not stop it; a
    Claude Code plan is as it was;
 i. through the command: `tares factory spawn helper` in a Codex crew starts the helper on Codex. The commands
    still ask Claude Code whether the station runs before they launch (TR-1593 gives a Codex crew its own
    answer), so this case has the fake Claude Code installed; the cases above call `launch.launch` with none.

Unix only (the app-server listens on a Unix socket): on Windows the test says so and passes.

Run: python tests/test_crew_launch_codex.py
"""
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import redirect_stderr
from pathlib import Path

if sys.platform == "win32":
    print("skipped on Windows: a Codex station listens on a Unix socket\n\n0 passed, 0 failed")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

# A short folder: a socket's path holds about a hundred bytes, and the system's temp folder can be half of that.
SCRATCH = Path(tempfile.mkdtemp(prefix="tcx-", dir="/tmp"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "FAKE_CODEX", "CLAUDE", "CODEX"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
BIN = SCRATCH / "bin"
BIN.mkdir()
HOME = SCRATCH / "home"
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
                  PATH=str(BIN))   # nothing on it but the fake codex: no claude, no tares
(HOME / ".codex").mkdir(parents=True)
LOGIN = HOME / ".codex" / "auth.json"
LOGIN.write_text('{"auth_mode": "chatgpt", "tokens": "fake"}\n')
(HOME / ".codex" / "config.toml").write_text('notify = ["/person/notify-program"]\n[mcp_servers.mine]\ncommand = "x"\n')
(HOME / ".codex" / "skills").mkdir()
(BIN / "codex").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "factory" / "fake_codex.py"}" "$@"\n')
(BIN / "codex").chmod(0o755)
(BIN / "tares").write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -c '
                           '"import sys; sys.argv[0] = \'tares\'; from tares.cli import main; main()" "$@"\n')
(BIN / "tares").chmod(0o755)   # the station's guard is `tares hook codex-guard`, found on its PATH
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import psutil  # noqa: E402

from tares.crew import codex as cx
from tares.crew import codex_station  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew import state  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.codex_wire import Wire, WireError  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
from tares.integrations.builtin.runtime_codex import LEAN_FLAGS  # noqa: E402
import launch_support as LS  # noqa: E402
import parity  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-launch-codex-0123456789"
MODEL, EFFORT = "gpt-5.6-luna", "low"
PROMPT = "Boot as the reviewer station."
UNFILLED = re.compile(r"\{[A-Z_]+\}")
_n = [0]
STARTED: list = []   # every pid a case started, ended at the close


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def crew(world=None, stations=None, home_name="crew"):
    """A fresh crew home and a fake Tares on the Codex runtime (unless `world` says otherwise); returns
    (tares, paths, said, make_client)."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": FAKE_TOKEN, "stations": stations or {}})
    if home_name != "crew":
        (tmp / "crew").rename(tmp / home_name)
    tares = LS.FakeTares({"runtime": "codex", **(world or {})})
    tares.__enter__()
    paths = CrewPaths.at(tmp / home_name)
    said = []

    def make_client():
        c = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}, say=said.append)
        c.prime()
        return c
    return tares, paths, said, make_client


CALLS = SCRATCH / "codex-calls.jsonl"   # every conversation request the fake codex took, from any station


def fake(**switches):
    """Set the fake codex's switches for the next start (and clear the last ones)."""
    for k in [k for k in os.environ if k.startswith("FAKE_CODEX_")]:
        del os.environ[k]
    os.environ.update({f"FAKE_CODEX_{k.upper()}": str(v) for k, v in switches.items()})
    os.environ["FAKE_CODEX_LOG"] = str(CALLS)
    os.environ["FAKE_CODEX_DAEMON"] = str(SCRATCH / "d")   # where the fake keeps its sockets: gone with the scratch folder


def calls(pid=None, method=None) -> list:
    """The conversation requests an app-server took: [(method, params)], in order."""
    try:
        rows = [json.loads(x) for x in CALLS.read_text().splitlines()]
    except OSError:
        return []
    # the read asked of Tares before a prompt (TR-1596) is listed only when asked for: its own cases look at it
    return [(r["method"], r["params"]) for r in rows if (pid is None or r["pid"] == pid)
            and (r["method"] == method if method else r["method"] != "mcpServer/tool/call")]


def start(client, station="crew-reviewer", role="reviewer", cwd=None, model=MODEL, prompt=PROMPT, resume="", **kw):
    """(error text or None, what the start wrote on stderr)."""
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            L.launch(client, station, role, "", "", "", cwd or str(client.paths.home), model, EFFORT, prompt, resume, **kw)
    except CrewError as e:
        return str(e), err.getvalue()
    finally:
        pid = (state.stations_load(client.paths).get(station) or {}).get("codex", {}).get("pid")
        if pid and pid not in STARTED:
            STARTED.append(pid)
    return None, err.getvalue()


def row(paths, station="crew-reviewer"):
    return state.stations_load(paths).get(station) or {}


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def gone(pid, wait=5.0):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not alive(pid):
            return True
        try:
            os.waitpid(pid, os.WNOHANG)   # our own child: collect it, so it does not stay a zombie
        except OSError:
            pass
        time.sleep(0.05)
    return not alive(pid)


def end(pid, sig=signal.SIGTERM):
    try:
        os.kill(pid, sig)
    except OSError:
        pass
    return gone(pid)


def refused(text, *words):
    return text is not None and all(w in text for w in words)


# ---------------------------------------------------------------------------------------------
print("a. the version rule")
notes = []
ck("the tested version starts", cx.check_version("codex-cli 0.171.0", notes.append) == "0.171.0" and not notes, notes)
ck("a newer one starts, with a line", cx.check_version("codex-cli 0.172.3", notes.append) == "0.172.3"
   and notes == ["Codex 0.172.3 is newer than 0.171.0, the version stations were tested on"], notes)
ck("a next major starts", cx.check_version("codex-cli 1.0.0") == "1.0.0")
for old in ("codex-cli 0.170.9", "codex-cli 0.167.0-alpha.4.1", "codex-cli 0.171.0-alpha.2"):
    try:
        cx.check_version(old)
        got = None
    except CrewError as e:
        got = str(e)
    ck(f"{old.split()[1]} is refused as older, with the reason",
       refused(got, old.split()[1], "older than 0.171.0", "experimental", "Update Codex"), got)
for bad in ("", "codex: command not found", "codex-cli"):
    try:
        cx.check_version(bad)
        got = None
    except CrewError as e:
        got = str(e)
    ck(f"a text with no version ({bad!r}) is refused", refused(got, "cannot tell the version", "0.171.0 or newer"), got)

# ---------------------------------------------------------------------------------------------
print("b. a start with no claude anywhere")
tares, paths, said, mk = crew()
record = SCRATCH / "record-b.json"
fake(record=record)
os.environ["TARES_FACTORY_SOURCE"] = "the-person's-key-source"   # a variable no station may inherit
ck("there is no claude on this PATH and CLAUDE_BIN is unset",
   shutil.which("claude") is None and "CLAUDE_BIN" not in os.environ, shutil.which("claude"))
err, out = start(mk(), prompt=PROMPT + "\n")
os.environ.pop("TARES_FACTORY_SOURCE")
ck("the start succeeds", err is None, err)
r = row(paths)
info = r.get("codex") or {}
sock = paths.station_file("crew-reviewer", "sock")
home = paths.station_file("crew-reviewer", "codex")
ck("the socket is <crew home>/stations/<station>.sock", info.get("socket") == str(sock) and sock.exists(), info)
ck("it leads to a socket the owner alone can reach (a link to it, as Codex makes on macOS)",
   sock.is_symlink() and stat.S_ISSOCK(os.stat(sock).st_mode) and stat.S_IMODE(os.stat(sock).st_mode) == 0o600
   and os.stat(sock).st_uid == os.getuid(), oct(os.stat(sock).st_mode))
try:
    with Wire(str(sock)) as w:
        hello = w.initialize()
except WireError as e:
    hello = {"error": str(e)}
ck("initialize answers on it, from the station's own Codex folder",
   os.path.realpath(hello.get("codexHome", "")) == os.path.realpath(home), hello)
rec = json.loads(record.read_text()) if record.exists() else {}
ck("codex was run as the spec says: the app-server on the socket, the account's extras off",
   rec.get("argv") == ["app-server", "--listen", f"unix://{sock}", *LEAN_FLAGS], rec.get("argv"))
ck("in the station's folder, with no terminal, in a session of its own",
   os.path.realpath(rec.get("cwd", "")) == os.path.realpath(paths.home) and rec.get("stdin_tty") is False
   and rec.get("sid") == rec.get("pid") == info.get("pid") and rec.get("sid") != os.getsid(0), rec)
env = rec.get("env") or {}
ck("the station's variables reach the app-server",
   env.get("FACTORY_STATION") == "crew-reviewer" and env.get("FACTORY_ROLE") == "reviewer"
   and env.get("FACTORY_CREW") == "crew" and env.get("FACTORY_HOME") == str(paths.home)
   and env.get("TARESD_URL") == tares.url and env.get("TARES_FACTORY_TOKEN_FILE") == f"{paths.home}/.tares-token"
   and env.get("CODEX_HOME") == str(home), env)
ck("no key travels: the token variable is blank and the person's key source is not inherited",
   env.get("TARES_AUTH_TOKEN") == "" and "TARES_FACTORY_SOURCE" not in env
   and FAKE_TOKEN not in json.dumps(rec), sorted(env))
ck("the record: runtime codex, the process and when it was born, the version, active",
   r.get("runtime") == "codex" and r.get("status") == "active" and r.get("role") == "reviewer"
   and r.get("model") == MODEL and r.get("effort") == EFFORT and r.get("id") == "" and info.get("launched")
   and info.get("version") == "0.171.0" and alive(info.get("pid", 0))
   and abs((info.get("born") or 0) - psutil.Process(info["pid"]).create_time()) < 0.01
   and info.get("home") == str(home) and r.get("startedAt") and r.get("kit") and r.get("plugin"), r)
settings_file = paths.station_file("crew-reviewer", "settings.json")
doc = json.loads(settings_file.read_text())
hook = doc["thread"]["config"]["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
ck("the settings file holds the thread's settings, every placeholder filled, readable by the owner alone",
   doc["thread"]["model"] == MODEL and doc["effort"] == EFFORT and doc["thread"]["approvalPolicy"] == "on-request"
   and doc["thread"]["sandbox"] == "workspace-write" and doc["thread"]["config"]["bypass_hook_trust"] is True
   and doc["thread"]["config"]["sandbox_workspace_write"]["writable_roots"] == [f"{paths.raw}/reports"]
   and hook.startswith("tares hook codex-guard --plugin-root ")
   and doc["thread"]["config"]["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == hook and not UNFILLED.search(json.dumps(doc)) and doc["env"]["FACTORY_STATION"] == "crew-reviewer"
   and stat.S_IMODE(settings_file.stat().st_mode) == 0o600 and r.get("settings", "").endswith("crew-reviewer.settings.json"),
   json.dumps(doc)[:500])
took = calls(info.get("pid"))
ck("the first prompt is handed over as a turn on a new conversation, with the thread's settings and the turn's effort",
   [m for m, _ in took] == ["thread/start", "turn/start"]
   and took[0][1] == {**doc["thread"], "cwd": str(paths.home),   # and the folder marked not trusted (TR-1594)
                      "config": {**doc["thread"]["config"], "projects": cx.not_trusted(str(paths.home)),
                                 "mcp_servers": doc["mcp_servers"]}}   # and its MCP servers (TR-1596)
   and took[1][1]["input"] == [{"type": "text", "text": PROMPT + "\n"}] and took[1][1]["effort"] == EFFORT
   and r.get("sessionId", "").startswith("thr-") and took[1][1]["threadId"] == r["sessionId"]
   and info.get("turn", "").startswith("turn-"), (took, r.get("sessionId")))
ck("the start says what it did", "started crew-reviewer on Codex 0.171.0 (gpt-5.6-luna, low)" in out
   and "note:" not in out, out)
kept = json.loads(paths.launch_file("crew-reviewer").read_text())
ck("the launch is kept as the cell gave it, and names its runtime", kept.get("runtime") == "codex"
   and kept["command"][2] == "unix://{STATION_SOCKET}" and kept["env"]["CODEX_HOME"] == "{CODEX_HOME}", kept.get("command"))
events = [json.loads(x) for x in paths.events.read_text().splitlines()]
ck("the start is in the crew's events", any(e["event"] == "started" and e["station"] == "crew-reviewer"
                                             and e["detail"] == f"codex 0.171.0, pid {info['pid']}" for e in events), events[-1:])
log = paths.station_file("crew-reviewer", "codex.log")
ck("the app-server's output goes to a log of the owner's", log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o600)

# ---------------------------------------------------------------------------------------------
print("c. the person's own Codex setup stays out")
ck("the station's Codex folder is the owner's alone", stat.S_IMODE(home.stat().st_mode) == 0o700, oct(home.stat().st_mode))
ck("of the person's it holds a link to their login, and nothing else",
   sorted(e for e in os.listdir(home) if e != "fake-threads.json") == ["auth.json", "config.toml"]
   and (home / "auth.json").is_symlink()
   and os.path.realpath(home / "auth.json") == os.path.realpath(LOGIN), sorted(os.listdir(home)))
marks = tomllib.loads((home / "config.toml").read_text())
ck("its own config says one thing: the folder it works in is not trusted, so a repository's own Codex setup is not read",
   list(marks) == ["projects"] and str(paths.home) in marks["projects"]
   and all(v == {"trust_level": "untrusted"} for v in marks["projects"].values())
   and stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600, marks)
ck("the person's own files are as they were", LOGIN.read_text().startswith('{"auth_mode"')
   and "notify" in (HOME / ".codex" / "config.toml").read_text() and not LOGIN.is_symlink())
pid_b = info["pid"]
ck("ended, the app-server takes its socket with it", end(pid_b) and not sock.exists())
(home / "config.toml").write_text('notify = ["/left/behind"]\n')
(home / "sessions").mkdir()
fake()
err, _ = start(mk())
ck("a config left in the station's folder is written anew at the next start; its conversations stay",
   err is None and tomllib.loads((home / "config.toml").read_text()) == marks and (home / "sessions").is_dir(), err)
end(row(paths)["codex"]["pid"])

for label, switches, words in (
        ("an MCP server the launch does not name", {"mcp": "slack"}, ("loaded an MCP server its launch does not name (slack)",)),
        ("a notify program", {"notify": 1}, ("notify program",)),
        ("another Codex folder", {"home": str(HOME / ".codex")}, ("came up on the Codex folder", "not on its own")),
        ("a socket open to others", {"mode": "666"}, ("open to others (mode 666)",))):
    tares_c, paths_c, _, mk_c = crew()
    rec_c = SCRATCH / f"record-c{_n[0]}.json"
    fake(record=rec_c, **switches)
    err, _ = start(mk_c())
    pid_c = json.loads(rec_c.read_text())["pid"]
    ck(f"a station that comes up with {label} is refused, stopped and not recorded",
       refused(err, "crew-reviewer", *words) and gone(pid_c) and not paths_c.station_file("crew-reviewer", "sock").exists()
       and not row(paths_c), (err, alive(pid_c), row(paths_c)))
tares_c, paths_c, _, mk_c = crew()
plan = L.launch_plan(mk_c(), "crew-reviewer", "reviewer", "", "", "", MODEL, EFFORT)
rec_c = SCRATCH / "record-c-apps.json"
fake(record=rec_c)
plan["args"] = [a for a in plan["args"] if a not in ("--disable", "apps")] + ["--disable", "plugins"]
try:
    with redirect_stderr(io.StringIO()):
        cx.start(mk_c(), "crew-reviewer", plan, str(paths_c.home), PROMPT, {"role": "reviewer"})
    err = None
except CrewError as e:
    err = str(e)
ck("without the flag that turns the account's connectors off, the station is stopped: the rule is checked, not trusted",
   refused(err, "does not name (codex_apps)") and gone(json.loads(rec_c.read_text())["pid"]), err)

# ---------------------------------------------------------------------------------------------
print("d. the socket")
tares, paths, said, mk = crew()
sock = paths.station_file("crew-reviewer", "sock")
fake()
err, _ = start(mk())
first = row(paths)["codex"]["pid"]
err2, _ = start(mk())
ck("a second start while the station runs is refused, and the first is left alone",
   err is None and refused(err2, "crew-reviewer is running", str(sock)) and alive(first)
   and row(paths)["codex"]["pid"] == first, (err, err2))
target = os.path.realpath(sock)
ck("killed, the app-server leaves its link and its socket behind",
   end(first, signal.SIGKILL) and sock.is_symlink() and os.path.exists(target))
err, _ = start(mk())
second = row(paths).get("codex", {}).get("pid")
ck("the stale socket does not block the next start", err is None and second and second != first and alive(second), err)
try:
    with Wire(str(sock)) as w:
        ok = bool(w.initialize().get("codexHome"))
except WireError as e:
    ok = False
ck("and the new app-server answers on it", ok)
end(second)
os.symlink(SCRATCH / "socket-that-is-gone", sock)
err, _ = start(mk())
ck("a link that outlived its socket does not block a start either", err is None and alive(row(paths)["codex"]["pid"]), err)
end(row(paths)["codex"]["pid"])
(SCRATCH / "a-file").write_text("x")
os.symlink(SCRATCH / "a-file", sock)
err, _ = start(mk())
ck("a link to something that is not a socket is refused, and left as it is",
   refused(err, "is not a socket") and os.readlink(sock) == str(SCRATCH / "a-file"), err)
sock.unlink()
fake(plain=1)
err, _ = start(mk())
plain_pid = row(paths).get("codex", {}).get("pid", 0)
ck("a Codex that makes the socket at the path itself, with no link, starts the same",
   err is None and stat.S_ISSOCK(os.lstat(sock).st_mode) and alive(plain_pid), err)
ck("killed, it leaves the socket, and the next start is not blocked by it", end(plain_pid, signal.SIGKILL) and sock.exists()
   and start(mk())[0] is None and row(paths)["codex"]["pid"] != plain_pid)
end(row(paths)["codex"]["pid"])
fake()
sock.write_text("not a socket\n")
err, _ = start(mk())
ck("a file in the way that is not a socket is refused, and left as it is",
   refused(err, "is not a socket", "move it away") and sock.read_text() == "not a socket\n", err)
sock.unlink()

long_name = "crew-build-" + "x" * 90
plain = paths.station_file(long_name, "sock")
short = cx.socket_path(paths, long_name)
ck("a station name too long for a socket's path gets a short name in the same folder, the same each time",
   len(os.fsencode(str(plain))) > cx.SOCKET_MAX and short.parent == paths.stations_dir and len(short.name) == 21
   and short == cx.socket_path(paths, long_name) and short != cx.socket_path(paths, long_name + "y")
   and len(os.fsencode(str(short))) <= cx.SOCKET_MAX, short)
err, _ = start(mk(), station=long_name, role="builder")
ck("and the station starts on it", err is None and row(paths, long_name)["codex"]["socket"] == str(short) and short.exists(), err)
far = SCRATCH / ("d" * 150)
far.mkdir()
os.symlink(os.path.realpath(short), far / "s.sock")
try:
    with Wire(str(far / "s.sock")) as w:
        ok = bool(w.initialize().get("codexHome"))
except WireError as e:
    ok = str(e)
ck("a path too long for a socket's address still connects when it is a link to the socket", ok is True, ok)
end(row(paths, long_name).get("codex", {}).get("pid", 0))
tares_d, paths_d, _, mk_d = crew(home_name="c" * 90)
err, _ = start(mk_d())
ck("a crew home too long for any socket is refused with the lengths",
   refused(err, "bytes long", f"holds {cx.SOCKET_MAX} at most", "shorter path") and not row(paths_d), err)

# ---------------------------------------------------------------------------------------------
print("e. what stops a start")
tares, paths, said, mk = crew()
record = SCRATCH / "record-e.json"
fake(version="0.170.2", record=record)
err, _ = start(mk())
ck("an older Codex is refused with the reason, and nothing is started or recorded",
   refused(err, "Codex 0.170.2 is older than 0.171.0", "Update Codex") and not record.exists() and not row(paths)
   and not paths.station_file("crew-reviewer", "sock").exists(), err)
fake(version="0.180.0")
err, out = start(mk())
ck("a newer Codex starts, with a line saying it was not tested",
   err is None and "note: Codex 0.180.0 is newer than 0.171.0, the version stations were tested on" in out
   and row(paths)["codex"]["version"] == "0.180.0", (err, out))
end(row(paths)["codex"]["pid"])

tares, paths, said, mk = crew()
fake()
os.environ["PATH"] = str(SCRATCH / "empty")
err, _ = start(mk())
os.environ["PATH"] = str(BIN)
ck("no codex on PATH is said by name", err == "codex not found on PATH (set CODEX_BIN to its path)", err)
os.environ["CODEX_BIN"] = str(BIN / "codex")
(SCRATCH / "empty").mkdir(exist_ok=True)   # empty of codex; the station still finds its guard, `tares`, there
shutil.copy2(BIN / "tares", SCRATCH / "empty" / "tares")
os.environ["PATH"] = str(SCRATCH / "empty")
err, _ = start(mk())
os.environ["PATH"] = str(BIN)
del os.environ["CODEX_BIN"]
ck("CODEX_BIN names it when PATH does not", err is None, err)
end(row(paths)["codex"]["pid"])

tares, paths, said, mk = crew()
LOGIN.rename(LOGIN.with_suffix(".away"))
err, _ = start(mk())
LOGIN.with_suffix(".away").rename(LOGIN)
ck("no login of the person's stops the start, with what to do",
   refused(err, "no Codex login at", "codex login") and not row(paths), err)
home = paths.station_file("crew-reviewer", "codex")
home.mkdir(parents=True, exist_ok=True)
(home / "auth.json").write_text("{}\n")
err, _ = start(mk())
ck("a login file of the station's own, where the link belongs, stops the start and is left as it is",
   refused(err, "is a file of its own", "compare the two") and (home / "auth.json").read_text() == "{}\n", err)
(home / "auth.json").unlink()
os.symlink(SCRATCH / "elsewhere.json", home / "auth.json")
err, _ = start(mk())
ck("a link that points somewhere else is set right", err is None and os.path.realpath(home / "auth.json") == os.path.realpath(LOGIN), err)
first = row(paths)["codex"]["pid"]
end(first)
os.environ["CODEX_HOME"] = str(home)   # as when a station starts another: its own folder is CODEX_HOME
tares2, paths2, _, mk2 = crew()
err, _ = start(mk2())
del os.environ["CODEX_HOME"]
home2 = paths2.station_file("crew-reviewer", "codex")
ck("started by a station, a station's login still links to the person's file, not to the other station's link",
   err is None and os.readlink(home2 / "auth.json") == os.path.realpath(LOGIN), err)
end(row(paths2).get("codex", {}).get("pid", 0))

tares, paths, said, mk = crew()
fake(die=1)
err, out = start(mk())
ck("an app-server that ends at once is said with its exit code and its last lines",
   refused(err, "could not start crew-reviewer", "ended at once (exit 3)") and "told to end at once" in out and not row(paths), (err, out))
record = SCRATCH / "record-e-silent.json"
fake(silent=1, record=record)
cx.READY_TIMEOUT = 1
err, _ = start(mk())
cx.READY_TIMEOUT = 30
ck("one that never opens its socket is stopped after the wait",
   refused(err, "did not open", "in 1 s") and gone(json.loads(record.read_text())["pid"]) and not row(paths), err)
fake(late=1.5)
err, _ = start(mk())
ck("a path that is there before the app-server listens on it is waited for, not taken for a failure",
   err is None and alive(row(paths).get("codex", {}).get("pid", 0)), err)
end(row(paths).get("codex", {}).get("pid", 0))
paths.stations.write_text("{}\n")
fake(late=30)
cx.READY_TIMEOUT = 1
record = SCRATCH / "record-e-late.json"
os.environ["FAKE_CODEX_RECORD"] = str(record)
err, _ = start(mk())
cx.READY_TIMEOUT = 30
ck("one that never listens is stopped after the wait",
   refused(err, "did not answer as one") and gone(json.loads(record.read_text())["pid"]) and not row(paths), err)
record = SCRATCH / "record-e-refuse.json"
fake(refuse="model is not available", record=record)
err, _ = start(mk())
ck("a station that will not take its first prompt is stopped, said with the reason, and not recorded",
   refused(err, "could not hand crew-reviewer its prompt", "model is not available")
   and gone(json.loads(record.read_text())["pid"]) and not row(paths), (err, row(paths)))
fake()
err, _ = start(mk(), cwd=str(SCRATCH / "no-such-folder"))
ck("a folder that does not exist stops the start", refused(err, "could not start crew-reviewer") and not row(paths), err)

# ---------------------------------------------------------------------------------------------
print("f. a crew stays on its runtime")
claude_row = {"crew-orchestrator": {"role": "orchestrator", "id": "abcd1234", "sessionId": "s-1", "status": "active"}}
tares, paths, said, mk = crew(stations=claude_row)
paths.launch_dir.mkdir(exist_ok=True)
claude_copy = {"runtime": "claude_code", "command": ["--bg"], "env": {}, "settings": {}}
state.write_json(paths.launch_file("crew-reviewer"), claude_copy)
fake()
err, _ = start(mk())
ck("a crew recorded on Claude Code refuses a Codex launch, by name",
   refused(err, "this crew runs on Claude Code", "a launch for Codex", "never moved to another runtime") and not row(paths), err)
ck("the launch kept from before is not replaced by the refused one",
   json.loads(paths.launch_file("crew-reviewer").read_text()) == claude_copy)
ck("a crew with no station recorded yet has no runtime", L.crew_runtime(CrewPaths.at(SCRATCH / "nowhere")) == "")
codex_row = {"crew-orchestrator": {"role": "orchestrator", "runtime": "codex", "status": "active"}}
tares, paths, said, mk = crew(world={"runtime": "claude_code"}, stations=codex_row)
try:
    L.launch_plan(mk(), "crew-reviewer", "reviewer", "", "", "", "sonnet", "high")
    err = None
except CrewError as e:
    err = str(e)
ck("a crew recorded on Codex refuses a Claude Code launch",
   refused(err, "this crew runs on Codex", "a launch for Claude Code") and not paths.launch_file("crew-reviewer").exists(), err)
tares, paths, said, mk = crew(world={"health": False}, stations=codex_row)
paths.launch_dir.mkdir(exist_ok=True)
state.write_json(paths.launch_file("crew-reviewer"), claude_copy)
try:
    L.launch_plan(TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}, say=said.append), "crew-reviewer", "reviewer", "", "", "", "sonnet", "high")
    err = None
except CrewError as e:
    err = str(e)
ck("with Tares down, a kept launch of the other runtime is refused too", refused(err, "this crew runs on Codex"), err)

# ---------------------------------------------------------------------------------------------
print("g. Tares down: a Codex station starts again from the kept launch")
tares, paths, said, mk = crew()
fake()
err, _ = start(mk())
first = row(paths)["codex"]["pid"]
thread = row(paths)["sessionId"]
time.sleep(0.4)   # the first turn ends
end(first)
tares.world["health"] = False
down = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}, say=said.append)
err, _ = start(down, model="gpt-5.6", resume=thread)
r = row(paths)
doc = json.loads(paths.station_file("crew-reviewer", "settings.json").read_text())
ck("it starts from the copy, and says so", err is None and any("from the launch kept from its last start" in s for s in said), (err, said))
ck("with the model this start asks for, in the thread's settings", doc["thread"]["model"] == "gpt-5.6" and r["model"] == "gpt-5.6", doc["thread"])
took = calls(r["codex"]["pid"])
ck("the conversation it had is loaded again and carried on, not replaced",
   r.get("sessionId") == thread and r["codex"]["pid"] != first and [m for m, _ in took] == ["thread/resume", "turn/start"]
   and took[0][1]["threadId"] == thread and took[0][1]["model"] == "gpt-5.6", (r.get("sessionId"), took))
end(r["codex"]["pid"])
before = len(calls())
err, out = start(down, resume="thr-unknown")
ck("a conversation that cannot be loaded, whose last turn nobody compared with the guard's record, is not given up (TR-1593)",
   err and "cannot be loaded" in err and "never compared with the guard's record" in err
   and [m for m, _ in calls()[before:]] == ["thread/resume"] and "codex" not in (row(paths) or {}).get("status", "codex")
   and (row(paths).get(codex_station.UNVERIFIED) or {}).get("thread") == "thr-unknown", (err, calls()[before:], row(paths)))
# the watch's look at the ended turn would have written this; then nothing of the conversation is unseen
codex_station._clear_unverified(paths, "crew-reviewer")
state.stations_put("crew-reviewer", {codex_station.COMPARED: row(paths)[codex_station.LAST_TURN]}, paths)
err, out = start(down, resume="thr-unknown")
r = row(paths)
took = calls(r["codex"]["pid"])
ck("one whose last turn was compared is given up for a new one, with a line and an event",
   err is None and [m for m, _ in took] == ["thread/resume", "thread/start", "turn/start"] and r["sessionId"] not in ("thr-unknown", thread)
   and "could not be carried on; it starts a new one" in out
   and any(json.loads(x)["event"] == "conversation-replaced" for x in paths.events.read_text().splitlines()), (err, took, out))
end(r["codex"]["pid"])

# ---------------------------------------------------------------------------------------------
print("h. the plan holds nothing of Claude Code's")
tares, paths, said, mk = crew()
(HOME / ".claude").mkdir(exist_ok=True)
(HOME / ".claude" / "settings.json").write_text("{ not json")
try:
    plan = L.launch_plan(mk(), "crew-reviewer", "reviewer", "", "", "", MODEL, EFFORT)
    err = None
except CrewError as e:
    plan, err = {}, str(e)
ck("an unreadable ~/.claude/settings.json does not stop a Codex plan", err is None, err)
ck("the plan names its runtime and has the shape a Claude Code plan has",
   plan.get("runtime") == "codex" and set(plan) == {"runtime", "args", "env", "settings", "otel", "otel_file", "mcp"}
   and plan["otel"] == plan["otel_file"] == plan["mcp"] == {}, sorted(plan))
ck("no flag, setting or file of Claude Code's is in it",
   not any(a.startswith(("--setting", "--no-chrome", "--mcp-config", "--plugin-dir", "--bg")) for a in plan.get("args", []))
   and not {"permissions", "autoMode", "enabledPlugins", "disableClaudeAiConnectors"} & set(plan.get("settings", {})), plan.get("args"))
ck("every placeholder is filled", not UNFILLED.search(json.dumps(plan))
   and plan["env"]["CODEX_HOME"] == str(paths.station_file("crew-reviewer", "codex")), plan.get("env"))
(HOME / ".claude" / "settings.json").unlink()
tares, paths, said, mk = crew(world={"runtime": "claude_code"})
plan = L.launch_plan(mk(), "crew-reviewer", "reviewer", "", "", "", "sonnet", "high")
ck("a Claude Code plan is as it was: no runtime key, its own flags",
   set(plan) == {"args", "env", "settings", "otel", "otel_file", "mcp"} and plan["args"][0] == "--bg"
   and "--setting-sources" in plan["args"] and "CODEX_HOME" not in plan["env"], sorted(plan))

# ---------------------------------------------------------------------------------------------
print("i. through the command: `tares factory spawn` starts a helper of a Codex crew on Codex")
tmp_i = SCRATCH / "i"
tmp_i.mkdir()
env_i = parity.crew_env(tmp_i)   # a throwaway crew of its own, with the fake Claude Code the command still asks first
(tmp_i / "home" / ".codex").mkdir(parents=True)
(tmp_i / "home" / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": "fake"}\n')
record = tmp_i / "record.json"
env_i.update(CODEX_BIN=str(BIN / "codex"), FAKE_CODEX_RECORD=str(record), FAKE_CODEX_LOG=str(CALLS), FAKE_CODEX_DAEMON=str(SCRATCH / "d"), LAUNCH_WORLD=json.dumps({"runtime": "codex"}),
             LAUNCH_SETUP=json.dumps({"token": FAKE_TOKEN, "stations": {"crew-reviewer": {
                 "role": "reviewer", "runtime": "codex", "cwd": str(tmp_i / "crew"), "model": MODEL, "effort": EFFORT,
                 "status": "active"}}}))
p = subprocess.run([sys.executable, str(HERE / "factory" / "launch_support.py"), "spawn", "helper", "crew-reviewer", "look",
                    "Read the diff of the open pull request."], cwd=str(ROOT), env=env_i, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=120)
paths_i = CrewPaths.at(tmp_i / "crew")
r = row(paths_i, "crew-reviewer-look")
pid_i = (r.get("codex") or {}).get("pid", 0)
STARTED.append(pid_i)
rec = json.loads(record.read_text()) if record.exists() else {}
ck("the command succeeds and says the station started on Codex",
   p.returncode == 0 and "started crew-reviewer-look on Codex 0.171.0" in p.stderr, (p.returncode, p.stderr[-400:]))
ck("the helper is recorded on Codex, under its parent, and its app-server runs",
   r.get("runtime") == "codex" and r.get("role") == "helper" and r.get("parent") == "crew-reviewer" and alive(pid_i), r)
ck("it was given the helper's identity and its parent in the environment",
   (rec.get("env") or {}).get("FACTORY_ROLE") == "helper" and (rec.get("env") or {}).get("FACTORY_PARENT") == "crew-reviewer"
   and (rec.get("env") or {}).get("CODEX_HOME") == str(paths_i.station_file("crew-reviewer-look", "codex")), rec.get("env"))
ck("its first prompt, the task, is handed over as a turn",
   any("Read the diff of the open pull request." in p["input"][0]["text"] for _, p in calls(pid_i, "turn/start")), calls(pid_i))
end(pid_i)

# ---------------------------------------------------------------------------------------------
for pid in STARTED:
    end(pid, signal.SIGKILL)
left = [pid for pid in STARTED if alive(pid)]
ck("no app-server of the test is left running", not left, left)
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
