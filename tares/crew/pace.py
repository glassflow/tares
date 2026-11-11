"""The watch's pace step: slow the crew before the plan's limits, and revive what a limit stopped (TR-962).

Ports these functions of `claude-plugin/bin/factory`: pace_read, pace_closed, pace_json, pace_json_update,
pace_stopped, pace_tried, pace_mark_tried, pace_hold_down, pace_revived, pace_tidy, pace_resume_prompt,
pace_fresh_prompt, pace_station_file, pace_station_read, pace_station_put (the last three live in `state` and
`paths`), pace_launch_note, pace_plan and pace_send, and the pace part of cmd_watch. `pace_status_line` is
`views.pace_status_line` and `rearm_text` is `lifecycle.rearm_text`; both read the pace through `read` here.

With the crew setting pace on, the watch reads how full the plan's limits are (Tares keeps the reading the usage
meter takes; GET /api/crew/pace) once a round and slows the crew before the limit instead of hitting it. At 85%
of the 5-hour window or 90% of the week no new builder starts and every station ticks at its longest interval;
at 95% of the window builders also finish their step, push, write down where they are and wait. A station that
stopped while a limit was full is not revived into it, and once it has reset it is revived in its own
conversation (a new one only when that fails). A reading that is missing changes nothing. With the setting off
none of this runs and no file is written.

Per station: stations/<station>.pace ({stretched, held}); crew-wide: pace.json (the stopped ones).

Differences from bash
  * Bash reads the pace at the top of the round, so every branch of the round sees the same reading. Here the
    steps are separate passes and the watch has no hook at the start of a round: the pace step reads the pace
    afresh and remembers it for READING_TTL seconds, and the revive step (which runs before it) asks `current`,
    which reads again only when the remembered reading is older than that. A round is never closer to the
    next than READING_TTL.
  * Bash decides what to do for a station in one `elif` chain, so a station gets at most one restart a round.
    Here this step is its own pass: a station an earlier step restarted is not between turns, so it is left
    alone, which keeps the one restart a station a round.
  * The pace is told to a station through the same restart bash uses (`lifecycle.restart`, in the same
    conversation), with the same prompts.

Standard library plus the crew modules (no duckdb, fastapi or daemon).
"""
from __future__ import annotations

import contextlib
import os
import sys
import time

from . import claude, locks, state, watch_revive
from .client import TaresClient, TaresHTTPError
from .errors import CrewError, SessionListUnreadableError
from .paths import ME, CrewPaths

ROLES = ("orchestrator", "reviewer", "releaser", "builder")
READING_TTL = 20.0   # seconds a reading is remembered; see "Differences from bash"
PACE_DEFAULT = {"limit_since": None, "until": None, "said": False, "stopped": [], "tried": []}

STRETCH_TEXT = ("Stretch your tick to 45 minutes: if you hold a CronCreate tick, delete it (CronList, then "
                "CronDelete) and create it again at 45 minutes, and keep it there until the factory tells you "
                "otherwise.")
HOLD_TEXT = ("Finish the step you are on, push, write in your report where you are and what comes next, then "
             "wait without starting new work until the factory tells you the plan has room again.")


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr, flush=True)


# ---------- the reading ----------


class Reading:
    """What Tares says about the plan's limits. `state` is "unknown" when pace is on but Tares gave no reading
    (or none could be had), "off" when the setting is off, "active" when there is a reading."""

    def __init__(self, state_: str = "unknown", level: int = 0, at_limit: bool = False, until: str = "",
                 note: str = "", raw=None):
        self.state = state_
        self.level = level
        self.at_limit = at_limit
        self.until = until
        self.note = note
        self.raw = raw

    @property
    def closed(self) -> bool:
        """A limit is full and has not reset."""
        return self.state == "active" and self.at_limit

    @property
    def paced(self) -> bool:
        """Active, and at least a slow-down."""
        return self.state == "active" and self.level >= 1


