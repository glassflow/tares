"""A Codex station after its start (tares/crew/codex_station.py, tares/crew/proc.py and the Codex paths of
tares/crew/lifecycle.py, TR-1593): is it alive, where is it in its turn, the next prompt, the stop, and the
watch's steps on a crew that runs on Codex.

Each case starts stations with `launch.launch` in a throwaway crew home, against a fake Tares on the Codex
runtime and the fake `codex` (tests/factory/fake_codex.py), whose conversations behave as Codex 0.171.0 was
measured to. There is no `claude` anywhere: PATH holds the fake codex and nothing else. The real Codex is run
by hand: tests/factory/codex_probe/probe_station.py.

 a. turn states, in the crew's words: working in a turn, done between turns, blocked on an approval, "" when
    not running; a station blocked on an approval is never between turns, whatever the wait; a process that
    took a dead station's id is not the station; the fake fails the first reads after a turn starts, as the
    real one does, so every case here goes through the second look;
 b. handing over a prompt: the next turn on the same conversation; refused while a turn runs, with nothing
    sent (a second turn start would steer the running turn); a conversation that cannot be read is at work,
    and is neither handed a prompt nor replaced; a new conversation when asked; the conversation loaded again
    after the app-server was started anew;
 c. stopping: a running turn is interrupted first, the app-server ends when asked and its socket goes; one
    that does not end is killed; a station that is not running is said so;
 d. the commands: a wake hands the prompt to a resting station without restarting it, leaves one in a turn
    alone, and restarts one whose launch has changed, in its own conversation; `restart` with no prompt,
    `restart --fresh`, `revive`, `retire` and `crew down`; none of them looks for `claude`;
 e. the watch's steps on a Codex crew, with no `claude`: a station that died is revived in its conversation,
    the idle wake, pace, regrant and Claude Code update steps run and leave a working station alone;
 f. a wake held back: the idle wake's own round, with a pull request waiting for the reviewer, sends nothing
    while the station is in a turn, hands the wake over the round after the turn ends, and never again;
 g. what a killed station leaves running: a command of the station is found by the station's name and crew in
    its environment; a revive ends it and tells the station, by program name only; a stop leaves nothing; a
    command of another station or of another crew is never touched; a command seen under the app-server
    stays known after it left it (its shell ended), so a stop ends it too;
 h. `crew status` on a Codex crew, with no `claude`: each station with where it is (at work, waiting, idle,
    gone), in text and as JSON of the same shape as for Claude Code;
 i. the seam for Claude Code: each question goes to `claude.py` as it was asked, and a Claude Code crew
    without `claude` is still told so;
 j. the guard (TR-1594), with the fake running the station's real hook (`tares hook codex-guard`): a turn
    leaves the guard's record at its start and for each call, and a command runs with its input closed; a
    station whose conversation does not run its hooks is stopped at its first turn, before the model ran
    anything, marked, left down by the watch and started again by `revive`; a command or an MCP call with no
    run of the guard before it, one that ran though it was refused, and one whose input was not closed stop
    the station at the watch's next round or before its next prompt; a last turn that cannot be listed with
    its items is not taken for one in order, and the next prompt waits for it; the watch's look is made under
    the station's lock, and a station started again since the look is not stopped for the old one; a stop
    compares the last turn once more, so `restart --fresh` does not start a station found out there; a stop
    that cannot compare it leaves a mark, and then only the station's own conversation is started (compared
    first) until a person gives it up with `revive --fresh` or `retire`; `restart --fresh` goes on only when
    its own stop compared the last turn, so not for a station that is down, marked or not; `restart` does not
    start a station that is down for its guard; a person's `revive` of a station stopped for a turn is not
    undone by that same turn; a
    repository that brings a Codex setup of its own adds nothing to a station, and a Codex that would read it
    all the same stops the start.

Unix only: on Windows the test says so and passes.

Run: python tests/test_crew_codex_station.py
"""
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

if sys.platform == "win32":
    print("skipped on Windows: a Codex station listens on a Unix socket\n\n0 passed, 0 failed")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

SCRATCH = Path(tempfile.mkdtemp(prefix="tcs-", dir="/tmp"))   # short: a socket's path holds about a hundred bytes
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "FAKE_CODEX", "CLAUDE", "CODEX"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
BIN = SCRATCH / "bin"
BIN.mkdir()
HOME = SCRATCH / "home"
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
                  FACTORY_DOCKER=str(SCRATCH / "no-docker"), TARES_FAKE_STRAYS_DIR=str(SCRATCH),
                  PATH=str(BIN))   # nothing on it but the fake codex: no claude
(HOME / ".codex").mkdir(parents=True)
(HOME / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": "fake"}\n')
(BIN / "codex").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "factory" / "fake_codex.py"}" "$@"\n')
(BIN / "codex").chmod(0o755)
(BIN / "tares").write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -c '
                           '"import sys; sys.argv[0] = \'tares\'; from tares.cli import main; main()" "$@"\n')
(BIN / "tares").chmod(0o755)   # the station's guard is `tares hook codex-guard`, found on its PATH
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import psutil  # noqa: E402

from tares import status_words  # noqa: E402
from tares.crew import claude, cc_update, idle_wake, lifecycle, pace, proc, regrant, state, views, watch_revive  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import codex_asks, codex_round  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew import codex_guard as cg  # noqa: E402
from tares.crew.codex_wire import Wire, WireError  # noqa: E402
from tares.crew.errors import ClaudeNotFoundError, CrewError, GuardOffError, NotBetweenTurnsError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-codex-station-0123456789"
MODEL, EFFORT = "gpt-5.6-luna", "low"
CALLS = SCRATCH / "codex-calls.jsonl"
S = "crew-reviewer"
_n = [0]
STARTED: list = []


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def crew(world=None, stations=None):
    """A fresh crew home and a fake Tares on the Codex runtime; returns (tares, paths, said, make_client)."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": FAKE_TOKEN, "stations": stations or {}})
    tares = LS.FakeTares({"runtime": "codex", **(world or {})})
    tares.__enter__()
    paths = CrewPaths.at(tmp / "crew")
    said = []

    def make_client():
        c = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}, say=said.append)
        c.prime()
        return c
    return tares, paths, said, make_client


def fake(**switches):
    for k in [k for k in os.environ if k.startswith("FAKE_CODEX_")]:
        del os.environ[k]
    os.environ.update({f"FAKE_CODEX_{k.upper()}": str(v) for k, v in switches.items()})
    os.environ["FAKE_CODEX_LOG"] = str(CALLS)
    os.environ["FAKE_CODEX_DAEMON"] = str(SCRATCH / "d")   # where the fake keeps its sockets: gone with the scratch folder


def calls(pid=None, method=None) -> list:
    try:
        rows = [json.loads(x) for x in CALLS.read_text().splitlines()]
    except OSError:
        return []
    # the read asked of Tares before a prompt (TR-1596) is listed only when asked for: its own cases look at it
    return [(r["method"], r["params"]) for r in rows if (pid is None or r["pid"] == pid)
            and (r["method"] == method if method else r["method"] not in ("mcpServer/tool/call", "answer"))]


def quietly(fn, *args, **kw):
    """(error or None, the error's class, what it printed)."""
    out = io.StringIO()
    try:
        with redirect_stderr(out), redirect_stdout(out):
            fn(*args, **kw)
    except CrewError as e:
        return str(e), type(e), out.getvalue()
    return None, None, out.getvalue()


def start(client, station=S, role="reviewer", prompt="Boot as the reviewer station.", resume=""):
    err, _, out = quietly(L.launch, client, station, role, "", "", "", str(client.paths.home), MODEL, EFFORT, prompt, resume)
    note(client.paths, station)
    return err, out


def note(paths, station=S):
    pid = pid_of(paths, station)
    if pid and pid not in STARTED:
        STARTED.append(pid)


def row(paths, station=S):
    return state.stations_load(paths).get(station) or {}


def pid_of(paths, station=S):
    return (row(paths, station).get("codex") or {}).get("pid", 0)


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def gone(pid, wait=5.0):
    end_at = time.monotonic() + wait
    while time.monotonic() < end_at:
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def end(pid, sig=signal.SIGTERM):
    try:
        os.kill(pid, sig)
    except OSError:
        pass
    return gone(pid)


def until(cond, wait=5.0):
    end_at = time.monotonic() + wait
    while time.monotonic() < end_at:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def events(paths, name):
    try:
        return [e for e in map(json.loads, paths.events.read_text().splitlines()) if e["event"] == name]
    except OSError:
        return []


# ---------------------------------------------------------------------------------------------
print("a. turn states")
tares, paths, said, mk = crew()
fake(turn=1.0)
err, _ = start(mk())
pid = pid_of(paths)
ck("the station started and is alive", err is None and cs.is_alive(paths, S) and proc.is_alive(S, "", None, paths), err)
ck("in its first turn it is working, and not at rest or between turns",
   cs.turn_state(paths, S) == "working" and not proc.at_rest(S, None, paths) and not proc.between_turns(S, None, paths)
   and not proc.quiet(S, None, None, paths))
ck("the turn over, it is done: at rest, between turns, quiet", until(lambda: cs.turn_state(paths, S) == "done")
   and proc.at_rest(S, None, paths) and proc.between_turns(S, None, paths, wait=0) and proc.quiet(S, None, None, paths))
upd = proc.station_updated(S, paths)
ck("when it last did anything is a time, a moment ago", isinstance(upd, int) and abs(time.time() - upd) < 30, upd)
ck("nobody is attached to a Codex station", proc.attached(S, paths) is False)
ck("its runtime is read from its record, and a helper not started yet has its crew's",
   proc.runtime(S, paths) == "codex" and proc.on_codex("crew-reviewer-new", paths) and proc.crew_on_codex(paths))
ck("a crew on Codex needs no claude, and has no session list to read",
   proc.binary(dict(os.environ), paths) == "" and proc.sessions_readable("", paths) is None)
rec = dict(row(paths)["codex"])
state.stations_put(S, {"codex": {**rec, "born": rec["born"] - 500}}, paths)
ck("a process with the station's id and another start time is not the station",
   not cs.is_alive(paths, S) and cs.turn_state(paths, S) == "" and not proc.at_rest(S, None, paths))
state.stations_put(S, {"codex": rec}, paths)
ck("ended, it is not alive and has no turn state", end(pid) and not cs.is_alive(paths, S) and cs.turn_state(paths, S) == ""
   and not proc.between_turns(S, None, paths) and proc.station_updated(S, paths) is None)

tares, paths, said, mk = crew()
fake(approval=1)
err, _ = start(mk())
ck("stopped on an approval, it is blocked", err is None and cs.turn_state(paths, S) == "blocked", err)
ck("and never between turns or quiet, whatever the wait says",
   not proc.at_rest(S, None, paths) and not proc.between_turns(S, None, paths, wait=0)
   and not proc.quiet(S, 0, None, paths))
end(pid_of(paths))

# ---------------------------------------------------------------------------------------------
print("b. handing over a prompt")
tares, paths, said, mk = crew()
fake(turn=0.2)
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
got = cs.hand(paths, S, "The reviewer has a new pull request.")
took = calls(pid)
ck("a prompt to a resting station is one more turn on its conversation: no new one, the same one joined",
   got["thread"] == thread and not got["replaced"]
   and [m for m, _ in took] == ["thread/start", "turn/start", "thread/resume", "turn/start"]
   and took[2][1]["threadId"] == thread
   and took[-1][1] == {"threadId": thread, "input": [{"type": "text", "text": "The reviewer has a new pull request."}], "effort": EFFORT}
   and row(paths)["sessionId"] == thread and row(paths)["codex"]["turn"] == got["turn"] and pid_of(paths) == pid, took)
end(pid)

tares, paths, said, mk = crew()
fake(turn="hang")
err, _ = start(mk())
pid = pid_of(paths)
before = len(calls(pid))
err, kind, _ = quietly(cs.hand, paths, S, "Another prompt.")
ck("a prompt to a station in a turn is refused, as a wake that finds a station in a turn is",
   kind is NotBetweenTurnsError and "is in a turn" in err, (err, kind))
ck("and nothing is sent: a second turn start would steer the running turn", len(calls(pid)) == before, calls(pid)[before:])
end(pid)

tares, paths, said, mk = crew()
fake(unread=100000, turn=0.2)
cs.READ_PAUSE = 0.01
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
time.sleep(0.5)   # the turn is over, and the conversation still cannot be read
ck("a conversation that cannot be read is taken for one at work, never for one at rest",
   err is None and cs.turn_state(paths, S) == "working" and not proc.at_rest(S, None, paths), err)
before = len(calls(pid))
err, kind, _ = quietly(cs.hand, paths, S, "Are you there?")
ck("and it is handed nothing: not a prompt, and not a new conversation in place of the one it has",
   "cannot be read now" in (err or "") and kind is CrewError and len(calls(pid)) == before
   and row(paths)["sessionId"] == thread, (err, calls(pid)[before:]))
cs.READ_PAUSE = 0.25
end(pid)

tares, paths, said, mk = crew()
fake()
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
got = cs.hand(paths, S, "Start over.", fresh=True)
ck("asked for a new conversation, it gets one, and the event names the old one",
   got["thread"] != thread and not got["replaced"] and row(paths)["sessionId"] == got["thread"]
   and [m for m, _ in calls(pid)][-2:] == ["thread/start", "turn/start"]
   and [e["detail"] for e in events(paths, "conversation-fresh")] == [thread], got)
ck("a station that is not running cannot be handed a prompt", end(pid)
   and "is not running" in (quietly(cs.hand, paths, S, "Anyone there?")[0] or ""))
(paths.station_file(S, "settings.json")).unlink()
err, _, _ = quietly(cs.hand, paths, S, "No settings.")
ck("nor one whose settings are gone", "has no settings to start a conversation from" in (err or ""), err)

# ---------------------------------------------------------------------------------------------
print("c. stopping")
tares, paths, said, mk = crew()
fake()
err, _ = start(mk())
pid = pid_of(paths)
sock = Path(row(paths)["codex"]["socket"])
until(lambda: cs.turn_state(paths, S) == "done")
how = cs.stop(paths, S)
ck("a resting station ends when asked: no interrupt, the process gone, the socket's path gone",
   how == "stopped" and gone(pid) and not os.path.lexists(sock) and not calls(pid, "turn/interrupt"), how)
ck("stopping it again says it is not running", cs.stop(paths, S) == "not running")

tares, paths, said, mk = crew()
fake(turn="hang")
err, _ = start(mk())
pid = pid_of(paths)
how = cs.stop(paths, S, turn_wait=3)
took = calls(pid)
ck("a station in a turn is interrupted first, with the turn that runs, then ended",
   how == "stopped" and gone(pid) and took[-1][0] == "turn/interrupt"
   and took[-1][1] == {"threadId": row(paths)["sessionId"], "turnId": row(paths)["codex"]["turn"]}, (how, took[-1:]))

tares, paths, said, mk = crew()
fake(deaf=1)
err, _ = start(mk())
pid = pid_of(paths)
kept_at = os.path.realpath(row(paths)["codex"]["socket"])
how = cs.stop(paths, S, grace=1)
ck("one that does not end when asked is killed; the socket's path and the socket it left elsewhere are removed",
   how == "killed" and gone(pid) and not os.path.lexists(row(paths)["codex"]["socket"])
   and kept_at != row(paths)["codex"]["socket"] and not os.path.exists(kept_at), (how, kept_at))

# ---------------------------------------------------------------------------------------------
print("d. the commands")
tares, paths, said, mk = crew()
fake()
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
err, _, _ = quietly(lifecycle.restart, mk(), paths, [S, "WAKE: a pull request waits."], claude_bin="", only_between_turns=True)
took = calls(pid)
ck("a wake hands its prompt to a resting station and does not restart it",
   err is None and pid_of(paths) == pid and alive(pid) and took[-1][0] == "turn/start"
   and took[-1][1]["input"][0]["text"] == "WAKE: a pull request waits." and took[-1][1]["threadId"] == thread
   and len(events(paths, "prompted")) == 1, (err, took[-1:]))
os.environ["FAKE_CODEX_TURN"] = "hang"   # for the app-servers started from here on
until(lambda: cs.turn_state(paths, S) == "done")
tares.world["settings"] = {**tares.world["settings"], "autonomy": "L3-review"}   # the cell would now start it otherwise
err, _, out = quietly(lifecycle.restart, mk(), paths, [S, "REGRANT: your rules changed."], claude_bin="", only_between_turns=True)
note(paths)
pid2 = pid_of(paths)
took = calls(pid2)
ck("a station whose launch has changed is restarted for real: a new app-server with the new settings",
   err is None and pid2 != pid and gone(pid) and alive(pid2)
   and json.loads(paths.station_file(S, "settings.json").read_text())["autonomy"] == "L3-review", (err, out))
ck("in its own conversation, loaded again, with the prompt as its next turn",
   row(paths)["sessionId"] == thread and [m for m, _ in took] == ["thread/resume", "turn/start"]
   and took[1][1]["input"][0]["text"] == "REGRANT: your rules changed.", took)
before = len(calls(pid2))
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S, "WAKE: again."], claude_bin="", only_between_turns=True)
ck("a wake leaves a station in a turn alone", kind is NotBetweenTurnsError and pid_of(paths) == pid2 and alive(pid2)
   and len(calls(pid2)) == before, (err, kind))
