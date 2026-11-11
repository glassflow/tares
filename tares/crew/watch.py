"""The watch: a detached background loop, its pid file and its stop file (TR-958).

Ports these functions of `claude-plugin/bin/factory`: cmd_watch (the loop skeleton), nap, watch_pid, start_watch
and stop_watch. What the loop does each round (revive a stopped station, regrant, wake an idle one, pace, move onto
an updated plugin) is not here: those steps are separate tickets, and each registers itself with `register` when it
lands (revive, regrant and idle_wake have: `tares/crew/watch_revive.py`, `regrant.py`, `idle_wake.py`; the Claude Code
move of the updates step has: `cc_update.py`). Every step not yet registered falls back to nothing. The Python watch is
the default (TR-1069); `TARES_FACTORY_WATCH=bash` or the bash engine (`TARES_FACTORY_ENGINE=bash`, `"engine": "bash"` in
crew.json) keeps the bash one (on Windows there is no bash, so it is always Python there).

Standard library plus the crew modules and psutil (no duckdb, fastapi or daemon).

How the watch runs
  * start() launches it detached, so closing the terminal or quitting the app does not end it: a new session on
    Mac and Linux (`start_new_session`), `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` with `pythonw.exe` (no
    console window) on Windows. It gets the environment, and so the PATH, of the process that started it.
  * The pid file `watch.pid` holds the pid alone, as bash's does, so the bash `watch_pid`, `crew down` and
    doctor read it. A second file, `watch.pid.meta`, holds `<pid> <create time> <engine>` on one line: the
    process's create time (seconds since the epoch) and the engine that runs it (`python` or `bash`). The meta
    file is used only while its pid is the pid file's; a pid file with no meta (bash's) has no create time and
    no engine.
  * A process is the watch only when its pid is alive, its command line has `crew watch`, and its create time is
    the one in the meta file (when there is one). After a reboot a pid is reused by some other process: the
    create time differs, and the pid file is ignored.
  * stop() asks the loop to stop with a stop file (`watch.stop`) instead of a signal, which means the same on
    every platform. The loop looks for the file at the start of each round and on every tick of its nap. Only when
    the process is still there after a timeout is it terminated, and killed if it ignores that. A watch that does
    not read the stop file (bash's, or a pid file with no engine line) is terminated at once, as before.

Differences from bash
  * The bash watch is stopped with `kill`; this one first gets a chance to finish its round and clean up.
  * A meta file next to the pid file has the create time and the engine.
  * The loop leaves on a stop file, and on SIGTERM where the platform has it, removing its pid file and stop file.
  * Only the steps registered so far run; see above.
  * A round that finds the engine setting turned to bash runs the bash watch in its place (`follow_engine`, TR-1069).
  * When the kit step sees the plugin updated, `restart_self` runs the watch again on the new code: `os.execve` where
    the platform has it (same pid, same pid file), a new detached watch on Windows (untested there, TR-946).

Untested on real Windows (TR-946 is deferred): the creation flags, `pythonw.exe` and the launcher case below.
On Windows a venv's `python.exe` starts the real interpreter as a child, so `Popen.pid` is the launcher's; the
loop then writes its own pid over the launcher's in the pid file (`claim_pidfile`, tried at the start of every
round, in case it ran before start() had written the file).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time

import psutil

from . import locks, state
from .errors import CrewError
from .paths import ME, CrewPaths

PID_FILE = "watch.pid"
META_FILE = "watch.pid.meta"
LOG_FILE = "watch.log"
STOP_FILE = "watch.stop"

DEFAULT_INTERVAL = 180      # seconds between rounds, as bash's FACTORY_WATCH_INTERVAL
DEFAULT_POLL = 5.0          # seconds between ticks of a nap, as bash's FACTORY_ASK_POLL
MIN_POLL = 0.05             # a loop that polls a file must pause: see the Windows replace retries in state
DEFAULT_STOP_TIMEOUT = 30.0  # seconds a stop file may wait before the process is terminated
TERMINATE_WAIT = 3.0        # seconds a terminated watch has before it is killed
IDENTITY_SLACK = 1.0        # seconds the create time in the pid file may differ from the process's own

WATCH_USAGE = "usage: factory crew watch [--once] [--interval <seconds>]"

# What a round does, in this order. A later ticket registers the function for its name.
STEP_NAMES = ("revive", "regrant", "idle_wake", "pace", "updates", "docker")
# What a nap does on each of its ticks (the readings the Crew page asks for, TR-967).
TICK_NAME = "readings"
# What a round does before its first step: bash reads the pace at the top of a round (TR-1157).
ROUND_NAME = "round_start"

_STEPS: dict = {}


def register(name: str, fn) -> None:
    """Give the watch the function for a step: fn(paths) is called once a round (the readings step: once a
    tick of the nap). A name that is not a step is refused, so a typo does not silently do nothing."""
    if name not in STEP_NAMES and name not in (TICK_NAME, ROUND_NAME):
        raise ValueError(f"no watch step called {name!r}")
    _STEPS[name] = fn


def unregister(name: str) -> None:
    _STEPS.pop(name, None)


def _idle_wake_step(paths: CrewPaths) -> None:
    from . import idle_wake   # here, not at the top: lifecycle imports this module and idle_wake imports lifecycle
    idle_wake.step(paths)


register("idle_wake", _idle_wake_step)   # TR-961


def _updates_step(paths: CrewPaths) -> None:
    """The kit part of the round (TR-965), then the Claude Code part: bash moves one station in a round, onto a new
    kit or a new Claude Code, and the kit comes first. A kit part that fails does not stop the Claude Code part."""
    from . import cc_update, kit   # here, not at the top: they import crew modules that import this one
    upgraded = False
    try:
        upgraded = kit.step(paths)
    except Exception as e:
        _say(f"the kit step failed: {type(e).__name__}: {e}")
    cc_update.step(paths, kit_upgraded=upgraded)


register("updates", _updates_step)  # TR-963


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr, flush=True)


def _note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------- the pid file ----------


def _num(text: str):
    try:
        return float(text)
    except ValueError:
        return None


def read_pidfile(paths: CrewPaths):
    """(pid, create_time or None, engine or "") from the pid file and its meta file; None when there is no
    usable pid file. A meta file that names another pid (bash rewrote the pid file) is ignored."""
    try:
        lines = state.read_text(paths.home / PID_FILE).split()
    except OSError:
        return None
    if not lines:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    try:
        meta = state.read_text(paths.home / META_FILE).split()
    except OSError:
        meta = []
    if len(meta) >= 3 and meta[0] == str(pid):
        return pid, _num(meta[1]), meta[2]
    return pid, None, ""


def write_pidfile(paths: CrewPaths, pid: int, created: float, engine: str) -> None:
    """The meta file first, so a reader that sees the new pid also finds what goes with it."""
    state.atomic_write_text(paths.home / META_FILE, f"{pid} {created:.3f} {engine}\n")
    state.atomic_write_text(paths.home / PID_FILE, f"{pid}\n")


def remove_pidfile(paths: CrewPaths) -> None:
    _unlink(paths.home / PID_FILE)
    _unlink(paths.home / META_FILE)


def _unlink(path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def is_the_watch(pid: int, created) -> bool:
    """Does this pid name the watch we started: alive, a command line with `crew watch`, and (when the pid file
    has one) the create time we wrote down. A reused pid fails the last two."""
    if not locks.pid_alive(pid):
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if "crew watch" not in " ".join(proc.cmdline()):
            return False
        return created is None or abs(proc.create_time() - created) <= IDENTITY_SLACK
    except (psutil.Error, OSError, SystemError):   # psutil on macOS can leak SystemError
        return False


def running_pid(paths: CrewPaths):
    """The pid of the background watch when it runs, else None."""
    row = read_pidfile(paths)
    if row is None or not is_the_watch(row[0], row[1]):
        return None
    return row[0]


def claim_pidfile(paths: CrewPaths) -> None:
    """If the pid file names this process's parent (a Windows venv launcher), name this process instead."""
    row = read_pidfile(paths)
    if row is None or row[0] == os.getpid():
        return
    try:
        me = psutil.Process()
        if me.ppid() != row[0]:
            return
        write_pidfile(paths, me.pid, me.create_time(), row[2] or "python")
    except (psutil.Error, OSError, SystemError):   # psutil on macOS can leak SystemError
        return


