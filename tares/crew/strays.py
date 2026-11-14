"""Processes the crew does not own (TR-1332): reported, never stopped.

`tares factory doctor`, `crew status` and the crew watch use this module to find:

  * Claude processes the crew did not start: a station session left running after the station was retired,
    a second background service (`claude daemon run`), a background session nobody owns;
  * on Windows, a background service that runs in session 0 (a service started from an SSH login) or in
    another session than the crew watch: its stations cannot use the person's GitHub login;
  * orphaned crew processes: made by the crew (its environment names the crew, or the command line or the
    folder is inside a crew worktree root), whose parent is gone and that are older than an hour.

Nothing here stops, signals or ends a process, and nothing takes a lock: the answer is a list for a person to
read. (A test checks that this file holds no call that could.) Standard library and psutil only.

Test hook: with TARES_FAKE_STRAYS_DIR (or TARES_FAKE_PS_DIR) set, the process table is `<dir>/ps-table.json` (a list of Proc rows as
objects: pid, ppid, started, cmdline, session, cwd, crew); a folder without it gives an empty table, so a
suite never reads the machine's real processes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

from . import state
from .paths import ME, CrewPaths

MIN_AGE = 3600            # seconds an orphan must be older than before it is reported
STEP_EVERY = 600          # seconds between two looks of the watch
STATE_FILE = "strays.json"
_LAST: dict = {}       # crew home -> when the watch last looked, for a crew that has no state file
SHOW_MAX = 10             # strays a doctor message lists before "and N more"
CMD_MAX = 160             # characters of a command line a report line shows
TABLE_FILE = "ps-table.json"

SESSION_FIX = ("Claude's background service was started from an SSH login (session 0); stations there can't use "
               "your GitHub login. Stop it from that SSH login (`claude daemon stop --any`) and start the crew "
               "again from the desktop.")
CLAUDE_FIX = ("these are not the crew's; stop them yourself if they are not yours "
              "(claude daemon stop for a second service)")
ORPHAN_FIX = "stop them yourself after checking they are not yours"

# Same pattern as cc_update._SPARE: a pre-warmed spare session.
_SPARE = re.compile(r"bg-pty-host.*--bg-spare.*\.claim\.sock")
_SHORT_CALLS = ("agents", "--version", "plugin")
_REAPERS = ("systemd", "init", "launchd", "subreaper")


@dataclass
class Proc:
    pid: int
    ppid: int
    started: float
    cmdline: tuple = ()
    session: int | None = None
    cwd: str = ""
    crew: str = ""            # the value of FACTORY_CREW in the process's environment, "" when absent or unreadable

    def line(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started))
        sess = f", session {self.session}" if self.session is not None else ""
        cmd = " ".join(self.cmdline)
        if len(cmd) > CMD_MAX:
            cmd = cmd[:CMD_MAX - 3] + "..."
        return f"pid {self.pid}, started {when}{sess}: {cmd}"


@dataclass
class Stray:
    kind: str                 # "claude", "service" or "orphan"
    proc: Proc
    why: str = ""

    def line(self) -> str:
        return self.proc.line()


# ---------- the process table ----------


def _windows() -> bool:
    """A function so a test can patch it."""
    return sys.platform == "win32"


def session_of(pid: int):
    """The Windows session a process runs in; None off Windows or when it cannot be read."""
    if not _windows():
        return None
    try:
        import ctypes
        out = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(out)):
            return int(out.value)
    except (OSError, AttributeError, ValueError, TypeError):
        pass
    return None


def _row(d: dict) -> Proc:
    return Proc(int(d["pid"]), int(d.get("ppid") or 0), float(d.get("started") or 0.0),
                tuple(str(x) for x in d.get("cmdline") or ()),
                d.get("session") if isinstance(d.get("session"), int) else None,
                str(d.get("cwd") or ""), str(d.get("crew") or ""))


def _fake_dir(env) -> str:
    return env.get("TARES_FAKE_STRAYS_DIR") or env.get("TARES_FAKE_PS_DIR") or ""


def _fake_table(d: str) -> list:
    try:
        rows = json.loads((Path(d) / TABLE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            out.append(_row(r))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def table(env=None) -> list:
    """Every process the user can see, one pass; a Proc each. A process that ends or may not be read while
    it is looked at gives the fields that could be read."""
    env = os.environ if env is None else env
    d = _fake_dir(env)
    if d:
        return _fake_table(d)
    out = []
    try:
        it = psutil.process_iter(["pid", "ppid", "cmdline", "create_time"])
    except (psutil.Error, OSError, SystemError):
        return out
    for p in it:
        try:   # one process at a time: psutil on macOS can raise SystemError for one that is just ending
            info = p.info
            out.append(Proc(int(info["pid"]), int(info.get("ppid") or 0), float(info.get("create_time") or 0.0),
                            tuple(info.get("cmdline") or ())))
        except (psutil.Error, OSError, SystemError, KeyError, TypeError, ValueError):
            continue
    return out


def _fill(p: Proc, fake: bool) -> None:
    """The cwd and the crew marker of one process, read only for an orphan candidate."""
    if fake:
        return
    try:
        proc = psutil.Process(p.pid)
        try:
            p.cwd = proc.cwd() or ""
        except (psutil.Error, OSError, SystemError):
            pass
        try:
            p.crew = (proc.environ() or {}).get("FACTORY_CREW", "") or ""
        except (psutil.Error, OSError, SystemError):
            pass
    except (psutil.Error, OSError, SystemError):
        pass


# ---------- Claude processes ----------


def _stem(cmd: str) -> str:
    base = re.split(r"[\\/]", cmd)[-1]
    return base.rsplit(".", 1)[0].lower() if base.lower().endswith((".exe", ".cmd", ".js", ".mjs")) else base.lower()


def is_claude(p: Proc) -> bool:
    if not p.cmdline:
        return False
    return _stem(p.cmdline[0]) == "claude" or any("@anthropic-ai/claude-code" in a.replace("\\", "/")
                                                 for a in p.cmdline)


def _name_of(p: Proc) -> str:
    """The value after -n, "" when there is none."""
    a = list(p.cmdline)
    for i, x in enumerate(a[:-1]):
        if x == "-n":
            return a[i + 1]
    return ""


def _args(p: Proc) -> list:
    """The words after the program: for `node .../cli.js daemon run` the words after the script."""
    a = list(p.cmdline)
    for i, x in enumerate(a):
        if "@anthropic-ai/claude-code" in x.replace("\\", "/") or _stem(x) == "claude":
            return a[i + 1:]
    return a[1:]


def _is_daemon(p: Proc) -> bool:
    a = _args(p)
    return a[:2] == ["daemon", "run"]


def _active(paths: CrewPaths) -> set:
    try:
        rows = state.stations_load(paths)
    except Exception:
        return set()
    return {s for s, row in rows.items() if isinstance(row, dict) and row.get("status") == "active"}


def _known(paths: CrewPaths) -> set:
    try:
        return set(state.stations_load(paths))
    except Exception:
        return set()


def _crew_pattern(paths: CrewPaths):
    try:
        crew = state.crew_name(paths)
    except Exception:
        crew = ""
    return re.compile(("^" + re.escape(crew) + "-|" if crew else "") + "-build-")


def _descendants(procs: list, roots: set) -> set:
    kids: dict = {}
    for p in procs:
        kids.setdefault(p.ppid, []).append(p.pid)
    out, todo = set(), list(roots)
    while todo:
        pid = todo.pop()
        for k in kids.get(pid, ()):
            if k not in out and k not in roots:
                out.add(k)
                todo.append(k)
    return out


def claude_strays(paths: CrewPaths, procs: list, watch_pid=None) -> tuple:
    """(strays, the service processes, the pids of every Claude process the crew owns). A stray is a Claude
    process the crew did not start; a plain interactive `claude` (no --bg, no -n) is the person's own."""
    active = _active(paths)
    known = _known(paths)
    prefix = _crew_pattern(paths)
    under_watch = _descendants(procs, {watch_pid}) if watch_pid else set()
    strays: list = []
    services: list = []
    owned: set = set()
    daemons = sorted((p for p in procs if is_claude(p) and _is_daemon(p)), key=lambda p: (p.started, p.pid))
    for i, p in enumerate(daemons):
        services.append(p)
        if i == 0:
            owned.add(p.pid)
        else:
            strays.append(Stray("claude", p, "second service"))
    for p in procs:
        if not is_claude(p) or _is_daemon(p):
            continue
        line = " ".join(p.cmdline)
        args = _args(p)
        name = _name_of(p)
        if name:
            crewish = name in known or bool(prefix.search(name))
            if name in active:
                owned.add(p.pid)
            elif crewish:
                why = "station retired" if name in known else "not an active station"
                strays.append(Stray("claude", p, f"{why}: {name}"))
            elif "--bg-pty-host" in line or "--bg" in args:
                strays.append(Stray("claude", p, f"background session {name} is not the crew's"))
            continue
        if _SPARE.search(line) or (args[:1] == ["attach"]) or (args[:1] and args[0] in _SHORT_CALLS) \
                or p.pid in under_watch:
            owned.add(p.pid)
            continue
        if "--bg-pty-host" in line or "--bg" in args:
            strays.append(Stray("claude", p, "background session the crew did not start"))
    return strays, services, owned