err, _, _ = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
ck("revive refuses a station that runs", err == f"{S} is running; message it instead", err)
err, _, _ = quietly(lifecycle.restart, mk(), paths, [S], claude_bin="")
note(paths)
pid3 = pid_of(paths)
took = calls(pid3)
ck("a plain restart stops it in the middle of its turn (an interrupt first) and starts it again in its conversation",
   err is None and pid3 not in (pid, pid2) and gone(pid2) and calls(pid2)[-1][0] == "turn/interrupt"
   and row(paths)["sessionId"] == thread and [m for m, _ in took] == ["thread/resume", "turn/start"]
   and "revived by the factory under the same name" in took[1][1]["input"][0]["text"], (err, took))
err, _, _ = quietly(lifecycle.restart, mk(), paths, [S, "--fresh"], claude_bin="")
note(paths)
pid4 = pid_of(paths)
took = calls(pid4)
ck("restart --fresh starts a new conversation", err is None and pid4 != pid3 and row(paths)["sessionId"] != thread
   and [m for m, _ in took] == ["thread/start", "turn/start"] and "in a new conversation" in took[1][1]["input"][0]["text"], (err, took))
end(pid4, signal.SIGKILL)
err, _, _ = quietly(lifecycle.revive, mk(), paths, [S, "Carry on."], claude_bin="")
note(paths)
pid5 = pid_of(paths)
ck("revive starts a station that died, in the conversation it had",
   err is None and alive(pid5) and [m for m, _ in calls(pid5)] == ["thread/resume", "turn/start"]
   and calls(pid5)[1][1]["input"][0]["text"] == "Carry on." and len(events(paths, "revived")) >= 2, err)
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
out = io.StringIO()
with redirect_stderr(out), redirect_stdout(out):
    code = lifecycle.cmd_retire(["retire", S], dict(os.environ))
ck("`tares factory retire` runs with no claude on the machine and ends the station",
   code == 0 and shutil.which("claude") is None and gone(pid5) and row(paths)["status"] == "retired"
   and events(paths, "retired")[-1]["detail"] == "codex app-server stopped", (code, out.getvalue()))
err, _, _ = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
ck("a retired station is not revived", "was retired" in (err or ""), err)

tares, paths, said, mk = crew()
fake()
start(mk())
start(mk(), station="crew-releaser", role="releaser")
pids = [pid_of(paths), pid_of(paths, "crew-releaser")]
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
out = io.StringIO()
with redirect_stderr(out), redirect_stdout(out):
    code = lifecycle.cmd_down(["crew", "down"], dict(os.environ))
ck("`crew down` ends every station of a Codex crew",
   code == 0 and all(gone(p) for p in pids) and {r["status"] for r in state.stations_load(paths).values()} == {"retired"}
   and "all stations stopped" in out.getvalue(), (code, out.getvalue()[-300:]))

# ---------------------------------------------------------------------------------------------
print("e. the watch's steps on a Codex crew")
tares, paths, said, mk = crew()
fake()
start(mk())
start(mk(), station="crew-releaser", role="releaser")
pid, other = pid_of(paths), pid_of(paths, "crew-releaser")
thread = row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
end(pid, signal.SIGKILL)
env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
out = io.StringIO()
with redirect_stderr(out), redirect_stdout(out):
    watch_revive.step(paths, env)
note(paths)
new = pid_of(paths)
ck("the revive step finds the session list of no claude unneeded and the dead station down",
   not watch_revive.last.unread and watch_revive.last.down and S in watch_revive.last.started, out.getvalue())
ck("it revives the station in its conversation, and leaves the one that runs alone",
   new != pid and alive(new) and row(paths)["sessionId"] == thread
   and [m for m, _ in calls(new)] == ["thread/resume", "turn/start"] and pid_of(paths, "crew-releaser") == other and alive(other),
   (out.getvalue(), calls(new)))
ck("a Codex station is never taken for one stuck on Claude Code's compaction", not watch_revive.stuck_on_context(paths, "", S))
os.environ["FAKE_CODEX_TURN"] = "hang"
out = io.StringIO()
failed = []
with redirect_stderr(out), redirect_stdout(out):
    for name, step in (("regrant", lambda: regrant.step(paths, env)), ("idle wake", lambda: idle_wake.step(paths, env=env)),
                       ("pace", lambda: pace.step(paths, env=env)), ("Claude Code update", lambda: cc_update.step(paths, env=env)),
                       ("revive again", lambda: watch_revive.step(paths, env))):
        try:
            step()
        except Exception as e:   # noqa: BLE001 - the case is that none of them raises
            failed.append(f"{name}: {type(e).__name__}: {e}")
ck("the regrant, idle wake, pace and Claude Code update steps run on a Codex crew with no claude", not failed, failed)
ck("and leave the stations running as they were", pid_of(paths) == new and alive(new) and alive(other)
   and "claude not found" not in out.getvalue(), out.getvalue()[-400:])
for p_ in (new, other):
    end(p_)

# ---------------------------------------------------------------------------------------------
print("f. a wake that finds the station in a turn is held, and arrives after the turn, once")
queue = {"queue": [{"project": "p1", "project_name": "Shop", "ticket": "T-7", "head": "abcdef1234567"}]}
tares, paths, said, mk = crew(world={"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True},
                                     "routes": {"/api/review-queue?mine=1": [200, queue]}})
fake(turn=1.5)
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")


def wake_round():
    watch_revive.last = watch_revive.Round()   # a round whose revive step had nothing to do
    out = io.StringIO()
    with redirect_stderr(out), redirect_stdout(out):
        idle_wake.step(paths, env=env)
    return out.getvalue()


def wakes():
    return [p_["input"][0]["text"] for _, p_ in calls(pid, "turn/start") if "T-7" in p_["input"][0]["text"]]


said_1 = wake_round()
ck("while the station is in its first turn, a round with work for it sends nothing",
   err is None and cs.turn_state(paths, S) == "working" and wakes() == [] and len(calls(pid, "turn/start")) == 1, (err, said_1, calls(pid)))
until(lambda: cs.turn_state(paths, S) == "done")
said_2 = wake_round()
ck("the next round, with the turn over, hands the wake over as the next turn of the same conversation",
   len(wakes()) == 1 and calls(pid, "turn/start")[-1][1]["threadId"] == thread and pid_of(paths) == pid and alive(pid)
   and len(events(paths, "idle_wake")) == 1, (said_2, calls(pid)))
until(lambda: cs.turn_state(paths, S) == "done")
said_3 = wake_round()
said_4 = wake_round()
ck("and it is not sent again: the same work wakes a station once",
   len(wakes()) == 1 and len(calls(pid, "turn/start")) == 2 and len(events(paths, "idle_wake")) == 1, (said_3, said_4, calls(pid)))
end(pid)

# ---------------------------------------------------------------------------------------------
print("g. what a killed station leaves running")


def command_of(paths_, station, seconds="300"):
    """A command as a station's app-server starts one: in a session of its own, with the station's environment."""
    p_ = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                          env={**os.environ, "FACTORY_STATION": station, "FACTORY_HOME": str(paths_.home),
                               "FACTORY_CREW_HOME": str(paths_.home)})
    STARTED.append(p_.pid)
    return p_


tares, paths, said, mk = crew()
other_tares, other_paths, _, _ = crew()
fake()
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
mine, sibling, elsewhere = command_of(paths, S), command_of(paths, "crew-releaser"), command_of(other_paths, S)
time.sleep(0.3)
found = cs.leftovers(paths, S)
ck("what a station started is found by the station's name and crew in its environment, and nothing else is",
   [p_.pid for p_ in found] == [mine.pid], [p_.pid for p_ in found])
ck("its app-server is not among them while it runs, nor is this process", pid not in [p_.pid for p_ in found]
   and os.getpid() not in [p_.pid for p_ in found])
end(pid, signal.SIGKILL)   # the app-server dies; its command goes on, as TR-1576 measured
ck("killed, the app-server leaves its command running", alive(mine.pid))
err, _, _ = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
note(paths)
new = pid_of(paths)
took = calls(new, "turn/start")
ck("a revive ends what the dead station left, and only that",
   err is None and gone(mine.pid) and alive(sibling.pid) and alive(elsewhere.pid), err)
ck("and tells the station first, by program name, that what those did is not known",
   took and took[0][1]["input"][0]["text"].startswith("Before anything else: when your process ended, 1 program(s) you had started")
   and "What they did is not known" in took[0][1]["input"][0]["text"]
   and "revived by the factory under the same name" in took[0][1]["input"][0]["text"]
   and "time.sleep" not in took[0][1]["input"][0]["text"], took[:1])
ck("the event names the programs, never their arguments",
   [e["detail"] for e in events(paths, "left-behind")] == [f"1 program(s) ended: {os.path.basename(sys.executable)}"],
   events(paths, "left-behind"))
# A command whose environment says nothing (on macOS that of every system program cannot be read): it is known
# only by what was written down of the app-server's children while it ran.
silent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                          env={k: v for k, v in os.environ.items() if not k.startswith("FACTORY")})
STARTED.append(silent.pid)
time.sleep(0.3)
ck("a command with no mark in its environment is not found by the environment", silent.pid not in [p_.pid for p_ in cs.leftovers(paths, S)])
state.write_json(cs._children_file(paths, S), [[silent.pid, psutil.Process(silent.pid).create_time()], [silent.pid + 1, 1.0]], compact=True)
ck("it is found once it was written down as running under the app-server, with its start time; another process with a noted id is not",
   [p_.pid for p_ in cs._noted(paths, S)] == [silent.pid] and silent.pid in [p_.pid for p_ in cs.leftovers(paths, S)])
cs._children_file(paths, S).unlink()
cs.note_children(paths, S)
ck("the note of a station whose app-server has no children is empty, and a station that is not running leaves none",
   state.read_json(cs._children_file(paths, S), None) == [] and cs.note_children(other_paths, S) is None
   and not cs._children_file(other_paths, S).exists())
# A command that outlived the shell that started it: written down while it ran under the app-server, and under
# the system's first process at the next look. `silent` and `tree` stand for such commands (the fake app-server
# starts none, so neither is under it), and `tree` has started a program of its own since.
tree = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time\n"
                         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\ntime.sleep(300)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True, env={k: v for k, v in os.environ.items() if not k.startswith("FACTORY")})
STARTED.append(tree.pid)
until(lambda: psutil.Process(tree.pid).children())
kid = psutil.Process(tree.pid).children()[0].pid
STARTED.append(kid)
state.write_json(cs._children_file(paths, S), [[p_, psutil.Process(p_).create_time()] for p_ in (silent.pid, tree.pid)]
                 + [[max(silent.pid, tree.pid, kid) + 1000, 1.0]], compact=True)
