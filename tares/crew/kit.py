"""The kit and its freeze: `crew freeze`, `crew unfreeze`, and the kit part of the watch's round (TR-965).

Ports these functions of `claude-plugin/bin/factory`: cmd_freeze, cmd_unfreeze, freeze_fail, frozen_text, tell_newer_kit,
freezing, reap_frozen_copy, frozen_copy_in_use, on_frozen_copy, upgrades_frozen, frozen_field, frozen_phys, kit_dir,
kit_clean, kit_version, stale_kit, movable and upgrade_station, and the kit half of cmd_watch's round (`step`). The
functions that only read the freeze (`Frozen`, `plugin_root`, `kit_version`, `frozen_phys`, `stale_kit`) lived in
views.py and launch.py as read-only copies; they are here now, and views.py imports them back. Where this module and
the bash differ, the bash is right; every difference is listed under "Differences from bash" below.
installed_tares and clash_text (the plugin installed twice) are TR-956's.

What a freeze is
  * The crew's settings on Tares hold `kit.frozen = {at, by, version}`. The crew home holds a copy of the kit as it was
    (`frozen-kit/`), the checkout it was copied from (`kit-checkout`) and, while a freeze is under way, a marker
    (`freezing`, honoured for five minutes). A station that starts or revives while frozen runs from the copy.
  * A frozen crew's watch moves no station onto a newer kit (or a newer Claude Code); it says so once for each new
    version (`kit-newer`, `claude-newer`), and still revives a station that stopped. Unfreeze lets the existing upgrade
    path run. The copy goes once no active station runs from it.
  * Tares unreadable: the freeze is "on" exactly when the copy exists, so a blip never lets a station move.

The kit part of the watch's round (`step`, chained in front of cc_update.step by the `updates` step)
  * Every round: a kit that has held for a whole round in a clean checkout counts as an update (`settled`). A frozen
    crew is told once there is a newer kit; an unfrozen watch restarts itself on the new code (`watch.restart_self`),
    and otherwise removes the frozen copy when no station needs it (reap_frozen_copy). The checkout being dirty is said
    once.
  * Then, when every station is up and the crew is neither halted nor frozen, it restarts ONE idle station that runs
    an older kit (or the frozen copy that is going away) in its own conversation (upgrade_station). A round that
    did this makes no Claude Code move (the bash `if ... elif`); `step` answers whether it did.

Differences from bash
  * The kit candidate. Bash picks it inside the revive loop, as the first station that is up and has had no stuck,
    regrant, idle-wake or pace action. Here each of those is a step of its own (see watch.STEP_NAMES): the candidate is
    the first active station (helpers and finished builders are not looked at) that the revive step did not start this
    round (`watch_revive.last.started`) and that is `movable`. A regrant, a wake or a pace note a later step sends this
    round is a turn into the station, so it is not quiet by then, and upgrade_station's own quiet check refuses it.
  * `down` is `watch_revive.last.down`; a round whose session list could not be read (`last.unread`) moves nothing.
  * The restart of the watch itself is `watch.restart_self`: `os.execv` where the platform has it (the same pid, the
    same pid file), a new detached watch on Windows. Untested on real Windows (TR-946 is deferred).
  * The state of the watch (the kit it started on, the kit the last round saw, whether the dirty checkout was said)
    is module state, set the first time `step` runs; `reset` clears it (tests).
  * `freezing` is "the marker is under 300 seconds old"; `find -mmin -5` rounds the age up to whole minutes.
  * The kit copy is made with shutil, links kept as links. On Windows the paths take the \\\\?\\ long form (tidy.long_path),
    so a kit past 260 characters copies and goes; a read-only file of the checkout (a wheel install, say) is copied
    writable, so the copy can be removed later (tidy.remove_tree retries a read-only entry too). Bash's cp -R keeps the
    mode, and so a read-only copy on Windows, which `rm -rf` could not remove either.
  * `crew freeze` and `crew unfreeze` take no arguments and ignore any, as bash does.

Untested on real Windows (TR-946 is deferred): the long-path copy and removal, the read-only handling and the
Windows branch of `watch.restart_self`.

Standard library plus the crew modules (no duckdb, fastapi or daemon).
"""
from __future__ import annotations