# ---------- the service and its session ----------


def service_session_problem(services: list, watch_session=None) -> str:
    """"" when the background service runs where the crew watch does; on Windows, the FIX text when it runs in
    session 0 or in another session than the watch's. An unreadable session says nothing."""
    if not _windows():
        return ""
    for s in services:
        if s.session is None:
            continue
        if s.session == 0:
            return SESSION_FIX
        if watch_session is not None and s.session != watch_session:
            return SESSION_FIX
    return ""


def _watch_session(paths: CrewPaths):
    from . import watch   # here, not at the top: watch imports this module
    try:
        pid = watch.running_pid(paths)
    except Exception:
        pid = None
    return session_of(pid) if pid else session_of(os.getpid())


# ---------- orphans ----------


def _parent_gone(p: Proc, by_pid: dict, exists) -> bool:
    if p.ppid <= 1:
        return True
    parent = by_pid.get(p.ppid)
    if parent is None:
        return not exists(p.ppid)
    if parent.started and p.started and parent.started > p.started:
        return True    # the parent's pid was reused after the child's parent ended
    return any(parent.cmdline and _stem(parent.cmdline[0]) == r for r in _REAPERS)


def _is_crew_made(p: Proc, crew: str, roots: list) -> bool:
    if p.crew:
        return p.crew == crew
    from . import watch   # here, not at the top: watch imports this module
    for r in roots:
        if not r:
            continue
        if p.cwd and watch._under(p.cwd, r):
            return True
        if any(os.path.isabs(a) and watch._under(a, r) for a in p.cmdline):
            return True
    return False


