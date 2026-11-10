"""The regrant step of the watch: restart a station between turns when its grants change (TR-960).

Ports these functions of `claude-plugin/bin/factory`, with the same behaviour and the same printed text:
grants_now, regranted, regrant_due and regrant_station, and the part of cmd_watch that calls them (the helper
branch and the `elif ! halted && between_turns && regrant_due` branch of the per-station loop). Where this
module and the bash differ, the bash is right; every difference is listed under "Differences from bash".

A station's grants are fingerprinted when it starts (launch.launch keeps the cksum in stations.json `grants` and
the text in stations/<station>.grants). Each round the step reads the grants on Tares now; a station whose
fingerprint differs, and that is not the one already acted on (`grants_tried`), is restarted in the same
conversation with a prompt that quotes the added grants, but only when it is between turns (done or blocked,
nothing in flight), has a session, has no `.done` file, is not open in someone's terminal, and the crew is not
halted. A Tares that does not answer is never a change. Once per change: `grants_tried` is written before the
restart, so a restart that fails is not tried again every round.

Standard library plus the crew modules (no duckdb, fastapi or daemon). lifecycle is imported as a module and
used at call time: it imports watch, and watch registers this step.

Differences from bash
  * The watch loop of bash does one thing a round for a station (an `elif` chain: stuck, regrant, idle wake,
    pace, update). Here each step is its own function; this one leaves a station alone that is stopped or stuck
    on its context (the revive step owns those) and one the revive step started this round (`watch_revive.last.started`).
  * A round whose session list the revive step could not read (`watch_revive.last.unread`), or that this step
    cannot read, ends this step for the round without a line: the revive step says
    "<UNREAD>; nothing is revived or restarted this round".
"""
from __future__ import annotations

import os
import sys

from . import claude, launch, state, watch_revive
from .client import TaresClient, grants_sum
from .errors import CrewError, SessionListUnreadableError
from .paths import ME, CrewPaths


# ---------- what changed ----------


def grants_now(paths: CrewPaths, client: TaresClient, station: str):
    """The fingerprint of the station's grants on Tares now (as a string); None when Tares can't give them."""
    try:
        text = client.grants_read(state.stations_get(station, "project", paths=paths))
    except CrewError:
        return None
    return str(grants_sum(text.rstrip("\n") + "\n"))


def regranted(paths: CrewPaths, client: TaresClient, station: str) -> bool:
    """True when the station's grants on Tares differ from those it started with, not yet acted on. A station
    started before grants were tracked has no fingerprint and is never regranted."""
    was = state.stations_get(station, "grants", paths=paths)
    if not was:
        return False
    now = grants_now(paths, client, station)
    if now is None:
        return False
    return now != was and now != state.stations_get(station, "grants_tried", paths=paths)


def regrant_due(paths: CrewPaths, client: TaresClient, station: str) -> bool:
    """True when the watch is to restart the station with new grants, once it is between turns."""
    return (bool(state.stations_get(station, "sessionId", paths=paths))
            and not paths.done_file(station).exists()
            and not claude.attached(station, paths=paths)
            and regranted(paths, client, station))