import contextlib
import getpass
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from . import claude, locks, state, tidy
from .client import TaresClient, TaresHTTPError, cksum
from .errors import CrewError, SessionListUnreadableError
from .paths import ME, CrewPaths

FREEZING_SECONDS = 300   # a freeze marker older than this is a freeze that died


# ---------- where the kit is ----------


def _truthy(value) -> bool:
    """jq's truthiness: everything but null and false."""
    return value is not None and value is not False


def _field(obj, key: str, default: str = "") -> str:
    """`jq -r '.key // default'`."""
    value = obj.get(key) if isinstance(obj, dict) else None
    return state.jq_raw(value) if _truthy(value) else default


def frozen_phys(paths: CrewPaths) -> str:
    """The frozen copy as stations record it: physical, so a symlinked crew home still matches."""
    home = os.path.realpath(paths.raw) if os.path.isdir(paths.raw) else paths.raw
    return f"{home}/frozen-kit"


def _within(path, base) -> bool:
    """Whether `path` is `base` or under it, whatever the separators (the frozen copy's name is built with "/",
    a Path on Windows spells it with backslashes) and, on Windows, whatever the case."""
    p = os.path.normcase(os.path.normpath(os.fspath(path)))
    b = os.path.normcase(os.path.normpath(os.fspath(base)))
    return p == b or p.startswith(b + os.sep)


def map_checkout(paths: CrewPaths, root) -> Path:
    """A factory run from the frozen copy still means the checkout the freeze was taken from (the kit-checkout
    file), while that checkout is there."""
    if paths.checkout_file.is_file() and _within(root, frozen_phys(paths)):
        c = state.read_text(paths.checkout_file).rstrip("\n")
        if c and os.path.isdir(c):
            return Path(c)
    return Path(root)


def plugin_root(paths: CrewPaths, env: Mapping) -> Path:
    """The plugin this engine ships in (KIT_PLUGIN): TARES_PLUGIN_DIR when set, else the install's or the
    checkout's; when that is the frozen copy, the checkout the freeze was taken from."""
    override = env.get("TARES_PLUGIN_DIR")
    if override:
        root = Path(override)
    else:
        here = Path(__file__).resolve().parent.parent
        root = here / "claude-plugin"
        for c in (here / "claude-plugin", here.parent / "claude-plugin"):
            if (c / "bin" / "factory").is_file():
                root = c
                break
    return map_checkout(paths, root)