def stop_requested(paths: CrewPaths) -> bool:
    return (paths.home / STOP_FILE).exists()


# ---------- start and stop ----------


def _watch_command(env: dict) -> tuple:
    """(argv, engine): the command that runs the watch. Python unless the bash engine or TARES_FACTORY_WATCH=bash
    asks for bash, and always Python where there is no bash (Windows, or a system without it), whatever the engine
    setting says."""
    from . import dispatch   # the one rule for which engine runs `crew watch`; imported late, dispatch imports us
    if os.name == "nt" or dispatch.bash_exe(env) is None or dispatch.route(["crew", "watch"], env)[0] == "python":
        exe = sys.executable
        if os.name == "nt":
            gui = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(gui):
                exe = gui   # no console window
        return [exe, "-m", "tares.crew", "crew", "watch"], "python"
    from ..cli import plugin_dir   # as `python -m tares.crew` does
    return ["bash", str(plugin_dir() / "bin" / "factory"), "crew", "watch"], "bash"


def _spawn(paths: CrewPaths, argv: list, env: dict) -> tuple:
    """Run `argv` detached from this process, its output in the watch log: (pid, create time)."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    with open(paths.home / LOG_FILE, "ab") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env=env, **kwargs)
    try:
        return proc.pid, psutil.Process(proc.pid).create_time()
    except (psutil.Error, OSError, SystemError):   # psutil on macOS can leak SystemError
        raise CrewError("the watch stopped as it started; see " + paths.shown(LOG_FILE))


def follow_engine(paths: CrewPaths, interval: float, env: dict | None = None) -> None:
    """A running Python watch whose engine setting has turned to bash (`"engine": "bash"` in crew.json, TR-1069) runs
    the bash watch instead, so the switch back needs no `crew down`. Looked at at the top of every round. The pid file
    is rewritten to name bash first: stop() ends a bash watch with a terminate, not the stop file. Does not return
    when it switches. An environment variable cannot change under a running process; that one takes a new watch."""
    env = dict(os.environ if env is None else env)
    if _watch_command(env)[1] != "bash":
        return
    _say("the engine setting is now bash; the bash watch takes over")
    try:
        me = psutil.Process()
        write_pidfile(paths, me.pid, me.create_time(), "bash")
    except (psutil.Error, OSError, SystemError):   # the pid file then keeps naming python; stop() terminates it anyway
        pass
    try:
        restart_self(paths, interval, env)
    except OSError as e:   # the exec failed: this watch is still here, so it stays the Python watch and says so
        _say(f"could not start the bash watch ({e}); staying on the Python watch")
        with contextlib.suppress(psutil.Error, OSError, SystemError):
            write_pidfile(paths, os.getpid(), psutil.Process().create_time(), "python")


def restart_self(paths: CrewPaths, interval: float, env: dict | None = None) -> None:
    """The plugin was updated: run the watch again on the new code (bash: `exec watch --interval`). Where the platform
    has exec, this process becomes the new watch (same pid, same pid file). On Windows there is none: a new watch is
    started, takes over the pid file, and this one leaves. Does not return. Untested on real Windows (TR-946)."""
    env = dict(os.environ if env is None else env)
    argv, engine = _watch_command(env)
    argv = [*argv, "--interval", f"{interval:g}"]
    if os.name == "nt":
        pid, created = _spawn(paths, argv, env)
        write_pidfile(paths, pid, created, engine)   # the pid file names the new watch, so this one's exit leaves it alone
        raise SystemExit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    # execve wants a path: the bash watch's argv[0] is "bash", so find it on the PATH the new watch will have
    os.execve(shutil.which(argv[0], path=env.get("PATH")) or argv[0], argv, env)


def start(paths: CrewPaths, env: dict | None = None, *, note=None) -> None:
    """Start the watch in the background, unless it already runs (or FACTORY_NO_WATCH is set)."""
    env = dict(os.environ if env is None else env)
    note = note or _note
    if env.get("FACTORY_NO_WATCH"):
        return
    pid = running_pid(paths)
    if pid is not None:
        note(f"  ✓ the watch keeps the crew alive (already running, pid {pid})")
        return
    argv, engine = _watch_command(env)
    _unlink(paths.home / STOP_FILE)   # a stop asked for the watch that ran before is not for this one
    proc_pid, created = _spawn(paths, argv, env)
    write_pidfile(paths, proc_pid, created, engine)
    note(f"  ✓ started the watch, which revives any station that stops (log: {paths.shown(LOG_FILE)})")


def _gone(pid: int, created) -> bool:
    return not is_the_watch(pid, created)


def _wait_gone(pid: int, created, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while True:
        if _gone(pid, created):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def stop(paths: CrewPaths, say, timeout: float | None = None, env: dict | None = None) -> None:
    """Stop the background watch and drop its pid file. A pid file that names nothing running is just removed.
    A Python watch is asked with the stop file and given `timeout` seconds (FACTORY_WATCH_STOP_TIMEOUT, else 30)
    to finish its round; then it is terminated, and killed if it is still there. Any other watch is terminated at
    once. `env` is where FACTORY_WATCH_STOP_TIMEOUT is read (default os.environ). A process that is already gone,
    or that may not be stopped, is left as it is."""
    row = read_pidfile(paths)
    if row is None or not is_the_watch(row[0], row[1]):
        remove_pidfile(paths)
        return
    pid, created, engine = row
    if timeout is None:
        try:
            timeout = float((os.environ if env is None else env).get("FACTORY_WATCH_STOP_TIMEOUT", DEFAULT_STOP_TIMEOUT))
        except ValueError:
            timeout = DEFAULT_STOP_TIMEOUT
    stopped = False
    if engine == "python":
        state.atomic_write_text(paths.home / STOP_FILE, "stop\n")
        stopped = _wait_gone(pid, created, timeout)
    if not stopped:
        try:
            proc = psutil.Process(pid)
            # On Windows the pid may be a venv launcher whose child is the real loop: end both.
            kids = proc.children(recursive=True) if os.name == "nt" else []
            for p in [proc, *kids]:
                with contextlib.suppress(psutil.Error, OSError):
                    p.terminate()
            if not _wait_gone(pid, created, TERMINATE_WAIT):
                for p in [proc, *kids]:
                    with contextlib.suppress(psutil.Error, OSError):
                        p.kill()
                _wait_gone(pid, created, TERMINATE_WAIT)
        except (psutil.Error, OSError, SystemError):   # psutil on macOS can leak SystemError
            pass
    if not _gone(pid, created):   # refused (access denied): it runs, so its pid file stays and we say so
        say(f"could not stop the watch (pid {pid}); its pid file is left as it is")
        return
    _unlink(paths.home / STOP_FILE)
    remove_pidfile(paths)
    say("stopped the watch")


# ---------- the loop ----------


def run_steps(paths: CrewPaths) -> None:
    """One round: each registered step in order. A step that raises is reported and the round goes on, so one
    broken step does not end the watch."""
    start = _STEPS.get(ROUND_NAME)
    if start is not None:
        try:
            start(paths)
        except Exception as e:
            _say(f"the {ROUND_NAME} step failed: {type(e).__name__}: {e}")
    for name in STEP_NAMES:
        fn = _STEPS.get(name)
        if fn is None:
            continue
        try:
            fn(paths)
        except Exception as e:   # a bug in a step must not end the watch
            _say(f"the {name} step failed: {type(e).__name__}: {e}")


def nap(paths: CrewPaths, seconds: float, poll: float = DEFAULT_POLL, *, sleep=time.sleep) -> bool:
    """Wait `seconds` out, serving readings and looking for the stop file on every tick. True when asked to stop."""
    poll = max(poll, MIN_POLL)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_requested(paths):
            return True
        fn = _STEPS.get(TICK_NAME)
        if fn is not None:
            try:
                fn(paths)
            except Exception as e:
                _say(f"the {TICK_NAME} step failed: {type(e).__name__}: {e}")
        sleep(min(poll, max(end - time.monotonic(), 0)))
    return stop_requested(paths)


class Loop:
    """How the running loop was asked to go (bash's ONCE and INTERVAL), for the steps that need it: the kit step
    restarts the watch on new code after the same interval, and never in a --once run."""

    def __init__(self):
        self.once = False
        self.interval = float(DEFAULT_INTERVAL)


LOOP = Loop()


def watch(paths: CrewPaths, interval: float, once: bool = False, poll: float = DEFAULT_POLL) -> None:
    """The loop: a round of steps, then a nap, until a stop file shows or SIGTERM comes."""
    LOOP.once, LOOP.interval = once, interval
    if once:
        run_steps(paths)
        return
    try:
        while not stop_requested(paths):
            claim_pidfile(paths)   # every round: the first try may have run before start() wrote the file
            follow_engine(paths, interval)
            run_steps(paths)
            if nap(paths, interval, poll):
                break
        _say("the watch was asked to stop; leaving")
    finally:
        row = read_pidfile(paths)
        if row is None or row[0] == os.getpid():   # a stop file or pid file that is another watch's stays
            _unlink(paths.home / STOP_FILE)
        if row is not None and row[0] == os.getpid():
            remove_pidfile(paths)


def _on_term(signum, frame):
    raise SystemExit(0)   # leave through the loop's finally, so the pid file goes


def cmd_watch(argv: list, env: dict) -> int:
    """factory [crew] watch [--once] [--interval <seconds>]"""
    args = list(argv)
    if args and args[0] == "crew":
        args = args[1:]
    if args and args[0] == "watch":
        args = args[1:]
    paths = CrewPaths.from_env()
    state.require_crew(paths)
    state.merge_crew_defaults(paths)   # bash primes before every command but a local one: the crew's defaults are filled in
    once = False
    try:
        interval = float(env.get("FACTORY_WATCH_INTERVAL") or DEFAULT_INTERVAL)
    except ValueError:
        interval = DEFAULT_INTERVAL
    while args:
        if args[0] == "--once":
            once = True
            args = args[1:]
        elif args[0] == "--interval" and len(args) > 1:
            try:
                interval = float(args[1])
            except ValueError:
                raise CrewError(f"{WATCH_USAGE}: {args[1]!r} is not a number of seconds")
            args = args[2:]
        else:
            raise CrewError(f"unknown option {args[0]}")
    try:
        poll = float(env.get("FACTORY_ASK_POLL") or DEFAULT_POLL)
    except ValueError:
        poll = DEFAULT_POLL
    if not once:
        what = ("it revives stations that stop, and gives one a permission you allowed in Tares" if _STEPS
                else "it has no steps yet, so it does nothing but run")
        if hasattr(signal, "SIGTERM"):
            with contextlib.suppress(ValueError):   # not the main thread
                signal.signal(signal.SIGTERM, _on_term)
        _say(f"watching the crew every {interval:g}s ({what}); Ctrl-C to stop")   # after the handler: the banner means it
    try:
        watch(paths, interval, once, poll)
    except KeyboardInterrupt:
        pass
    return 0


# ---------- the steps that have landed ----------

from . import regrant as _regrant  # noqa: E402  (after the registry above; regrant imports lifecycle, which imports this module)
from . import watch_revive   # noqa: E402  (TR-959: revive a stopped station, restart a stuck one)

register("revive", watch_revive.step)
register("regrant", _regrant.step)   # TR-960: restart a station between turns when its grants change

from . import pace as _pace  # noqa: E402

watch_revive.pace = _pace.Seam()   # TR-962: the revive loop asks the real pace, not the inert stand-in
register("pace", _pace.step)
register(ROUND_NAME, _pace.round_start)   # TR-1157: the pace is read once, at the top of the round

from . import readings as _readings  # noqa: E402

register("readings", _readings.step)   # TR-1163: the Crew page's asks and the plan's limits, on every tick of a nap

from . import docker_cleanup as _docker_cleanup  # noqa: E402

register("docker", _docker_cleanup.step)   # TR-700: Docker's leftovers, once a day, never during a heavy command
