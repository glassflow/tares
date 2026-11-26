"""What `tares factory doctor` checks for a crew on Codex (TR-1593, TR-1598): nothing of Claude Code's.

A crew on Codex is asked for what its stations start on and run with, and never for `claude`:

  the machine   the `codex` program, its version against the oldest one a station starts on
                (codex.TESTED_VERSION), the person's login file, and the kit's guard (`hooks/guard.py`
                under the plugin root, which `tares hook codex-guard` loads) with a `tares` on PATH to
                run it;
  a station     the app-server it runs on and its Codex version; its own Codex folder (the owner's
                alone, the login a link to the person's, no setup of its own); its guard: registered in
                the settings its conversation starts from for every tool call and every turn start,
                with the hook trust the thread needs, the record the guard left of its last turn, and
                the two marks a station carries when its guard was found not to run or its last turn
                could not be compared; and, while it runs, its connection to the Tares MCP endpoint
                (`station_mcp`: asked of its app-server on its own conversation, each server connected
                and a read going through with its identity and the crew's key).

Every function returns rows `(level, id, message, fix)` with level "ok", "warn" or "fix", for
`doctor.Doctor.add`. Nothing here starts, stops or changes a station: a doctor reports.

Standard library, psutil through the crew modules.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import time
from pathlib import Path

from . import codex as cx
from . import codex_guard, codex_station, state
from .errors import CrewError
from .paths import ME, CrewPaths

GUARD_WORDS = ("tares", "hook", "codex-guard")
GUARD_EVENTS = ("PreToolUse", "UserPromptSubmit")
# what codex.home_config writes, and nothing else, belongs in a station's own config.toml
_OWN_CONFIG = re.compile(r'^(\[projects\."(?:[^"\\]|\\.)*"\]|trust_level = "untrusted")$')


def machine(env: dict) -> tuple:
    """(rows, the installed Codex version or ""): the `codex` program, its version and the person's login."""
    rows = []
    version = ""
    try:
        path = cx.codex_bin(env)
    except CrewError as e:
        rows.append(("fix", "codex", e.message, "install the Codex CLI, or set CODEX_BIN to its path"))
        path = ""
    if path:
        notes: list = []
        try:
            version = cx.check_version(cx.codex_version(path), notes.append)
        except CrewError as e:
            rows.append(("fix", "codex", f"codex: {path}: {e.message}", "update the Codex CLI"))
        else:
            if notes:
                rows.append(("warn", "codex", f"codex: {path} ({version}); {notes[0]}",
                             f"use Codex {cx._dotted(cx.TESTED_VERSION)}, or check the stations on this one"))
            else:
                rows.append(("ok", "codex", f"codex: {path} ({version})", ""))
    login = cx.person_login(env)
    if login.is_file():
        rows.append(("ok", "codex_login", f"Codex login: {login}", ""))
    else:
        rows.append(("fix", "codex_login", f"no Codex login at {login}: a station runs on the login of the person who "
                     "starts it (a login kept in the system keyring, or an API key in the environment, is not used "
                     "by a station yet)", "codex login"))
    return rows, version


def kit(root: str, env: dict) -> list:
    """The guard a Codex station runs: the kit's guard.py, and the `tares` that loads it."""
    guard = Path(root, "hooks", "guard.py")
    if not guard.is_file():
        return [("fix", "plugin", f"the kit at {root} has no hooks/guard.py: a Codex station's guard cannot run, and "
                 "a station whose guard cannot run does no work", "reinstall tares")]
    if not shutil.which("tares", path=env.get("PATH")):
        return [("fix", "plugin", "tares is not on PATH: a Codex station's guard is `tares hook codex-guard`, and a "
                 "station whose guard cannot run does no work", "put the folder that holds tares on PATH")]
    return [("ok", "plugin", f"stations run the guard of the kit at {root}", "")]


def models(paths: CrewPaths) -> list:
    """A warning for each role crew.json sets to a Claude model (its template does): Codex takes none, so
    that role's stations run on the model Codex gives the account by default."""
    rows = []
    for role in ("orchestrator", "reviewer", "releaser", "builder"):
        model = state.ccfg("stations", role, "model", paths=paths)
        if cx.claude_model(model):
            rows.append(("warn", f"model_{role}", f"station {role} is set to {model}, a Claude model: on Codex it runs "
                         "on the model and effort the account gives by default",
                         f"set stations.{role}.model in crew.json to a Codex model to choose one"))
    return rows


