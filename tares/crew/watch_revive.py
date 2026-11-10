"""The watch's revive step: bring back a station that stopped, and start one fresh that is stuck on its context (TR-959).

Ports the first branches of `cmd_watch`'s station loop in `claude-plugin/bin/factory`: the session list check
("nothing is revived or restarted this round"), the filter on which stations are looked at (helpers and a finished
builder are not), the revive of a station that is down, and the fresh restart of one that Claude Code stopped for
thrashing on autocompact. `watch.py` registers `step` as its `revive` step; it runs once a round.

How it differs from the bash loop
  * Bash walks the stations once and decides each by an elif chain: stuck, then regrant, then idle wake, then pace,
    then an update candidate. Here each of those is its own step of the round (see `watch.STEP_NAMES`), so a station
    this step started or restarted is listed in `last.started`; a later step skips it, as the elif chain would.
  * Bash keeps `down` (some station was down or unreadable this round) for the update step, which only moves a
    station while all are up. It is `last.down`, for the same step.
  * A round that cannot read Claude Code's session list revives nothing and sets `last.unread`.
  * The pace branches (a closed limit holds a down station, a station stopped by the limit comes back with the
    resume prompt) belong to the pace ticket. They are the `pace` object below: every method is inert until that
    ticket replaces it, so a station is never held back here yet.

A restart or revive that fails is reported and tried again next round, and never ends the round: the other
stations are still looked at.

Standard library plus the crew modules (no duckdb, fastapi or daemon).
"""
from __future__ import annotations

import os
import re
import sys

from . import claude, state
from .errors import CrewError, SessionListUnreadableError
from .paths import ME, CrewPaths


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr, flush=True)


class Round:
    """What this step found in the last round, for the steps after it."""

    def __init__(self):
        self.down = False        # a station was down, or could not be told apart from down
        self.unread = False      # the session list could not be read: nothing was done
        self.started: set = set()   # stations this step revived or restarted
        self.handled: set = set()   # stations this step dealt with, however it went (a skip, a stuck restart, a revive, a regrant restart)

    def __repr__(self):
        return f"Round(down={self.down}, unread={self.unread}, started={sorted(self.started)})"


last = Round()


class PaceSeam:
    """What the revive loop asks of the plan's pace (bash's pace_revived, pace_closed, pace_hold_down, pace_stopped,
    pace_tried, pace_resume_prompt, pace_fresh_prompt and pace_mark_tried). The pace ticket replaces these; until
    then nothing is paced, so the answers are the ones of a crew that is not."""

    def revived(self, paths: CrewPaths, station: str) -> None:
        """It runs again: nothing left to carry out for it."""

    def closed(self, paths: CrewPaths) -> bool:
        """The plan's limit is full: no reviving into a closed door."""
        return False

    def hold_down(self, paths: CrewPaths, station: str) -> None:
        """A down station waits for the limit to reset."""

    def stopped(self, paths: CrewPaths, station: str) -> bool:
        """It stopped while the limit was full, and the limit has since reset."""
        return False

    def tried(self, paths: CrewPaths, station: str) -> bool:
        """Its own conversation was already tried once and could not be resumed."""
        return False

    def resume_prompt(self, paths: CrewPaths) -> str:
        return ""

    def fresh_prompt(self, paths: CrewPaths, station: str) -> str:
        return ""

    def mark_tried(self, paths: CrewPaths, station: str) -> None:
        """A resume failed; two in a row and the next is a new conversation."""


pace = PaceSeam()


# Claude Code stops a session whose context refills right after each compaction, three times running, and waits
# for someone to clear it. Resuming it would thrash again, so the watch starts it fresh.
_THRASH = re.compile(r'"<synthetic>".*Autocompact is thrashing')


