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
restart replace a prompt nobody answered; on Codex a second prompt into a waiting turn would steer it, not
replace it. So it takes no prompt, and what it asks goes to the person's desk (`asked` and `answer` below, and
`tares.crew.codex_asks`, which the watch runs once a round).

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

The guard (TR-1594). A station's guard is a hook of its conversation (`tares.crew.codex_guard`), and Codex
skips a hook it was not told to trust without an error. So the crew does not take the guard for granted; it
checks, with what a short visit can see:
  * before a turn starts (`hand`): the station's folder brings nothing of its own. The folder is marked not
    trusted, in the station's own `config.toml` at its start and again in the conversation's start, so the
    repository's `.codex/config.toml` is not read (`codex.not_trusted` says what happens without the
    mark); then the app-server is asked what hooks and MCP servers it has for the folder, and any the
    station was not started with stops it;
  * at the start of every turn (`hand`): the guard's record of that turn must be there before the turn's
    first item. Codex runs the hook after `turn/start` and before it asks the model, so a turn without the
    record is interrupted before the model has said anything, and nothing has run;
  * for every command and MCP call (`check_guard`, by the watch every round, `hand` before the next turn,
    and `stop` before it ends the app-server): each one the last turn made must have the guard's record
    saying it was let through, and a command must carry the line that closes its input. An app-server
    lists a command only once it has ended, so this is seen after the fact, at the next look. The look at
    a stop is the last one a conversation gets when the station is then started in a new one
    (`restart --fresh`), or not at all (`retire`); a station found out there is marked like any other, and
    a restart does not start it again. A look at a stop that could not be made is written on the station's
    row (`unverified`), and from then on nothing gives that conversation up by itself: a start in the
    same conversation compares first, as always, and `hand` starts no turn in any other one, so
    `restart --fresh`, the watch, and a start that finds the conversation can no longer be loaded leave
    the station down. A person gives it up, with `tares factory revive <station> --fresh` or by retiring
    the station (`give_up_unverified`). `restart --fresh` goes on only when its own stop made the
    comparison: a station that was not running then has nobody to ask, which is a comparison that could
    not be made, and is marked the same way (`lifecycle._restart_codex`).
A station that fails one is stopped and marked (`guard_off`); the watch does not start it again by itself,
`tares factory revive` does. Two things keep that from going wrong:
  * a turn that cannot be listed with its items is not a turn that was compared. The watch then looks again
    next round, and `hand` refuses the next prompt until the comparison could be made: only the newest turn
    is ever compared, so a prompt handed over now would leave this one never looked at;
  * the comparison and the stop are one step under the station's lock, and the stop ends only the app-server
    that was looked at. A restart between the two would otherwise have its new app-server stopped, and
    marked, for what the old one did.
What is not covered: a helper conversation the station starts inside Codex runs the same hook (TR-1576), but
its commands are not compared.