def kit_version(root) -> str:
    """A fingerprint of what the stations run on: every file of the plugin, in byte order, through cksum
    (`find | sort -z | xargs cat | cksum`). Never fails: an unreadable folder fingerprints as nothing."""
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.startswith(".") or name == "4913" or name.endswith(("~", ".orig", ".rej", ".swp", ".pyc")):
                continue
            path = os.path.join(base, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = "./" + os.path.relpath(path, root).replace(os.sep, "/")
            found.append((rel.encode("utf-8", "surrogateescape"), path))
    found.sort()
    data = bytearray()
    for _, path in found:
        try:
            with open(path, "rb") as f:
                data += f.read()
        except OSError:
            pass
    return str(cksum(bytes(data)))


def kit_clean(root) -> bool:
    """True unless the plugin is a git checkout with uncommitted changes to tracked files: a checkout being
    edited is no release."""
    root = os.fspath(root)
    try:
        if subprocess.run(["git", "-C", root, "rev-parse", "--git-dir"], capture_output=True, timeout=30).returncode != 0:
            return True
        out = subprocess.run(["git", "--no-optional-locks", "-C", root, "status", "--porcelain",
                              "--untracked-files=no", "--", "."], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return True   # no git to ask, or it did not answer: nothing says the checkout is being edited
    return not out.stdout.strip()


# ---------- whether the kit is frozen ----------


class Frozen:
    """Whether the kit is frozen and what the freeze recorded, from the crew settings read once.
    Tares unreadable: a copy on disk means frozen, so a blip never lets a station move."""

    def __init__(self, paths: CrewPaths, client: TaresClient):
        self.paths = paths
        try:
            self.settings = client.crew_settings()
        except CrewError:
            self.settings = None

    @property
    def on(self) -> bool:
        if self.settings is None:
            return self.paths.frozen_kit.is_dir()
        kit = self.settings.get("kit")
        return isinstance(kit, dict) and kit.get("frozen") is not None

    def field(self, name: str) -> str:
        """`.kit.frozen[name] // empty`."""
        if self.settings is None:
            return ""
        kit = self.settings.get("kit")
        frozen = kit.get("frozen") if isinstance(kit, dict) else None
        return _field(frozen, name) if isinstance(frozen, dict) else ""

    def text(self) -> str:
        if self.field("at"):
            return f"since {self.field('at')} by {self.field('by')}"
        return "(Tares does not say since when)"

    @property
    def idle_wake(self) -> bool:
        return self.settings is not None and self.settings.get("idle_wake") is True


def upgrades_frozen(paths: CrewPaths, client: TaresClient, *, fresh: bool = False, env=None) -> bool:
    """Whether the crew's kit is frozen. `fresh` reads the settings again, on a client of its own (bash:
    SETTINGS="" first), so the caller's reading is not touched."""
    if fresh:
        with TaresClient(paths, client.env if env is None else env, lambda _m: None) as other:
            return Frozen(paths, other).on
    return Frozen(paths, client).on


def kit_dir(paths: CrewPaths, client: TaresClient, checkout) -> str:
    """The kit a station starts on: the frozen copy while the crew is frozen and the copy is there, else this
    checkout (`checkout`, mapped back from the frozen copy when this factory runs from it)."""
    if paths.frozen_kit.is_dir() and upgrades_frozen(paths, client):
        return frozen_phys(paths)
    return os.fspath(map_checkout(paths, checkout))


def _active_stations(paths: CrewPaths) -> dict:
    return {s: row for s, row in state.stations_load(paths).items()
            if isinstance(row, dict) and row.get("status") == "active"}


def frozen_copy_in_use(paths: CrewPaths) -> bool:
    """While an active station runs from the frozen copy."""
    where = frozen_phys(paths)
    try:
        return any(row.get("plugin") == where for row in _active_stations(paths).values())
    except CrewError:
        return False   # jq on a stations.json that is not an object fails, and so is "no"


def on_frozen_copy(paths: CrewPaths, station: str) -> bool:
    """True when the station runs from the frozen copy, which is going away unless the crew is frozen."""
    return state.stations_get(station, "plugin", paths=paths) == frozen_phys(paths)


def stale_kit(paths: CrewPaths, station: str, kit: str, frozen: Frozen) -> bool:
    """True when the station runs another kit version, or from a frozen copy that is going away."""
    if state.stations_get(station, "kit", paths=paths) != kit:
        return True
    return not frozen.on and on_frozen_copy(paths, station)


def freezing(paths: CrewPaths, now: float | None = None) -> bool:
    """A freeze is under way: its marker is under five minutes old (a freeze that died leaves it behind)."""
    try:
        age = (time.time() if now is None else now) - paths.freezing.stat().st_mtime
    except OSError:
        return False
    return age < FREEZING_SECONDS


def _rm(path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _rm_tree(path) -> None:
    """rm -rf: a missing folder is fine; a link is removed, not followed; read-only entries go too."""
    p = os.fspath(path)
    if os.path.islink(p) or os.path.isfile(p):
        _rm(p)
        return
    if os.path.isdir(p):
        tidy.remove_tree(p)


def reap_frozen_copy(paths: CrewPaths) -> None:
    """The frozen copy goes once no station runs from it; until then a station that does would lose its hooks.
    A freeze under way has the copy before Tares has the setting, so that one stays."""
    if not paths.frozen_kit.is_dir() or frozen_copy_in_use(paths) or freezing(paths):
        return
    _rm_tree(paths.frozen_kit)
    for f in (paths.newer_file, paths.cc_newer_file, paths.checkout_file):
        _rm(f)


def tell_newer_kit(paths: CrewPaths, client: TaresClient, frozen: Frozen, live: str) -> None:
    """Said once for each distinct kit newer than the frozen one, and again only for another change."""
    was = frozen.field("version")
    if not was:
        return
    if live == was:
        _rm(paths.newer_file)
        return
    try:
        said = state.read_text(paths.newer_file).rstrip("\n")
    except OSError:
        said = ""
    if said == live:
        return
    client.say(f"kit {live} is newer than the frozen kit {was}; not moving stations ({ME} crew unfreeze lets them follow it)")
    state.event("kit-newer", "", live, paths=paths)
    state.atomic_write_text(paths.newer_file, live + "\n")


# ---------- the commands ----------


def _copy_file(src, dst, *, follow_symlinks=True):
    """copy2, then writable: a checkout installed read-only (a wheel, a package manager) must not make a
    copy that cannot be removed."""
    out = shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
    if not os.path.islink(dst):
        with contextlib.suppress(OSError):
            os.chmod(dst, os.stat(dst).st_mode | stat.S_IWUSR)
    return out


def copy_kit(src, dst) -> None:
    """`cp -R src/. dst/`: every file and folder of the kit, links as links, into the existing folder `dst`.
    Raises OSError (shutil.Error is one) when anything could not be copied."""
    s, d = tidy.long_path(os.fspath(src)), tidy.long_path(os.fspath(dst))
    shutil.copytree(s, d, symlinks=True, ignore_dangling_symlinks=True, dirs_exist_ok=True, copy_function=_copy_file)
    for base, dirs, _files in os.walk(d):   # folders of a read-only install are read-only too
        for name in dirs:
            p = os.path.join(base, name)
            if not os.path.islink(p):
                with contextlib.suppress(OSError):
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR | stat.S_IXUSR)


def _move(src, dst) -> None:
    os.replace(tidy.long_path(os.fspath(src)), tidy.long_path(os.fspath(dst)))


def _freeze_fail(paths: CrewPaths, made: bool, message: str) -> NoReturn:
    """Nothing of a freeze that did not happen stays behind."""
    _rm(paths.freezing)
    if made:
        _rm_tree(paths.frozen_kit)
        _rm(paths.checkout_file)
    raise CrewError(message)


def _unreadable(client: TaresClient) -> CrewError:
    return CrewError(f"could not read the crew settings from Tares at {client.tares}{client.no_key_hint()}")


def freeze(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str = "") -> None:
    """factory crew freeze: keep the crew on today's kit."""
    state.require_crew(paths)
    client._settings = None
    try:
        client.crew_settings()
    except CrewError:
        raise _unreadable(client) from None
    frozen = Frozen(paths, client)
    if frozen.on:
        client.say(f"the kit is already frozen {frozen.text()}; {ME} crew unfreeze lifts it")
        return
    root = plugin_root(paths, client.env)
    paths.freezing.touch()
    made = False
    if frozen_copy_in_use(paths):
        if kit_version(paths.frozen_kit) != kit_version(root):
            _freeze_fail(paths, False, f"stations still run from the kit copy of an earlier freeze, and the checkout has changed "
                                       f"since: wait until {ME} crew watch has moved them ({ME} crew status), then freeze again")
    else:
        made = True
        new = Path(f"{paths.frozen_kit}.new")
        _rm_tree(new)
        try:
            new.mkdir(parents=True, exist_ok=True)
            copy_kit(root, new)
        except OSError:
            _rm_tree(new)
            _freeze_fail(paths, False, f"could not copy the kit to {paths.frozen_kit}")
        _rm_tree(paths.frozen_kit)
        _move(new, paths.frozen_kit)
    state.atomic_write_text(paths.checkout_file, f"{root}\n")
    version = kit_version(paths.frozen_kit)
    who = client.env.get("FACTORY_STATION") or getpass.getuser()
    body = {"kit": {"frozen": {"at": state.utc_stamp(), "by": who, "version": version}}}
    try:
        out = client.tares_http("PUT", "/api/crew/settings", body)
    except TaresHTTPError:
        _freeze_fail(paths, made, f"could not save the freeze on Tares at {client.tares}{client.no_key_hint()}")
    try:
        kept = state.jq_get(state.loads_json(out), ("kit", "frozen", "version")) == version
    except ValueError:
        kept = False
    if not kept:
        _freeze_fail(paths, made, f"Tares at {client.tares} did not keep the freeze; it needs a newer version")
    _rm(paths.freezing)
    client._settings = None
    with contextlib.suppress(CrewError):
        client.crew_settings()
    for f in (paths.newer_file, paths.cc_newer_file):
        _rm(f)
    state.event("freeze", "", version, paths=paths)
    client.say(f"kit frozen {Frozen(paths, client).text()}: stations that start or revive use this kit; running ones keep "
               f"reading the checkout until they restart, and the watch moves none onto a newer kit. {ME} crew unfreeze lifts it.")


def unfreeze(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str = "") -> None:
    """factory crew unfreeze: let the stations follow the checkout's kit again."""
    state.require_crew(paths)
    client._settings = None
    try:
        client.crew_settings()
    except CrewError:
        raise _unreadable(client) from None
    if not Frozen(paths, client).on:
        client.say("the kit is not frozen")
        return
    try:
        client.tares_http("PUT", "/api/crew/settings", {"kit": {"frozen": None}})
    except TaresHTTPError:
        raise CrewError(f"could not lift the freeze on Tares at {client.tares}{client.no_key_hint()}") from None
    client._settings = None
    for f in (paths.newer_file, paths.cc_newer_file):
        _rm(f)
    state.event("unfreeze", paths=paths)
    if frozen_copy_in_use(paths):
        client.say("kit unfrozen: the watch moves each station onto the checkout's kit when it's idle, and removes "
                   f"{paths.frozen_kit} once none runs from it")
    else:
        _rm_tree(paths.frozen_kit)
        _rm(paths.checkout_file)
        client.say("kit unfrozen: the watch moves each station onto the checkout's kit when it's idle")


def _command(fn):
    """cmd_x(argv, env) for fn(client, paths, args): primed like every command that is not local_only."""
    def run(argv: list, env: dict) -> int:
        from . import lifecycle   # here, not at the top: lifecycle imports launch, and launch imports this module
        return lifecycle._command(fn, local_only=False)(argv, env)
    run.__name__ = f"cmd_{fn.__name__}"
    run.__doc__ = fn.__doc__
    return run


cmd_freeze = _command(freeze)
cmd_unfreeze = _command(unfreeze)


# ---------- the watch's round ----------


class _Watch:
    """What the running watch remembers between rounds (bash's kit_start, seen and said_dirty)."""

    def __init__(self):
        self.start: str | None = None   # the kit the watch started on
        self.seen: str | None = None    # the kit the last round saw
        self.said_dirty = False  # the dirty checkout was said
        self.root: Path | None = None   # the plugin the watch started on (bash maps KIT_PLUGIN once, at its start)


_watch = _Watch()


def reset() -> None:
    """Forget the watch's state (tests)."""
    global _watch
    _watch = _Watch()


def movable(paths: CrewPaths, station: str, kit: str, frozen: Frozen, binary: str) -> bool:
    """It runs another version, has a conversation to resume, and is quiet."""
    return (stale_kit(paths, station, kit, frozen) and bool(state.stations_get(station, "sessionId", paths=paths))
            and claude.quiet(station, None, binary, paths))


@locks.scoped   # the station's lock goes when its upgrade ends, as with bash's per-station subshell (TR-1171)
def upgrade_station(paths: CrewPaths, client: TaresClient, station: str, kit: str, *, claude_bin: str) -> int:
    """Restart it on the new version, in the same conversation. 0 it did, 3 Tares does not answer, 4 it is not
    idle any more or the crew froze since the round began, 1 it could not be restarted. Says what happened."""
    from . import lifecycle
    skill = state.stations_get(station, "role", paths=paths)
    rc = 0
    with TaresClient(paths, client.env, client.say) as fresh:   # Tares and the settings are asked again, as in bash's subshell
        try:
            if not fresh.tares_on():
                rc = 3   # without Tares there are no settings to restart it with
            else:
                fresh.crew_settings()
                if Frozen(paths, fresh).on:
                    rc = 4   # frozen since the round began
                elif not claude.quiet(station, None, claude_bin, paths):
                    rc = 4   # it started something since the first check
                else:
                    client.say(f"{station} runs an older version of the plugin; restarting it on this one now that it's "
                               "idle, in the same conversation")
                    lifecycle.restart(fresh, paths, [station, (
                        "The Tares plugin was updated, so the factory restarted you on it, in this same conversation, with "
                        f"fresh settings. Reload the skills factory-protocol and station-{skill} (they may have changed), "
                        f"re-read your report, {lifecycle.rearm_text(fresh)}, then carry on where you were.")],
                        claude_bin=claude_bin)
                    state.event("upgraded", station, kit, paths=paths)
        except CrewError as e:
            print(e.line, file=sys.stderr, flush=True)
            rc = 1
        except OSError as e:
            print(f"{ME}: {e}", file=sys.stderr, flush=True)
            rc = 1
    if rc == 3:
        client.say(f"Tares doesn't answer; {station} moves onto the new version once it does")
    elif rc not in (0, 4):
        client.say(f"could not restart {station} on the new version; the watch revives it as before, and no other "
                   "station moves until it is up")
    return rc


def pick(paths: CrewPaths, kit: str, frozen: Frozen, binary: str, started) -> str:
    """The station the kit upgrade would move: the first active one the revive step did not just start, helpers and
    finished builders aside, that is movable. "" when none."""
    for s in _active_stations(paths):
        role = state.stations_get(s, "role", paths=paths)
        if role == "helper" or (role == "builder" and paths.done_file(s).exists()):
            continue
        if s in started:
            continue
        try:
            if movable(paths, s, kit, frozen, binary):
                return s
        except SessionListUnreadableError:
            return ""
    return ""


def step(paths: CrewPaths, env: Mapping | None = None, claude_bin: str | None = None) -> bool:
    """The kit part of one watch round. True when it restarted a station on a new kit: the round then makes no
    Claude Code move (bash: `if <upgrade> elif <Claude Code move>`)."""
    from . import watch, watch_revive
    env = os.environ if env is None else env
    if _watch.root is None:   # a frozen copy the watch started from may go later; the checkout it meant stays the root
        _watch.root = plugin_root(paths, env)
    root = _watch.root
    kit = kit_version(root)
    if _watch.start is None:   # cmd_watch reads it once before its first round
        _watch.start = _watch.seen = kit
    clean = kit_clean(root)
    # An update counts once it has held for a whole round in a clean checkout: a pull or an edit in progress is
    # not a release.
    settled = kit == _watch.seen and clean
    with TaresClient(paths, env) as client:
        client.tares_on()
        if not _watch.said_dirty and not clean:
            client.say(f"the plugin checkout at {root} has uncommitted changes; no station moves onto a new version until it is clean")
            _watch.said_dirty = True
        _watch.seen = kit
        frozen = Frozen(paths, client)
        if frozen.on:
            if settled:
                tell_newer_kit(paths, client, frozen, kit)
        elif settled and kit != _watch.start and not watch.LOOP.once:
            client.say("the plugin was updated; the watch restarts on the new version")
            # a watch started from a frozen copy restarts on the checkout it meant, not on the copy that is gone (TR-686)
            watch.restart_self(paths, watch.LOOP.interval, {**env, "TARES_PLUGIN_DIR": str(root)} if env.get("TARES_PLUGIN_DIR") else env)
        else:
            reap_frozen_copy(paths)
        last = watch_revive.last
        if last.unread:
            return False   # the revive step said so; nothing is moved this round
        try:
            binary = claude_bin or claude.claude_bin(env)
            candidate = "" if frozen.on else pick(paths, kit, frozen, binary, last.started)
        except CrewError:
            return False
        from . import cc_update   # here, not at the top: cc_update imports crew modules that import this one
        down = last.down or (not last.ran and cc_update._down(paths, binary))
        if candidate and settled and not down and not state.halted(paths) and not frozen.on:
            upgrade_station(paths, client, candidate, kit, claude_bin=binary)
            return True
    return False
