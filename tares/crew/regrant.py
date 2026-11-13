"""The regrant step of the watch: restart a station between turns when its grants change (TR-960).

Ports these functions of the retired bash crew, with the same behaviour and the same printed text:
grants_now, regranted, regrant_due and regrant_station, and the part of the watch that calls them (the helper
branch and the between-turns branch of the per-station loop). Every difference from the bash is listed under
"Differences from bash". The prompt quotes the rules the station's last start did not have, as Tares renders
them now (TR-864, `rules.added`); the fingerprint of the grants still decides whether to restart.

A station's grants are fingerprinted when it starts (launch.launch keeps the cksum in stations.json `grants` and
the text in stations/<station>.grants). Each round the step reads the grants on Tares now; a station whose
fingerprint differs, and that is not the one already acted on (`grants_tried`), is restarted in the same
conversation with a prompt that quotes the added grants, but only when it is between turns (done or blocked,
nothing in flight), has a session, is not open in someone's terminal, and the crew is not halted. A station with
a `.done` file is restarted only for a new grant that names it (TR-645); any other change is noted once in the
event log as `regrant-skipped`. A Tares that does not answer is never a change. Once per change: `grants_tried` is written before the
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
import re
import sys

from . import claude, launch, locks, state, watch_revive
from . import rules as rules_mod
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
            and not claude.attached(station, paths=paths)
            and regranted(paths, client, station)
            and (not paths.done_file(station).exists() or _done_regrant_due(paths, client, station)))


def names_station(added: str, station: str) -> bool:
    """True when a line of the added grants names this station as a whole name: s-1 is not s-10."""
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(station)}(?![A-Za-z0-9_-])", added) is not None


def _done_regrant_due(paths: CrewPaths, client: TaresClient, station: str) -> bool:
    """True when a new grant names this done station; otherwise says so in the event log, once per change."""
    try:
        now = client.grants_read(state.stations_get(station, "project", paths=paths)).rstrip("\n")
    except CrewError:
        return False
    kept = paths.station_file(station, "grants")
    if names_station(added_grants(now, state.read_text(kept) if kept.is_file() else None), station):
        return True
    total = str(grants_sum(now + "\n"))
    if state.stations_get(station, "grants_skipped", paths=paths) != total:
        state.stations_put(station, {"grants_skipped": total}, paths=paths)
        state.event("regrant-skipped", station, "done", paths=paths)
    return False


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


def _new_rules(client: TaresClient, paths: CrewPaths, station: str, project: str) -> list:
    """The sentences Tares renders for the station now that its last start did not have (TR-864), written as the
    start will write them. Empty when Tares cannot give the rules: the prompt then says the change without quoting."""
    role = state.stations_get(station, "role", paths=paths)
    work = launch.parent_role(state.stations_get(station, "parent", paths=paths), paths) if role == "helper" else role
    pid = state.stations_get(station, "project_id", paths=paths) or project
    try:
        new = rules_mod.fetch(client, station, pid, work)
    except (CrewError, ValueError):   # an older Tares, or one that cannot answer
        return []
    repo = state.stations_get(station, "repo", paths=paths)
    values = launch.rule_values(client, work, project, repo)
    last = rules_mod.saved(paths, station, launch.machine_values(client, station, project, repo), values["tares"])
    return rules_mod.added(last, new, values)


@locks.scoped   # the station's lock goes with its regrant, as with the end of bash's per-station subshell (TR-1171)
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
            rules = "\n".join("- " + r for r in _new_rules(client, paths, station, project))
            state.stations_put(station, {"grants_tried": total}, paths=paths)
            prompt = _prompt(human, rules, lifecycle.rearm_text(client))
            client.say(f"{station} has new permissions from Tares; restarting it with them now that it's "
                       "between turns, in the same conversation")
            lifecycle.restart(client, paths, [station, prompt], claude_bin=claude_bin)
            paths.done_file(station).unlink(missing_ok=True)   # it has work again; it says so itself when that is over
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
                # it ended with nothing left to do: only a new grant that names it brings it back
                if (not state.halted(paths) and claude.between_turns(station, binary, paths)
                        and regrant_due(paths, client, station)):
                    watch_revive.last.handled.add(station)
                    regrant_station(paths, station, claude_bin=binary, env=env)
                continue
            if role != "helper":
                # is_alive and the stuck check on one read of the session list (bash reads it once here too)
                try:
                    agents = claude.active_json(binary)
                except SessionListUnreadableError:
                    continue
                cwd = state.stations_get(station, "cwd", paths=paths) or paths.raw
                sid = state.stations_get(station, "id", paths=paths)
                if not any(not claude.gone(r) and ((r.get("name") == station and claude.same_dir(r.get("cwd"), cwd))
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