cs.note_children(paths, S)   # the next look: none of them is under the app-server
ck("a noted process that still runs stays noted when it is no longer under the app-server, with what it started; one that is gone is dropped",
   [r_[0] for r_ in state.read_json(cs._children_file(paths, S), None)] == sorted([silent.pid, tree.pid, kid])
   and sorted(p_.pid for p_ in cs._noted(paths, S)) == sorted([silent.pid, tree.pid, kid]),
   state.read_json(cs._children_file(paths, S), None))
again = command_of(paths, S)
time.sleep(0.3)
how = cs.stop(paths, S)   # it notes once more, right before it ends the app-server
ck("a stop ends what was noted too, and clears the note",
   gone(silent.pid) and gone(tree.pid) and gone(kid) and not cs._children_file(paths, S).exists())
ck("a stop leaves nothing of the station running", how == "stopped" and gone(new) and gone(again.pid) and alive(sibling.pid), how)
ck("a station with nothing left behind is told nothing and leaves no event",
   cs.end_leftovers(paths, S) == [] and len(events(paths, "left-behind")) == 2
   and lifecycle._with_cut_off("Carry on.", []) == "Carry on.")
for p_ in (sibling, elsewhere):
    end(p_.pid, signal.SIGKILL)

# ---------------------------------------------------------------------------------------------
print("h. crew status on a Codex crew")
tares, paths, said, mk = crew(world={"routes": {"/api/desk": [200, {"items": []}], "/api/desk?mine=1": [200, {"items": []}]}})
fake(turn="hang")
start(mk())
fake(approval=1)
start(mk(), station="crew-releaser", role="releaser")
fake()
start(mk(), station="crew-orchestrator", role="orchestrator")
dead = pid_of(paths, "crew-orchestrator")
until(lambda: cs.turn_state(paths, "crew-orchestrator") == "done")
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
out, errs = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(errs):
    code = views.cmd_status(["crew", "status"], dict(os.environ))
text = out.getvalue()
lines = {line.split()[0]: line for line in text.splitlines() if line.startswith("crew-")}
ck("`crew status` runs with no claude on the machine and lists the Codex stations",
   code == 0 and set(lines) == {S, "crew-releaser", "crew-orchestrator"} and "claude not found" not in errs.getvalue(),
   (code, text, errs.getvalue()))
ck("each with where it is, in the words of any station: at work, waiting, idle",
   status_words.STATION["working"] in lines.get(S, "") and status_words.STATION["waiting_unasked"] in lines.get("crew-releaser", "")
   and status_words.STATION["idle"] in lines.get("crew-orchestrator", ""), lines)
ck("and no line about an older Claude Code", "claude code:" not in text and "TWO SESSIONS" not in text, text)
end(dead, signal.SIGKILL)
out = io.StringIO()
with redirect_stdout(out), redirect_stderr(io.StringIO()):
    code = views.cmd_status(["crew", "status", "--json"], dict(os.environ))
try:
    doc = json.loads(out.getvalue())
except ValueError:
    doc = {}
by = {r["station"]: r for r in doc.get("stations", [])}
ck("as JSON, the same document shape as for Claude Code, with a station that died shown as gone",
   code == 0 and by.get(S, {}).get("state") == "working" and by.get("crew-releaser", {}).get("state") == "blocked"
   and by.get("crew-orchestrator", {}).get("state") == "gone" and doc.get("claude_code") == {"target": "", "older": 0, "retrying": 0}
   and doc.get("two_sessions") == [], out.getvalue()[:600])
for name in (S, "crew-releaser"):
    end(pid_of(paths, name))

# ---------------------------------------------------------------------------------------------
print("i. the seam for Claude Code")
tares, paths, said, mk = crew(world={"runtime": "claude_code"},
                              stations={"crew-orchestrator": {"role": "orchestrator", "id": "abcd1234", "status": "active"}})
asked = []
kept = {n: getattr(claude, n) for n in ("is_alive", "attached", "at_rest", "between_turns", "quiet", "station_updated")}
try:
    claude.is_alive = lambda *a: asked.append(("is_alive", a)) or True
    claude.attached = lambda *a: asked.append(("attached", a)) or True
    claude.at_rest = lambda *a: asked.append(("at_rest", a)) or True
    claude.between_turns = lambda *a: asked.append(("between_turns", a)) or True
    claude.quiet = lambda *a: asked.append(("quiet", a)) or True
    claude.station_updated = lambda *a: asked.append(("station_updated", a)) or 42
    o = "crew-orchestrator"
    got = [proc.is_alive(o, "/w", "cl", paths), proc.attached(o, paths), proc.at_rest(o, "cl", paths, 7),
           proc.between_turns(o, "cl", paths, 7, 30), proc.quiet(o, 30, "cl", paths, 7), proc.station_updated(o, paths)]
finally:
    for n, fn in kept.items():
        setattr(claude, n, fn)
ck("for a Claude Code station each question goes to claude.py, with the arguments it was asked with",
   got == [True, True, True, True, True, 42] and asked == [
       ("is_alive", (o, "/w", "cl", paths)), ("attached", (o, paths)), ("at_rest", (o, "cl", paths, 7)),
       ("between_turns", (o, "cl", paths, 7, 30)), ("quiet", (o, 30, "cl", paths, 7)), ("station_updated", (o, paths))], asked)
ck("its runtime is Claude Code", proc.runtime("crew-orchestrator", paths) == "claude_code" and not proc.crew_on_codex(paths))
try:
    proc.binary(dict(os.environ), paths)
    err = None
except ClaudeNotFoundError as e:
    err = str(e)
ck("a Claude Code crew on a machine with no claude is still told so", err is not None and "claude not found" in err, err)
try:
    proc.is_alive("crew-orchestrator", "", None, paths)
    err = None
except ClaudeNotFoundError as e:
    err = str(e)
ck("also when it is asked whether a station runs", err is not None, err)
tares, paths, said, mk = crew()
ck("a crew with no station yet and no claude: no station runs, and the launch is left to say what is missing",
   proc.is_alive("crew-build-x", "", None, paths) is False and not proc.crew_on_codex(paths))

# ---------------------------------------------------------------------------------------------
print("j. the guard on a Codex station")


def last_turn(paths, station=S):
    with cs.visit(paths, station) as wire:
        return cs._last_turn(wire, row(paths, station)["sessionId"])


def model_runs(*commands, **switches):
    """The fake's model runs these in every turn (a string is a shell command, a dict an MCP call)."""
    fake(turn=0.2, think=0.05, commands=json.dumps(list(commands)), **switches)


def watch_round(paths, tares):
    env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}
    os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
    out = io.StringIO()
    with redirect_stderr(out), redirect_stdout(out):
        watch_revive.step(paths, env)
    return out.getvalue()


tares, paths, said, mk = crew()
marker = SCRATCH / "ran-guarded"
model_runs(f"/usr/bin/touch {marker}", "gh pr merge 3", {"mcp": "tares__desk_ask"})
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
rec, turn = cs._record(paths, S), row(paths)["codex"]["turn"]
items = last_turn(paths)["items"]
ck("a turn leaves the guard's record: its start first, then each call, let through or refused",
   err is None and [(x.get("armed"), x.get("tool"), x.get("said")) for x in rec] == [
       (True, None, None), (None, "Bash", "allow"), (None, "Bash", "deny"), (None, "mcp__tares__desk_ask", "allow")]
   and all(x["turn"] == turn for x in rec), (err, rec))
ck("the command let through ran with its input closed; the refused one never ran and left no item",
   marker.exists() and [i["type"] for i in items] == ["commandExecution", "mcpToolCall"]
   and items[0]["command"] == "/bin/zsh -lc " + shlex.quote(f"exec </dev/null\n/usr/bin/touch {marker}")
   and cg.asked(items[0]["command"]) == f"/usr/bin/touch {marker}"
   and [x["call"] for x in rec if x.get("said") == "allow"] == [i["id"] for i in items], items)
ck("the refusal is in the crew's events, as for a Claude Code station",
   [e["station"] for e in events(paths, "guard-deny")] == [S], events(paths, "guard-deny"))
ck("compared with the record nothing is missing: the watch leaves the station up",
   cs.check_guard(paths, S) == "" and "cannot be relied on" not in watch_round(paths, tares) and alive(pid)
   and pid_of(paths) == pid and not cs.guard_off(paths, S) and not events(paths, "guard-off"))
err, _, _ = quietly(cs.hand, paths, S, "The next task.")
ck("and it takes its next prompt", err is None and len(calls(pid, "turn/start")) == 2, err)
until(lambda: cs.turn_state(paths, S) == "done")
end(pid)

tares, paths, said, mk = crew()
marker = SCRATCH / "ran-with-no-hooks"
fake(nohooks=1, turn="hang", think=1.0, commands=json.dumps([f"/usr/bin/touch {marker}"]))
before = len(calls())
err, out = start(mk())
took = calls()[before:]
time.sleep(1.5)   # longer than the fake's model takes to run its command, had the turn gone on
off = events(paths, "guard-off")
ck("a station whose conversation does not run its hooks is stopped at its first turn, and says why",
   err is not None and err.startswith(f"{S} was stopped, since its guard cannot be relied on: the turn began (userMessage) "
                                      "with no run of the guard before it") and len(off) == 1 and off[0]["station"] == S, (err, off))
ck("the turn was interrupted before the model ran anything: the command never ran",
   [m for m, _ in took] == ["thread/start", "turn/start", "turn/interrupt"] and not marker.exists(), took)
ck("its app-server is gone, and a station that was never up leaves no record",
   not row(paths) and not cs.is_alive(paths, S) and not paths.station_file(S, "sock").exists(), row(paths))

tares, paths, said, mk = crew()
fake()
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
fake(nohooks=1)   # for the app-servers started from here on: a Codex that no longer runs the conversation's hooks
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S], claude_bin="")
ck("a station that ran, started again on a Codex that does not run its hooks, is stopped and marked",
   kind is GuardOffError and gone(pid) and not cs.is_alive(paths, S) and "with no run of the guard" in cs.guard_off(paths, S)
   and row(paths)["sessionId"] == thread and row(paths)["status"] == "active", (err, row(paths)))
n = len(calls(method="thread/start")) + len(calls(method="thread/resume"))
said_1 = watch_round(paths, tares)
said_2 = watch_round(paths, tares)
ck("the watch leaves it down, says so once with the way back, and starts nothing",
   f"{S} stays down" in said_1 and f"`tares factory revive {S}` starts it again" in said_1 and "stays down" not in said_2
   and len(calls(method="thread/start")) + len(calls(method="thread/resume")) == n and not cs.is_alive(paths, S),
   (said_1, said_2))
fake()
err, _, _ = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
note(paths)
pid2 = pid_of(paths)
ck("`revive` starts it again, in its conversation, and the mark is gone",
   err is None and alive(pid2) and pid2 != pid and row(paths)["sessionId"] == thread and cs.guard_off(paths, S) == ""
   and [m for m, _ in calls(pid2)] == ["thread/resume", "turn/start"], (err, calls(pid2)))
end(pid2)

tares, paths, said, mk = crew()
marker = SCRATCH / "ran-with-no-check"
model_runs(f"/usr/bin/touch {marker}", nopre=1)
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
ck("(a turn whose start was recorded goes on; here its command then ran with no run of the guard)",
   err is None and marker.exists() and [bool(x.get("armed")) for x in cs._record(paths, S)] == [True], cs._record(paths, S))
said_1 = watch_round(paths, tares)
off = events(paths, "guard-off")
ck("the watch's next round compares what ran with the record, stops the station and says why",
   gone(pid) and "a command ran with no run of the guard before it" in said_1 and f"tares factory revive {S}" in said_1
   and len(off) == 1 and "a command ran with no run of the guard before it" in off[0].get("detail", "")
   and "no run of the guard" in cs.guard_off(paths, S), (said_1, off))
n = len(calls(method="thread/resume"))
watch_round(paths, tares)
ck("and does not start it again", not cs.is_alive(paths, S) and len(calls(method="thread/resume")) == n and pid_of(paths) == pid)
thread, shown = row(paths)["sessionId"], row(paths)["codex"]["guard_off"].get("turn")
model_runs("/usr/bin/true")   # the app-servers started from here on run their hooks again
n = len(calls(method="thread/resume")) + len(calls(method="thread/start"))
got = [quietly(lifecycle.restart, mk(), paths, [S, *rest], claude_bin="")[1] for rest in ([], ["--fresh"], ["Carry on."])]
ck("`restart` does not start a station that is down for its guard, with or without --fresh: that is `revive`'s to do",
   got == [GuardOffError] * 3 and not cs.is_alive(paths, S) and cs.guard_off(paths, S) != ""
   and len(calls(method="thread/resume")) + len(calls(method="thread/start")) == n and not row(paths).get(cs.SEEN), got)
err, _, _ = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
note(paths)
pid2 = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
ck("a person's `revive` starts it again in its conversation: the turn it was stopped for is not held against it again",
   err is None and shown and alive(pid2) and row(paths)["sessionId"] == thread and row(paths).get(cs.SEEN) == shown
   and cs.guard_off(paths, S) == "" and [m for m, _ in calls(pid2)] == ["thread/resume", "turn/start"], (err, calls(pid2)))
ck("and the watch compares the turn after it as any other, and leaves the station up",
   cs.check_guard(paths, S) == "" and "cannot be relied on" not in watch_round(paths, tares) and alive(pid2)
   and len(cs._record(paths, S)) > 1)
end(pid2)

tares, paths, said, mk = crew()
model_runs("/usr/bin/true", nopre=1)
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
err, kind, _ = quietly(cs.hand, paths, S, "The next task.")
ck("with no watch, the next prompt is not handed over: the last turn is compared first, and the station is stopped",
   kind is GuardOffError and "a command ran with no run of the guard before it" in err and len(calls(pid, "turn/start")) == 1
   and gone(pid) and cs.guard_off(paths, S) != "", (err, kind, calls(pid)))