def stuck_on_context(paths: CrewPaths, binary: str, station: str, agents=None) -> bool:
    """True when Claude Code's thrashing notice is the last thing said and nothing new runs."""
    try:
        transcript = claude.transcript_of(station, binary, paths, agents)
    except (CrewError, OSError):
        return False
    if not transcript:
        return False
    try:
        with open(transcript, encoding="utf-8", errors="replace", newline="\n") as f:
            tail = f.read().split("\n")
    except OSError:
        return False
    if tail and tail[-1] == "":
        tail.pop()
    last = None
    for line in tail[-200:]:
        try:
            obj = state.loads_json(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") in ("assistant", "user"):
            last = obj
    if last is None or not _THRASH.search(state.dumps_compact(last)):
        return False
    try:
        turn_state = claude.turn_state(state.stations_get(station, "id", paths=paths), binary)
    except SessionListUnreadableError:
        return False
    return turn_state != "working"


def _active(paths: CrewPaths) -> list:
    return [s for s, row in state.stations_load(paths).items()
            if isinstance(row, dict) and row.get("status") == "active"]


def _run(cmd, argv: list, env: dict) -> int:
    """One revive or restart on its own: its refusal is printed and counted, and never ends the round. Tares and
    the crew's settings are read afresh by the command, as bash re-primes in a subshell for each."""
    try:
        return cmd(argv, env)
    except CrewError as e:
        print(e.line, file=sys.stderr, flush=True)
    except Exception as e:   # one station's failure must not end the round for the rest
        print(f"{ME}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return 1


def step(paths: CrewPaths, env: dict | None = None) -> None:
    """One round of the revive step, for every active station."""
    global last
    # imported here: lifecycle imports the watch, which registers this step when it loads
    from . import lifecycle
    env = dict(os.environ if env is None else env)
    last = this = Round()
    try:
        binary = claude.claude_bin(env)
        claude.active_json(binary)
    except CrewError as e:   # the session list is unreadable (or there is no claude): nothing is guessed
        this.unread = True
        _say(f"{e}; nothing is revived or restarted this round")
        return
    for s in _active(paths):
        # Never revive or restart what ended with nothing left to do: helpers are short-lived, and a builder
        # whose tickets are all merged leaves stations/<name>.done for the orchestrator to retire.
        role = state.stations_get(s, "role", paths=paths)
        if role == "helper":
            continue
        if role == "builder" and (paths.stations_dir / f"{s}.done").exists():
            continue
        try:
            up = claude.is_alive(s, "", binary, paths)
        except CrewError as e:
            print(e.line, file=sys.stderr, flush=True)
            _say(f"skipping {s} this round")
            this.down = True
            this.handled.add(s)
            continue
        if up:
            if paths.pace_file.exists():
                pace.revived(paths, s)
            if stuck_on_context(paths, binary, s):
                _say(f"{s} is stuck: Claude Code stopped it for thrashing on autocompact; starting it fresh under the same name")
                state.event("thrashing", s, paths=paths)
                this.handled.add(s)
                if _run(lifecycle.cmd_restart, ["restart", s, "--fresh"], env) == 0:
                    this.started.add(s)
                else:
                    _say(f"could not restart {s}; will try again next round")
            continue
        this.down = True
        this.handled.add(s)
        if pace.closed(paths):   # the plan's limit is full: no reviving into a closed door
            pace.hold_down(paths, s)
            continue
        fresh, pprompt = "", ""
        if stuck_on_context(paths, binary, s):
            fresh = "--fresh"
        if not fresh and pace.stopped(paths, s):
            # It stopped while the limit was full, and the limit has reset: back in its own conversation, and
            # only when that fails, a new one.
            if pace.tried(paths, s):
                fresh = "--fresh"
                pprompt = pace.fresh_prompt(paths, s)
            else:
                pprompt = pace.resume_prompt(paths)
            why = ", in a new conversation since its own could not be resumed" if fresh else ""
            _say(f"{s} stopped while the plan's limit was full, which has reset; reviving it under the same name{why}")
        else:
            why = ", in a new conversation since its last one thrashed" if fresh else ""
            _say(f"{s} stopped; reviving it under the same name{why}")
        argv = ["revive", s, *([fresh] if fresh else []), pprompt]
        if _run(lifecycle.cmd_revive, argv, env) == 0:
            this.started.add(s)
            pace.revived(paths, s)
        else:
            _say(f"could not revive {s}; will try again next round")
            if pprompt and not fresh:   # a resume that failed
                pace.mark_tried(paths, s)