Tares, before a prompt (TR-1596). A station's conversation is started with the MCP servers of its settings
(`mcp_servers`: the crew's Tares endpoint, marked required), and `hand` starts no turn until two things hold,
both asked of the app-server on the visit, with no turn and no model:
  * each of those servers is connected for the conversation (`mcpServerStatus/list`). Codex itself refuses to
    start or join a conversation whose required server does not answer; that refusal is told apart from a
    conversation that is lost, so a station is never given a new conversation because Tares was down;
  * the read named by `mcp_check` goes through (`mcpServer/tool/call`: Codex makes the call on the
    conversation's own connection, so it carries the station's identity and the crew's key as the model's
    calls will). The Tares endpoint takes any key when a conversation connects and hands the cell's refusal
    back as a tool's text, so this read is what finds a key the cell does not take.
A station that fails either gets no turn and the caller is told why; it is not stopped for it (a first start
that fails is undone by `codex.start`). The key itself never passes through here: the settings hold the path
of the crew's key file, which the headers helper reads.

psutil and the standard library.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import sys
import time

import psutil

from . import codex as cx
from . import codex_guard, locks, state
from .codex_wire import Wire, WireError
from .errors import CrewError, GuardOffError, NeverComparedError, NotBetweenTurnsError, StationLockedError
from .paths import CrewPaths

TURN_STOP_WAIT = 20   # seconds an interrupted turn may take to end before the app-server is asked to end
BORN_SLACK = 1.0      # seconds two readings of one process's start time may differ by
ARM_WAIT = 15         # seconds the guard's record of a turn may take (1.7 s for a first turn, measured)
RECORD_MAX = 256 * 1024   # bytes of a station's guard record above which older turns' lines are dropped


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


# ---- the guard (see the module's note) ----

def _record(paths: CrewPaths, station: str) -> list:
    return codex_guard.read_record(paths.station_file(station, codex_guard.RECORD))


def _thread_start(thread: dict, cwd: str, servers=None) -> dict:
    """The parameters a conversation is started (or joined) with: the station's settings, its folder, the
    folder marked not trusted, and the MCP servers it is started with (`servers`, {name: what Codex takes})."""
    config = dict(thread.get("config") or {})
    config["projects"] = {**(config.get("projects") or {}), **cx.not_trusted(cwd)}
    if servers:
        config["mcp_servers"] = {**(config.get("mcp_servers") or {}), **servers}
    return {**thread, "cwd": cwd, "config": config}


# ---- Tares, before a prompt (see the module's note) ----

MCP_WAIT = 20   # seconds a server may take to connect for a conversation that was just started or joined
_REQUIRED = "required MCP servers failed to initialize:"
# How a Tares tool words what the cell refused: `error <status>: <detail>` as its text, with no error flag
# (tares/mcp_server.py `_out`).
_REFUSAL = re.compile(r"\s*error \d{3}\b")
_RUST_TYPE = re.compile(r"Transport \[[^\]]*\] error: ")


def _short(text: str, limit: int = 300) -> str:
    """Codex's words for a failed MCP connection, without the names of its own types."""
    text = _RUST_TYPE.sub("", str(text))
    half = text.find(", when send ")   # "when send initialize request: " and the same sentence a second time
    if half > 0:
        text = text[:half]
    return " ".join(text.split())[:limit]


def mcp_refused(e) -> str:
    """Why Codex would not start or join a conversation, when the reason is a required MCP server that did
    not answer; "" for any other failure."""
    text = str(e)
    at = text.find(_REQUIRED)
    return _short(text[at + len(_REQUIRED):]) if at >= 0 else ""


def mcp_ready(wire: Wire, thread: str, servers, sleep=time.sleep, wait: float = MCP_WAIT) -> str:
    """"" when every server of `servers` is connected for the conversation, else what is wrong with the first
    that is not. One still starting is waited for, `wait` seconds at most."""
    deadline = time.monotonic() + wait
    for name in sorted(servers or {}):
        while True:
            try:
                rows = wire.request("mcpServerStatus/list", {"threadId": thread, "serverName": name,
                                                             "detail": "toolsAndAuthOnly"}).get("data")
            except WireError as e:
                return f"its MCP server {name} could not be asked about ({_short(e)})"
            row = next((r for r in rows or [] if isinstance(r, dict) and r.get("name") == name), None)
            status = str((row or {}).get("runtimeStatus") or "")
            if status == "connected":
                break
            if status not in ("starting", "notStarted") or time.monotonic() >= deadline:
                return f"its MCP server {name} is not connected ({status or 'not listed for its conversation'})"
            sleep(0.3)
    return ""


def mcp_answers(wire: Wire, thread: str, check) -> str:
    """"" when the read `check` names ({server, tool, arguments}) goes through on the conversation's own
    connection, else why not, in the server's words. No `check`: nothing is asked."""
    if not isinstance(check, dict) or not check.get("server") or not check.get("tool"):
        return ""
    name = f"{check['server']}.{check['tool']}"
    try:
        got = wire.request("mcpServer/tool/call", {"server": check["server"], "threadId": thread,
                                                   "tool": check["tool"], "arguments": check.get("arguments") or {}})
    except WireError as e:
        return f"{name} did not go through ({_short(e)})"
    text = " ".join(str(c.get("text") or "") for c in got.get("content") or [] if isinstance(c, dict))
    if got.get("isError") or _REFUSAL.match(text):
        return f"{name} was refused ({_short(text) or 'no reason given'})"
    return ""


DOCTOR_WAIT = 3   # seconds the doctor waits for a server still connecting: it looks, it does not wait for a start


def mcp_health(paths: CrewPaths, station: str) -> tuple:
    """For the doctor (TR-1598): (level, what was found) for the station's connection to the MCP servers it is
    started with, level ok, warn or fix. The two checks `hand` makes before a prompt, asked of the running
    app-server on the station's own conversation: each server connected, and the read of `mcp_check` going
    through (it carries the station's identity and the crew's key, as the model's calls do). Nothing is joined
    or started: a conversation the app-server does not have open now is said to be checked before its next
    prompt."""
    try:
        doc = settings(paths, station)
    except CrewError as e:
        return "fix", e.message
    servers = doc.get("mcp_servers") if isinstance(doc.get("mcp_servers"), dict) else {}
    if not servers:
        return "fix", f"{station} is started with no MCP server: it would work without Tares"
    thread = _recorded_thread(paths, station)
    if not thread:
        return "warn", f"{station} has no conversation yet; it is checked before its first prompt"
    try:
        with visit(paths, station, timeout=5) as wire:
            kind = _where(wire, thread)
            if kind == "notLoaded":
                return "warn", (f"the conversation of {station} is not open in its app-server now; the connection "
                                "is checked before its next prompt")
            if kind == "unreadable":
                return "warn", f"the conversation of {station} cannot be read now; look again in a moment"
            blind = mcp_ready(wire, thread, servers, wait=DOCTOR_WAIT) or mcp_answers(wire, thread, doc.get("mcp_check"))
    except CrewError as e:
        return "warn", e.message
    except WireError as e:
        return "warn", f"{station} does not answer: {_short(e)}"
    if blind:
        return "fix", f"{station} would work without Tares: {blind}"
    check = doc.get("mcp_check") if isinstance(doc.get("mcp_check"), dict) else {}
    read = f", and {check['server']}.{check['tool']} goes through" if check.get("server") and check.get("tool") else ""
    return "ok", f"{station} is connected to {', '.join(sorted(servers))}{read}"


def _brought_along(wire: Wire, cwd: str, allowed) -> str:
    """What the app-server has for the station's folder that the station was not started with: a hook from
    anywhere but the conversation itself, an MCP server not among `allowed`. "" when there is nothing. An
    app-server that cannot say is not taken to have nothing."""
    try:
        listed = wire.request("hooks/list", {"cwds": [cwd]}).get("data")
        config = wire.request("config/read", {"cwd": cwd, "includeLayers": False}).get("config")
    except WireError as e:
        return f"what its folder adds to it could not be read ({e})"
    hooks = [h for entry in listed if isinstance(entry, dict) for h in entry.get("hooks") or []
             if isinstance(h, dict) and h.get("source") != "sessionFlags"] if isinstance(listed, list) else []
    if hooks:
        where = sorted({str(h.get("sourcePath") or h.get("source") or "?") for h in hooks})
        return f"a hook of its own would run next to the guard, from {', '.join(where)}"[:300]
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    extra = sorted(set(servers) - set(allowed)) if isinstance(servers, dict) else []
    if extra:
        return f"its folder brings an MCP server it was not started with: {', '.join(extra)}"[:300]
    return ""


AUDITED = ("commandExecution", "mcpToolCall")
# Not the model's own calls: a command a person typed for the conversation, and what Codex lists when a
# running command is looked at again. Neither passes a hook.
NOT_A_CALL = ("userShell", "unifiedExecInteraction")


def unguarded(items: list, record: list) -> list:
    """What a turn's items show the guard did not do, one sentence each; [] when every command and MCP call
    has the guard's record saying it was let through, and every command has its input closed. Pure."""
    said = {r.get("call"): r.get("said") for r in record if isinstance(r, dict) and r.get("call")}
    found = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") not in AUDITED or item.get("source") in NOT_A_CALL:
            continue
        what = "a command" if item["type"] == "commandExecution" else "an MCP call"
        call = str(item.get("id") or "")
        if said.get(call) is None:
            found.append(f"{what} ran with no run of the guard before it ({call})")
        elif said[call] != "allow":
            found.append(f"{what} ran though the guard refused it ({call})")
        elif item["type"] == "commandExecution" and not codex_guard.input_closed(item):
            found.append(f"a command ran without the guard's line that closes its input ({call})")
    return found


class NotCompared(Exception):
    """The conversation's newest turn could not be had with all its items, so what it ran is not known. Never
    the same as a turn that was compared and found in order."""


def _last_turn(wire: Wire, thread: str):
    """The conversation's newest turn with all its items, or None when it has no turn yet. Raises NotCompared
    when the turns cannot be listed, or the turn comes without its items (Codex says so in `itemsView`, and
    leaves `items` empty)."""
    try:
        data = wire.request("thread/turns/list", {"threadId": thread, "limit": 1, "itemsView": "full"}).get("data")
    except WireError as e:
        raise NotCompared(f"its turns could not be listed ({e})"[:300]) from None
    if not isinstance(data, list):
        raise NotCompared("the list of its turns was not a list")
    if not data:
        return None
    turn = data[0]
    if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
        raise NotCompared("its newest turn came without a list of items")
    if turn.get("itemsView", "full") != "full":
        raise NotCompared(f"its newest turn came without its items ({str(turn.get('itemsView'))[:40]})")
    return turn


def _audit(wire: Wire, thread: str, paths: CrewPaths, station: str, ended: bool = False) -> tuple:
    """(what the newest turn shows the guard did not do, "" when nothing; that turn's id). Raises NotCompared
    when the turn could not be looked at. With `ended` (the last look a turn gets, at a stop) a turn that still
    runs and shows nothing so far is not compared either: a command of it that has not ended is not listed."""
    turn = _last_turn(wire, thread)
    if turn is None:
        return "", ""
    if turn.get("id") and turn.get("id") == state.stations_get(station, SEEN, paths=paths):
        _note_compared(paths, station, str(turn["id"]))
        return "", str(turn["id"])   # the station was stopped for this turn, and a person started it again
    found = unguarded(turn.get("items"), _record(paths, station))
    if ended and not found and turn.get("status") == "inProgress":
        raise NotCompared("its newest turn had not ended")
    if not found and turn.get("id") and turn.get("status") != "inProgress":
        _note_compared(paths, station, str(turn["id"]))   # all of it was there to see, and nothing was found
    why = found[0] + (f", and {len(found) - 1} more" if len(found) > 1 else "") if found else ""
    return why, str(turn.get("id") or "")


# A conversation that can no longer be loaded (TR-1593). An app-server started again loads a conversation from
# the station's own Codex folder; when that fails the station used to get a new conversation, and whatever its
# last turn ran was then never compared. So the row keeps two turn ids: the last turn the crew started
# (LAST_TURN, written by `hand`) and the last one found in order after it had ended (COMPARED, written by
# `_audit`: the watch every round, `hand` before the next turn, a stop). Only when they are the same is a lost
# conversation replaced. Otherwise the station is marked like one whose stop could not compare (`unverified`),
# and a person gives the conversation up.
LAST_TURN = "guardLastTurn"
COMPARED = "guardCompared"


def _note_compared(paths: CrewPaths, station: str, turn: str) -> None:
    if turn and state.stations_get(station, COMPARED, paths=paths) != turn:
        state.stations_put(station, {COMPARED: turn}, paths)


def last_turn_compared(paths: CrewPaths, station: str) -> bool:
    """Was the last turn the crew started on the station compared with the guard's record after it ended?
    True for a station no turn was started on (there is nothing to compare)."""
    row = state.stations_load(paths).get(station)
    last = row.get(LAST_TURN) if isinstance(row, dict) else ""
    return not last or row.get(COMPARED) == last


# A call whose outcome is not known (TR-1593). The guard's hook writes a line before a call runs; Codex lists
# the call as an item once it has ended. A turn cut off in between (the app-server died, or was ended in the
# middle of a command) leaves a line with no ended item: the command may have done all of its work, part of
# it, or none. Such calls are found when the conversation is loaded again by an app-server started anew
# (`hand`), and when a person gives up a conversation that was never compared (`give_up_unverified`: then
# every call of its last turn is one). They are kept in stations/<station>.codex.unknown.json, named in the
# station's next prompt, and refused by the guard as a repeat (`codex_guard.repeats`) until the station has
# looked and said what it found (`reconcile`, `tares factory outcome`).
ENDED = ("completed", "failed", "declined")
ASKS_KEPT = "codex.asks.json"   # what the station asked the person (tares.crew.codex_asks keeps it)


def never_let_run(paths: CrewPaths, station: str) -> set:
    """The calls that stopped on a question to the person and were never given a yes: they did not run, so
    their outcome is known even with no item. A call that was given a yes may have run, and is not here."""
    rows = state.read_json(paths.station_file(station, ASKS_KEPT), None)
    return {str((r.get("ask") or {}).get("item")) for r in (rows.values() if isinstance(rows, dict) else [])
            if isinstance(r, dict) and isinstance(r.get("ask"), dict) and r.get("gave") != "accept"}


def unknown(paths: CrewPaths, station: str) -> list:
    return codex_guard.read_unknown(paths.station_file(station, codex_guard.UNKNOWN))


def unknown_of(turn: str, items, record: list) -> list:
    """The calls of `turn` the guard let through that no item says have ended. `items` None: the turn's items
    cannot be had at all, and every call it was let through is one. Pure."""
    ended = {i.get("id") for i in items or [] if isinstance(i, dict) and i.get("status") in ENDED}
    return [{"call": r["call"], "turn": turn, "tool": str(r.get("tool") or ""), "command": str(r.get("command") or ""),
             "cwd": str(r.get("cwd") or ""), "at": r.get("at")}
            for r in record if isinstance(r, dict) and turn and r.get("turn") == turn and r.get("call")
            and r.get("said") == "allow" and r["call"] not in ended]


def note_unknown(paths: CrewPaths, station: str, found: list) -> list:
    """Add `found` to the station's unknown calls (one row a call); the rows that are new."""
    rows = unknown(paths, station)
    new = [r for r in found if r["call"] not in {x.get("call") for x in rows}]
    if new:
        state.write_json(paths.station_file(station, codex_guard.UNKNOWN), rows + new, mode=0o600)
        for r in new:
            state.event("outcome-unknown", station, (r["command"] or r["tool"] or r["call"])[:300], paths=paths)
    return new


def reconcile(paths: CrewPaths, station: str, ref: str, verdict: str, words: str) -> dict:
    """Take one call off the station's unknown calls: `ref` is its call id, as the prompt and the list name it
    (never a place in the list: settling one call would move the others, and a second answer would land on
    the wrong one). `verdict` is what was found (done, not-done), `words` the station's own account. Records
    the event and returns the row. Dies when there is no such call, or the account is empty."""
    rows = unknown(paths, station)
    at = next((i for i, r in enumerate(rows) if r.get("call") and r.get("call") == str(ref)), -1)
    if at < 0:
        named = ", ".join(str(r.get("call")) for r in rows) or "none"
        raise CrewError(f"{station} has no call {ref} with an unknown outcome; name it by its call id ({named})")
    if verdict not in ("done", "not-done"):
        raise CrewError("say what you found: done (it had done its work) or not-done (it had not)")
    if not str(words or "").strip():
        raise CrewError("say what you looked at and what you found, in a sentence")
    row = rows.pop(at)
    state.write_json(paths.station_file(station, codex_guard.UNKNOWN), rows, mode=0o600)
    state.event("outcome-reconciled", station,
                f"{verdict}: {row.get('command') or row.get('tool') or row.get('call')} | {' '.join(str(words).split())}"[:500],
                paths=paths)
    return row


def unknown_note(rows: list) -> str:
    """What a station is told, in its next prompt, of the calls whose outcome is not known."""
    if not rows:
        return ""
    lines = [f"- call {r.get('call')}: `{r.get('command') or r.get('tool') or 'a call'}`" + (f" (in {r['cwd']})" if r.get("cwd") else "")
             for r in rows]
    return ("\n\nBefore anything else: your last turn was cut off, and these calls of it were started and never "
            "reported an end. What each did is not known: all of its work, part of it, or none.\n" + "\n".join(lines) +
            "\nDo not run any of them again yet. For each, look at what it left (git status and git log, the remote, "
            "the pull requests, the ticket and its checks), then say what you found with `tares factory outcome <call> "
            "done \"<what you found>\"` or `tares factory outcome <call> not-done \"<what you found>\"`, naming the call "
            "as it is written here. The guard refuses a repeat of one until you have.")


def cmd_outcome(argv: list, env: dict) -> int:
    """`tares factory outcome [<call> done|not-done <what you found>]`: with nothing, list the calls of this
    station whose outcome is not known; else take one off the list (see `reconcile`). The station is
    FACTORY_STATION, or `--station NAME` for a person. A refusal is said in one line, exit 1."""
    try:
        return _outcome(argv, env)
    except CrewError as e:
        sys.stdout.flush()
        print(e.line, file=sys.stderr)
        return 1


def _outcome(argv: list, env: dict) -> int:
    args = list(argv)
    if args[:1] == ["outcome"]:   # the dispatcher passes the command word too (TR-1598: it was taken for the call)
        args = args[1:]
    station = env.get("FACTORY_STATION", "")
    if "--station" in args:
        at = args.index("--station")
        station = args[at + 1] if at + 1 < len(args) else ""
        del args[at:at + 2]
    if not station:
        raise CrewError("name the station: tares factory outcome --station <name> ...")
    home = env.get("FACTORY_CREW_HOME") or env.get("FACTORY_HOME")
    paths = CrewPaths.at(home) if home else CrewPaths.from_env()
    if not args:
        rows = unknown(paths, station)
        for r in rows:
            print(f"{r.get('call')}  {r.get('command') or r.get('tool') or ''}" + (f"  (in {r['cwd']})" if r.get("cwd") else ""))
        if not rows:
            print(f"{station} has no call with an unknown outcome")
        return 0
    if len(args) < 3:
        raise CrewError("usage: tares factory outcome <call> done|not-done \"<what you found>\"")
    row = reconcile(paths, station, args[0], args[1], " ".join(args[2:]))
    print(f"recorded: {args[1]} for {row.get('command') or row.get('tool') or row.get('call')}")
    return 0


def _record_now(paths: CrewPaths, station: str, closing: str = "") -> None:
    """Record the station's session now (TR-1595): one look, or with `closing` (its app-server has ended) the
    closing of its open starts. Never fails: a recording Tares does not take is sent again later."""
    from . import codex_record   # here: it imports this module
    from .client import TaresClient
    client = TaresClient(paths, dict(os.environ))
    try:
        if closing:
            codex_record.close_station(client, paths, station, closing)
        else:
            codex_record.record_station(client, paths, station)
    except Exception:   # noqa: BLE001 - a stop is never kept from ending a station by its recording
        pass
    finally:
        try:
            client.close()
        except Exception:   # noqa: BLE001
            pass


def _trim_record(paths: CrewPaths, station: str, keep_turn: str) -> None:
    """Drop the lines of older turns from a record that grew large, keeping the newest turn's (a look by the
    watch may still compare it). Only between turns, when no hook of the station writes. Never fails. The
    session is recorded first: the lines dropped are where the starts of those turns' commands are read."""
    file = paths.station_file(station, codex_guard.RECORD)
    try:
        if file.stat().st_size <= RECORD_MAX:
            return
        _record_now(paths, station)
        kept = [r for r in codex_guard.read_record(file) if keep_turn and r.get("turn") == keep_turn]
        tmp = file.with_name(file.name + ".tmp")
        tmp.write_text("".join(state.dumps_compact(r) + "\n" for r in kept), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
    except OSError:
        pass


def _armed(wire: Wire, paths: CrewPaths, station: str, thread: str, turn: str) -> str:
    """"" once the guard's record shows it ran at the start of `turn`; else why the turn must not go on. The
    connection has joined the conversation, so it is told of the turn's items: Codex runs the hook before the
    first of them, so an item with no record yet means the conversation does not run its hooks."""
    def shown() -> bool:
        return any(r.get("armed") and r.get("turn") == turn for r in _record(paths, station))

    deadline = time.monotonic() + ARM_WAIT
    while True:
        if shown():
            return ""
        if time.monotonic() > deadline:
            return f"the guard left no record of the turn within {ARM_WAIT} s"
        try:
            msg = wire.next(0.05)
        except WireError:
            return "" if shown() else "the app-server hung up before the guard left a record of the turn"
        params = msg.get("params") if msg else None
        if not isinstance(params, dict) or params.get("threadId") not in (thread, None):
            continue
        if msg.get("method") == "item/started" and params.get("turnId") in (turn, None):
            kind = (params.get("item") or {}).get("type") if isinstance(params.get("item"), dict) else None
            return "" if shown() else (f"the turn began ({kind or 'an item'}) with no run of the guard before it: "
                                       "the conversation does not run its hooks")
        ended = params.get("turn") if msg.get("method") == "turn/completed" else None
        if isinstance(ended, dict) and ended.get("id") in (turn, None):
            return "" if shown() else "the turn ended with no run of the guard"


SEEN = "guardSeenTurn"   # on a station's row, beside `codex`: see `seen_by_a_person`


def mark_off(paths: CrewPaths, station: str, why: str, turn: str = "") -> None:
    """Write down that the station was stopped for its guard, on its row, when it has one. `turn` is the turn
    that showed it, when a comparison did."""
    row = state.stations_load(paths).get(station)
    if isinstance(row, dict):
        state.stations_put(station, {"codex": {**info(paths, station), "guard_off": {
            "at": state.utc_stamp(), "why": why, **({"turn": turn} if turn else {})}}}, paths)


def seen_by_a_person(paths: CrewPaths, station: str) -> None:
    """A person starts a station again that was stopped for what a turn ran (`tares factory revive`): that turn
    is written on the row, and is not held against the station again. Without this the start would compare the
    same turn, find the same thing and stop the station once more, every time. A turn after it is compared as
    any other. Nothing when the station was not stopped for a turn."""
    mark = info(paths, station).get("guard_off")
    turn = mark.get("turn") if isinstance(mark, dict) else None
    if turn:
        state.stations_put(station, {SEEN: str(turn)}, paths)


UNVERIFIED = "guardUnverified"   # on a station's row, beside `codex`: a start writes `codex` anew, and keeps this


def unverified(paths: CrewPaths, station: str) -> dict:
    """{} or {"thread", "why", "at"}: the conversation whose last turn could not be compared with the guard's
    record when the station was stopped, and has not been compared since."""
    row = state.stations_load(paths).get(station)
    mark = row.get(UNVERIFIED) if isinstance(row, dict) else None
    return mark if isinstance(mark, dict) and mark.get("thread") else {}


def mark_unverified(paths: CrewPaths, station: str, thread: str, why: str, event: bool = True) -> None:
    """Write on the station's row that the last turn of `thread` could not be compared, and record the event
    (`event` False: only the row, for a mark written again after the row was put back)."""
    row = state.stations_load(paths).get(station)
    if isinstance(row, dict):
        state.stations_put(station, {UNVERIFIED: {"at": state.utc_stamp(), "thread": thread, "why": why[:300]}}, paths)
        if event:
            state.event("guard-unverified", station, why[:300], paths=paths)


def _clear_unverified(paths: CrewPaths, station: str) -> None:
    data = state.stations_load(paths)
    row = data.get(station)
    if isinstance(row, dict) and UNVERIFIED in row:
        del row[UNVERIFIED]
        state.write_json(paths.stations, data)


def give_up_unverified(paths: CrewPaths, station: str, how: str) -> None:
    """A person's word that the conversation whose last turn was never compared is given up (`how`: the command
    that said so). The mark goes, and an event keeps that it was there. Nothing when there is no mark."""
    pending = unverified(paths, station)
    if pending:
        _clear_unverified(paths, station)
        state.event("guard-unverified-given-up", station, f"{how}: {pending.get('thread')}"[:300], paths=paths)
        # nobody will list that turn's items now: whatever it was let through has an outcome nobody knows
        last, held = state.stations_get(station, LAST_TURN, paths=paths), never_let_run(paths, station)
        note_unknown(paths, station, [r for r in unknown_of(last, None, _record(paths, station)) if r["call"] not in held])


def guard_off(paths: CrewPaths, station: str) -> str:
    """Why the station was stopped for its guard, "" when it was not. A new start clears it."""
    mark = info(paths, station).get("guard_off")
    return str(mark.get("why") or "its guard was not running") if isinstance(mark, dict) else ""


def _is(rec: dict, now: dict) -> bool:
    """Do two records name one app-server: the same process id, started at the same time?"""
    return isinstance(rec.get("pid"), int) and rec.get("pid") == now.get("pid") and rec.get("born") == now.get("born")


def _stop_unguarded(paths: CrewPaths, station: str, why: str, rec: dict, turn: str = "") -> bool:
    """Stop a station whose guard cannot be relied on, mark it and record the event. `rec` is the app-server
    that was looked at (`info`, read before the look): when the station's record names another one by now, it
    was started again since, and nothing is stopped or marked for what the old one did. Returns whether it
    was stopped."""
    if not _is(rec, info(paths, station)):
        return False
    stop(paths, station, compare=False)
    mark_off(paths, station, why, turn)
    state.event("guard-off", station, why[:300], paths=paths)
    return True


def check_guard(paths: CrewPaths, station: str) -> str:
    """Compare what the station's newest turn did with the guard's record; a station that ran something the
    guard has no record of is stopped. Returns why it was stopped, "" when nothing was found or nothing could
    be looked at. The watch calls this every round for a station that runs.

    The look and the stop are made under the station's lock, which every command that starts or stops a
    station takes: a station another command holds is left for the next round. The lock is let go at the end
    (one this process held before stays held)."""
    with locks.scope():
        try:
            locks.station_lock(station, paths)
        except StationLockedError:
            return ""
        rec = info(paths, station)
        thread = _recorded_thread(paths, station)
        if not thread or not _same_process(rec):
            return ""
        try:
            with visit(paths, station) as wire:
                why, turn = _audit(wire, thread, paths, station)
        except (CrewError, NotCompared):
            return ""
        return why if why and _stop_unguarded(paths, station, why, rec, turn) else ""


def failed_turn(paths: CrewPaths, station: str) -> tuple:
    """(the id of the station's newest turn, why it failed) when that turn failed, ("", "") when it did not
    or cannot be read. A turn that fails (a model the account cannot use, a limit, an error of Codex's own)
    leaves the conversation at rest: `turn_state` says "done", and the station did nothing."""
    thread = _recorded_thread(paths, station)
    if not thread:
        return "", ""
    try:
        with visit(paths, station) as wire:
            found, _ = _thread(wire, thread, turns=True)
    except CrewError:
        return "", ""
    turns = found.get("turns") if isinstance(found, dict) else None
    last = turns[-1] if isinstance(turns, list) and turns and isinstance(turns[-1], dict) else {}
    if last.get("status") != "failed":
        return "", ""
    error = last.get("error")
    why = error.get("message") if isinstance(error, dict) else error
    return str(last.get("id") or ""), " ".join(str(why or "Codex gave no reason").split())[:300]


# ---- what a station asks (TR-1593: approvals and questions) ----
#
# A turn that needs a yes stops and asks: Codex sends a request of its own to whoever is connected to the
# conversation, and the turn waits ("waiting" above). Measured on 0.171.0 with nobody connected: nothing runs;
# a visitor that only reads is asked nothing and sees no item for the call; a visitor that joins the
# conversation is asked again, with the same turn, item and start time, and a request id of its own
# connection; when one answers and leaves at once, the turn goes on by itself; after an answer nobody is
# asked again. So an approval needs nobody to stay connected: one visit reads what is asked, and a later one
# gives the answer, to that same request only.

ASKS = {"item/commandExecution/requestApproval": "command", "item/fileChange/requestApproval": "file",
        "item/tool/requestUserInput": "question"}
ASK_LISTEN = 2.0   # seconds a visit listens for what the station asks, after joining its conversation
ASK_DONE = 5.0     # seconds an answer may take to be taken (the app-server says so)


def _plain_command(command) -> str:
    """A command as the model asked for it: without the shell Codex wraps it in and without the line the
    guard put in front (`codex_guard.asked`)."""
    text = str(command or "")
    try:
        words = shlex.split(text)
    except ValueError:
        words = []
    if len(words) == 3 and words[1] in ("-lc", "-c") and os.path.basename(words[0]) in ("zsh", "bash", "sh"):
        text = words[2]
    return codex_guard.asked(text)


def ask_of(doc: dict) -> dict:
    """What a request of the app-server's own asks, in the crew's words: {kind, method, thread, turn, item,
    started, approval, key, command, cwd, reason, root, questions}. `kind` is command, file, question or
    other (a request the crew has no answer for). `key` names the request whichever connection it came on;
    two visits that are asked the same thing get the same key."""
    method = str(doc.get("method") or "")
    p = doc.get("params") if isinstance(doc.get("params"), dict) else {}
    ask = {"kind": ASKS.get(method, "other"), "method": method, "thread": str(p.get("threadId") or ""),
           "turn": str(p.get("turnId") or ""), "item": str(p.get("itemId") or ""), "started": p.get("startedAtMs"),
           "approval": p.get("approvalId")}
    name = state.dumps_compact([ask[k] for k in ("method", "thread", "turn", "item", "started", "approval")])
    ask["key"] = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    ask.update(command=_plain_command(p.get("command")) if p.get("command") else "", cwd=str(p.get("cwd") or ""),
               reason=str(p.get("reason") or ""), root=str(p.get("grantRoot") or ""),
               questions=p.get("questions") if isinstance(p.get("questions"), list) else [])
    return ask


def _asked_now(wire: Wire, start: dict, thread: str, listen: float) -> list:
    """Join the conversation and read what it asks: [(the request's id on this connection, ask)]. A request
    arrives right after the join; reading stops when none came for `listen` seconds."""
    wire.request("thread/resume", {**start, "threadId": thread})
    found = []
    while True:
        doc = wire.asked(listen if not found else min(listen, 0.5))
        if doc is None:
            return found
        found.append((doc.get("id"), ask_of(doc)))


def _join(paths: CrewPaths, station: str) -> tuple:
    doc = settings(paths, station)
    cwd = state.stations_get(station, "cwd", paths=paths) or paths.raw
    servers = doc.get("mcp_servers") if isinstance(doc.get("mcp_servers"), dict) else {}
    return _thread_start(doc["thread"], cwd, servers), _recorded_thread(paths, station)


def asked(paths: CrewPaths, station: str, listen: float = ASK_LISTEN):
    """What the station waits for an answer to: a list of asks (`ask_of`), [] when it waits for nothing, None
    when it could not be asked now (it does not answer, or its conversation cannot be read: nothing is known).
    One visit that joins the conversation only when it waits, reads and leaves; nothing is answered.

    The caller holds the station's lock."""
    try:
        start, thread = _join(paths, station)
        if not thread:
            return []
        with visit(paths, station) as wire:
            kind = _where(wire, thread)
            if kind == "unreadable":
                return None
            if kind != "waiting":
                return []
            return [ask for _, ask in _asked_now(wire, start, thread, listen)]
    except (CrewError, WireError):
        return None


def answer(paths: CrewPaths, station: str, key: str, result: dict, listen: float = ASK_LISTEN) -> str:
    """Give the station the answer to one thing it asked. Returns "answered", or why not: "gone" when the
    station no longer asks it (the turn ended, somebody else answered, the station was started again), or
    "unreachable" when it could not be asked now (try again later).

    The request is looked for again on this visit and answered only when it is the same one (`key`: the same
    conversation, turn, item and start time). An answer kept for a request that is no longer asked is never
    given to another one. The caller holds the station's lock, and has checked the process is the one it
    looked at."""
    try:
        start, thread = _join(paths, station)
        if not thread:
            return "gone"
        with visit(paths, station) as wire:
            kind = _where(wire, thread)
            if kind in ("unreadable", "active"):   # active: between two asks of one turn; nothing to answer now
                return "unreachable" if kind == "unreadable" else "gone"
            if kind != "waiting":
                return "gone"
            for request_id, ask in _asked_now(wire, start, thread, listen):
                if ask["key"] != key:
                    continue
                wire.answer(request_id, result)
                deadline = time.monotonic() + ASK_DONE
                while time.monotonic() < deadline:   # leaving before it is taken could lose it
                    told = wire.next(deadline - time.monotonic())
                    if told is None:
                        break
                    if told.get("method") == "serverRequest/resolved":
                        return "answered"
                return "answered" if _where(wire, thread) != "waiting" else "unreachable"
            return "gone"
    except (CrewError, WireError):
        return "unreachable"


def hand(paths: CrewPaths, station: str, prompt: str, *, fresh: bool = False) -> dict:
    """Hand the station a prompt: one turn start on its conversation. Returns {thread, turn, replaced}.

    The recorded conversation is carried on: joined (`thread/resume`) whether the app-server still has it open
    or was started anew since, so that this connection is told of the turn's first events. `fresh`, or a
    conversation the app-server no longer knows, starts a new one from the station's settings; `replaced`
    says a recorded one was given up, and an event records it. Dies (NotBetweenTurnsError) when the station is
    in a turn, (GuardOffError) when its guard is found not to run, after stopping it (see the module's
    note), and (CrewError) when it is not running or refuses, or when what its last turn ran cannot be
    compared with the guard's record now (the prompt is then not handed over; a later try may be).

    The caller holds the station's lock, as every command that starts or stops a station does."""
    doc = settings(paths, station)
    rec = info(paths, station)
    pending = unverified(paths, station)
    cwd = state.stations_get(station, "cwd", paths=paths) or paths.raw
    servers = doc.get("mcp_servers") if isinstance(doc.get("mcp_servers"), dict) else {}
    start = _thread_start(doc["thread"], cwd, servers)
    allowed = set(servers)
    recorded = _recorded_thread(paths, station)
    thread, replaced, turn, off, shown = ("" if fresh else recorded), False, "", "", ""
    anew = False   # the conversation was loaded again by an app-server that did not have it open
    untold = []    # the calls with an unknown outcome this prompt tells the station of
    with visit(paths, station) as wire:
        try:
            last = ""
            if thread:
                kind = _where(wire, thread)
                if kind in ("active", "waiting"):
                    raise NotBetweenTurnsError(f"{station} is in a turn; not handing it a prompt")
                if kind == "unreadable":   # never taken for a lost conversation: that would start a new one
                    raise CrewError(f"the conversation of {station} cannot be read now; not handing it a prompt")
                try:
                    wire.request("thread/resume", {**start, "threadId": thread})
                    anew = kind == "notLoaded"
                except WireError as e:
                    if mcp_refused(e):   # Tares did not answer: the conversation is there, and is kept
                        raise CrewError(f"{station} cannot reach an MCP server it is started with "
                                        f"({mcp_refused(e)}); not handing it a prompt") from None
                    if kind != "notLoaded":   # one the app-server has open is not given up for this
                        raise CrewError(f"could not join the conversation of {station}: {e}") from None
                    if pending:   # its last turn was never compared, and now cannot be: only a person gives it up
                        raise CrewError(
                            f"the conversation of {station} cannot be loaded ({e}), and what its last turn ran was "
                            f"never compared with the guard's record ({pending.get('why')}); `tares factory revive "
                            f"{station} --fresh` starts it in a new conversation without that") from None
                    if not last_turn_compared(paths, station):   # never compared, and now it cannot be
                        mark_unverified(paths, station, thread, f"its conversation cannot be loaded ({e})"[:300])
                        raise NeverComparedError(station, thread, str(e)[:200]) from None
                    thread, replaced = "", True   # the app-server was started again since and cannot load it
            if pending and thread != pending.get("thread"):   # a turn anywhere else would leave it never compared
                raise CrewError(
                    f"{station} is not started in a new conversation: what the last turn of its own ran could not be "
                    f"compared with the guard's record ({pending.get('why')}). `tares factory revive {station}` "
                    f"starts it in its own, where the comparison is made first; `tares factory revive {station} "
                    "--fresh` starts it in a new one without the comparison")
            if not thread:
                try:
                    thread = str((wire.request("thread/start", start).get("thread") or {}).get("id") or "")
                except WireError as e:
                    if mcp_refused(e):
                        raise CrewError(f"{station} cannot reach an MCP server it is started with "
                                        f"({mcp_refused(e)}); not handing it a prompt") from None
                    raise
                if not thread:
                    raise CrewError(f"{station} gave no conversation to start a turn in")
            off = _brought_along(wire, cwd, allowed)
            if not off and thread == recorded:
                try:
                    off, last = _audit(wire, thread, paths, station)
                    shown = last if off else ""
                except NotCompared as e:   # not known to be in order: the next turn waits until it is
                    raise CrewError(f"what the last turn of {station} ran cannot be compared with the guard's "
                                    f"record now ({e}); not handing it a prompt") from None
            if not off:
                blind = mcp_ready(wire, thread, servers) or mcp_answers(wire, thread, doc.get("mcp_check"))
                if blind:
                    raise CrewError(f"{station} would work without Tares: {blind}; not handing it a prompt")
                if anew:   # what the turn that was cut off started and never ended (see `unknown_of`)
                    try:
                        cut = _last_turn(wire, thread) or {}
                    except NotCompared as e:
                        raise CrewError(f"what the last turn of {station} left unfinished cannot be listed now "
                                        f"({e}); not handing it a prompt") from None
                    held = never_let_run(paths, station)
                    note_unknown(paths, station, [r for r in unknown_of(str(cut.get("id") or ""), cut.get("items"),
                                                                       _record(paths, station)) if r["call"] not in held])
                untold = [r for r in unknown(paths, station) if not r.get("told")]
                prompt += unknown_note(untold)
                _trim_record(paths, station, last)
                params = {"threadId": thread, "input": [{"type": "text", "text": prompt}]}
                if doc.get("effort"):
                    params["effort"] = doc["effort"]
                turn = str((wire.request("turn/start", params).get("turn") or {}).get("id") or "")
                off = _armed(wire, paths, station, thread, turn)
                if off:
                    try:
                        wire.request("turn/interrupt", {"threadId": thread, "turnId": turn})
                    except WireError:
                        pass
        except WireError as e:
            raise CrewError(f"could not hand {station} its prompt: {e}") from None
    if off:
        if not _stop_unguarded(paths, station, off, rec, shown):
            raise CrewError(f"{station} was started again while it was handed a prompt; try again")
        raise GuardOffError(station, off, shown)
    state.stations_put(station, {"sessionId": thread, LAST_TURN: turn,
                                 "codex": {**info(paths, station), "turn": turn, "turn_at": state.utc_stamp()}}, paths)
    if pending:   # the same conversation, compared above before its turn was started
        _clear_unverified(paths, station)
    if untold:   # named in the prompt that was just taken
        rows = unknown(paths, station)
        state.write_json(paths.station_file(station, codex_guard.UNKNOWN), [{**r, "told": True} for r in rows], mode=0o600)
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
    that what outlives the app-server can be told from any other process afterwards. Never fails.

    A process written down before stays written down while it runs (same id, same start time), with what
    runs under it. A command that outlives the shell that started it is handed to the system's first process:
    the next look under the app-server no longer finds it, and on macOS its environment may not say whose
    it is either, so forgetting it here would leave it running after a stop."""
    rec = info(paths, station)
    if not _same_process(rec):
        return
    try:
        seen = psutil.Process(rec["pid"]).children(recursive=True)
    except (psutil.Error, OSError):
        return
    for p in _noted(paths, station):
        seen.append(p)
        try:
            seen.extend(p.children(recursive=True))
        except (psutil.Error, OSError):
            pass
    born = {}
    for p in seen:
        try:
            if p.pid != rec["pid"]:
                born[p.pid] = p.create_time()
        except (psutil.Error, OSError):
            continue
    rows = [[pid, born[pid]] for pid in sorted(born)]
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


def _compared_at_stop(paths: CrewPaths, station: str, thread: str) -> tuple:
    """The station's newest turn, compared with the guard's record once more before its app-server is ended:
    (what it shows the guard did not do, that turn's id, why it could not be compared). ("", id, "") is a turn
    that was compared and is in order; one that could not be looked at is never that. Never fails."""
    try:
        with visit(paths, station, timeout=5) as wire:
            return (*_audit(wire, thread, paths, station, ended=True), "")
    except NotCompared as e:
        return "", "", str(e)
    except CrewError as e:
        return "", "", f"the station did not answer ({e})"


def stop(paths: CrewPaths, station: str, *, turn_wait: float = TURN_STOP_WAIT, grace: float = cx.STOP_GRACE,
         sleep=time.sleep, compare: bool = True) -> str:
    """End the station's app-server (see the module's note for the order). Returns what it took: "not running",
    "stopped" (it ended when asked) or "killed". The socket's path in the crew home goes with it, and so does
    whatever the station had started that still runs (`end_leftovers`): after a stop nothing of it is left.

    Its session is recorded last (TR-1595): one look while the app-server answers, then, once it has ended,
    every command that started and has no end is recorded as one whose outcome nobody will know.

    Before the app-server is ended its newest turn is compared with the guard's record once more (`compare`),
    since nothing can ask it afterwards: a station that ran something the guard has no record of is marked
    (`guard_off`) and the event recorded, as when the watch finds it. When the comparison cannot be made the
    stop goes on, and the station's row says so (`unverified`): the turn is not known to be in order."""
    rec = info(paths, station)
    sock = rec.get("socket") or str(cx.socket_path(paths, station))
    if not _same_process(rec):
        cx.remove_socket(sock)
        end_leftovers(paths, station, grace=grace, sleep=sleep)
        _record_now(paths, station, closing="the station was not running when it was stopped")
        return "not running"
    pid = rec["pid"]
    _interrupt(paths, station, turn_wait, sleep)
    thread = _recorded_thread(paths, station) if compare else ""
    why, shown, unknown = _compared_at_stop(paths, station, thread) if thread else ("", "", "")
    _record_now(paths, station)   # the last look, while the app-server still answers (TR-1595)
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
    _record_now(paths, station, closing="the station was stopped while the command ran; how it ended was not recorded")
    if why:
        mark_off(paths, station, why, shown)
        state.event("guard-off", station, why[:300], paths=paths)
    elif unknown:
        mark_unverified(paths, station, thread, unknown)
    return how