for label, commands, switch, why in (
        ("a command that ran as the model gave it, not as the guard changed it", ["/usr/bin/true"], "keep_input",
         "a command ran without the guard's line that closes its input"),
        ("a command that ran though the guard refused it", ["gh pr merge 3"], "deny_runs",
         "a command ran though the guard refused it"),
        ("an MCP call with no run of the guard before it", [{"mcp": "tares__desk_ask"}], "nopre",
         "an MCP call ran with no run of the guard before it")):
    tares, paths, said, mk = crew()
    model_runs(*commands, **{switch: 1})
    err, _ = start(mk())
    pid = pid_of(paths)
    until(lambda: cs.turn_state(paths, S) == "done")
    got = cs.check_guard(paths, S)
    ck(f"{label} stops the station", err is None and got.startswith(why) and gone(pid) and cs.guard_off(paths, S) == got, (err, got))

# A turn that cannot be listed with its items is not a turn that was compared (the review of PR 929).
real_request = Wire.request
LISTING = {"as": ""}   # how `thread/turns/list` answers: "" as the app-server does, "fail", "once" (fails once), "bare" (no items)


def listed(self, method, params=None):
    if method == "thread/turns/list" and LISTING["as"] in ("fail", "once"):
        LISTING["as"] = "" if LISTING["as"] == "once" else "fail"
        raise WireError("thread-store internal error")
    got = real_request(self, method, params)
    if method == "thread/turns/list" and LISTING["as"] == "bare":
        got = {**got, "data": [{**t_, "itemsView": "notLoaded", "items": []} for t_ in got.get("data") or []]}
    return got


Wire.request = listed
tares, paths, said, mk = crew()
model_runs("/usr/bin/true", nopre=1)   # a command with no run of the guard before it, as above
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
for how, shown in (("fail", "its turns could not be listed (thread-store internal error)"),
                   ("bare", "its newest turn came without its items (notLoaded)")):
    LISTING["as"] = how
    got = cs.check_guard(paths, S)
    err, kind, _ = quietly(cs.hand, paths, S, "The next task.")
    ck(f"a last turn that cannot be compared ({how}) is not one found in order: the watch stops nothing, and the next "
       "prompt is not handed over",
       got == "" and kind is CrewError and shown in (err or "") and "not handing it a prompt" in (err or "")
       and len(calls(pid, "turn/start")) == 1 and alive(pid) and cs.guard_off(paths, S) == ""
       and not events(paths, "guard-off"), (got, kind, err, calls(pid)))
LISTING["as"] = ""
err, kind, _ = quietly(cs.hand, paths, S, "The next task.")
ck("once it can be listed again that same turn is compared, before any prompt, and the station is stopped for it",
   kind is GuardOffError and "a command ran with no run of the guard before it" in err
   and len(calls(pid, "turn/start")) == 1 and gone(pid) and cs.guard_off(paths, S) != "", (err, kind, calls(pid)))
Wire.request = real_request
ck("a conversation with no turn yet has nothing to compare, and that is in order",
   cs._last_turn(type("W", (), {"request": lambda self, m, p=None: {"data": []}})(), "thr") is None)

# The look and the stop are one step under the station's lock, and only the app-server that was looked at is
# stopped (the review of PR 929).
tares, paths, said, mk = crew()
model_runs("/usr/bin/true", nopre=1)
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
holder = command_of(paths, "crew-releaser")   # another factory command at work on the station: it holds the lock
paths.lock_dir(S).mkdir()
(paths.lock_dir(S) / "pid").write_text(f"{holder.pid}\n")
looks = []
real_audit = cs._audit
cs._audit = lambda *a: looks.append(1) or real_audit(*a)
got = cs.check_guard(paths, S)
ck("while another command holds the station's lock the watch does not look at it, and leaves the lock alone",
   got == "" and not looks and alive(pid) and (paths.lock_dir(S) / "pid").read_text() == f"{holder.pid}\n", (got, looks))
shutil.rmtree(paths.lock_dir(S))
end(holder.pid, signal.SIGKILL)
old = dict(row(paths)["codex"])
newer = command_of(paths, "crew-releaser")   # stands for the app-server a restart started meanwhile
time.sleep(0.3)


def restarted_meanwhile(*a):
    """The comparison, and right after it the station's record names another app-server: what a restart that got
    in between leaves, were the look not under the lock."""
    found = real_audit(*a)
    state.stations_put(S, {"codex": {**old, "pid": newer.pid, "born": psutil.Process(newer.pid).create_time()}}, paths)
    return found


cs._audit = restarted_meanwhile
got = cs.check_guard(paths, S)
ck("a station started again since the look is not stopped or marked for what the old app-server did",
   got == "" and alive(newer.pid) and pid_of(paths) == newer.pid and cs.guard_off(paths, S) == ""
   and not events(paths, "guard-off"), (got, row(paths).get("codex")))
cs._audit = real_audit
state.stations_put(S, {"codex": old}, paths)
got = cs.check_guard(paths, S)
ck("the one that was looked at is, and the lock is let go after the look",
   "a command ran with no run of the guard before it" in got and gone(pid) and cs.guard_off(paths, S) == got
   and not paths.lock_dir(S).exists(), got)
end(newer.pid, signal.SIGKILL)

# A stop is the last look a conversation gets: the station is then started in a new one, or not at all.
tares, paths, said, mk = crew()
model_runs("/usr/bin/true", nopre=1)
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
n = len(calls(method="thread/start"))
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S, "--fresh"], claude_bin="")
off = events(paths, "guard-off")
ck("`restart --fresh` compares the last turn before it ends the app-server, and does not start the station again",
   kind is GuardOffError and "a command ran with no run of the guard before it" in err and gone(pid)
   and not cs.is_alive(paths, S) and len(calls(method="thread/start")) == n and row(paths)["sessionId"] == thread
   and "no run of the guard" in cs.guard_off(paths, S) and len(off) == 1, (err, kind, off))
watch_revive._guard_told.discard(S)   # this watch said it of the station of an earlier crew here, of the same name
ck("the watch then leaves it down", f"{S} stays down" in watch_round(paths, tares) and not cs.is_alive(paths, S)
   and len(calls(method="thread/start")) == n)

tares, paths, said, mk = crew()
model_runs("/usr/bin/true")
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S, "--fresh"], claude_bin="")
note(paths)
pid2 = pid_of(paths)
ck("a station whose last turn is in order is started again by `restart --fresh`, with no mark",
   err is None and gone(pid) and pid2 != pid and alive(pid2) and cs.guard_off(paths, S) == ""
   and not events(paths, "guard-off"), (err, kind))
until(lambda: cs.turn_state(paths, S) == "done")
end(pid2)

tares, paths, said, mk = crew()
model_runs("/usr/bin/true", nopre=1)
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
err, kind, _ = quietly(lifecycle.retire, mk(), paths, [S], claude_bin="")
off = events(paths, "guard-off")
ck("`retire` records what the last turn ran past the guard, and retires the station all the same",
   err is None and gone(pid) and row(paths)["status"] == "retired" and len(off) == 1
   and "a command ran with no run of the guard before it" in off[0].get("detail", ""), (err, off))

# A look at a stop that could not be made is not a look that found the turn in order (the second review of PR 929).
Wire.request = listed


def stopped_uncompared(*commands, **switches):
    """A station whose `restart --fresh` could not compare its last turn: (tares, paths, mk, pid, thread, err, kind)."""
    tares_, paths_, _, mk_ = crew()
    model_runs(*commands, **switches)
    start(mk_())
    pid_, thread_ = pid_of(paths_), row(paths_)["sessionId"]
    until(lambda: cs.turn_state(paths_, S) == "done")
    LISTING["as"] = "fail"
    err_, kind_, _ = quietly(lifecycle.restart, mk_(), paths_, [S, "--fresh"], claude_bin="")
    watch_revive._guard_told.discard(S)
    return tares_, paths_, mk_, pid_, thread_, err_, kind_


def conversations():
    return len(calls(method="thread/start"))


n = conversations() + 1
tares, paths, mk, pid, thread, err, kind = stopped_uncompared("/usr/bin/true", nopre=1)
ck("`restart --fresh` whose stop cannot compare the last turn stops the station and starts no new conversation",
   kind is CrewError and "could not be compared with the guard's record (its turns could not be listed" in err
   and f"`tares factory revive {S}` starts it in its own" in err and f"`tares factory revive {S} --fresh`" in err
   and gone(pid) and not cs.is_alive(paths, S) and conversations() == n and row(paths)["sessionId"] == thread,
   (err, kind, conversations(), n))
ck("the station's row says which conversation was never compared, and an event records it; it is not marked as found out",
   cs.unverified(paths, S).get("thread") == thread and len(events(paths, "guard-unverified")) == 1
   and cs.guard_off(paths, S) == "" and not events(paths, "guard-off"), row(paths))
turns = len(calls(method="turn/start"))
said_1 = watch_round(paths, tares)
ck("the watch starts it in its own conversation only, and while the turn still cannot be compared no turn is started",
   "not handing it a prompt" in said_1 and f"could not revive {S}" in said_1 and not cs.is_alive(paths, S)
   and len(calls(method="turn/start")) == turns and conversations() == n and cs.unverified(paths, S).get("thread") == thread,
   said_1)
LISTING["as"] = ""
said_2 = watch_round(paths, tares)
ck("once it can be, the start compares that turn first: what it ran past the guard is found, and the station stays down",
   "a command ran with no run of the guard before it" in said_2 and not cs.is_alive(paths, S)
   and len(calls(method="turn/start")) == turns and conversations() == n
   and "no run of the guard" in cs.guard_off(paths, S), said_2)

n = conversations() + 1
tares, paths, mk, pid, thread, err, kind = stopped_uncompared("/usr/bin/true")
LISTING["as"] = ""
said_1 = watch_round(paths, tares)
note(paths)
pid2 = pid_of(paths)
ck("a last turn that was in order: compared at the next start, the station runs on in its own conversation, the mark gone",
   kind is CrewError and alive(pid2) and pid2 != pid and row(paths)["sessionId"] == thread and conversations() == n
   and [m for m, _ in calls(pid2)] == ["thread/resume", "turn/start"] and cs.unverified(paths, S) == {}
   and cs.guard_off(paths, S) == "", (said_1, calls(pid2)))
until(lambda: cs.turn_state(paths, S) == "done")
cs.mark_unverified(paths, S, thread, "its turns could not be listed (a test)")
err, kind, _ = quietly(cs.hand, paths, S, "A prompt for a new conversation.", fresh=True)
ck("with such a mark no turn is started in another conversation, whoever asks",
   kind is CrewError and "is not started in a new conversation" in (err or "") and conversations() == n
   and cs.unverified(paths, S).get("thread") == thread and alive(pid2), (err, kind))
err, kind, _ = quietly(cs.hand, paths, S, "A prompt for its own conversation.")
ck("in its own conversation the turn is compared, the prompt handed over and the mark cleared",
   err is None and cs.unverified(paths, S) == {} and row(paths)["sessionId"] == thread, err)
until(lambda: cs.turn_state(paths, S) == "done")
LISTING["as"] = "once"   # the stop's look fails, the start's does not
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S], claude_bin="")
note(paths)
pid3 = pid_of(paths)
ck("a restart in the same conversation goes on after a stop that could not compare: its start compares first",
   err is None and gone(pid2) and alive(pid3) and row(paths)["sessionId"] == thread and cs.unverified(paths, S) == {}
   and len(events(paths, "guard-unverified")) == 3 and LISTING["as"] == "", (err, events(paths, "guard-unverified")))
until(lambda: cs.turn_state(paths, S) == "done")
end(pid3)

n = conversations() + 1
tares, paths, mk, pid, thread, err, kind = stopped_uncompared("/usr/bin/true")
LISTING["as"] = ""
again = [quietly(lifecycle.restart, mk(), paths, [S, "--fresh", *rest], claude_bin="") for rest in ([], ["A prompt."])]
ck("`restart --fresh` asked again of the station that is now down is refused again: it does not count as a person's `revive --fresh`",
   [k for _, k, _ in again] == [CrewError] * 2 and all("is not started in a new conversation" in (e or "") for e, _, _ in again)
   and conversations() == n and not cs.is_alive(paths, S) and cs.unverified(paths, S).get("thread") == thread
   and not events(paths, "guard-unverified-given-up") and len(events(paths, "guard-unverified")) == 1, again)
(paths.station_file(S, "codex") / "fake-threads.json").unlink()   # the conversation can no longer be loaded
LISTING["as"] = ""
err, kind, _ = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
ck("a conversation that was never compared and can no longer be loaded is not replaced by a new one",
   kind is CrewError and "cannot be loaded" in (err or "") and "never compared with the guard's record" in err
   and f"`tares factory revive {S} --fresh`" in err and conversations() == n and not cs.is_alive(paths, S)
   and cs.unverified(paths, S).get("thread") == thread, (err, kind))


class AlwaysFresh(watch_revive.PaceSeam):
    """A pace that sends the watch to a new conversation, as after a resume that failed twice."""

    def stopped(self, paths, station):
        return True

    def tried(self, paths, station):
        return True


real_pace, watch_revive.pace = watch_revive.pace, AlwaysFresh()
said_1 = watch_round(paths, tares)
said_2 = watch_round(paths, tares)
watch_revive.pace = real_pace
ck("nor does the watch start it in a new conversation: it stays down, said once with the way on",
   f"{S} stays down: what its last turn ran could not be compared" in said_1
   and f"`tares factory revive {S} --fresh` does that" in said_1 and "stays down" not in said_2
   and conversations() == n and not cs.is_alive(paths, S), (said_1, said_2))
err, kind, _ = quietly(lifecycle.revive, mk(), paths, [S, "--fresh"], claude_bin="")
note(paths)
pid2 = pid_of(paths)
given = events(paths, "guard-unverified-given-up")
ck("a person's `revive --fresh` gives the conversation up: a new one, the mark gone, and an event that keeps it",
   err is None and alive(pid2) and conversations() == n + 1 and row(paths)["sessionId"] not in ("", thread)
   and cs.unverified(paths, S) == {} and len(given) == 1 and given[0].get("detail") == f"revive --fresh: {thread}",
   (err, given))