def _ago(at, now: float) -> str:
    try:
        s = max(0, int(now - float(at)))
    except (TypeError, ValueError):
        return "at an unknown time"
    if s < 120:
        return f"{s} s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def home_problems(paths: CrewPaths, station: str, env: dict) -> list:
    """What is wrong with the station's own Codex folder, one sentence each; [] when it is in order."""
    home = cx.station_home(paths, station)
    try:
        found = os.lstat(home)
    except OSError:
        return [f"{home} does not exist"]
    if not stat.S_ISDIR(found.st_mode):
        return [f"{home} is not a folder"]
    out = []
    if hasattr(os, "getuid") and (found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) & 0o077):
        out.append(f"{home} is open to others (mode {stat.S_IMODE(found.st_mode):o})")
    link = home / "auth.json"
    login = cx.person_login(env)
    if not link.is_symlink():
        out.append(f"{link} is not a link to the person's login" if link.exists() else f"{link} is missing")
    elif os.path.realpath(link) != str(login):
        out.append(f"{link} points at {os.path.realpath(link)}, not at the person's login {login}")
    try:
        text = (home / "config.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    extra = [line.strip() for line in text.splitlines() if line.strip() and not _OWN_CONFIG.match(line.strip())]
    if extra:
        out.append(f"its config.toml holds a setup of its own ({extra[0][:60]})")
    return out


def _guard_commands(doc: dict, event: str) -> list:
    config = (doc.get("thread") or {}).get("config")
    groups = (config.get("hooks") or {}).get(event) if isinstance(config, dict) else None
    out = []
    for group in groups if isinstance(groups, list) else []:
        for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                out.append(hook["command"])
    return out


def _is_guard(command: str) -> bool:
    try:
        return tuple(shlex.split(command)[:3]) == GUARD_WORDS
    except ValueError:
        return False


def guard_registered(paths: CrewPaths, station: str) -> str:
    """Why the settings the station's conversation starts from do not run the guard, "" when they do."""
    try:
        doc = codex_station.settings(paths, station)
    except CrewError as e:
        return e.message
    for event in GUARD_EVENTS:
        if not any(_is_guard(c) for c in _guard_commands(doc, event)):
            return f"its settings register no guard for {event}"
    config = doc["thread"].get("config")
    if not (isinstance(config, dict) and config.get("bypass_hook_trust") is True):
        return "its settings do not give the thread its trust for hooks, and Codex skips a hook it was not told to trust"
    return ""


def guard(paths: CrewPaths, station: str, now: float | None = None) -> tuple:
    """One row for the station's guard: how it is registered, what its record says, and its marks."""
    now = time.time() if now is None else now
    cid = f"codex_guard_{station}"
    off = codex_station.guard_off(paths, station)
    if off:
        return ("fix", cid, f"{station} was stopped because its guard was found not to run ({off}); the watch leaves "
                f"it down. Read what its last turn did, then {ME} revive {station}", f"{ME} revive {station}")
    why = guard_registered(paths, station)
    if why:
        return ("fix", cid, f"the guard of {station}: {why}", f"{ME} restart {station}")
    pending = codex_station.unverified(paths, station)
    if pending:
        return ("warn", cid, f"what the last turn of {station} ran was never compared with its guard's record "
                f"({pending.get('why')}); {ME} revive {station} compares it first, in the same conversation",
                f"{ME} revive {station}")
    record = codex_guard.read_record(paths.station_file(station, codex_guard.RECORD))
    armed = [r for r in record if r.get("armed")]
    if not armed:
        if state.stations_get(station, "sessionId", paths=paths):
            return ("warn", cid, f"the guard of {station} is registered, and left no record of a turn's start; the "
                    "watch stops a station whose turn shows no record", f"{ME} crew watch")
        return ("ok", cid, f"the guard of {station} is registered for every tool call and every turn start; no turn "
                "has started yet", "")
    last = armed[-1]
    calls = [r for r in record if r.get("call") and r.get("turn") == last.get("turn")]
    denied = sum(1 for r in calls if r.get("said") == "deny")
    told = f"{len(calls)} call{'' if len(calls) == 1 else 's'} checked" + (f", {denied} refused" if denied else "")
    return ("ok", cid, f"the guard of {station} ran at the start of its last turn, {_ago(last.get('at'), now)} "
            f"({told})", "")


def station_mcp(paths: CrewPaths, station: str) -> tuple:
    """The running station's connection to the Tares MCP endpoint, asked of its app-server on its own
    conversation (codex_station.mcp_health): each server connected, and a read going through with the station's
    identity and the crew's key."""
    level, said = codex_station.mcp_health(paths, station)
    fix = {"ok": "", "warn": f"{ME} doctor again later",
           "fix": f"{ME} doctor (the tares and mcp rows say why the endpoint refuses), then {ME} restart {station}"}
    return (level, f"codex_mcp_{station}", said, fix[level])


def station(paths: CrewPaths, name: str, row: dict, env: dict, installed: str, now: float | None = None) -> list:
    """The rows of one active station on Codex."""
    rows = []
    rec = row.get("codex") if isinstance(row.get("codex"), dict) else {}
    runs = str(rec.get("version") or "")
    alive = codex_station.is_alive(paths, name)
    if not alive and codex_station.guard_off(paths, name):
        rows.append(("warn", f"codex_{name}", f"{name} is not running", f"{ME} revive {name}"))
    elif not alive:
        rows.append(("warn", f"codex_{name}", f"{name} is not running; the watch revives it in its conversation",
                     f"{ME} crew watch, or {ME} revive {name}"))
    elif installed and runs and runs != installed:
        rows.append(("warn", f"codex_{name}", f"{name} runs Codex {runs}, and {installed} is installed; it moves to "
                     "that one when it is started again", f"{ME} restart {name}"))
    else:
        rows.append(("ok", f"codex_{name}", f"{name} runs Codex {runs or '(version not recorded)'}", ""))
    wrong = home_problems(paths, name, env)
    if wrong:
        rows.append(("fix", f"codex_home_{name}", f"the Codex folder of {name}: {'; '.join(wrong)}",
                     f"{ME} restart {name} (a start sets the folder up again)"))
    else:
        rows.append(("ok", f"codex_home_{name}", f"the Codex folder of {name} is its own "
                     f"({cx.station_home(paths, name)}), with the person's login linked and no setup of its own", ""))
    rows.append(guard(paths, name, now))
    if alive:   # a station that is not running has no connection to look at: its first row says so
        rows.append(station_mcp(paths, name))
    return rows