def orphans(procs: list, roots, crew: str, protect: set, now: float, min_age: float = MIN_AGE,
            exists=None, fill=None) -> list:
    """Crew-made processes whose parent is gone and that are older than min_age seconds, not in `protect`.
    `roots` is a list, or a function that gives one (asked only when a candidate has no crew marker);
    `fill(proc)` reads a candidate's cwd and marker."""
    by_pid = {p.pid: p for p in procs}
    exists = exists or (lambda pid: pid in by_pid)
    out: list = []
    cache: list = []

    def get_roots() -> list:
        if not cache:
            cache.append(list(roots() if callable(roots) else roots))
        return cache[0]

    for p in sorted(procs, key=lambda x: x.pid):
        if p.pid in protect or p.pid == os.getpid() or now - p.started <= min_age:
            continue
        if not p.cmdline or not _parent_gone(p, by_pid, exists):
            continue
        if fill is not None:
            fill(p)
        if p.crew and p.crew != crew:
            continue
        if not p.crew and not _is_crew_made(p, crew, get_roots()):
            continue
        out.append(Stray("orphan", p, "parent gone"))
    return out


def _roots(paths: CrewPaths, env: dict) -> list:
    roots = [str(paths.home / "worktrees")]
    if env.get("FACTORY_WORKTREES"):
        roots.append(env["FACTORY_WORKTREES"])
    try:
        from .client import TaresClient
        with TaresClient(paths, env, say=lambda _m: None) as client:
            repos = client.all_repos() if client.tares_on() else []
    except Exception:   # the roots from Tares are a bonus: no Tares, fewer roots
        repos = []
    return roots + [f"{r}/.factory/worktrees" for r in repos]


# ---------- the whole look ----------