def _alt(value, default):
    """jq's `v // d`."""
    return default if value is None or value is False else value


def _is_true(value) -> bool:
    """What `[ "$(jq -r '.k // false')" = true ]` makes of a value: true, or the text "true"."""
    return state.jq_raw(_alt(value, False)) == "true"


def _level(value) -> int:
    v = _alt(value, 0)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).strip())
    except ValueError:
        return 0


def read(client: TaresClient) -> Reading:
    """bash's pace_read: the pace of the plan. state "unknown" when Tares does not answer or has no reading, "off"
    when pace is off. The reading is remembered for `current`."""
    r = _read(client)
    remember(client.paths, r)
    return r


def _read(client: TaresClient) -> Reading:
    if not client.tares_on():
        return Reading()
    try:
        p = state.loads_json(client.tares_http("GET", "/api/crew/pace"))
    except (TaresHTTPError, ValueError):
        return Reading()
    return _read_doc(p)


def _read_doc(p) -> Reading:
    """The reading in what GET /api/crew/pace answered."""
    if not isinstance(p, dict):
        return Reading()
    if not _is_true(p.get("on")):
        return Reading("off")
    if not _is_true(p.get("known")):
        return Reading()
    return Reading("active", _level(p.get("level")), _is_true(p.get("at_limit")),
                   state.jq_raw(_alt(p.get("until"), "")), state.jq_raw(_alt(p.get("note"), "")), p)


_LAST: dict = {}   # crew home -> (when, Reading)


def remember(paths: CrewPaths, reading: Reading) -> None:
    _LAST[str(paths.home)] = (time.monotonic(), reading)


def has_read(paths: CrewPaths) -> bool:
    """bash's PACE_READ: this process has read the pace (in its main shell, not in a command substitution)."""
    return str(paths.home) in _LAST


def unremember(paths: CrewPaths) -> None:
    """Drop the reading of one crew home (a reading taken in a command substitution leaves nothing behind)."""
    _LAST.pop(str(paths.home), None)


def forget() -> None:
    """Drop every remembered reading (tests, and a crew home that changed)."""
    _LAST.clear()
    _ROUND.clear()


def last(paths: CrewPaths) -> Reading:
    """The reading this process took last, whatever its age; unknown when it took none. This is what bash's
    PACE_STATE is: a command that has not read the pace has none."""
    hit = _LAST.get(str(paths.home))
    return Reading() if hit is None else hit[1]


_ROUND: dict = {}   # crew home -> the Reading the round started with, for the pace step to use once


def round_start(paths: CrewPaths, env=None) -> None:
    """The top of a watch round, as bash's `pace_read || true; pace_tidy || true`: read the pace afresh, remember
    it for the whole round (the revive step asks `current`, the pace step takes it), and tidy the pace file. A
    limit that fills later in the round is not acted on until the next one; only a move that restarts a station
    onto a new Claude Code reads it again, just before it restarts."""
    env = os.environ if env is None else env
    client = TaresClient(paths, env)
    try:
        r = read(client)
    finally:
        client.close()
    tidy(paths, r)
    _ROUND[str(paths.home)] = r


def current(paths: CrewPaths, env=None, *, round_start: bool = False) -> Reading:
    """The reading of this round: the one the pace step remembered if it is younger than READING_TTL, else a new
    one. For the steps that run before the pace step and the commands that need the pace. A new reading with
    `round_start` also tidies the pace file, as bash does at the top of a round: the revive step then never sees
    a stopped station of a pace that has since been turned off."""
    hit = _LAST.get(str(paths.home))
    if hit is not None and time.monotonic() - hit[0] < READING_TTL:
        return hit[1]
    client = TaresClient(paths, env)
    try:
        r = read(client)
    finally:
        client.close()
    if round_start:
        tidy(paths, r)
    return r


# ---------- pace.json: the stopped ones ----------


