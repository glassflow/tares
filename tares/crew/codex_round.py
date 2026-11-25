"""The watch's round for a crew on Codex (TR-1593): the one place where the watch does, once a round, what
a Codex station needs and a Claude Code station gets from Claude Code itself.

Nobody stays connected to a Codex station; each part visits it for a moment (`tares.crew.codex_station`).
The parts run in the order of PARTS, each with the round's client and the crew's paths, and each says what
it did. A part that fails says so and the next one runs: a cell that is down for one must not stop the
others. What is here:

  asks      what a station asks the person goes to the desk, and their answer goes back (`codex_asks`)
  turns     a turn that failed is said, recorded and put on the desk, once: Codex leaves such a station at
            rest, where it reads like one that finished its work

Recording (TR-1595) and the account's limits (TR-1597) join here, so that there is one such step and no
second watcher. A crew on Claude Code is left alone: the step does nothing for it.
"""
from __future__ import annotations

import os
import sys

from . import codex as cx
from . import codex_asks, proc, state
from . import codex_station as cs
from .client import TaresClient
from .errors import CrewError
from .paths import ME, CrewPaths

FAILED_TOLD = "turnFailedTold"   # on a station's row: the failed turn the person was told of


def failed_turns(client, paths: CrewPaths, say=None) -> list:
    """Tell of each station whose newest turn failed, once per turn: a line, an event and a desk item (not a
    blocking one: nothing waits on an answer). Returns the stations it told of."""
    told = []
    for station, row in sorted(state.stations_load(paths).items()):
        if not isinstance(row, dict) or row.get("runtime") != cx.RUNTIME or not cs.is_alive(paths, station):
            continue
        turn, why = cs.failed_turn(paths, station)
        if not turn or row.get(FAILED_TOLD) == turn:
            continue
        state.stations_put(station, {FAILED_TOLD: turn}, paths)
        state.event("turn-failed", station, why, paths=paths)
        if say:
            say(f"the last turn of {station} failed: {why}. It is at rest and did nothing")
        try:
            client.tares_http("POST", "/api/desk", {
                "question": f"The last turn of {station} failed: {why}"[:990],
                "recommendation": f"Fix what it names, then start it again: tares factory restart {station}",
                "why": "Codex left the station at rest, where it looks like one that finished its work. It did nothing "
                       "in that turn, and it takes its next prompt as if nothing had happened.",
                "station": station, "asked_by": "the crew watch"})
        except CrewError:
            pass   # the line and the event stand; the desk is asked again for no turn twice
        told.append(station)
    return told


PARTS = [("asks", codex_asks.step), ("turns", failed_turns)]   # (name, fn(client, paths, say) -> anything)


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr, flush=True)


def step(paths: CrewPaths, env=None) -> None:
    """One round for a Codex crew: every part of PARTS, in order."""
    if not proc.crew_on_codex(paths):
        return
    client = TaresClient(paths, dict(os.environ if env is None else env))
    try:
        client.tares_on()   # this round's Tares, asked once
    except CrewError:
        pass
    for name, part in PARTS:
        try:
            part(client, paths, _say)
        except CrewError as e:
            _say(f"the Codex round's {name} part did not finish: {e}")
