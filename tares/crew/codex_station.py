"""A Codex station between its start and its end (TR-1593): is it alive, where is it in its turn, handing it
a prompt, stopping it. `tares.crew.proc` sends the crew's questions here for a station recorded on Codex.

No process of the crew stays connected to a station. A station is its own app-server (`tares.crew.codex`
starts it), and each question is a short visit: connect to its socket, ask, leave. Measured on Codex 0.171.0:
a turn goes on when whoever started it leaves at once; a visitor that never joined the conversation reads its
state (`thread/read`: idle, active, waiting on an approval) and when it last did anything; and a visitor starts
the next turn on a conversation that is still loaded. After the app-server was started again the conversation
is not loaded, and `thread/resume` loads it from the station's own Codex folder.

What a turn state means here, in the words the crew already uses for Claude Code (`claude.turn_state`):
  done      alive and between turns (idle, or no conversation yet, or one that is not loaded)
  working   in a turn, or a conversation that cannot be read: for a moment after a turn starts the app-server
            answers `thread/read` with an error, so an unreadable conversation is looked at again a few
            times and then taken for one at work, never for one at rest
  blocked   in a turn that waits on an approval or on an answer
  ""        not alive
A blocked station is never "between turns" here, however long it has waited. Claude Code's wait rule lets a
restart replace a prompt nobody answered; on Codex nothing answers an approval yet (the desk is the next step
of TR-1593), and a second prompt into a waiting turn would steer it, not replace it. So it is left alone.

Handing over a prompt (`hand`) is a turn start, only on a station that is done; one in a turn is refused with
NotBetweenTurnsError, as a wake that finds a Claude Code station in a turn is. A second turn start into a
running turn is never sent: Codex takes it as a steer of that turn, not as a message that waits.

Stopping (`stop`): an interrupt for a running turn, a wait for the turn to end, then SIGTERM and, after a
grace period, SIGKILL. TR-1576 measured that an interrupt alone ends the turn and not its command, that
SIGTERM in the middle of a command did not end the app-server in 20 s, and that interrupt then SIGTERM did,
with the command gone. A kill leaves a running command behind, in a session of its own; `leftovers` finds
what a station started (by the station's name in its environment, and by what `note_children` wrote down
while the app-server ran), and a stop, and a revive of a station that died, end it (`end_leftovers`). The
revived station is told, since what such a command did is not known.

psutil and the standard library.
"""
from __future__ import annotations

import os
import signal
import time

import psutil

from . import codex as cx
from . import state
from .codex_wire import Wire, WireError
from .errors import CrewError, NotBetweenTurnsError
from .paths import CrewPaths

TURN_STOP_WAIT = 20   # seconds an interrupted turn may take to end before the app-server is asked to end
BORN_SLACK = 1.0      # seconds two readings of one process's start time may differ by


def info(paths: CrewPaths, station: str) -> dict:
    """What the start recorded of the station's app-server (`codex` in its row): pid, born, socket, home."""
    row = state.stations_load(paths).get(station)
    found = row.get("codex") if isinstance(row, dict) else None
    return found if isinstance(found, dict) else {}


def _same_process(rec: dict) -> bool:
    """Is the recorded app-server still the process with that id? The id alone is not enough: the system
    gives a dead process's id to a new one."""
    pid, born = rec.get("pid"), rec.get("born")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        return not isinstance(born, (int, float)) or abs(p.create_time() - born) <= BORN_SLACK
    except (psutil.Error, OSError):
        return False


def is_alive(paths: CrewPaths, station: str) -> bool:
    """True while the app-server the start recorded runs."""
    return _same_process(info(paths, station))


def visit(paths: CrewPaths, station: str, timeout: float = 15) -> Wire:
    """A connection to the station's app-server, greeted. Dies when the station is not running or does not
    answer. The caller closes it (`with visit(...) as wire:`)."""
    rec = info(paths, station)
    if not _same_process(rec):
        raise CrewError(f"{station} is not running")
    try:
        wire = Wire(str(rec.get("socket") or cx.socket_path(paths, station)), timeout)
    except WireError as e:
        raise CrewError(f"{station} does not answer: {e}") from None
    try:
        wire.initialize()
    except WireError as e:
        wire.close()
        raise CrewError(f"{station} does not answer: {e}") from None
    return wire