until(lambda: cs.turn_state(paths, S) == "done")
LISTING["as"] = "fail"
err, kind, _ = quietly(lifecycle.retire, mk(), paths, [S], claude_bin="")
ck("`retire` goes through when the last turn cannot be compared, and the events keep that it was not",
   err is None and gone(pid2) and row(paths)["status"] == "retired" and cs.unverified(paths, S) == {}
   and len(events(paths, "guard-unverified")) == 2
   and [e.get("detail", "").split(":")[0] for e in events(paths, "guard-unverified-given-up")] == ["revive --fresh", "retire"],
   (err, events(paths, "guard-unverified-given-up")))
LISTING["as"] = ""
Wire.request = real_request

# A station that is not running has nobody to ask: for `restart --fresh` that is a comparison that could not be made.
tares, paths, said, mk = crew()
model_runs("/usr/bin/true")
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
end(pid)   # it ends by itself: no stop of the crew's, so no look at its last turn
n = conversations()
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S, "--fresh"], claude_bin="")
ck("`restart --fresh` of a station that is not running starts no new conversation, and marks the old one as never compared",
   kind is CrewError and "the station was not running, so there was nobody to ask" in (err or "")
   and conversations() == n and not cs.is_alive(paths, S) and cs.unverified(paths, S).get("thread") == thread
   and len(events(paths, "guard-unverified")) == 1, (err, kind))
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S], claude_bin="")
note(paths)
pid2 = pid_of(paths)
ck("`restart` without --fresh starts it in its own conversation, compared first, and the mark is gone",
   err is None and alive(pid2) and row(paths)["sessionId"] == thread and conversations() == n
   and [m for m, _ in calls(pid2)] == ["thread/resume", "turn/start"] and cs.unverified(paths, S) == {}, (err, calls(pid2)))
until(lambda: cs.turn_state(paths, S) == "done")
end(pid2)
tares, paths, said, mk = crew()
fake()
err, _ = start(mk(), prompt="")   # started with no prompt: it has no conversation yet
pid = pid_of(paths)
end(pid)
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S, "--fresh"], claude_bin="")
note(paths)
ck("a station with no conversation yet has nothing to compare: `restart --fresh` starts it",
   err is None and alive(pid_of(paths)) and cs.unverified(paths, S) == {}, (err, kind, row(paths)))
until(lambda: cs.turn_state(paths, S) == "done")
end(pid_of(paths))


class OneTurn:
    """A wire whose conversation has one turn, as given."""

    def __init__(self, turn):
        self.turn = turn

    def request(self, method, params=None):
        return {"data": [self.turn]}


running = {"id": "t-run", "status": "inProgress", "itemsView": "full", "items": [{"type": "userMessage", "id": "m"}]}
try:
    late = cs._audit(OneTurn(running), "thr", paths, S, ended=True)
except cs.NotCompared as e:
    late = str(e)
ck("at a stop, a turn that still runs and shows nothing so far is not a turn that was compared; for the watch's look it is, as far as it goes",
   late == "its newest turn had not ended" and cs._audit(OneTurn(running), "thr", paths, S) == ("", "t-run"), late)
shows = {**running, "items": [{"type": "commandExecution", "id": "exec-x", "command": "ls"}]}
ck("and what such a turn already shows is found all the same",
   cs._audit(OneTurn(shows), "thr", paths, S, ended=True)[0].startswith("a command ran with no run of the guard before it"))

tares, paths, said, mk = crew()
fake(project=1)
err, _ = start(mk())
pid = pid_of(paths)
home_config = paths.station_file(S, "codex") / "config.toml"
brought = None
if err is None:
    with cs.visit(paths, S) as wire:
        brought = cs._brought_along(wire, str(paths.home), {"tares"})
ck("a repository that brings a Codex setup of its own adds nothing to a station: its folder is marked not trusted",
   err is None and alive(pid) and brought == "" and '"untrusted"' in home_config.read_text()
   and '"trusted"' not in home_config.read_text(), (err, brought, home_config.read_text()))
end(pid)
tares, paths, said, mk = crew()
fake(project="force")   # a Codex that reads the repository's setup whatever the mark says
before = len(calls())
err, _ = start(mk())
ck("a Codex that would read it all the same: the start stops before any turn, and says what the folder brings",
   err is not None and "a hook of its own would run next to the guard, from" in err and ".codex/config.toml" in err
   and [m for m, _ in calls()[before:]] == ["thread/start"] and not cs.is_alive(paths, S), (err, calls()[before:]))


class StubWire:
    def __init__(self, hooks=(), servers=(), fail=False):
        self.hooks, self.servers, self.fail = list(hooks), list(servers), fail

    def request(self, method, params=None):
        if self.fail:
            raise WireError("no such method")
        if method == "hooks/list":
            return {"data": [{"cwd": "/w", "hooks": self.hooks}]}
        return {"config": {"mcp_servers": {n: {} for n in self.servers}}}


ck("an MCP server the station was not started with is named; the one it was started with is not",
   cs._brought_along(StubWire(servers=["tares", "repo_server"]), "/w", {"tares"})
   == "its folder brings an MCP server it was not started with: repo_server"
   and cs._brought_along(StubWire(servers=["tares"]), "/w", {"tares"}) == "")
ck("a hook of the conversation itself is not one the folder brought",
   cs._brought_along(StubWire(hooks=[{"source": "sessionFlags", "eventName": "preToolUse"}]), "/w", set()) == "")
ck("an app-server that cannot say what the folder brings is not taken to have nothing",
   "could not be read" in cs._brought_along(StubWire(fail=True), "/w", set()))

rec = [{"turn": "t", "armed": True}, {"turn": "t", "call": "exec-1", "tool": "Bash", "said": "allow"},
       {"turn": "t", "call": "exec-2", "tool": "Bash", "said": "deny"}, {"turn": "t", "call": "exec-3", "tool": "mcp__a__b", "said": "allow"}]
closed = "/bin/zsh -lc 'exec </dev/null\nls'"
ck("`unguarded`: a turn whose calls all have their line, let through and closed, shows nothing",
   cs.unguarded([{"type": "userMessage", "id": "m"}, {"type": "commandExecution", "id": "exec-1", "command": closed},
                 {"type": "mcpToolCall", "id": "exec-3"}, {"type": "agentMessage", "id": "a"}], rec) == [])
ck("`unguarded`: each thing it can show, one sentence each",
   cs.unguarded([{"type": "commandExecution", "id": "exec-9", "command": closed},
                 {"type": "commandExecution", "id": "exec-2", "command": closed},
                 {"type": "commandExecution", "id": "exec-1", "command": "/bin/zsh -lc ls"},
                 {"type": "mcpToolCall", "id": "exec-8"}], rec) == [
       "a command ran with no run of the guard before it (exec-9)", "a command ran though the guard refused it (exec-2)",
       "a command ran without the guard's line that closes its input (exec-1)",
       "an MCP call ran with no run of the guard before it (exec-8)"])
ck("`unguarded`: what a person typed for the conversation, and a second look at a running command, are not the model's calls",
   cs.unguarded([{"type": "commandExecution", "id": "x1", "source": "userShell", "command": "ls"},
                 {"type": "commandExecution", "id": "x2", "source": "unifiedExecInteraction", "command": "ls"}], rec) == [])

tares, paths, said, mk = crew()
paths.stations_dir.mkdir(parents=True, exist_ok=True)
file = paths.station_file(S, cg.RECORD)
small = [{"at": 1, "turn": "old", "armed": True}, {"at": 2, "turn": "new", "armed": True}]
file.write_text("".join(json.dumps(x) + "\n" for x in small))
cs._trim_record(paths, S, "new")
ck("a small record is left as it is", cs._record(paths, S) == small)
big = [{"at": i, "turn": "old", "call": f"exec-{i}", "tool": "Bash", "said": "allow", "pad": "x" * 200} for i in range(2000)]
file.write_text("".join(json.dumps(x) + "\n" for x in big + [{"at": 9, "turn": "new", "armed": True},
                                                           {"at": 9, "turn": "new", "call": "exec-n", "tool": "Bash", "said": "allow"}]))
cs._trim_record(paths, S, "new")
ck("one that grew large keeps the newest turn's lines, which a look may still compare, and drops the rest",
   [x.get("call") for x in cs._record(paths, S)] == [None, "exec-n"] and file.stat().st_size < 1000
   and stat.S_IMODE(file.stat().st_mode) == 0o600, file.stat().st_size)

# ---------------------------------------------------------------------------------------------
print("k. Tares before a prompt (TR-1596)")


def every_call(pid=None) -> list:
    """Every request an app-server took, the read asked of Tares among them, in order."""
    try:
        rows = [json.loads(x) for x in CALLS.read_text().splitlines()]
    except OSError:
        return []
    return [(r["method"], r["params"]) for r in rows if pid is None or r["pid"] == pid]


def as_station(tares) -> list:
    """What reached the fake cell's MCP endpoint with a station's name on it (the crew's own probe has none)."""
    return [e for e in tares.log if e.get("post") and e["path"] == "/mcp" and e.get("x-tares-station")]


fake(turn=0.2)
record = SCRATCH / "k-record.json"
os.environ["FAKE_CODEX_RECORD"] = str(record)
tares, paths, said, mk = crew()
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths).get("sessionId")
rec = json.loads(record.read_text()) if record.exists() else {}
reached = as_station(tares)
ck("a start connects the station to the crew's MCP endpoint as itself: its name, role and crew, and the crew's key",
   err is None and reached and reached[0]["method"] == "initialize" and reached[0]["auth"] == f"Bearer {FAKE_TOKEN}"
   and reached[0]["x-tares-station"] == S and reached[0]["x-tares-role"] == "reviewer"
   and reached[0]["x-tares-crew"] == rec.get("env", {}).get("FACTORY_CREW") != ""
   and "x-tares-parent" not in reached[0] and "x-tares-project" not in reached[0], (err, reached[:1]))
took = every_call(pid)
ck("before the first turn it asks one read of Tares, on the conversation's own connection",
   [m for m, _ in took] == ["thread/start", "mcpServer/tool/call", "turn/start"]
   and took[1][1] == {"server": "tares", "tool": "list_crew", "arguments": {"mine": True}, "threadId": thread}
   and [(e["method"], e.get("tool"), e["auth"]) for e in reached[1:2]] == [("tools/call", "list_crew", f"Bearer {FAKE_TOKEN}")]
   and reached[1]["x-tares-station"] == S, (took, reached[1:2]))
config = took[0][1].get("config", {}).get("mcp_servers", {}).get("tares", {}) if took else {}
ck("the conversation is started with the server marked required and the helper told the key file by its path",
   config.get("required") is True and config.get("url") == tares.url + "/mcp"
   and config.get("http_headers_helper", "").startswith(f"TARES_FACTORY_CRED_FILE={paths.home}/.tares-token tares hook mcp-headers"),
   config)
everywhere = [record.read_text(), CALLS.read_text(), paths.station_file(S, "settings.json").read_text(),
              paths.stations.read_text(), paths.station_file(S, "codex.log").read_text()]
ck("the key is in no argument, environment, settings file, record or request to the app-server",
   all(FAKE_TOKEN not in text for text in everywhere) and len(everywhere[0]) > 100)
until(lambda: cs.turn_state(paths, S) == "done")
tares.world["mcp"] = False
n = len(calls(pid, "turn/start"))
err, kind, _ = quietly(cs.hand, paths, S, "The reviewer has a new pull request.")
ck("a station whose Tares went away gets no next turn, and is told why; it is not stopped for it",
   kind is CrewError and "would work without Tares" in str(err) and "list_crew did not go through" in str(err)
   and len(calls(pid, "turn/start")) == n and cs.is_alive(paths, S) and row(paths)["sessionId"] == thread, err)
tares.world["mcp"] = True
err, kind, _ = quietly(cs.hand, paths, S, "The reviewer has a new pull request.")
ck("once Tares answers again the prompt is taken, in the same conversation",
   err is None and len(calls(pid, "turn/start")) == n + 1 and row(paths)["sessionId"] == thread, err)
until(lambda: cs.turn_state(paths, S) == "done")

# the app-server ends, and Tares is down when the station is started again: Codex refuses to join the conversation
end(pid)
tares.world["mcp"] = False
before = len(every_call())
err, _ = start(mk(), resume=thread)
took = every_call()[before:]
ck("a station started again while Tares is down is not started, and its conversation is not given up for a new one",
   err and "cannot reach an MCP server it is started with" in err and "HTTP 404" in err
   and [m for m, _ in took] == ["thread/resume"] and row(paths).get("sessionId") == thread
   and not cs.is_alive(paths, S) and not events(paths, "conversation-replaced"), (err, took))
ck("what Codex said is shown without the names of its own types", err and "Transport [" not in err and len(err) < 500, err)
tares.world["mcp"] = True
before = len(every_call())
err, _ = start(mk(), resume=thread)
ck("with Tares back it is started in the conversation it had",
   err is None and [m for m, _ in every_call()[before:]] == ["thread/resume", "mcpServer/tool/call", "turn/start"]
   and row(paths)["sessionId"] == thread, (err, every_call()[before:]))
end(pid_of(paths))

tares, paths, said, mk = crew(world={"token": FAKE_TOKEN, "mcp_key": "a-key-the-cell-takes-instead"})
before = len(every_call())
err, _ = start(mk())
took = every_call()[before:]
ck("a key the cell refuses stops the first turn: the station is not started, and the refusal is the cell's own words",
   err and "would work without Tares" in err and "tares.list_crew was refused (error 401: authentication required)" in err
   and [m for m, _ in took] == ["thread/start", "mcpServer/tool/call"] and not row(paths)
   and gone(json.loads(record.read_text())["pid"]), (err, took))

tares, paths, said, mk = crew(world={"mcp": False})
before = len(every_call())
err, _ = start(mk())
ck("an endpoint that does not answer stops the start before any conversation: the server is required",
   err and "cannot reach an MCP server it is started with (tares:" in err and [m for m, _ in every_call()[before:]] == ["thread/start"]
   and not row(paths) and gone(json.loads(record.read_text())["pid"]), (err, every_call()[before:]))
del os.environ["FAKE_CODEX_RECORD"]


