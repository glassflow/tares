"""`tares factory heavy <timeout-seconds> <command...>`: the port of claude-plugin/bin/factory-heavy.

Runs a heavy command (a docker build, a build across many modules, a scan) under a machine-wide lock,
so one runs at a time, and kills it, with everything it started, once the timeout passes. A Claude
Code Bash timeout does not kill the process.

The lock is the same folder the bash script makes, `<TMPDIR or /tmp>/tares-factory-heavy.lock`, with
a `pid` file and a `what` file, so a bash holder and a Python holder make each other wait. A holder
whose process is gone is cleared. A waiter says who it waits for at once and every minute, and gives
up after an hour (exit 75).

Exit codes: the command's own; 2 for bad usage; 75 after an hour of waiting; 142 when the timeout
killed the command (the bash script's SIGALRM status, kept so callers see the same number); 127 when
the command cannot be started; 130 when interrupted. A timeout of 0 means no limit (as perl's alarm 0
did).

Standard library, plus psutil only when a command has to be killed.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .locks import pid_alive

LOCK_NAME = "tares-factory-heavy.lock"
USAGE = "usage: factory-heavy <timeout-seconds> <command...>"
WAIT_LIMIT = 3600.0     # seconds a waiter gives up after
SAY_EVERY = 60.0        # seconds between "waiting for ..." lines
POLL = 0.25             # seconds between tries at the lock
TIMED_OUT = 142         # 128 + SIGALRM, what the bash script exited with
GRACE = 3.0             # seconds a killed tree gets to stop before it is killed hard


def lock_dir(env: dict) -> str:
    """Where the lock folder lives: TMPDIR, else /tmp (the bash script's choice), else the platform's
    temp folder where /tmp is not a thing."""
    base = env.get("TMPDIR")
    if not base:
        base = tempfile.gettempdir() if sys.platform == "win32" else "/tmp"
    return os.path.join(base, LOCK_NAME)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _holder_gone(lock: str) -> bool:
    """True when the lock names a process that no longer runs. An empty pid file (the holder is between
    mkdir and writing its pid) is not stale."""
    holder = _read(os.path.join(lock, "pid"))
    if not holder:
        return False
    try:
        return not pid_alive(int(holder))
    except ValueError:
        return True


def _write(path: str, text: str) -> None:
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


def _acquire(lock: str, what: str) -> int | None:
    """Take the lock; None when taken, else the exit code to leave with."""
    waited = 0.0
    said = -SAY_EVERY
    start = time.monotonic()
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            pass
        except PermissionError:
            pass  # Windows: the holder is deleting the folder right now
        except OSError as e:
            print(f"factory-heavy: cannot make {lock}: {e}", file=sys.stderr)
            return 1
        if _holder_gone(lock):
            shutil.rmtree(lock, ignore_errors=True)
            continue
        waited = time.monotonic() - start
        if waited >= WAIT_LIMIT:
            print(f"factory-heavy: waited an hour for "
                  f"{_read(os.path.join(lock, 'what')) or 'another heavy command'}; giving up", file=sys.stderr)
            return 75
        if waited - said >= SAY_EVERY:
            said = waited
            print(f"factory-heavy: waiting for "
                  f"{_read(os.path.join(lock, 'what')) or 'another heavy command'} to finish", file=sys.stderr)
        time.sleep(POLL)
    _write(os.path.join(lock, "pid"), f"{os.getpid()}\n")
    _write(os.path.join(lock, "what"), what + "\n")
    return None


def _release(lock: str) -> None:
    for _ in range(5):
        shutil.rmtree(lock, ignore_errors=True)
        if not os.path.isdir(lock):
            return
        time.sleep(0.05)


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the command and everything it started: ask, wait a moment, then kill what is left."""
    try:
        import psutil
    except ImportError:  # psutil is a dependency of tares; this is only a belt
        proc.kill()
        return
    try:
        parent = psutil.Process(proc.pid)
        victims = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=GRACE)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=GRACE)


class _Stopped(BaseException):
    """SIGTERM or SIGHUP reached us: stop the command, release the lock, leave."""


def _on_signal(signum, frame):
    raise _Stopped(128 + signum)


def _status(returncode: int) -> int:
    """The exit code a shell would report: a signal death is 128 + the signal."""
    return 128 - returncode if returncode < 0 else returncode


def cmd_heavy(argv: list[str], env: dict) -> int:
    """argv is `heavy <seconds> <command...>`."""
    args = argv[1:]
    if len(args) < 2 or not re.fullmatch(r"[0-9]+", args[0]):
        print(USAGE, file=sys.stderr)
        return 2
    limit = int(args[0])
    command = args[1:]
    what = f"{' '.join(command)} (station {env.get('FACTORY_STATION') or 'unknown'})"

    lock = lock_dir(env)
    refused = _acquire(lock, what)
    if refused is not None:
        return refused

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, _on_signal)
            except ValueError:  # not the main thread
                pass
    proc = None
    try:
        from . import docker_cleanup   # TR-700: low on disk, clean up Docker first (the lock is held)
        docker_cleanup.before_heavy(env)
        try:
            proc = subprocess.Popen(command, env=env)
        except OSError as e:
            print(f"factory-heavy: cannot run {command[0]}: {e.strerror or e}", file=sys.stderr)
            return 127
        try:
            code = proc.wait(timeout=limit or None)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            print(f"factory-heavy: killed after {limit}s: {' '.join(command)}", file=sys.stderr)
            return TIMED_OUT
        return _status(code)
    except KeyboardInterrupt:
        if proc is not None:
            kill_tree(proc)
        return 130
    except _Stopped as stop:
        if proc is not None:
            kill_tree(proc)
        return stop.args[0]
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, TypeError):
                pass
        _release(lock)