READ_TRIES = 8        # looks at a conversation that cannot be read, READ_PAUSE apart, before it is given up as unreadable
READ_PAUSE = 0.25
# What the app-server says of a conversation it does not have open, or does not know (0.171.0).
_NOT_HERE = ("thread not loaded", "no rollout found", "invalid thread id", "thread not found")


def _thread(wire: Wire, thread: str, turns: bool = False):
    """(the conversation as the app-server gives it, "") or (None, why it could not be read)."""
    try:
        found = wire.request("thread/read", {"threadId": thread, "includeTurns": turns}).get("thread")
    except WireError as e:
        return None, str(e)
    return (found, "") if isinstance(found, dict) else (None, "no conversation in the answer")


def _kind(found, why: str = "") -> str:
    """Where a conversation is: idle, active, waiting (on an approval or an answer), notLoaded (the app-server
    has it on disk or does not know it: nothing of it runs), or unreadable. Unreadable is not idle: for a
    moment after a turn starts the app-server cannot read the conversation at all (measured on 0.171.0)."""
    if found is None:
        return "notLoaded" if any(text in why for text in _NOT_HERE) else "unreadable"
    status = found.get("status")
    if not isinstance(status, dict):
        return "unreadable"
    kind = status.get("type")
    if kind == "active":
        return "waiting" if status.get("activeFlags") else "active"
    return kind if kind in ("idle", "notLoaded") else "unreadable"


def _where(wire: Wire, thread: str, sleep=time.sleep) -> str:
    """`_kind` of the conversation, looked at again while it cannot be read."""
    kind = "unreadable"
    for i in range(READ_TRIES):
        kind = _kind(*_thread(wire, thread))
        if kind != "unreadable":
            return kind
        if i + 1 < READ_TRIES:
            sleep(READ_PAUSE)
    return kind


def _recorded_thread(paths: CrewPaths, station: str) -> str:
    return state.stations_get(station, "sessionId", paths=paths)


def turn_state(paths: CrewPaths, station: str) -> str:
    """done, working or blocked for a running station, "" for one that is not running (see the module's note).
    A station that runs and does not answer, or whose conversation cannot be read, is "working": nothing is
    handed to what cannot be seen."""
    if not is_alive(paths, station):
        return ""
    thread = _recorded_thread(paths, station)
    if not thread:
        return "done"
    try:
        with visit(paths, station) as wire:
            kind = _where(wire, thread)
    except CrewError:
        return "working" if is_alive(paths, station) else ""
    return {"idle": "done", "notLoaded": "done", "waiting": "blocked"}.get(kind, "working")


def at_rest(paths: CrewPaths, station: str) -> bool:
    """True when the station runs and is between turns."""
    return turn_state(paths, station) == "done"


def updated(paths: CrewPaths, station: str):
    """When the station's conversation last did anything, seconds since 1970; None when unknown."""
    thread = _recorded_thread(paths, station)
    if not thread:
        return None
    try:
        with visit(paths, station) as wire:
            found, _ = _thread(wire, thread)
    except CrewError:
        return None
    at = found.get("updatedAt") if isinstance(found, dict) else None
    return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def settings(paths: CrewPaths, station: str) -> dict:
    """The settings the station was started with (stations/<station>.settings.json): `thread` is what its
    conversation starts with, `effort` a turn's."""
    doc = state.read_json(paths.station_file(station, "settings.json"), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("thread"), dict):
        raise CrewError(f"{station} has no settings to start a conversation from "
                        f"({paths.station_file(station, 'settings.json')}); start it again")
    return doc