class McpWire:
    """An app-server that answers the two questions about a conversation's MCP servers from a script."""

    def __init__(self, statuses=(), call=None, fail=""):
        self.statuses, self.call, self.fail, self.asked = list(statuses), call, fail, []

    def request(self, method, params=None):
        self.asked.append((method, params))
        if self.fail:
            raise WireError(self.fail)
        if method == "mcpServerStatus/list":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else (self.statuses or [None])[0]
            return {"data": [] if status is None else [{"name": params["serverName"], "runtimeStatus": status}]}
        return self.call


nap = []
w = McpWire(["starting", "starting", "connected"])
ck("a server still starting is waited for", cs.mcp_ready(w, "thr", {"tares": {}}, sleep=nap.append) == "" and len(nap) == 2
   and w.asked[0] == ("mcpServerStatus/list", {"threadId": "thr", "serverName": "tares", "detail": "toolsAndAuthOnly"}), (nap, w.asked))
ck("one that never comes up is not waited for longer than the time given",
   cs.mcp_ready(McpWire(["starting"]), "thr", {"tares": {}}, sleep=lambda s: None, wait=0)
   == "its MCP server tares is not connected (starting)")
ck("a failed one, one not listed for the conversation, and an app-server that cannot say are all not ready",
   cs.mcp_ready(McpWire(["failed"]), "thr", {"tares": {}}) == "its MCP server tares is not connected (failed)"
   and cs.mcp_ready(McpWire([None]), "thr", {"tares": {}}) == "its MCP server tares is not connected (not listed for its conversation)"
   and "could not be asked about" in cs.mcp_ready(McpWire(fail="no such method"), "thr", {"tares": {}})
   and cs.mcp_ready(McpWire(), "thr", {}) == "")
check = {"server": "tares", "tool": "list_crew", "arguments": {"mine": True}}
text = lambda t, **kw: {"content": [{"type": "text", "text": t}], **kw}  # noqa: E731
ck("the read goes through for an answer, whatever it lists",
   cs.mcp_answers(McpWire(call=text("[]")), "thr", check) == "" and cs.mcp_answers(McpWire(call=text('[{"station": "error 500"}]')), "thr", check) == "")
ck("a refusal worded as the tool's text is a refusal, as one flagged an error is",
   cs.mcp_answers(McpWire(call=text("error 403: this needs an admin or a crew station")), "thr", check)
   == "tares.list_crew was refused (error 403: this needs an admin or a crew station)"
   and cs.mcp_answers(McpWire(call=text("no", isError=True)), "thr", check) == "tares.list_crew was refused (no)"
   and "did not go through" in cs.mcp_answers(McpWire(fail="tool call failed"), "thr", check)
   and cs.mcp_answers(McpWire(), "thr", None) == "")
long = ("error creating thread: Fatal error: Failed to initialize session: required MCP servers failed to initialize: tares: "
        "handshaking with MCP server failed: Send message error Transport [a::B<c::D<e::F>>] error: unexpected server "
        "response: HTTP 401: unauthorized, when send initialize request: Send message error Transport [a::B<c::D<e::F>>] "
        "error: unexpected server response: HTTP 401: unauthorized, when send initialize request")
ck("Codex's refusal to start a conversation without a required server is told apart from any other failure",
   cs.mcp_refused(WireError(long)) == "tares: handshaking with MCP server failed: Send message error unexpected server response: HTTP 401: unauthorized"
   and cs.mcp_refused(WireError("no rollout found for thread id thr-1")) == "", cs.mcp_refused(WireError(long)))
started = cs._thread_start({"model": "m", "config": {"hooks": {}}}, "/w", {"tares": {"url": "http://c/mcp", "required": True}})
ck("a conversation's start names its MCP servers next to its hooks, and none when the station has none",
   started["config"]["mcp_servers"] == {"tares": {"url": "http://c/mcp", "required": True}} and "hooks" in started["config"]
   and "mcp_servers" not in cs._thread_start({"model": "m"}, "/w")["config"], started)

# ---------------------------------------------------------------------------------------------
print("l. what a station asks goes to the desk, and the person's answer back (TR-1593)")


def codex_step(paths, tares):
    """One round of the watch's Codex step; what it said."""
    env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url, "TARES_FACTORY_MCP_URL": tares.url + "/mcp"}
    os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
    out = io.StringIO()
    with redirect_stderr(out), redirect_stdout(out):
        codex_round.step(paths, env)
    return out.getvalue()


def asking(*commands, **switches):
    """A station whose first turn stops on what it asks: (tares, paths, mk, pid, thread)."""
    tares_, paths_, _, mk_ = crew()
    model_runs(*commands, **switches)
    start(mk_())
    until(lambda: cs.turn_state(paths_, S) == "blocked")
    return tares_, paths_, mk_, pid_of(paths_), row(paths_)["sessionId"]


def answers_given(pid):
    return [p["result"] for _, p in calls(pid, "answer")]


def to_the_person(tares):
    """The requests the fake cell took that only a person may make: an answer to a desk item."""
    return [e["path"] for e in tares.log if e.get("post") and e["path"].rsplit("/", 1)[-1] in ("reply", "allow", "answer")]


marker = SCRATCH / "asked-and-allowed"
tares, paths, mk, pid, thread = asking({"ask": "command", "command": f"/usr/bin/touch {marker}", "reason": "it writes outside"})
said_1 = codex_step(paths, tares)
desk = tares.world["desk"]
kept = codex_asks.kept(paths, S)
key = next(iter(kept), "")
ck("a command the station asks for is put on the desk, once, with the station, the command as it was asked and its folder",
   len(desk) == 1 and desk[0]["station"] == S and desk[0]["options"] == ["Approve", "Decline"] and desk[0]["blocking"] is True
   and f"`/usr/bin/touch {marker}`" in desk[0]["question"] and "exec </dev/null" not in json.dumps(desk[0])
   and "Its reason: it writes outside" in desk[0]["question"] and f"Folder: {paths.home}" in desk[0]["context"]
   and "D1 on the desk" in said_1, (desk, said_1))
ck("the item names the request itself: the conversation, the turn, the call and when it was first asked",
   len(kept) == 1 and kept[key]["state"] == "open" and kept[key]["label"] == "D1"
   and all(text in desk[0]["context"] for text in (f"conversation {thread}", f"turn {kept[key]['ask']['turn']}",
                                                  f"call {kept[key]['ask']['item']}", f"Request {key}."))
   and kept[key]["ask"]["thread"] == thread and isinstance(kept[key]["ask"]["started"], int), (kept, desk[0]["context"]))
ck("what was filed is kept in a file of the owner's alone, and an event records it",
   stat.S_IMODE(paths.station_file(S, codex_asks.KEPT).stat().st_mode) == 0o600
   and [e for e in events(paths, "asked") if "D1 command" in str(e)], events(paths, "asked"))
ck("nothing ran, and the station still waits", not marker.exists() and cs.turn_state(paths, S) == "blocked")
codex_step(paths, tares)
codex_step(paths, tares)
ck("later rounds, each asked the same thing again, file nothing more", len(tares.world["desk"]) == 1
   and list(codex_asks.kept(paths, S)) == [key], tares.world["desk"])
paths.station_file(S, codex_asks.KEPT).unlink()
codex_step(paths, tares)
ck("a watch that lost what it wrote down finds the open item by the request's name, and files no second one",
   len(tares.world["desk"]) == 1 and codex_asks.kept(paths, S)[key]["label"] == "D1", tares.world["desk"])
tares.world["answers"].append({"id": "question:D1", "kind": "question", "choice": None, "said": "Approve",
                               "via": "station", "by": None})
codex_step(paths, tares)
ck("an answer that is not a person's is not one: nothing is given to the station, and nothing runs",
   answers_given(pid) == [] and not marker.exists() and codex_asks.kept(paths, S)[key]["state"] == "open")
tares.world["answers"].clear()
tares.reply("D1", "Approve")
said_2 = codex_step(paths, tares)
until(lambda: cs.turn_state(paths, S) == "done")
ck("the person's Approve lets that one call through: the station is told, the command runs and the turn ends by itself",
   answers_given(pid) == [{"decision": "accept"}] and marker.exists() and cs.turn_state(paths, S) == "done"
   and codex_asks.kept(paths, S)[key]["state"] == "answered" and "was given the answer to D1 (accept)" in said_2
   and [e for e in events(paths, "ask-answered") if "D1 accept" in str(e)], (answers_given(pid), said_2))
ck("the crew itself answered no desk item: only what a person gave was read", to_the_person(tares) == [], to_the_person(tares))
codex_step(paths, tares)
ck("an answer is given once", answers_given(pid) == [{"decision": "accept"}])
end(pid)

for words, why in (("Decline", "the person's Decline"), ("not now, ask me tomorrow", "words that pick neither option")):
    marker.unlink(missing_ok=True)
    tares, paths, mk, pid, thread = asking({"ask": "command", "command": f"/usr/bin/touch {marker}"})
    codex_step(paths, tares)
    tares.reply("D1", words)
    codex_step(paths, tares)
    until(lambda: cs.turn_state(paths, S) == "done")
    ck(f"{why} is a no: the station is told so, nothing runs and its turn goes on",
       answers_given(pid) == [{"decision": "decline"}] and not marker.exists() and cs.turn_state(paths, S) == "done",
       answers_given(pid))
    end(pid)

tares, paths, mk, pid, thread = asking({"ask": "command", "command": f"/usr/bin/touch {marker}"})
codex_step(paths, tares)
tares.world["desk"][0]["state"] = "withdrawn"
codex_step(paths, tares)
until(lambda: cs.turn_state(paths, S) == "done")
ck("an item withdrawn without an answer is a no", answers_given(pid) == [{"decision": "decline"}] and not marker.exists())
end(pid)

# the station stops asking before the person answers: the item goes, and a late answer reaches nothing
tares, paths, mk, pid, thread = asking({"ask": "command", "command": f"/usr/bin/touch {marker}"})
codex_step(paths, tares)
key = next(iter(codex_asks.kept(paths, S)))
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S], claude_bin="")
note(paths)
pid2 = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "blocked")
said_3 = codex_step(paths, tares)
kept = codex_asks.kept(paths, S)
key2 = next(k for k in kept if k != key)
ck("a station started again no longer asks what its old turn asked: that item is withdrawn",
   err is None and pid2 != pid and kept[key]["state"] == "gone" and tares.world["desk"][0]["state"] == "withdrawn"
   and "no longer asks" in tares.world["desk"][0]["reason"] and "D1; the item is withdrawn" in said_3, (err, kept, said_3))
ck("what its new turn asks is another request, with an item of its own",
   key2 != key and kept[key2]["label"] == "D2" and kept[key2]["state"] == "open" and kept[key2]["ask"]["turn"] != kept[key]["ask"]["turn"],
   kept)
tares.reply("D1", "Approve")
codex_step(paths, tares)
ck("a yes given late to the old item is handed to nobody: the new request still waits",
   answers_given(pid2) == [] and not marker.exists() and cs.turn_state(paths, S) == "blocked"
   and codex_asks.kept(paths, S)[key2]["state"] == "open", answers_given(pid2))
ck("an answer kept for one request is not given to another, whoever asks",
   cs.answer(paths, S, key, {"decision": "accept"}) == "gone" and answers_given(pid2) == [] and not marker.exists())
tares.reply("D2", "Approve")
cs.stop(paths, S)
said_4 = codex_step(paths, tares)
ck("a yes for a station that was stopped meanwhile is not given either; the request is closed as gone",
   codex_asks.kept(paths, S)[key2]["state"] == "gone" and not marker.exists() and "no longer asks D2" in said_4, said_4)

# what the station changes, and what it asks in words
out_file = SCRATCH / "outside-the-workspace.txt"
tares, paths, mk, pid, thread = asking({"ask": "file", "path": str(out_file)})
codex_step(paths, tares)
item = tares.world["desk"][0]
ck("a file change is put on the desk with what the change is, as the guard's record of the call holds it",
   "asks to change files outside its workspace" in item["question"] and f"Add File: {out_file}" in item["context"]
   and item["options"] == ["Approve", "Decline"], item)
unseen = {**next(iter(codex_asks.kept(paths, S).values()))["ask"], "item": "exec-the-guard-never-saw"}
ck("without the guard's record of the call, the item says plainly that the crew did not see the change",
   "did not see what the change is" in codex_asks.item_for(paths, S, unseen)["context"])
tares.reply("D1", "Approve")
codex_step(paths, tares)
until(lambda: cs.turn_state(paths, S) == "done")
ck("approved, the change is made", out_file.exists() and answers_given(pid) == [{"decision": "accept"}])
end(pid)

question = {"id": "q1", "header": "Branch", "question": "Which branch do I start from?",
            "options": [{"label": "main", "description": "the default"}, {"label": "release", "description": "the last release"}]}
tares, paths, mk, pid, thread = asking({"ask": "question", "questions": [question]})
codex_step(paths, tares)
item = tares.world["desk"][0]
ck("a question in words goes to the desk with its own options", item["question"].endswith("asks: Which branch do I start from?")
   and item["options"] == ["main", "release"] and "Branch: Which branch" in item["context"], item)
tares.reply("D1", "release, and rebase first")
codex_step(paths, tares)
until(lambda: cs.turn_state(paths, S) == "done")
ck("the person's words are handed to the station as they gave them",
   answers_given(pid) == [{"answers": {"q1": {"answers": ["release, and rebase first"]}}}], answers_given(pid))
end(pid)

two = [{"id": "q1", "header": "Branch", "question": "Which branch?", "options": [{"label": "main", "description": ""}]},
       {"id": "q2", "header": "Region", "question": "Which region?", "options": [{"label": "eu-west", "description": ""},
                                                                              {"label": "us-east", "description": ""}]}]
tares, paths, mk, pid, thread = asking({"ask": "question", "questions": two})
codex_step(paths, tares)
items = tares.world["desk"]
ck("a request with several questions gets one desk item for each, with its own options",
   [d["question"] for d in items] == [f"{S} asks (question 1 of 2): Which branch?", f"{S} asks (question 2 of 2): Which region?"]
   and items[0]["options"] == ["main"] and items[1]["options"] == ["eu-west", "us-east"]
   and "question q1." in items[0]["context"] and "question q2." in items[1]["context"], items)
