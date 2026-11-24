"""Whose process layer answers for a station: Claude Code's (`claude.py`) or Codex's (`codex_station.py`).

The watch and the station commands ask a handful of questions about a station: does it run, is it between
turns, is someone attached, when did it last do anything. Each function here takes the arguments of the
function of the same name in `claude.py` and gives the question to the layer of the station's runtime. For a
station on Claude Code the answer is that function's, called as it was (a test that stands in for
`claude.is_alive` still stands in for this), so nothing changes for a Claude Code crew.

A station's runtime is the one recorded on it (`runtime` in stations.json; none is Claude Code). A station that
is not recorded yet, such as a helper about to be started, has its crew's.

Standard library and the crew modules.
"""
from __future__ import annotations

from . import claude, codex_station, launch, state
from .codex import RUNTIME as CODEX
from .errors import ClaudeNotFoundError
from .paths import CrewPaths


def runtime(station: str, paths: CrewPaths | None = None) -> str:
    """The runtime the station runs on: its own record's, else its crew's, else Claude Code."""
    paths = paths or CrewPaths.from_env()
    return (state.stations_get(station, "runtime", paths=paths) or launch.crew_runtime(paths)
            or launch.RUNTIME_DEFAULT)


def on_codex(station: str, paths: CrewPaths | None = None) -> bool:
    return runtime(station, paths) == CODEX


def crew_on_codex(paths: CrewPaths | None = None) -> bool:
    """Is this crew's runtime Codex? False for a crew that has recorded no station yet."""
    return launch.crew_runtime(paths or CrewPaths.from_env()) == CODEX


def binary(env=None, paths: CrewPaths | None = None) -> str:
    """The `claude` program the questions about a Claude Code crew need; "" for a crew on Codex, which needs
    none. Raises ClaudeNotFoundError for a Claude Code crew without one, as `claude.claude_bin` does."""
    paths = paths or CrewPaths.from_env()
    if launch.crew_runtime(paths) == CODEX:
        return ""
    return claude.claude_bin(env)


def sessions_readable(claude_bin, paths: CrewPaths | None = None) -> None:
    """Dies when Claude Code's session list cannot be read, for a Claude Code crew. A Codex crew has no such
    list: each station is asked itself."""
    paths = paths or CrewPaths.from_env()
    if launch.crew_runtime(paths) != CODEX:
        claude.active_json(claude_bin)


def is_alive(station: str, cwd: str = "", claude_bin=None, paths=None) -> bool:
    paths = paths or CrewPaths.from_env()
    if on_codex(station, paths):
        return codex_station.is_alive(paths, station)
    try:
        return claude.is_alive(station, cwd, claude_bin, paths)
    except ClaudeNotFoundError:
        # A crew with no station recorded yet may be about to start on Codex, on a machine with no Claude
        # Code. Without `claude` no Claude Code session runs, and a launch for Claude Code says so itself.
        if launch.crew_runtime(paths) == "":
            return False
        raise


def attached(station: str, paths=None) -> bool:
    """Is a person in the station's session? Nobody attaches to a Codex station's app-server."""
    paths = paths or CrewPaths.from_env()
    if on_codex(station, paths):
        return False
    return claude.attached(station, paths)


def at_rest(station: str, claude_bin=None, paths=None, now=None) -> bool:
    paths = paths or CrewPaths.from_env()
    if on_codex(station, paths):
        return codex_station.at_rest(paths, station)
    return claude.at_rest(station, claude_bin, paths, now)


def between_turns(station: str, claude_bin=None, paths=None, now=None, wait=0) -> bool:
    """On Codex a station that waits on an approval is not between turns, whatever `wait` says (see
    codex_station)."""
    paths = paths or CrewPaths.from_env()
    if on_codex(station, paths):
        return codex_station.at_rest(paths, station)
    return claude.between_turns(station, claude_bin, paths, now, wait)


def quiet(station: str, wait=None, claude_bin=None, paths=None, now=None) -> bool:
    paths = paths or CrewPaths.from_env()
    if on_codex(station, paths):
        return codex_station.at_rest(paths, station)
    return claude.quiet(station, wait, claude_bin, paths, now)


def station_updated(station: str, paths=None):
    paths = paths or CrewPaths.from_env()
    if on_codex(station, paths):
        return codex_station.updated(paths, station)
    return claude.station_updated(station, paths)