def _lines(text: str) -> list:
    """The lines of a file as grep reads them: a last line with no newline counts, an empty file has none."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def added_grants(now: str, kept) -> str:
    """The lines of `now` that are not whole lines of `kept` (grep -Fxv -f kept); all of them without `kept`."""
    if kept is None:
        return now
    have = set(_lines(kept))
    return "\n".join(line for line in now.split("\n") if line not in have).rstrip("\n")


# ---------- restarting ----------


def _prompt(human: str, rules: str, rearm: str) -> str:
    if rules:
        return (f"{human} allowed new permissions in Tares, so the factory restarted you in this same "
                f"conversation with fresh settings. These are in your settings now:\n{rules}\n"
                "If something you tried was refused for want of one of these and is still needed, run it again "
                f"now. Then re-read your report, {rearm}, and carry on where you were.")
    return (f"{human} changed what the crew is allowed in Tares, so the factory restarted you in this same "
            "conversation with fresh settings that have the change (read_grants shows them). Re-read your "
            f"report, {rearm}, then carry on where you were.")


def regrant_station(paths: CrewPaths, station: str, *, claude_bin: str, env=None) -> None:
    """Restart the station with its new grants, in the same conversation, saying which. Tares and the settings
    are read afresh. Without Tares, or if the station started something meanwhile, nothing happens; a restart
    that fails is said, and the station gets the grants when it next starts."""
    from . import lifecycle   # at call time: lifecycle imports watch, which registers this step
    client = TaresClient(paths, env)
    try:
        try:
            if not client.tares_on():
                return   # without Tares there are no grants to restart it with
            client.crew_settings()
            if not claude.between_turns(station, claude_bin, paths):
                return   # it started something since the first check
            project = state.stations_get(station, "project", paths=paths)
            try:
                now = client.grants_read(project)
            except CrewError:
                return
            now = now.rstrip("\n")
            total = str(grants_sum(now + "\n"))
            human = launch._ccfg_or("the user", "human", "name", paths=paths) or "the user"
            kept = paths.station_file(station, "grants")
            added = now
            if kept.is_file():
                added = added_grants(now, state.read_text(kept))
            rules = "\n".join("- " + r for r in launch.grant_rules(added + "\n", project, human))
            state.stations_put(station, {"grants_tried": total}, paths=paths)
            prompt = _prompt(human, rules, lifecycle.rearm_text(client))
            client.say(f"{station} has new permissions from Tares; restarting it with them now that it's "
                       "between turns, in the same conversation")
            lifecycle.restart(client, paths, [station, prompt], claude_bin=claude_bin)
            state.event("regranted", station, total, paths=paths)
        except (CrewError, OSError) as e:   # bash says "could not restart" for any failure of the restart
            sys.stdout.flush()
            print(getattr(e, "line", None) or str(e), file=sys.stderr)
            client.say(f"could not restart {station} with its new permissions; it gets them when it next "
                       f"starts ({ME} restart {station})")
    finally:
        client.close()


# ---------- the step ----------


def step(paths: CrewPaths, env=None) -> None:
    """One round of the regrant step: every active station that is between turns and has new grants is
    restarted with them, one after the other."""
    env = dict(os.environ if env is None else env)
    if watch_revive.last.unread:
        return   # the revive step could not read the session list and said so; nothing is restarted this round
    binary = claude.claude_bin(env)
    try:
        claude.active_json(binary)
    except SessionListUnreadableError:
        return   # the revive step says so; nothing is restarted this round
    client = TaresClient(paths, env)
    try:
        client.tares_on()   # this round's Tares; one that doesn't answer changes no grants
        rows = state.stations_load(paths)
        for station, row in rows.items():
            if not isinstance(row, dict) or row.get("status") != "active":
                continue
            if station in watch_revive.last.started:
                continue   # the revive step started it this round: bash's elif chain stops there
            role = row.get("role")
            if role == "builder" and paths.done_file(station).exists():
                continue   # it ended with nothing left to do
            if role != "helper":
                # is_alive and the stuck check on one read of the session list (bash reads it once here too)
                try:
                    agents = claude.active_json(binary)
                except SessionListUnreadableError:
                    continue
                cwd = state.stations_get(station, "cwd", paths=paths) or paths.raw
                sid = state.stations_get(station, "id", paths=paths)
                if not any(not claude.gone(r) and ((r.get("name") == station and r.get("cwd") == cwd)
                                                   or (sid != "" and r.get("id") == sid)) for r in agents):
                    continue   # stopped: the revive step starts it with fresh settings
                if watch_revive.stuck_on_context(paths, binary, station, agents):
                    continue   # the revive step starts it fresh
            if (not state.halted(paths) and claude.between_turns(station, binary, paths)
                    and regrant_due(paths, client, station)):
                # bash's elif chain stops here, whether or not the restart goes through: the steps after this
                # one (idle wake, pace) leave the station alone this round
                watch_revive.last.handled.add(station)
                regrant_station(paths, station, claude_bin=binary, env=env)
    finally:
        client.close()