def json_read(paths: CrewPaths) -> dict:
    """The crew-wide pace file, the defaults under whatever it has."""
    data = state.read_json(paths.pace_file, None)
    out = {**PACE_DEFAULT, **data} if isinstance(data, dict) else dict(PACE_DEFAULT)
    for k in ("stopped", "tried"):
        if not isinstance(out[k], list):
            out[k] = []
    return out


def json_update(paths: CrewPaths, fn) -> None:
    """Apply fn to the pace file's content and write it whole."""
    doc = json_read(paths)
    fn(doc)
    state.write_json(paths.pace_file, doc, compact=True)


def _unlink(path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def stopped(paths: CrewPaths, station: str, reading: Reading) -> bool:
    """It stopped while a limit was full."""
    return reading.state != "unknown" and station in json_read(paths)["stopped"]


def tried(paths: CrewPaths, station: str) -> bool:
    """Its resume failed twice in a row."""
    return json_read(paths)["tried"].count(station) >= 2


def mark_tried(paths: CrewPaths, station: str) -> None:
    try:
        json_update(paths, lambda d: d["tried"].append(station))
    except OSError:
        pass


def hold_down(paths: CrewPaths, station: str, reading: Reading) -> None:
    """It is down while a limit is full: remember it, and say so once per episode. A full limit again starts its
    failed resumes over."""
    said = json_read(paths)["said"]

    def apply(d: dict) -> None:
        d["tried"] = [x for x in d["tried"] if x != station]
        d["stopped"] = sorted(set(d["stopped"] + [station]))
        d["limit_since"] = _alt(d["limit_since"], state.now_s())
        d["until"] = reading.until
        d["said"] = True
    try:
        json_update(paths, apply)
    except OSError:
        pass
    if said is not True:
        until = f" until {reading.until}" if reading.until else ""
        _say(f"the plan's limit is full{until}, so a stopped station is not revived now ({station} is the "
             "first); the watch revives each one in its own conversation after the reset")


def revived(paths: CrewPaths, station: str, reading: Reading) -> None:
    """It runs again (revived, or by hand): forget it; the file goes when nothing is left to carry out."""
    if not paths.pace_file.exists():
        return

    def apply(d: dict) -> None:
        d["stopped"] = [x for x in d["stopped"] if x != station]
        d["tried"] = [x for x in d["tried"] if x != station]
    try:
        json_update(paths, apply)
    except OSError:
        pass
    if not json_read(paths)["stopped"] and not reading.closed:
        _unlink(paths.pace_file)


def tidy(paths: CrewPaths, reading: Reading) -> None:
    """Once a round: forget stations that will not be revived (retired, done, a helper) and a pace file with
    nothing left to carry out. A reading that is missing changes nothing."""
    if not paths.pace_file.exists() or reading.state == "unknown":
        return
    if reading.state == "off":
        _unlink(paths.pace_file)
        return
    live = []
    for n, row in state.stations_load(paths).items():
        if not isinstance(row, dict) or row.get("status") != "active" or row.get("role") == "helper":
            continue
        if row.get("role") == "builder" and paths.station_file(n, "done").exists():
            continue
        live.append(n)

    def apply(d: dict) -> None:
        d["stopped"] = [x for x in d["stopped"] if x in live]
        d["tried"] = [x for x in d["tried"] if x in live]
        if not reading.closed:
            d["said"] = False
    try:
        json_update(paths, apply)
    except OSError:
        return
    if not json_read(paths)["stopped"] and not reading.closed:
        _unlink(paths.pace_file)


# ---------- what a revived station is told ----------


def resume_prompt(client: TaresClient) -> str:
    """A short one: the conversation is its own, so it needs no reload of the skills."""
    from . import lifecycle
    return ("The plan's limit was reset. Carry on from where you stopped: re-read your report and your work on "
            f"Tares, {lifecycle.rearm_text(client)}, then continue.")


def fresh_prompt(client: TaresClient, paths: CrewPaths, station: str) -> str:
    """Its conversation could not be resumed."""
    from . import lifecycle
    role = state.stations_get(station, "role", paths=paths)
    return ("The plan's limit was reset, but your last conversation could not be resumed, so this is a new one "
            "under the same name. Nothing from it carries over except your report and what is on Tares. Reload "
            f"the skills factory-protocol and station-{role}, re-read your report and your work on Tares, "
            f"{lifecycle.rearm_text(client)}, then carry on where your report left off.")


# ---------- a start while the crew is paced ----------


def idle_wake_state(client: TaresClient) -> str:
    """on, off, or unknown (Tares gave no settings: nothing changes on a blip)."""
    try:
        settings = client.crew_settings()
    except CrewError:
        return "unknown"
    return "on" if settings.get("idle_wake") is True else "off"


def launch_note(client: TaresClient, paths: CrewPaths, station: str) -> None:
    """A start while the crew is paced is a start with the stretched tick (`rearm_text` says so): the station's
    pace file is written. Stations of other roles, and a crew with idle_wake on, write none."""
    if state.stations_get(station, "role", paths=paths) not in ROLES:
        return
    if not last(paths).paced or idle_wake_state(client) != "off":
        return
    state.pace_station_put(station, True, False, paths)


# ---------- telling a station ----------
# Two steps, like the idle wake: plan decides, send does the restart.


def plan(client: TaresClient, paths: CrewPaths, s: str, reading: Reading, *, claude_bin=None):
    """What to tell station `s` now, or None when it is not to be told. A dict: the station, the `want`
    (stretch, hold, unhold or release), the prompt, and what the station will have been told afterwards."""
    if reading.state == "unknown":
        return None
    role = state.stations_get(s, "role", paths=paths)
    if role not in ROLES:
        return None
    iw = idle_wake_state(client)
    if iw == "on":
        _unlink(paths.pace_station_file(s))   # a released tick is not stretched
        return None
    if iw != "off":
        return None
    cur = state.pace_station_read(s, paths)
    stretched, held = cur["stretched"] is True, cur["held"] is True
    want = ""
    if reading.state == "off" or reading.level == 0:
        if stretched or held:
            want = "release"
    elif not stretched:
        want = "stretch"
    elif role == "builder" and reading.level >= 2 and not held:
        want = "hold"
    elif held and reading.level < 2:
        want = "unhold"
    if not want:
        return None
    if state.halted(paths) or not state.stations_get(s, "sessionId", paths=paths):
        return None
    if claude.attached(s, paths) or not claude.between_turns(s, claude_bin, paths):
        return None

    note = reading.note
    new_stretched, new_held = True, False
    if want == "stretch":
        if role == "builder" and reading.level >= 2:
            prompt = f"{note} {STRETCH_TEXT} {HOLD_TEXT}"
            new_held = True
        else:
            prompt = f"{note} {STRETCH_TEXT} Carry on from your report."
    elif want == "hold":
        prompt = f"{note} {HOLD_TEXT}"
        new_held = True
    elif want == "unhold":
        prompt = ("The plan's limit has room for builders again: pick up where your report says you stopped. "
                  "Keep your tick at 45 minutes while the factory still paces the crew.")
    else:
        prompt = ("The factory has stopped pacing the crew (the plan has room, or pace was turned off). Put your "
                  "tick back to its normal interval: delete the 45-minute CronCreate tick and create it again as "
                  "your manual says. Then carry on from your report, including anything you held back.")
        new_stretched = False
    return {"station": s, "want": want, "prompt": prompt, "stretched": new_stretched, "held": new_held}


@locks.scoped   # the station's lock goes with this restart, as with the end of bash's per-station subshell (TR-1171)
def send(client: TaresClient, paths: CrewPaths, p: dict, *, claude_bin: str) -> None:
    """Restart the station `plan` chose, in the same conversation, with the note. Never raises for a station that
    cannot be told: it is said, and tried again next round."""
    from . import lifecycle
    s, want = p["station"], p["want"]
    fresh = TaresClient(paths, client.env, client.say)   # Tares and the settings, read again
    try:
        if not fresh.tares_on():
            return
        if idle_wake_state(fresh) != "off":
            return   # the setting changed since the round began
        if (state.halted(paths) or claude.attached(s, paths)
                or not claude.between_turns(s, claude_bin, paths)):
            return   # it started something since the first check
        client.say(f"pacing: telling {s} ({want}) in the same conversation")
        try:
            lifecycle.restart(fresh, paths, [s, p["prompt"]], claude_bin=claude_bin)
        except (CrewError, OSError) as e:   # bash: any failure of the restart (a file it cannot write) is the same
            sys.stdout.flush()
            print(e.line if isinstance(e, CrewError) else e, file=sys.stderr)
            client.say(f"could not tell {s} about the pace; will try again next round")
            return
        state.event("pace", s, want, paths=paths)
    finally:
        fresh.close()
    try:
        state.pace_station_put(s, p["stretched"], p["held"], paths)
    except OSError:
        pass


# ---------- what revive asks of the pace ----------


class Seam:
    """What the revive step asks of the pace (TR-959's `pace` object): the same calls, answered by the reading of
    this round."""

    def __init__(self, env=None):
        self.env = env

    def _reading(self, paths: CrewPaths) -> Reading:
        return current(paths, self.env, round_start=True)

    def revived(self, paths: CrewPaths, station: str) -> None:
        revived(paths, station, self._reading(paths))

    def closed(self, paths: CrewPaths) -> bool:
        return self._reading(paths).closed

    def hold_down(self, paths: CrewPaths, station: str) -> None:
        hold_down(paths, station, self._reading(paths))

    def stopped(self, paths: CrewPaths, station: str) -> bool:
        return stopped(paths, station, self._reading(paths))

    def tried(self, paths: CrewPaths, station: str) -> bool:
        return tried(paths, station)

    def resume_prompt(self, paths: CrewPaths) -> str:
        client = TaresClient(paths, self.env)
        try:
            return resume_prompt(client)
        finally:
            client.close()

    def fresh_prompt(self, paths: CrewPaths, station: str) -> str:
        client = TaresClient(paths, self.env)
        try:
            return fresh_prompt(client, paths, station)
        finally:
            client.close()

    def mark_tried(self, paths: CrewPaths, station: str) -> None:
        mark_tried(paths, station)


# ---------- the step ----------


def step(paths: CrewPaths, *, env=None, claude_bin=None) -> None:
    """One round of the pace: read the plan's limits, tidy the pace file, and tell every active, live station of a
    crew role that is between turns what the pace now asks of it."""
    env = os.environ if env is None else env
    taken = _ROUND.pop(str(paths.home), None)   # the reading the round started with, when there was a round
    # Bash skips the whole round when the session list is unreadable; the revive step says so. Read it as
    # `watch_revive.last`: the revive step rebinds `last` every round.
    if watch_revive.last.unread:
        return
    client = TaresClient(paths, env)
    try:
        if taken is not None:
            reading = taken
        else:
            reading = read(client)
            tidy(paths, reading)
        names = [k for k, v in state.stations_load(paths).items()
                 if isinstance(v, dict) and v.get("status") == "active"]
        if not names or reading.state == "unknown":
            return
        if claude_bin is None:
            claude_bin = claude.claude_bin(env)
        for s in names:
            role = state.stations_get(s, "role", paths=paths)
            if role == "helper":
                continue
            if role == "builder" and paths.station_file(s, "done").is_file():
                continue
            try:
                if not claude.is_alive(s, "", claude_bin, paths):
                    continue
                p = plan(client, paths, s, reading, claude_bin=claude_bin)
                if p is not None:
                    send(client, paths, p, claude_bin=claude_bin)
            except CrewError as e:   # an unreadable session list too: the revive step has said so
                client.say(f"skipping {s} this round" if isinstance(e, SessionListUnreadableError)
                           else e.message)
    finally:
        client.close()