def scan(paths: CrewPaths, env=None, now=None, procs=None) -> dict:
    """{"claude": [Stray], "orphan": [Stray], "service": str}."""
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    fake = bool(_fake_dir(env))
    live = procs is None and not fake
    procs = table(env) if procs is None else procs
    from . import watch
    try:
        watch_pid = watch.running_pid(paths)
    except Exception:
        watch_pid = None
    if live:
        for p in procs:
            if p.session is None and is_claude(p):
                p.session = session_of(p.pid)
    strays, services, owned = claude_strays(paths, procs, watch_pid)
    protect = set(owned) | {s.proc.pid for s in strays}
    if watch_pid:
        protect.add(watch_pid)
        protect |= _descendants(procs, {watch_pid})
    protect |= _descendants(procs, set(owned))
    try:
        crew = state.crew_name(paths)
    except Exception:
        crew = ""
    found = orphans(procs, lambda: _roots(paths, env), crew, protect, now,
                    exists=(psutil.pid_exists if live else None),
                    fill=(lambda p: _fill(p, False)) if live else None)
    problem = service_session_problem(services, _watch_session(paths)) if services else ""
    return {"claude": strays, "orphan": found, "service": problem}


def counts(result: dict) -> tuple:
    """(Claude strays, other: orphans, plus one when the service is in the wrong session)."""
    return len(result["claude"]), len(result["orphan"]) + (1 if result["service"] else 0)


def listing(strays: list) -> str:
    """The strays as a doctor message: each line, at most SHOW_MAX, then "and N more"."""
    shown = [f"{s.line()} ({s.why})" for s in strays[:SHOW_MAX]]
    if len(strays) > SHOW_MAX:
        shown.append(f"and {len(strays) - SHOW_MAX} more")
    return "; ".join(shown)


# ---------- the watch step ----------


def _read_state(paths: CrewPaths) -> dict:
    try:
        d = json.loads(state.read_text(paths.home / STATE_FILE))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _deliver(paths: CrewPaths, env: dict, prompt: str) -> bool:
    """Tell the orchestrator, in its own conversation, between its turns; the same delivery the pace uses.
    True only when the message was sent."""
    from . import claude, lifecycle, locks
    from .client import TaresClient
    from .errors import CrewError

    @locks.scoped
    def go() -> bool:
        orch = state.crew_orchestrator(paths)
        row = state.stations_load(paths).get(orch)
        if not isinstance(row, dict) or row.get("status") != "active" or not row.get("sessionId"):
            return False
        claude_bin = claude.claude_bin(env)
        if state.halted(paths) or claude.attached(orch, paths):
            return False
        if not claude.is_alive(orch, "", claude_bin, paths) or not claude.between_turns(orch, claude_bin, paths):
            return False
        with TaresClient(paths, env) as fresh:
            if not fresh.tares_on():
                return False
            lifecycle.restart(fresh, paths, [orch, prompt], claude_bin=claude_bin)
        return True

    try:
        return bool(go())
    except (CrewError, OSError):
        return False


def step(paths: CrewPaths, env=None, now=None) -> None:
    """The watch's round: at most every ten minutes, look; say it in the log when there are strays, and tell the
    orchestrator once each time the count rises. Nothing is stopped."""
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    old = _read_state(paths)
    at = old.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        at = _LAST.get(str(paths.home))   # nothing found so far writes no file: the look is remembered here
    if isinstance(at, (int, float)) and 0 <= now - at < STEP_EVERY:
        return
    _LAST[str(paths.home)] = now
    result = scan(paths, env, now)
    c, o = counts(result)
    total, told = c + o, _int(old.get("told"))
    if total:
        from . import watch
        watch._say(f"strays: {c} claude, {o} other ({ME} doctor)")
        if total > told:
            prompt = (f"FYI strays: {c} claude, {o} other process(es) the crew does not own were found on this "
                      f"machine. Nothing was stopped; {ME} doctor lists them.")
            if _deliver(paths, env, prompt):
                told = total
    if total < told:
        told = total
    if total != _int(old.get("claude")) + _int(old.get("other")) and total:
        state.event("strays", "", f"{c} claude, {o} other", paths=paths)
    if paths.home.is_dir() and (total or old):   # a crew with no stray, and none before, leaves no file
        state.atomic_write_text(paths.home / STATE_FILE,
                                json.dumps({"at": now, "claude": c, "other": o, "told": told}) + "\n")
