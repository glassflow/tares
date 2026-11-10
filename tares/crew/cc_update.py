"""Moving the stations onto a newer Claude Code, in the same conversation, one idle station a round (TR-963).

Ports these functions of `claude-plugin/bin/factory`, with the same behaviour and the same printed text:
station_cc, prune_spares, duplicate_sessions, claude_auto, cc_behind, tell_newer_claude, cc_movable, cc_pick,
claude_move and watch_claude_move, and the Claude Code part of the watch's round (`step`). Where this module
and the bash differ, the bash is right; every difference is listed under "Differences from bash" below.

Standard library plus the crew modules and psutil (no duckdb, fastapi or daemon).

How a move goes
  * A new Claude Code is installed by swapping a symlink, and a running station keeps the version it started
    on. The watch counts a version as new once `claude --version` has said the same for a whole round.
  * Each round it picks one station that runs an older version (cc_pick): an idle one that nobody has open,
    then a helper, the orchestrator only when no other station is behind. claude_move restarts it in its own
    conversation and checks its process runs the new version. Claude Code's daemon keeps pre-warmed spare
    sessions, and a restart can claim one of an older version (TR-669): an older spare nobody has claimed is
    stopped before each try (prune_spares), and a station that still comes back on one is tried again. The last
    try of a version starts the station in a new conversation, once per version; a helper never is, since its
    task lives only in its conversation.
  * claude_move answers 0 (it runs the new version, or its version cannot be told), 3 (Tares does not answer),
    4 (it is not idle any more, someone opened it, or the crew is halted), 5 (an older spare came back every
    time; the station is marked cc_failed with a retry time), 6 (the plan's limit is full) or 1 (anything else).

Stubs, each behind one small named function so the ticket that owns the real thing replaces just that
  * cc_backoff (TR-964): never waiting. A station that failed a move is tried again the next round.
  * cc_delay (TR-964): FACTORY_CC_BACKOFF seconds (default 600), no doubling and no FACTORY_CC_BACKOFF_MAX.
  * cc_retry_sync (TR-964): does nothing, so the Crew page's retry flags are not sent.
  * _pace_closed (TR-962): reads stations.pace_read_stub, which says "not paced" until the pace is ported.
  * upgrades_frozen (TR-965): views.Frozen, the read-only copy of the freeze check.
cc_retry_in (the text of the retry time) is views.cc_retry_in, imported when needed (TR-964 owns it).

Differences from bash
  * The kit upgrade (upgrade_station, TR-965) shares an `if ... elif` with the Claude Code move in the bash
    round: a kit upgrade this round means no Claude Code move. The kit part is not ported; `step` marks where
    it chains in.
  * `step` runs after the revive step has had its turn in the same round (the steps run in order, each with its
    own reading). A station the revive step started this round is up when `step` looks, where bash still counts
    it as down for this round. It has just started, so it is not quiet and is not picked.
  * The unreadable session list is reported once, by the revive step; `step` just moves nothing then.
  * prune_spares reads the process list with psutil on every platform (bash runs `ps -axo pid=,command=`), and
    ends a spare with a polite terminate (SIGTERM; TerminateProcess on Windows), as `kill` does. A process this
    user may not inspect or end is skipped, as it is invisible to `ps` and `kill` too. A Windows path
    (`...\\versions\\<v>`) in a command line names the version as well.
  * claude_move works with a client of its own, so the settings and the answer of /health it reads are new, as
    bash's subshell does (the caller's client is not touched). A CrewError or an OSError from a restart is
    reported with `say` and the exit code is 1, as bash's errexit does.
  * cc_seen (the version the last round saw) is module state, set from `claude --version` the first time `step`
    runs, as cmd_watch does once when it starts. `reset` clears it (tests).

Untested on real Windows (TR-946 is deferred): the process listing and terminate in prune_spares go through
psutil, whose Windows command lines are taken on trust, as in claude.py.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time

import psutil

from . import claude, launch, lifecycle, stations, state
from .client import TaresClient
from .errors import CrewError, SessionListUnreadableError
from .paths import ME, CrewPaths

DEFAULT_TRIES = 3          # restarts of one version before the last, new-conversation one (FACTORY_CC_TRIES)
DEFAULT_BACKOFF = 600      # seconds a station waits after a failed move (FACTORY_CC_BACKOFF)
VERSION_POLLS = 6          # looks at a restarted station's version before it counts as unknowable
_SPARE = re.compile(r"bg-pty-host.*--bg-spare.*\.claim\.sock")
_SPARE_SOCKET = re.compile(r"--bg-spare ([^ ]+\.claim\.sock)")

_cc_seen = None            # the version the last round saw; None until the first round (bash: cc_seen)


def reset() -> None:
    """Forget the version the last round saw (a new watch process, or a test)."""
    global _cc_seen
    _cc_seen = None


# ---------- what a station runs ----------


def _active(paths: CrewPaths) -> list:
    return [s for s, row in state.stations_load(paths).items()
            if isinstance(row, dict) and row.get("status") == "active"]


def station_cc(paths: CrewPaths, station: str, agents=None) -> str:
    """The Claude Code version a station's process runs; "" when it cannot be told. `agents` is a session list
    already read (without one it is read here)."""
    return claude.running_version(state.stations_get(station, "id", paths=paths), agents)


def cc_behind(paths: CrewPaths, target: str, agents=None) -> int:
    """How many stations that matter run an older Claude Code than target: active, not finished. An unreadable
    session list counts none."""
    if agents is None:
        try:
            agents = claude.active_json()
        except SessionListUnreadableError:
            return 0
    n = 0
    for s in _active(paths):
        if paths.done_file(s).exists():
            continue
        if claude.older_than(station_cc(paths, s, agents), target):
            n += 1
    return n


# ---------- spare sessions ----------


def _listing():
    """[(pid, command line)] of the processes this user can see; the command line joined by spaces, as
    `ps -o command=` shows it."""
    out = []
    for pid in psutil.pids():
        try:   # one process at a time: psutil on macOS can raise SystemError for one that is just ending
            out.append((pid, " ".join(psutil.Process(pid).cmdline())))
        except (psutil.Error, OSError, SystemError):
            continue
    return out


def prune_spares(paths: CrewPaths, target: str) -> None:
    """Stop the pre-warmed spares of an older Claude Code that nobody has claimed (their claim socket is still
    there), so a restart cannot land on one. Records event spare-pruned (no station, the spare's version) for
    each. A claimed spare (no socket: a session runs in it), a spare of the target or a newer version, and one
    whose socket went away since the listing are left alone."""
    for pid, cmd in _listing():
        if not _SPARE.search(cmd):
            continue
        m = _SPARE_SOCKET.search(cmd)
        if not m:
            continue
        sock = m.group(1)
        if not os.path.exists(sock):
            continue   # claimed: a session runs in it
        v = claude._VERSIONS_IN_COMMAND.search(cmd)
        v = v.group(1) if v else ""
        if not claude.older_than(v, target):
            continue
        if not os.path.exists(sock):
            continue   # claimed since the listing: leave it
        try:
            psutil.Process(pid).terminate()
        except (psutil.Error, OSError, SystemError):   # gone, or not ours to end
            continue
        state.event("spare-pruned", "", v, paths=paths)


def duplicate_sessions(paths: CrewPaths, binary=None) -> list:
    """A line "name: id, id" for each station that more than one live session answers to. An unreadable
    session list or stations file gives none."""
    if not paths.stations.is_file():
        return []
    try:
        names = set(state.stations_load(paths))
        rows = claude.active_json(binary)
    except (CrewError, SessionListUnreadableError, OSError):
        return []
    groups: dict = {}
    for r in rows:
        name = r.get("name")
        if isinstance(name, str) and name in names and not claude.gone(r):
            groups.setdefault(name, []).append(r)
    return [f"{name}: " + ", ".join("" if r.get("id") is None else state.jq_raw(r.get("id")) for r in grp)
            for name, grp in sorted(groups.items()) if len(grp) > 1]


# ---------- whether to move ----------


def claude_auto(client: TaresClient) -> bool:
    """Whether the watch may move stations onto a new Claude Code: the crew setting claude_update (default on),
    for people who pin a version. Tares not answering counts as off. The settings are read afresh, and the
    client's own copy is left as it is."""
    with TaresClient(client.paths, client.env, lambda _m: None) as fresh:
        try:
            settings = fresh.crew_settings()
        except CrewError:
            return False
    return state.jq_raw(settings.get("claude_update")) != "false"


def _pace_closed(client: TaresClient) -> bool:
    """Whether a plan limit is full and has not reset (bash: pace_read, then pace_closed). TR-962 replaces this
    with the real reading; until then stations.pace_read_stub says "not paced"."""
    pace = stations.pace_read_stub()
    return isinstance(pace, dict) and pace.get("at_limit") is True


def upgrades_frozen(paths: CrewPaths, client: TaresClient, *, fresh: bool = False) -> bool:
    """Whether the crew's kit is frozen. TR-965 owns the real port; this is views.Frozen, the read-only copy.
    `fresh` reads the settings again (bash: SETTINGS="" first)."""
    from . import views   # here, not at the top: views imports this module
    if fresh:
        with TaresClient(client.paths, client.env, lambda _m: None) as other:
            return views.Frozen(paths, other).on
    return views.Frozen(paths, client).on


def tell_newer_claude(paths: CrewPaths, client: TaresClient, version: str) -> None:
    """A frozen crew moves no station onto a newer Claude Code either, but says so: once for each version.
    Nothing behind removes the file that remembers which version it said it for."""
    n = cc_behind(paths, version)
    if n == 0:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(paths.cc_newer_file)
        return
    try:
        said = state.read_text(paths.cc_newer_file).rstrip("\n")
    except OSError:
        said = ""
    if said == version:
        return
    client.say(f"Claude Code {version} is newer than the one {n} station(s) run; not moving stations while the kit "
               f"is frozen ({ME} crew unfreeze lets them follow it)")
    state.event("claude-newer", "", version, paths=paths)
    state.atomic_write_text(paths.cc_newer_file, version + "\n")


# ---------- backoff stubs (TR-964 owns the real ones) ----------


def cc_backoff(paths: CrewPaths, station: str, target: str) -> bool:
    """Whether the station is waiting out the backoff after a failed move onto target. STUB (TR-964): never."""
    return False


def cc_delay(failed: int, env=None) -> int:
    """The seconds a station waits before the watch tries to move it again. STUB (TR-964): FACTORY_CC_BACKOFF
    (default 600) every time, no doubling."""
    try:
        return int((os.environ if env is None else env).get("FACTORY_CC_BACKOFF") or DEFAULT_BACKOFF)
    except ValueError:
        return DEFAULT_BACKOFF


def cc_retry_sync(paths: CrewPaths, client: TaresClient) -> None:
    """Tell Tares which stations are retrying a move. STUB (TR-964): nothing is sent."""


# ---------- picking a station ----------


def cc_movable(paths: CrewPaths, station: str, target: str, binary=None) -> bool:
    """True when the watch may restart the station now: a conversation to resume, quiet, nobody attached."""
    return (state.stations_get(station, "sessionId", paths=paths) != ""
            and not cc_backoff(paths, station, target)
            and not claude.attached(station, paths)
            and claude.quiet(station, claude=binary, paths=paths))


def cc_pick(paths: CrewPaths, target: str, binary=None) -> str:
    """The one station to move this round, "" when none: a station on an older Claude Code that is idle, then a
    helper, the orchestrator only once no other station is behind. A helper never holds the orchestrator back,
    since a helper can work for a long time; finished builders stay stopped, and a station backing off after a
    failed move does not hold the orchestrator back either. A station that is not running shows no version, so
    is not behind."""
    try:
        agents = claude.active_json(binary)
    except SessionListUnreadableError:
        return ""
    others_behind = False
    orch = helper = ""
    for s in _active(paths):
        role = state.stations_get(s, "role", paths=paths)
        if role != "helper" and paths.done_file(s).exists():
            continue
        if not claude.older_than(station_cc(paths, s, agents), target):
            continue
        if role == "orchestrator":
            orch = s
            continue
        if role == "helper":
            if not helper and cc_movable(paths, s, target, binary):
                helper = s
            continue
        if cc_backoff(paths, s, target):
            continue
        others_behind = True
        if cc_movable(paths, s, target, binary):
            return s
    if helper:
        return helper
    if not others_behind and orch and cc_movable(paths, orch, target, binary):
        return orch
    return ""


# ---------- the move ----------


def _int(text: str, default: int) -> int:
    try:
        return int(text)
    except ValueError:
        return default


def claude_move(paths: CrewPaths, client: TaresClient, station: str, target: str, *, claude_bin=None) -> int:
    """Restart a station on the Claude Code now on PATH, in the same conversation, and check its process runs
    it; an older spare from the pool is stopped and the restart tried again. When the tries keep landing on an
    old spare, the last one starts the station in a new conversation (once per version), so the station is never
    left on the old version. Exit status: 0 it runs the new version (or its version can't be told), 3 Tares
    doesn't answer, 4 it isn't idle any more or the crew is halted, 5 an older spare came back every time (the
    station is marked cc_failed with a retry time), 6 the plan's limit is full, 1 anything else."""
    with TaresClient(client.paths, client.env, client.say) as own:   # bash's subshell: nothing cached carries in
        try:
            return _move(paths, own, station, target, claude_bin)
        except (CrewError, OSError, subprocess.SubprocessError) as e:
            client.say(getattr(e, "message", "") or str(e))
            return 1


def _move(paths: CrewPaths, client: TaresClient, s: str, target: str, claude_bin) -> int:
    if not client.tares_on():
        return 3   # without Tares there are no settings to restart it with
    client.crew_settings()
    if state.halted(paths):
        return 4
    if _pace_closed(client):
        return 6
    if not (claude.quiet(s, claude=claude_bin, paths=paths) and not claude.attached(s, paths)):
        return 4   # it started something, or someone opened it, since the first check
    env = client.env
    tries = _int(env.get("FACTORY_CC_TRIES") or str(DEFAULT_TRIES), DEFAULT_TRIES)
    pause = launch._pause(env)
    helper = state.stations_get(s, "role", paths=paths) == "helper"
    if helper:
        skill = launch.parent_role(state.stations_get(s, "parent", paths=paths), paths)
    else:
        skill = state.stations_get(s, "role", paths=paths)
    prompt = (f"Claude Code was updated to {target}, so the factory restarted you on it, in this same conversation, "
              f"with fresh settings. Reload the skills factory-protocol and station-{skill}, re-read your report, "
              f"{lifecycle.rearm_text(client)}, then carry on where you were.")
    fresh_prompt = (f"Claude Code was updated to {target}, but your old conversation kept coming back on an older "
                    f"Claude Code, so the factory started you again under the same name, in a new conversation, on "
                    f"{target}. Nothing from the old one carries over except your report and what is on Tares. "
                    f"Reload the skills factory-protocol and station-{skill}, re-read your report and your work on "
                    f"Tares, {lifecycle.rearm_text(client)}, then carry on where your report left off.")
    total = tries
    if state.stations_get(s, "cc_fresh", paths=paths) != target:
        total = tries + 1   # one new conversation per version
    if helper:
        total = tries   # a helper's task lives only in its conversation (its spawn prompt), so it is never started fresh: it is retried after a backoff instead
    tried = 0
    while tried < total:
        tried += 1
        if tried > 1:
            if state.halted(paths) or claude.attached(s, paths):
                return 4   # the first restart started it on the prompt, so only a halt or someone opening it stops a retry
            if _pace_closed(client):
                return 6
        prune_spares(paths, target)
        if tried > tries:
            client.say(f"{s} still comes back on an older Claude Code after {tries} tries; starting it again in a "
                       f"new conversation on {target}")
            lifecycle.restart(client, paths, [s, "--fresh", fresh_prompt], claude_bin=claude_bin)
            state.stations_put(s, {"cc_fresh": target}, paths)   # after the restart: one that died leaves the new conversation to a later round
        else:
            lifecycle.restart(client, paths, [s, prompt], claude_bin=claude_bin)
        v = ""
        for _ in range(VERSION_POLLS):
            v = station_cc(paths, s)
            if v:
                break
            time.sleep(pause)
        if not claude.older_than(v, target):
            if not v:
                client.say(f"could not tell which Claude Code {s} runs; taking it for {target}")
            state.stations_put(s, {"cc_failed": None, "cc_fails": None, "cc_retry_at": None, "cc_fresh": None}, paths)
            cc_retry_sync(paths, client)
            state.event("claude-updated", s, v or target, paths=paths)
            return 0
        client.say(f"{s} came back on Claude Code {v}, an older spare from Claude Code's pool, not {target}; "
                   "stopping it and starting it again")
        state.event("stale-spare", s, v, paths=paths)
    fails = 0
    if state.stations_get(s, "cc_failed", paths=paths) == target:
        raw = state.stations_get(s, "cc_fails", paths=paths)
        fails = int(raw) if raw.isdigit() else 0
    fails += 1
    at = int(time.time()) + cc_delay(fails, env)
    state.stations_put(s, {"cc_failed": target, "cc_fails": fails, "cc_retry_at": at}, paths)
    cc_retry_sync(paths, client)
    state.event("claude-retry", s, target, paths=paths)
    return 5


def watch_claude_move(paths: CrewPaths, client: TaresClient, station: str, target: str, *, claude_bin=None) -> None:
    """The watch's turn at claude_move, with what it says."""
    if upgrades_frozen(paths, client, fresh=True):
        return   # frozen since the round began: the freeze wins
    if _pace_closed(client):
        return   # the plan's limit filled since the round began: wait for the reset (TR-674)
    client.say(f"{station} runs an older Claude Code than {target}; restarting it on {target} now that it's idle, "
               "in the same conversation")
    rc = claude_move(paths, client, station, target, claude_bin=claude_bin)
    helper = state.stations_get(station, "role", paths=paths) == "helper"
    if rc == 0:
        client.say(f"{station} runs Claude Code {target}")
        dup = "\n".join(duplicate_sessions(paths, claude_bin))
        if dup:
            client.say(f"TWO SESSIONS named {dup}; stop the one stations.json does not list (factory roster)")
            state.event("two-sessions", station, dup, paths=paths)
    elif rc == 3:
        client.say(f"Tares doesn't answer; {station} moves onto Claude Code {target} once it does")
    elif rc in (4, 6):
        pass
    elif rc == 5:
        from . import views   # TR-964 owns cc_retry_in; views.cc_retry_in is the copy status and roster use
        again = f"the watch tries again in {views.cc_retry_in(paths, station)}, and {ME} crew update tries again now"
        if helper:
            client.say(f"{station} keeps coming back on an older Claude Code than {target} (an old pre-warmed spare "
                       f"in Claude Code's pool); a helper keeps its conversation, since its task is only there; {again}")
        else:
            client.say(f"{station} keeps coming back on an older Claude Code than {target} (an old pre-warmed spare "
                       f"in Claude Code's pool), even in a new conversation; {again}")
    elif helper:
        client.say(f"could not restart {station} on Claude Code {target}; the watch does not revive helpers, so ask "
                   f"{state.stations_get(station, 'parent', paths=paths)} to spawn it again if it is down")
    else:
        client.say(f"could not restart {station} on Claude Code {target}; the watch revives it as before, and no "
                   "other station moves until it is up")


# ---------- the watch's round ----------


def _down(paths: CrewPaths, binary) -> bool:
    """Whether any station the watch revives is not running (or cannot be told): the revive step's rules, so
    a helper and a builder whose tickets are all merged do not count."""
    down = False
    for s in _active(paths):
        role = state.stations_get(s, "role", paths=paths)
        if role == "helper":
            continue
        if role == "builder" and paths.done_file(s).exists():
            continue
        try:
            if not claude.is_alive(s, "", binary, paths):
                down = True
        except SessionListUnreadableError:
            down = True
    return down


def step(paths: CrewPaths, *, env=None, claude_bin=None) -> None:
    """The Claude Code part of one watch round: when `claude --version` has said the same for a whole round and
    every station is up, the crew is not halted and the setting allows it, move one idle station onto it (a
    frozen crew is only told there is a newer one)."""
    global _cc_seen
    env = os.environ if env is None else env
    if claude_bin is None:
        claude_bin = claude.claude_bin(env)
    if _cc_seen is None:
        _cc_seen = claude.claude_version(claude_bin)   # cmd_watch reads it once before its first round
    cc_now = claude.claude_version(claude_bin)
    settled = bool(cc_now) and cc_now == _cc_seen   # the installer swaps a symlink: a version that flips back and forth is not a release
    _cc_seen = cc_now
    try:
        claude.active_json(claude_bin)
    except SessionListUnreadableError:
        return   # the revive step says so; nothing is moved this round
    down = _down(paths, claude_bin)
    with TaresClient(paths, env) as client:
        # TR-965 chains in here: bash's round is `if <a station is due a kit upgrade> then upgrade_station
        # elif <the Claude Code part below>`, so the Claude Code part must be skipped on a round that upgraded one.
        if settled and not down and not state.halted(paths) and claude_auto(client):
            # the same, for Claude Code itself: one idle station a round, the orchestrator last; a frozen crew
            # only hears that there is a newer one
            if upgrades_frozen(paths, client):
                tell_newer_claude(paths, client, cc_now)
            elif _pace_closed(client):
                pass   # the plan's limit is full: a restart is a model turn into it, so the move waits for the reset (TR-674)
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(paths.cc_newer_file)
                candidate = cc_pick(paths, cc_now, claude_bin)
                if candidate:
                    watch_claude_move(paths, client, candidate, cc_now, claude_bin=claude_bin)
        if settled and not down:
            cc_retry_sync(paths, client)   # the Crew page's retry flags follow the marks every round