def hand(paths: CrewPaths, station: str, prompt: str, *, fresh: bool = False) -> dict:
    """Hand the station a prompt: one turn start on its conversation. Returns {thread, turn, replaced}.

    The recorded conversation is carried on: used as it is while loaded, loaded again (`thread/resume`) after
    the app-server was started anew. `fresh`, or a conversation that cannot be loaded, starts a new one from
    the station's settings; `replaced` says a recorded one was given up, and an event records it. Dies
    (NotBetweenTurnsError) when the station is in a turn, and (CrewError) when it is not running or refuses."""
    doc = settings(paths, station)
    cwd = state.stations_get(station, "cwd", paths=paths) or paths.raw
    start = {**doc["thread"], "cwd": cwd}
    recorded = _recorded_thread(paths, station)
    thread, replaced = ("" if fresh else recorded), False
    with visit(paths, station) as wire:
        try:
            if thread:
                kind = _where(wire, thread)
                if kind in ("active", "waiting"):
                    raise NotBetweenTurnsError(f"{station} is in a turn; not handing it a prompt")
                if kind == "unreadable":   # never taken for a lost conversation: that would start a new one
                    raise CrewError(f"the conversation of {station} cannot be read now; not handing it a prompt")
                if kind == "notLoaded":   # the app-server was started again since, or does not know it
                    try:
                        wire.request("thread/resume", {**start, "threadId": thread})
                    except WireError:
                        thread, replaced = "", True
            if not thread:
                thread = str((wire.request("thread/start", start).get("thread") or {}).get("id") or "")
                if not thread:
                    raise CrewError(f"{station} gave no conversation to start a turn in")
            params = {"threadId": thread, "input": [{"type": "text", "text": prompt}]}
            if doc.get("effort"):
                params["effort"] = doc["effort"]
            turn = str((wire.request("turn/start", params).get("turn") or {}).get("id") or "")
        except WireError as e:
            raise CrewError(f"could not hand {station} its prompt: {e}") from None
    state.stations_put(station, {"sessionId": thread, "codex": {**info(paths, station), "turn": turn,
                                                              "turn_at": state.utc_stamp()}}, paths)
    if recorded and thread != recorded:
        state.event("conversation-replaced" if replaced else "conversation-fresh", station, recorded, paths=paths)
    return {"thread": thread, "turn": turn, "replaced": replaced}


def _running_turn(thread) -> str:
    for turn in reversed(thread.get("turns") or [] if isinstance(thread, dict) else []):
        if isinstance(turn, dict) and turn.get("status") == "inProgress":
            return str(turn.get("id") or "")
    return ""


def _interrupt(paths: CrewPaths, station: str, wait: float, sleep) -> None:
    """Interrupt the station's running turn and wait, at most `wait` seconds, for it to end. Never fails."""
    thread = _recorded_thread(paths, station)
    if not thread:
        return
    try:
        with visit(paths, station, timeout=5) as wire:
            if _where(wire, thread, sleep) in ("idle", "notLoaded"):
                return
            # the turn that runs, as the conversation lists it; else the one the last prompt started
            turn = _running_turn(_thread(wire, thread, turns=True)[0]) or str(info(paths, station).get("turn") or "")
            if turn:
                wire.request("turn/interrupt", {"threadId": thread, "turnId": turn})
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and _kind(*_thread(wire, thread)) != "idle":
                sleep(0.2)
    except (CrewError, WireError):
        pass


def _gone(pid: int, wait: float, sleep) -> bool:
    deadline = time.monotonic() + wait
    while True:
        try:
            os.waitpid(pid, os.WNOHANG)   # when it is this process's own child: collect it
        except OSError:
            pass
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except (psutil.Error, OSError):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.1)


def _marked(env: dict, paths: CrewPaths, station: str) -> bool:
    """Does a process's environment say it was started by this station of this crew?"""
    if env.get("FACTORY_STATION") != station:
        return False
    home = env.get("FACTORY_CREW_HOME") or env.get("FACTORY_HOME") or ""
    return bool(home) and os.path.realpath(home) == os.path.realpath(paths.raw)


def _children_file(paths: CrewPaths, station: str):
    return paths.station_file(station, "codex.children")


def note_children(paths: CrewPaths, station: str) -> None:
    """Write down what runs under the station's app-server now: each process's id and start time. The watch
    does this every round for a station that runs, and a stop does it right before it ends the app-server, so
    that what outlives the app-server can be told from any other process afterwards. Never fails."""
    rec = info(paths, station)
    if not _same_process(rec):
        return
    rows = []
    try:
        for p in psutil.Process(rec["pid"]).children(recursive=True):
            try:
                rows.append([p.pid, p.create_time()])
            except (psutil.Error, OSError):
                continue
    except (psutil.Error, OSError):
        return
    file = _children_file(paths, station)
    try:
        if state.read_json(file, None) != rows:
            state.write_json(file, rows, compact=True)
    except (OSError, ValueError, CrewError):
        pass