codex_step(paths, tares)
ck("a later round files none of them again", len(tares.world["desk"]) == 2)
tares.reply("D1", "main, from the last tag")
codex_step(paths, tares)
ck("one answered and one not: nothing is given yet", answers_given(pid) == [] and cs.turn_state(paths, S) == "blocked")
tares.reply("D2", "eu-west")
codex_step(paths, tares)
until(lambda: cs.turn_state(paths, S) == "done")
ck("once each has its answer, each question gets its own",
   answers_given(pid) == [{"answers": {"q1": {"answers": ["main, from the last tag"]}, "q2": {"answers": ["eu-west"]}}}],
   answers_given(pid))
end(pid)
tares, paths, mk, pid, thread = asking({"ask": "question", "questions": two})
codex_step(paths, tares)
cs.stop(paths, S)
codex_step(paths, tares)
ck("a request that is no longer asked has every one of its items withdrawn",
   [d["state"] for d in tares.world["desk"]] == ["withdrawn", "withdrawn"], tares.world["desk"])
q_ask = {"kind": "question", "questions": two}
ck("a single answer is never copied to several questions",
   codex_asks.result_for(q_ask, "main") == {"answers": {"q1": {"answers": [codex_asks.WITHDRAWN]}, "q2": {"answers": [codex_asks.WITHDRAWN]}}}
   and codex_asks.result_for({"kind": "question", "questions": two[:1]}, "main") == {"answers": {"q1": {"answers": ["main"]}}}
   and codex_asks.result_for(q_ask, {"q1": "main", "q2": None})["answers"]["q2"] == {"answers": [codex_asks.WITHDRAWN]})

for asked_, shown in (({"ask": "question", "questions": [{**question, "isSecret": True}]}, "asks for a secret, which the crew does not pass on"),
                      ({"ask": "other"}, "waits on a request the crew cannot answer (item/permissions/requestApproval)")):
    tares, paths, mk, pid, thread = asking(asked_)
    codex_step(paths, tares)
    item = tares.world["desk"][0]
    tares.reply("D1", "Approve")
    codex_step(paths, tares)
    ck(f"what the crew cannot answer ({asked_['ask']}) is on the desk as that, with how to stop the station, and is never answered",
       shown in item["question"] and item["options"] == [] and f"tares factory restart {S}" in item["recommendation"]
       and answers_given(pid) == [] and cs.turn_state(paths, S) == "blocked", (item, answers_given(pid)))
    end(pid)

# a round that cannot look changes nothing
tares, paths, mk, pid, thread = asking({"ask": "command", "command": f"/usr/bin/touch {marker}"})
codex_step(paths, tares)
real_asked = cs.asked
cs.asked = lambda *a, **k: None
codex_step(paths, tares)
cs.asked = real_asked
ck("a station that could not be asked this round keeps its open item: not asking and not answering are not the same",
   tares.world["desk"][0]["state"] == "open" and next(iter(codex_asks.kept(paths, S).values()))["state"] == "open")
held = subprocess.Popen([sys.executable, "-c", "import sys, time\nsys.path.insert(0, sys.argv[1])\n"
                         "from tares.crew import locks\nfrom tares.crew.paths import CrewPaths\n"
                         "locks.station_lock(sys.argv[3], CrewPaths.at(sys.argv[2]))\nprint('held', flush=True)\ntime.sleep(30)\n",
                         str(ROOT), str(paths.home), S], stdout=subprocess.PIPE, text=True)
held.stdout.readline()
tares.reply("D1", "Approve")
codex_step(paths, tares)
ck("a station another command holds is left for the next round", answers_given(pid) == [] and not marker.exists())
held.kill()
held.wait()
codex_step(paths, tares)
until(lambda: cs.turn_state(paths, S) == "done")
ck("and gets its answer then", answers_given(pid) == [{"decision": "accept"}] and marker.exists())
end(pid)

same = {"threadId": "t", "turnId": "u", "itemId": "i", "startedAtMs": 5, "command": "/bin/zsh -lc 'exec </dev/null\ntouch x'", "cwd": "/w"}
a1 = cs.ask_of({"id": 0, "method": "item/commandExecution/requestApproval", "params": same})
a2 = cs.ask_of({"id": 7, "method": "item/commandExecution/requestApproval", "params": dict(same)})
ck("a request has one name on every connection, and another turn, call or time is another request",
   a1["key"] == a2["key"] and a1["command"] == "touch x" and a1["kind"] == "command"
   and len({cs.ask_of({"method": "item/commandExecution/requestApproval", "params": {**same, k: "other"}})["key"]
            for k in ("threadId", "turnId", "itemId", "startedAtMs")} | {a1["key"]}) == 5, (a1, a2))
ck("only Approve, as the person gave it, is a yes",
   [codex_asks.result_for(a1, w)["decision"] for w in ("Approve", " approve. ", "APPROVE", "Approve all of them", "yes", "", None)]
   == ["accept", "accept", "accept", "decline", "decline", "decline", "decline"])
long = cs.ask_of({"method": "item/commandExecution/requestApproval", "params": {**same, "command": "x" * 5000, "reason": "r" * 900}})
body = codex_asks.item_for(paths, S, long, "uc_1")
ck("an item fits what the desk takes, whatever the station asked", len(body["question"]) <= 1000 and len(body["context"]) <= 1500
   and body["project"] == "uc_1" and len(body["recommendation"]) <= 500 and len(body["why"]) <= 1000, {k: len(str(v)) for k, v in body.items()})

fake(turn=0.2, fail="The 'opus' model is not supported when using Codex with a ChatGPT account.")
tares, paths, said, mk = crew()
err, _ = start(mk())
pid = pid_of(paths)
until(lambda: cs.turn_state(paths, S) == "done")
said_f = codex_step(paths, tares)
items = [d for d in tares.world["desk"] if "failed" in d["question"]]
ck("a turn that failed is told of: Codex leaves the station at rest, where it reads like one that finished",
   err is None and cs.turn_state(paths, S) == "done" and cs.failed_turn(paths, S)[1].startswith("The 'opus' model is not supported")
   and f"the last turn of {S} failed: The 'opus' model" in said_f and len(items) == 1 and items[0]["station"] == S
   and f"tares factory restart {S}" in items[0]["recommendation"] and len(events(paths, "turn-failed")) == 1, (said_f, items))
said_f2 = codex_step(paths, tares)
ck("once for a turn", "failed" not in said_f2 and len([d for d in tares.world["desk"] if "failed" in d["question"]]) == 1
   and len(events(paths, "turn-failed")) == 1, said_f2)
end(pid)
tares, paths, said, mk = crew(world={"runtime": "claude_code"})
before = len(tares.log)
codex_step(paths, tares)
ck("a crew on Claude Code is left alone by the Codex round", len(tares.log) == before, tares.log[before:])

# ---------------------------------------------------------------------------------------------
print("m. a conversation that can no longer be loaded (TR-1593)")
LISTING["as"] = ""
tares, paths, said, mk = crew()
model_runs("/usr/bin/true")
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: cs.turn_state(paths, S) == "done")
ck("a turn the crew started is written on the station's row, and is not yet one that was compared",
   row(paths)[cs.LAST_TURN] == row(paths)["codex"]["turn"] and not cs.last_turn_compared(paths, S), row(paths))
watch_round(paths, tares)
ck("the watch's look at the ended turn writes that it was found in order",
   row(paths).get(cs.COMPARED) == row(paths)[cs.LAST_TURN] and cs.last_turn_compared(paths, S), row(paths))
end(pid, signal.SIGKILL)
(paths.station_file(S, "codex") / "fake-threads.json").unlink()   # the conversation can no longer be loaded
n = conversations()
err, kind, out = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
note(paths)
ck("a lost conversation whose last turn was compared is replaced by a new one, with an event, as before",
   err is None and conversations() == n + 1 and row(paths)["sessionId"] != thread and cs.is_alive(paths, S)
   and len(events(paths, "conversation-replaced")) == 1 and cs.unverified(paths, S) == {}, (err, out))
end(pid_of(paths), signal.SIGKILL)

tares, paths, said, mk = crew()
model_runs("/usr/bin/true")
fake(turn="hang", think=0.05, commands=json.dumps(["/usr/bin/true"]))
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
end(pid, signal.SIGKILL)   # it dies in its turn: nobody looked at what the turn ran
(paths.station_file(S, "codex") / "fake-threads.json").unlink()
n = conversations()
err, kind, out = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
ck("a lost conversation whose last turn was never compared is not replaced: the station stays down, and is marked",
   kind is not None and "cannot be loaded" in (err or "") and "never compared with the guard's record" in err
   and f"`tares factory revive {S} --fresh`" in err and conversations() == n and not cs.is_alive(paths, S)
   and cs.unverified(paths, S).get("thread") == thread and row(paths)["sessionId"] == thread
   and not events(paths, "conversation-replaced") and len(events(paths, "guard-unverified")) == 1, (err, kind, row(paths)))
watch_revive._guard_told.discard(S)
said_w = watch_round(paths, tares)
ck("the watch does not start it in a new conversation either", conversations() == n and not cs.is_alive(paths, S)
   and cs.unverified(paths, S).get("thread") == thread, said_w)
err, kind, out = quietly(lifecycle.revive, mk(), paths, [S, "--fresh"], claude_bin="")
note(paths)
ck("a person's `revive --fresh` gives the conversation up and starts the station in a new one",
   err is None and conversations() == n + 1 and cs.is_alive(paths, S) and cs.unverified(paths, S) == {}
   and row(paths)["sessionId"] != thread and len(events(paths, "guard-unverified-given-up")) == 1, (err, out))
end(pid_of(paths), signal.SIGKILL)

print("n. a call whose outcome is not known (TR-1593)")
tares, paths, said, mk = crew()
again = SCRATCH / "ran-again.log"
again.unlink(missing_ok=True)
command = f"echo started >> {again}; {sys.executable} -c 'import time; time.sleep(6)'"   # PATH here has no sleep
fake(turn=0.2, think=0.05, commands=json.dumps([command]))
err, _ = start(mk())
pid, thread = pid_of(paths), row(paths)["sessionId"]
until(lambda: again.exists())
end(pid, signal.SIGKILL)   # the app-server dies while the command runs: the guard let it through, no item says it ended
line = [r for r in cs._record(paths, S) if r.get("call")][-1]
ck("the guard's line for a call it lets through keeps the command as it was asked, and its folder",
   line.get("said") == "allow" and line.get("command") == command and line.get("cwd") == str(paths.home), line)
err, kind, out = quietly(lifecycle.revive, mk(), paths, [S], claude_bin="")
note(paths)
pid2 = pid_of(paths)
rows = cs.unknown(paths, S)
prompt = calls(pid2, "turn/start")[-1][1]["input"][0]["text"] if calls(pid2, "turn/start") else ""
ck("started again in its conversation, the call that was cut off is written down as one whose outcome is not known",
   err is None and row(paths)["sessionId"] == thread and len(rows) == 1 and rows[0]["command"] == command
   and rows[0]["call"] == line["call"] and stat.S_IMODE(paths.station_file(S, cg.UNKNOWN).stat().st_mode) == 0o600
   and len(events(paths, "outcome-unknown")) == 1, (err, rows))
ck("its first prompt names the call, says not to run it again yet, and how to say what was found",
   "Before anything else" in prompt and f"- call {line['call']}: `{command}`" in prompt and "tares factory outcome <call> done" in prompt
   and rows[0].get("told") is True, prompt[-700:])
until(lambda: cs.turn_state(paths, S) == "done")
last = [r for r in cs._record(paths, S) if r.get("call")][-1]
ck("the same command, asked again by the new turn, is refused by the guard and does not run",
   last.get("said") == "deny" and last["call"] != line["call"] and again.read_text().count("started") == 1, (last, again.read_text()))
got = cs.hand(paths, S, "Carry on.")
ck("a later prompt does not tell of it a second time, and the call stays refused",
   "Before anything else" not in calls(pid2, "turn/start")[-1][1]["input"][0]["text"] and len(cs.unknown(paths, S)) == 1)
until(lambda: cs.turn_state(paths, S) == "done")
env_s = {"FACTORY_STATION": S, "FACTORY_CREW_HOME": str(paths.home), "FACTORY_HOME": str(paths.home), "FACTORY_ROLE": "reviewer"}
out_l = io.StringIO()
with redirect_stdout(out_l):
    code_l = cs.cmd_outcome([], env_s)
call_id = line["call"]
bad = [quietly(cs.cmd_outcome, argv, env_s)[0] for argv in ([call_id], [call_id, "maybe", "x"], ["exec-nothing", "done", "x"],
                                                            [call_id, "done", " "], ["1", "done", "a place in the list"])]
ck("`tares factory outcome` lists it by its call id, and takes no answer that says nothing or names a place in the list",
   code_l == 0 and out_l.getvalue().startswith(f"{call_id}  {command}") and all(bad) and "name it by its call id" in bad[4]
   and len(cs.unknown(paths, S)) == 1, (out_l.getvalue(), bad))
out_r = io.StringIO()
with redirect_stdout(out_r):
    cs.cmd_outcome([call_id, "not-done", "the", "log", "has", "one", "line", "and", "no", "process", "is", "left"], env_s)
ck("the station's account of what it found takes the call off the list, and an event keeps the account",
   cs.unknown(paths, S) == [] and "recorded: not-done" in out_r.getvalue()
   and [e for e in events(paths, "outcome-reconciled") if "not-done:" in str(e) and "the log has one line" in str(e)],
   events(paths, "outcome-reconciled"))
cs.hand(paths, S, "Now run it.")
until(lambda: again.read_text().count("started") == 2, 8)
ck("after that the command is let through again", again.read_text().count("started") == 2, again.read_text())
end(pid2, signal.SIGKILL)

turn_items = [{"id": "c1", "status": "completed"}, {"id": "c2", "status": "inProgress"}, {"id": "c4", "status": "failed"}]
rec = [{"turn": "t1", "armed": True}, {"turn": "t1", "call": "c1", "tool": "Bash", "said": "allow", "command": "true"},
       {"turn": "t1", "call": "c2", "tool": "Bash", "said": "allow", "command": "git push", "cwd": "/w"},
       {"turn": "t1", "call": "c3", "tool": "mcp__tares__set_stage", "said": "allow"},
       {"turn": "t1", "call": "c4", "tool": "Bash", "said": "allow", "command": "false"},
       {"turn": "t1", "call": "c5", "tool": "Bash", "said": "deny", "command": "gh pr merge 3"},
       {"turn": "t0", "call": "c0", "tool": "Bash", "said": "allow", "command": "old"}]
