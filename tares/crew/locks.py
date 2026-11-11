"""The per-station lock, the same protocol as bash's station_lock, so a bash command and a Python one
refuse each other.

The lock is a folder, `<crew home>/stations/<station>.lock`, made with mkdir (atomic everywhere). The
holder writes its process id into `<lock>/pid`. A second command that finds the folder reads the pid:
a live holder means refuse; no pid yet may be a command that has only just made the folder, so wait up
to 10 x 0.2 s; otherwise the lock is stale and is cleared under a second lock, `<lock>.takeover`, and
only if it is still the one judged stale. A takeover folder left for over five seconds (25 x 0.2 s)
was left by a killed command and is removed. This is not a file lock library: the folder and the pid
file are what bash reads.
"""
import atexit
import contextlib
import os
import time

from .errors import StationLockedError
from .paths import CrewPaths

STEP = 0.2          # seconds between tries, as bash's sleep 0.2
PID_GRACE_TRIES = 10
TAKEOVER_TRIES = 25

_HELD = {}           # lock folder -> pid of the process that took it
_REGISTERED = False


def _is_windows() -> bool:
    return os.name == "nt"


def _pid_alive_windows(pid: int) -> bool:
    """Ask Windows whether the process still runs. Never os.kill(pid, 0): on Windows that terminates it."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # no such process is "invalid parameter"; a process we may not open is alive
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Does a process with this id run. A process we may not signal still runs."""
    if pid <= 0 or pid > 0x7FFFFFFF:
        # Not a process id (a corrupt pid file): no such process, and os.kill would raise OverflowError.
        return False
    if _is_windows():
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _holder_alive(holder: str) -> bool:
    try:
        pid = int(holder)
    except ValueError:
        return False
    return pid_alive(pid)


def _read_pid(lock) -> str:
    """`$(cat <lock>/pid)`: the file's text without trailing newlines; "" when there is none."""
    try:
        with open(os.path.join(lock, "pid"), encoding="utf-8", errors="replace", newline="\n") as f:
            return f.read().rstrip("\n")
    except OSError:
        return ""


def _rm(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _rmdir(path) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def _mkdir(path) -> bool:
    """mkdir, True when this call made it. A folder that exists, or (Windows, while another process is
    deleting it) is refused with PermissionError, is "someone else's"; any other failure is real."""
    try:
        os.mkdir(path)
        return True
    except FileExistsError:
        return False
    except PermissionError:
        if _is_windows():
            return False
        raise


def release_locks() -> None:
    """Release every lock this process holds (bash's release_locks, run at exit): the pid file first, then the folder."""
    me = os.getpid()
    for lock, owner in list(_HELD.items()):
        if owner != me:
            continue    # a forked child never releases its parent's lock
        _rm(os.path.join(lock, "pid"))
        _rmdir(lock)
        _HELD.pop(lock, None)


def release_for_exec() -> list[str]:
    """Release every lock this process holds, because exec runs no atexit, and return the folders let go, so a failed
    exec can take them back with retake_locks."""
    me = os.getpid()
    held = [lock for lock, owner in _HELD.items() if owner == me]
    release_locks()
    return held


def retake_locks(held: list[str]) -> None:
    """Take back the locks release_for_exec let go, after an exec that failed and left this process running. A lock
    another command took meanwhile stays theirs."""
    me = os.getpid()
    for lock in held:
        if _mkdir(lock):
            with open(os.path.join(lock, "pid"), "w", encoding="utf-8", newline="\n") as f:
                f.write(f"{me}\n")
            _HELD[lock] = me


def release_lock(station: str, paths=None) -> None:
    """Release one station's lock if this process holds it."""
    paths = paths or CrewPaths.from_env()
    lock = str(paths.lock_dir(station))
    if _HELD.get(lock) == os.getpid():
        _rm(os.path.join(lock, "pid"))
        _rmdir(lock)
        _HELD.pop(lock, None)


def station_lock(station: str, paths=None) -> None:
    """Take the station's lock, held until this process exits (or release_lock). Raises StationLockedError
    when another live command holds it. Taking a lock this process already holds does nothing."""
    global _REGISTERED
    paths = paths or CrewPaths.from_env()
    lock = str(paths.lock_dir(station))
    me = os.getpid()
    if _HELD.get(lock) == me:
        return
    paths.stations_dir.mkdir(parents=True, exist_ok=True)
    tries = takes = 0
    while not _mkdir(lock):
        holder = _read_pid(lock)
        if holder and _holder_alive(holder):
            raise StationLockedError(station, holder)
        # No pid yet may be a command that has only just taken the lock: give it a moment before calling it stale.
        if not holder and tries < PID_GRACE_TRIES:
            tries += 1
            time.sleep(STEP)
            continue
        # Clear a stale lock only under a second one, and only if it is still the one judged stale.
        # It is held for an instant, so one still there after five seconds was left by a killed command.
        if _mkdir(lock + ".takeover"):
            if _read_pid(lock) == holder:
                _rm(os.path.join(lock, "pid"))
                _rmdir(lock)
            _rmdir(lock + ".takeover")
        elif takes >= TAKEOVER_TRIES:
            _rmdir(lock + ".takeover")
            takes = 0
        else:
            takes += 1
            time.sleep(STEP)
    with open(os.path.join(lock, "pid"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{me}\n")
    _HELD[lock] = me
    if not _REGISTERED:
        atexit.register(release_locks)
        _REGISTERED = True


@contextlib.contextmanager
def held_station_lock(station: str, paths=None):
    """`with held_station_lock("crew-reviewer"):` takes the lock and lets it go at the end of the block
    (a lock this process already held stays held)."""
    paths = paths or CrewPaths.from_env()
    lock = str(paths.lock_dir(station))
    already = _HELD.get(lock) == os.getpid()
    station_lock(station, paths)
    try:
        yield
    finally:
        if not already:
            release_lock(station, paths)