def _noted(paths: CrewPaths, station: str) -> list:
    """The processes written down by `note_children` that still run (same id, same start time)."""
    rows = state.read_json(_children_file(paths, station), None)
    found = []
    for row in rows if isinstance(rows, list) else []:
        try:
            pid, born = row
            p = psutil.Process(pid)
            if p.status() != psutil.STATUS_ZOMBIE and abs(p.create_time() - born) <= BORN_SLACK:
                found.append(p)
        except (psutil.Error, OSError, TypeError, ValueError):
            continue
    return found


def leftovers(paths: CrewPaths, station: str) -> list:
    """The processes a station started that still run, apart from its app-server: [psutil.Process].

    A station's commands run in sessions of their own, so nothing that ends the app-server reaches them when
    it is killed (TR-1576). Two things tell them from any other process:
      * the station's environment, which the app-server hands to every command it runs (FACTORY_STATION and
        the crew's home). Always readable on Linux. On macOS the environment of the system's own programs
        (sh, git, sleep) cannot be read by anyone, so this alone finds little there;
      * what `note_children` wrote down while the app-server ran, and what runs under those processes now.
    What neither finds: on macOS, a system program started after the last note (the watch's last round) of a
    station that then died by itself. This process and the ones above it are never among the leftovers (a
    station may run this very command), nor is the station's app-server while it runs."""
    rec = info(paths, station)
    mine = {os.getpid()}
    try:
        mine.update(p.pid for p in psutil.Process().parents())
    except (psutil.Error, OSError):
        pass
    if _same_process(rec):
        mine.add(rec["pid"])
    found = {}
    for p in _noted(paths, station):
        found[p.pid] = p
        try:
            found.update({c.pid: c for c in p.children(recursive=True)})
        except (psutil.Error, OSError):
            pass
    for p in psutil.process_iter():
        if p.pid in mine or p.pid in found:
            continue
        try:
            if p.status() != psutil.STATUS_ZOMBIE and _marked(p.environ(), paths, station):
                found[p.pid] = p
        except (psutil.Error, OSError):   # not ours to read, or gone meanwhile
            continue
    return [p for pid, p in sorted(found.items()) if pid not in mine]


def _name(p) -> str:
    """A process's program, without its arguments: an argument can hold a secret, a program's name does not."""
    try:
        line = p.cmdline()
        return os.path.basename(line[0]) if line else p.name()
    except (psutil.Error, OSError, IndexError):
        return "?"


def end_leftovers(paths: CrewPaths, station: str, *, grace: float = cx.STOP_GRACE, sleep=time.sleep) -> list:
    """End what `leftovers` finds: SIGTERM, then SIGKILL for what is still there after `grace` seconds. Call it
    only when the station's app-server is not running. Returns the programs' names, and records an event when
    there were any."""
    found = leftovers(paths, station)
    if not found:
        return []
    names = sorted({_name(p) for p in found})
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in found:
            try:
                p.send_signal(sig)
            except (psutil.Error, OSError):
                pass
        _, found = psutil.wait_procs(found, timeout=grace)
        if not found:
            break
    _children_file(paths, station).unlink(missing_ok=True)
    state.event("left-behind", station, f"{len(names)} program(s) ended: {', '.join(names)}"[:200], paths=paths)
    return names


def stop(paths: CrewPaths, station: str, *, turn_wait: float = TURN_STOP_WAIT, grace: float = cx.STOP_GRACE,
         sleep=time.sleep) -> str:
    """End the station's app-server (see the module's note for the order). Returns what it took: "not running",
    "stopped" (it ended when asked) or "killed". The socket's path in the crew home goes with it, and so does
    whatever the station had started that still runs (`end_leftovers`): after a stop nothing of it is left."""
    rec = info(paths, station)
    sock = rec.get("socket") or str(cx.socket_path(paths, station))
    if not _same_process(rec):
        cx.remove_socket(sock)
        end_leftovers(paths, station, grace=grace, sleep=sleep)
        return "not running"
    pid = rec["pid"]
    _interrupt(paths, station, turn_wait, sleep)
    note_children(paths, station)   # what runs under it now: told apart from any other process once it is gone
    how = "stopped"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    if not _gone(pid, grace, sleep):
        how = "killed"
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _gone(pid, grace, sleep)
    cx.remove_socket(sock)
    end_leftovers(paths, station, grace=grace, sleep=sleep)
    return how