ck("unknown is a call of that turn that was let through and has no item saying it ended: not a denied one, not an older turn's",
   [r["call"] for r in cs.unknown_of("t1", turn_items, rec)] == ["c2", "c3"]
   and cs.unknown_of("t1", turn_items, rec)[0] == {"call": "c2", "turn": "t1", "tool": "Bash", "command": "git push", "cwd": "/w", "at": None}
   and [r["call"] for r in cs.unknown_of("t1", None, rec)] == ["c1", "c2", "c3", "c4"] and cs.unknown_of("", None, rec) == [])
tares, paths, said, mk = crew()
paths.stations_dir.mkdir(parents=True, exist_ok=True)
(paths.stations_dir / f"{S}.{cg.UNKNOWN}").write_text(json.dumps([{"call": "c2", "command": "git push  origin HEAD"}]))
env_g = {"FACTORY_STATION": S, "FACTORY_HOME": str(paths.home), "FACTORY_ROLE": "builder"}
ck("the guard refuses that command however it is spaced or wrapped, and no other",
   cg.repeats(env_g, "git push origin HEAD")["call"] == "c2" and cg.repeats(env_g, cg.CLOSE_STDIN + "git push origin HEAD") is not None
   and cg.repeats(env_g, "git push origin main") is None and cg.repeats(env_g, "git status") is None
   and cg.repeats({**env_g, "FACTORY_STATION": "another"}, "git push origin HEAD") is None
   and "tares factory outcome c2 done" in cg.repeat_reason("c2"))
# three calls of unknown outcome, settled one after the other (the review of b52f7f01): each answer lands on the
# call it names, whatever was settled before it
(paths.stations_dir / f"{S}.{cg.UNKNOWN}").write_text(json.dumps(
    [{"call": "exec-a", "command": "git push"}, {"call": "exec-b", "command": "gh pr create"}, {"call": "exec-c", "command": "make deploy"}]))
first_ = cs.reconcile(paths, S, "exec-a", "done", "the branch is on the remote")
second_ = cs.reconcile(paths, S, "exec-c", "not-done", "nothing was deployed")
ck("settling one call does not move the others: the second answer settles the call it names, not the one after it",
   (first_["command"], second_["command"]) == ("git push", "make deploy")
   and [r["call"] for r in cs.unknown(paths, S)] == ["exec-b"], cs.unknown(paths, S))
ck("the prompt and the guard name each call by its id, never by a place in the list",
   "- call exec-b: `gh pr create`" in cs.unknown_note(cs.unknown(paths, S)) and "1." not in cs.unknown_note(cs.unknown(paths, S))
   and "(call exec-b)" in cg.repeat_reason(cg.repeats(env_g, "gh pr create")["call"]))

# ---------------------------------------------------------------------------------------------
print("o. the session is recorded by the watch's rounds and at every stop (TR-1595 wired in by TR-1593)")
from types import SimpleNamespace  # noqa: E402

from tares.connectors import codex as cell_codex  # noqa: E402
from tares.connectors.claude_code import ClaudeCodeConnector  # noqa: E402
from tares.store import Store  # noqa: E402
from tares.crew import codex_record, watch  # noqa: E402


def rows_on_the_cell(tares, source="claude_code"):   # the session source /ingest/codex stores into
    """What the real cell makes of every post the fake cell took: the session store's command rows, by command."""
    s = Store(str(SCRATCH / f"cell-{len(os.listdir(SCRATCH))}.duckdb"))
    conn = ClaudeCodeConnector(SimpleNamespace(name=source, type="agent_session", config={}), s)
    for i in range(0, len(tares.world["ingested"]), 50):
        s.append(conn.map_payload(cell_codex.session_lines(tares.world["ingested"][i:i + 50])))
    sessions = sorted({f.get("sessionId") for f in tares.world["ingested"] if f.get("sessionId")})
    got = s.recorded_commands(source, sessions)
    s.con.close()
    return got


def posted(tares, kind, **match):
    return [f for f in tares.world["ingested"] if f.get("kind") == kind
            and all(f.get(k) == v or (isinstance(f.get("item"), dict) and f["item"].get(k) == v) for k, v in match.items())]


ck("the Codex step comes first in a round, and records before it does anything else",
   watch.STEP_NAMES[0] == "codex" and [n for n, _ in codex_round.PARTS][0] == "record", (watch.STEP_NAMES, codex_round.PARTS))
done_file, slow_file = SCRATCH / "o-done", SCRATCH / "o-slow"
tares, paths, said, mk = crew()
fake(turn=0.2, think=0.05, unread=0, commands=json.dumps(
    [f"echo built > {done_file}", "/bin/sh -c 'echo boom; exit 7'",
     f"echo started > {slow_file}; {sys.executable} -c 'import time; time.sleep(30)'"]))
err, _ = start(mk())
pid, thread = pid_of(paths, S), row(paths)["sessionId"]
until(lambda: slow_file.exists(), 8)
time.sleep(0.3)
said_1 = codex_step(paths, tares)
starts = {f["command"]: f for f in posted(tares, "start")}
ck("a round in the middle of a turn posts what started, with the command as it was asked, and the state",
   err is None and set(starts) >= {f"echo built > {done_file}", "/bin/sh -c 'echo boom; exit 7'"}
   and any(c.endswith("time.sleep(30)'") for c in starts) and posted(tares, "state", state="working")
   and all(f["sessionId"] == thread and f["factory_station"] == S for f in tares.world["ingested"]), (said_1, sorted(starts)))
ended_1 = {f["item"]["id"] for f in posted(tares, "item") if f["item"].get("type") == "commandExecution"}
ck("and the ends of the two commands that have ended, with their real exit codes",
   len(ended_1) == 2 and sorted(f["item"]["exitCode"] for f in posted(tares, "item") if f["item"].get("type") == "commandExecution") == [0, 7],
   ended_1)
n = len(tares.world["ingested"])
codex_step(paths, tares)
codex_step(paths, tares)
ck("later rounds with nothing new post nothing", len(tares.world["ingested"]) == n, tares.world["ingested"][n:])

# the stop: a command still runs when the station is stopped
err, kind, _ = quietly(cs.stop, paths, S)
unknown = posted(tares, "unknown")
ck("a stop records last: the command still running is closed as one whose outcome nobody knows",
   len(unknown) == 1 and starts[next(c for c in starts if c.endswith("time.sleep(30)'"))]["call"] == unknown[0]["call"]
   and "stopped while the command ran" in unknown[0]["why"] and posted(tares, "state", state="ended"), unknown)
got = {c["command"]: c for c in rows_on_the_cell(tares)}
slow = next(c for c in got if c.endswith("time.sleep(30)'"))
ck("on the cell the finished commands keep their end, and the unfinished one is unknown, not a failure and not missing",
   got[f"echo built > {done_file}"]["outcome"] == "ended" and got[f"echo built > {done_file}"]["exit_code"] == 0
   and got["/bin/sh -c 'echo boom; exit 7'"]["exit_code"] == 7 and got[slow]["outcome"] == "unknown"
   and got[slow]["exit_code"] is None and len(got) == 3, got)
n = len(tares.world["ingested"])
codex_step(paths, tares)
ck("a round after the stop sends nothing again", len(tares.world["ingested"]) == n, tares.world["ingested"][n:])

# replay: the cell took a post but its answer was lost, so the recorder sends the same facts again
before = rows_on_the_cell(tares)
tares.world["ingested"].extend(list(tares.world["ingested"]))
ck("the same facts posted twice make the same rows on the cell: no duplicate", rows_on_the_cell(tares) == before)
end(pid)

tares, paths, said, mk = crew()
fake(turn=0.2, think=0.05, unread=0, commands=json.dumps(["echo one", "echo two"]))
start(mk())
pid = pid_of(paths, S)
until(lambda: cs.turn_state(paths, S) == "done")
tares.world["ingest_fail"] = 1   # taken, and the answer lost
codex_step(paths, tares)
codex_step(paths, tares)
calls_once = [f["call"] for f in posted(tares, "start")]
ck("a post whose answer was lost is sent again whole, and the cell keeps each command once",
   len(calls_once) == 4 and len(set(calls_once)) == 2 and sorted(c["command"] for c in rows_on_the_cell(tares)) == ["echo one", "echo two"],
   (calls_once, rows_on_the_cell(tares)))
n = len(tares.world["ingested"])
watch_revive.last = watch_revive.Round()
codex_step(paths, tares)
ck("a watch started again (what was sent is kept on disk) sends nothing twice", len(tares.world["ingested"]) == n)

# restart, retire and crew down all record last
err, kind, _ = quietly(lifecycle.restart, mk(), paths, [S, "Carry on."], claude_bin="")
note(paths)
ck("a restart in the same conversation goes on, and its new turn is recorded by the next round",
   err is None and row(paths)["sessionId"] == thread or err is None, err)
until(lambda: cs.turn_state(paths, S) == "done")
n = len(tares.world["ingested"])
codex_step(paths, tares)
ck("the next round records the new turn's commands", len(posted(tares, "start")) > len(calls_once), tares.world["ingested"][n:])
end(pid_of(paths, S))
tares, paths, said, mk = crew()
# a stop records with the cell its process names, as `tares factory retire` and `crew down` do
os.environ.update(TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp", FACTORY_CREW_HOME=str(paths.home))
fake(turn="hang", think=0.05, unread=0, commands=json.dumps(["echo first", f"{sys.executable} -c 'import time; time.sleep(30)'"]))
start(mk())
until(lambda: any("time.sleep(30)" in str(r.get("command")) for r in cs._record(paths, S)))
time.sleep(0.3)
err, kind, _ = quietly(lifecycle.retire, mk(), paths, [S], claude_bin="")
ck("a retire records last, with no round between: the command it cut off is unknown on the cell",
   err is None and not cs.is_alive(paths, S)
   and sorted(("sleep" if c["command"].endswith("time.sleep(30)'") else c["command"], c["outcome"])
              for c in rows_on_the_cell(tares)) == [("echo first", "ended"), ("sleep", "unknown")], (err, rows_on_the_cell(tares)))
n = len(tares.world["ingested"])
codex_step(paths, tares)
ck("and nothing more is sent for it", len(tares.world["ingested"]) == n, tares.world["ingested"][n:])

tares, paths, said, mk = crew()
os.environ.update(TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp", FACTORY_CREW_HOME=str(paths.home))
fake(turn="hang", think=0.05, unread=0, commands=json.dumps([f"{sys.executable} -c 'import time; time.sleep(30)'"]))
start(mk())
until(lambda: any("time.sleep(30)" in str(r.get("command")) for r in cs._record(paths, S)))
time.sleep(0.3)
tares.world["health"] = True
tares.world["ingest_fail"] = 0
real_post = codex_record._post
codex_record._post = lambda client, found: (_ for _ in ()).throw(CrewError("Tares is down"))
err, kind, _ = quietly(lifecycle.down, mk(), paths, [], claude_bin="")
codex_record._post = real_post
ck("crew down with Tares down, before any round looked at the station: it still ends, and its closing is kept",
   not cs.is_alive(paths, S) and codex_record._load(paths, S).get("closing")
   and [u["command"].endswith("time.sleep(30)'") for u in codex_record._load(paths, S)["untold"]] == [True],
   (err, codex_record._load(paths, S)))
codex_step(paths, tares)
ck("and sent by the next look once Tares answers: the unfinished command is unknown",
   [c["outcome"] for c in rows_on_the_cell(tares)] == ["unknown"] and not codex_record._load(paths, S).get("closing"),
   rows_on_the_cell(tares))

# the same, but the station is started in a new conversation before any look could send the closing (the
# review of b52f7f01): the closing kept from the stop is sent with the old conversation's end, not dropped
tares, paths, said, mk = crew()
os.environ.update(TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp", FACTORY_CREW_HOME=str(paths.home))
fake(turn="hang", think=0.05, unread=0, commands=json.dumps([f"{sys.executable} -c 'import time; time.sleep(30)'"]))
start(mk())
old_thread = row(paths)["sessionId"]
until(lambda: any("time.sleep(30)" in str(r.get("command")) for r in cs._record(paths, S)))
time.sleep(0.3)
codex_record._post = lambda client, found: (_ for _ in ()).throw(CrewError("Tares is down"))
quietly(cs.stop, paths, S)
codex_record._post = real_post
kept_ = codex_record._load(paths, S)
fake(turn=0.2, think=0.05, unread=0, commands=json.dumps(["echo fresh"]))
err, kind, _ = quietly(lifecycle.revive, mk(), paths, [S, "--fresh"], claude_bin="")
note(paths)
until(lambda: cs.turn_state(paths, S) == "done")
codex_step(paths, tares)
cell_rows = rows_on_the_cell(tares)
ck("a stop's closing kept while Tares was down is sent when the station moves to a new conversation: nothing is lost",
   kept_.get("closing") and kept_.get("untold") and err is None and row(paths)["sessionId"] != old_thread
   and sorted((r["session"] == old_thread, "sleep" if "time.sleep(30)" in r["command"] else r["command"], r["outcome"])
              for r in cell_rows) == [(False, "echo fresh", "ended"), (True, "sleep", "unknown")]
   and not codex_record._load(paths, S).get("untold"), (kept_, cell_rows))
end(pid_of(paths))

tares, paths, said, mk = crew(world={"runtime": "claude_code"})
before = len(tares.log)
codex_step(paths, tares)
ck("a crew on Claude Code is left alone: nothing is recorded or asked by the Codex step",
   len(tares.log) == before and tares.world["ingested"] == [], tares.log[before:])

# ---------------------------------------------------------------------------------------------
for pid in STARTED:
    end(pid, signal.SIGKILL)
left = [pid for pid in STARTED if alive(pid)]
ck("no app-server of the test is left running", not left, left)
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
