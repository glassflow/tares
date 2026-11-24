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
 f. the seam for Claude Code: each question goes to `claude.py` as it was asked, and a Claude Code crew
    without `claude` is still told so.

Unix only: on Windows the test says so and passes.

Run: python tests/test_crew_codex_station.py
"""
import io
import json
import os
import shutil
import signal
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
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import psutil  # noqa: E402

from tares.crew import claude, cc_update, idle_wake, lifecycle, pace, proc, regrant, state, watch_revive  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import ClaudeNotFoundError, CrewError, NotBetweenTurnsError  # noqa: E402
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
        c = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url}, say=said.append)
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
    return [(r["method"], r["params"]) for r in rows if (pid is None or r["pid"] == pid) and (method is None or r["method"] == method)]


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
ck("a prompt to a resting station is one more turn on its conversation: no new one, nothing loaded again",
   got["thread"] == thread and not got["replaced"] and [m for m, _ in took] == ["thread/start", "turn/start", "turn/start"]
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
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url)
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
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url)
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
env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url}
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url)
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
print("f. the seam for Claude Code")
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
for pid in STARTED:
    end(pid, signal.SIGKILL)
left = [pid for pid in STARTED if alive(pid)]
ck("no app-server of the test is left running", not left, left)
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
