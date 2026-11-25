"""The pace of a crew on Codex (TR-1597): the account's own limits, read from a station and sent to Tares, and
what the watch's pace step does on Codex that it does not do on Claude Code.

A crew on Codex has no Claude plan and no status line. Its limits are the Codex account's, and a station's own
app-server answers for them: `account/rateLimits/read` gives the windows of the account the station is signed
in with (it asks no model). So once a round the pace step (`tares.crew.pace`) asks the first station that
answers, on a short visit, and sends the answer to Tares (PUT /api/crew/codex-limits). Tares keeps it, maps the
windows by their length to the limits the pace knows, and answers GET /api/crew/pace?runtime=codex from it and
from nothing else (tares/codex_usage.py): a Codex crew is never paced from a Claude reading. No connection is
kept open and nothing new runs between rounds.

When no station answers, nothing is sent. The reading Tares has then ages: after 30 minutes it is not used and
the pace is unknown, which slows nothing.

A limit reached in the middle of a turn. A Codex station is its app-server, and the app-server outlives a turn
that failed: the station does not stop the way a Claude Code session does, it is left between turns with a
failed turn behind it, and the revive step (which looks after stopped stations) never sees it. So the pace
step looks at the last turn of each resting station. One that failed with Codex's `usageLimitExceeded` is
written down (stations/<station>.codex.limit: the turn, when it was found, when the limit resets if the
reading says) and an event records it. Nothing else is done to the station: its work is where the turn left it.
Once the reading says no limit is full and the reset time has passed, the station is handed one prompt to
carry on, in the same conversation, and the mark is removed. A station whose limit has no known reset time is
not handed anything: the watch says so once, and a person, or the idle wake, carries it on.

What is told to a station is worded for Codex here: a Codex station has no CronCreate, CronList or CronDelete.

Standard library and the crew modules.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone

from . import codex_station, locks, state
from .client import TaresClient, TaresHTTPError
from .codex import RUNTIME
from .codex_wire import WireError
from .errors import CrewError, NotBetweenTurnsError
from .paths import ME, CrewPaths

PACE_PATH = "/api/crew/pace?runtime=codex"
LIMITS_PATH = "/api/crew/codex-limits"
VISIT_TIMEOUT = 10          # seconds a station's app-server may take over the account's limits
LIMIT_ERROR = "usageLimitExceeded"   # Codex's name for a turn that the account's limit ended
MARK = "codex.limit"        # stations/<station>.codex.limit

STRETCH_TEXT = ("Slow down until the factory tells you otherwise: finish what you are on and start nothing that can "
                "wait. If you keep a schedule of your own, set it to 45 minutes.")
UNHOLD_TEXT = ("The account's limit has room for builders again: pick up where your report says you stopped. Keep "
               "to the slower pace while the factory still paces the crew.")
RELEASE_TEXT = ("The factory has stopped pacing the crew (the account has room, or pace was turned off). If you keep "
                "a schedule of your own, put it back to its normal interval. Then carry on from your report, "
                "including anything you held back.")


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr, flush=True)


# ---------- the reading ----------


def _codex_stations(paths: CrewPaths) -> list:
    return [n for n, row in state.stations_load(paths).items()
            if isinstance(row, dict) and row.get("status") == "active" and row.get("runtime") == RUNTIME]


def take(paths: CrewPaths) -> dict | None:
    """The account's limits as the first station that answers gives them: {limits, account, taken_at,
    station}, or None when no station of the crew answers. `limits` holds the windows only and `account` its
    type and plan: an account id, an address or a workspace name never leaves the station. An account that is
    not a ChatGPT one and refuses the question (an API key has no plan) gives empty `limits`: Tares then says
    the account reports none, which is not the same as no reading."""
    for s in _codex_stations(paths):
        try:
            with codex_station.visit(paths, s, VISIT_TIMEOUT) as wire:
                try:
                    account = wire.request("account/read", {"refreshToken": False}).get("account")
                except WireError:
                    account = None
                account = account if isinstance(account, dict) else {}
                try:
                    got = wire.request("account/rateLimits/read")
                except WireError:
                    # An account that is not a ChatGPT one has no plan to read: that is an answer ("no
                    # limits"), and Tares is told so. For a ChatGPT account it is a read that failed.
                    if account.get("type") in (None, "chatgpt"):
                        raise
                    got = {}
        except (CrewError, WireError, OSError):
            continue
        if not isinstance(got, dict):
            continue
        return {"limits": {k: got.get(k) for k in ("rateLimits", "rateLimitsByLimitId") if got.get(k) is not None},
                "account": {"type": account.get("type"), "planType": account.get("planType")},
                "taken_at": state.utc_stamp(), "station": s}
    return None


def buckets(limits: dict) -> list:
    """[(bucket name, its windows)] of a reading's `limits`, the main bucket first and named "": for printing."""
    out = []
    main = limits.get("rateLimits") if isinstance(limits.get("rateLimits"), dict) else {}
    more = limits.get("rateLimitsByLimitId") if isinstance(limits.get("rateLimitsByLimitId"), dict) else {}
    for name, snap in [("", main), *sorted((str(k), v) for k, v in more.items()
                                           if isinstance(v, dict) and k != main.get("limitId"))]:
        windows = [w for _, w in sorted(snap.items()) if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float))]
        if windows:
            out.append((name, windows))
    return out


_QUIET: dict = {}   # crew home -> what was said last about a reading that could not be taken or sent


def _once(paths: CrewPaths, said: str) -> None:
    """Say why there is no new reading, once: again only after a reading worked or the reason changed."""
    if _QUIET.get(str(paths.home)) != said:
        _QUIET[str(paths.home)] = said
        _say(said)


def refresh(client: TaresClient, paths: CrewPaths) -> str:
    """Take the account's limits and send them to Tares. "sent", or why not: "no station" (none of the crew
    answers), "tares off", "not sent". Never raises: a round goes on without a new reading."""
    if not client.tares_on():
        return "tares off"
    reading = take(paths)
    if reading is None:
        if _codex_stations(paths):
            _once(paths, "could not read the Codex account's limits (no station answers); the pace goes on from the "
                         "last reading until it is 30 minutes old")
        return "no station"
    body = {k: reading[k] for k in ("limits", "account", "taken_at")}
    try:
        client.tares_http("PUT", LIMITS_PATH, body)
    except TaresHTTPError:
        _once(paths, "could not send the Codex account's limits to Tares; trying again next round")
        return "not sent"
    _QUIET.pop(str(paths.home), None)
    return "sent"


def forget() -> None:
    _QUIET.clear()


# ---------- what a revived or resumed station is told ----------


def schedule_text(client: TaresClient) -> str:
    """What a restart tells a Codex station about its schedule, in place of lifecycle.rearm_text (which names
    Claude Code's Cron tools)."""
    from . import launch
    if not client.tares_on():
        return "keep the schedule you had, if you had one"
    if launch.idle_wake_on(client):
        return "keep no schedule of your own (idle_wake is on: the factory wakes you when there is work)"
    return "keep the schedule you had, if you had one"


def resume_prompt(client: TaresClient) -> str:
    from . import lifecycle
    return ("The account's limit was reset. Carry on from where you stopped: "
            f"{lifecycle.REPORT_READ} and your work on Tares, {schedule_text(client)}, then continue.")


def fresh_prompt(client: TaresClient, paths: CrewPaths, station: str) -> str:
    from . import lifecycle
    return ("The account's limit was reset, but your last conversation could not be resumed, so this is a new one "
            "under the same name. Nothing from it carries over except your report and what is on Tares: "
            f"{lifecycle.REPORT_READ} and your work on Tares, {schedule_text(client)}, then carry on where your "
            "report left off.")


def limit_prompt(client: TaresClient) -> str:
    """For a station whose turn the limit ended, and whose app-server went on running."""
    from . import lifecycle
    return ("Your last turn was ended by the account's limit, which has reset. Nothing was lost but that turn. "
            f"Check what it had done before you repeat anything, {lifecycle.REPORT_READ} and your work on Tares, "
            f"{schedule_text(client)}, then carry on.")


# ---------- a limit reached in the middle of a turn ----------


def mark_file(paths: CrewPaths, station: str):
    return paths.station_file(station, MARK)


def mark_read(paths: CrewPaths, station: str) -> dict | None:
    doc = state.read_json(mark_file(paths, station), None)
    return doc if isinstance(doc, dict) and doc.get("turn") else None


def _mark_write(paths: CrewPaths, station: str, doc: dict) -> None:
    try:
        state.write_json(mark_file(paths, station), doc, compact=True)
    except OSError:
        pass


def _mark_drop(paths: CrewPaths, station: str) -> None:
    try:
        mark_file(paths, station).unlink()
    except OSError:
        pass


class _Unasked(Exception):
    """The station's turns could not be listed now."""


def _newest_turn(paths: CrewPaths, station: str):
    """The station's newest turn as Codex lists it, or None when its conversation has none (or it has no
    conversation). Raises _Unasked when the station cannot be asked: silence is never taken for an answer."""
    thread = state.stations_get(station, "sessionId", paths=paths)
    if not thread:
        return None
    try:
        with codex_station.visit(paths, station, VISIT_TIMEOUT) as wire:
            data = wire.request("thread/turns/list", {"threadId": thread, "limit": 1}).get("data")
    except (CrewError, WireError, OSError) as e:
        raise _Unasked(str(e)) from None
    if not isinstance(data, list):
        raise _Unasked("the list of its turns was not a list")
    return data[0] if data and isinstance(data[0], dict) else None


def limit_of(turn) -> dict | None:
    """{turn, message} when `turn` is one the account's limit ended, else None. Pure."""
    if not isinstance(turn, dict) or turn.get("status") != "failed" or not turn.get("id"):
        return None
    error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
    if error.get("codexErrorInfo") != LIMIT_ERROR:
        return None
    return {"turn": str(turn["id"]), "message": " ".join(str(error.get("message") or "").split())[:300]}


def limit_turn(paths: CrewPaths, station: str) -> dict | None:
    """The station's newest turn when the account's limit ended it: {turn, message}. None for any other turn,
    for a station with no conversation, and for one that cannot be asked."""
    try:
        return limit_of(_newest_turn(paths, station))
    except _Unasked:
        return None


def held(paths: CrewPaths, station: str) -> str:
    """Why the station gets no automatic prompt now, "" when it may: its last turn was ended by the account's
    limit, which holds it until the reset carries it on (`limit_step`). Read from the mark the pace step
    writes and, for a turn ended since the pace step last looked, from the turn itself, so a step that runs
    before the pace step in a round (idle wake, regrant) holds it too. Whatever the reading says: a model's
    own limit, and a reading that is missing or stale, hold it the same."""
    mark = mark_read(paths, station)
    if mark:
        return f"the account's limit ended its turn {mark.get('turn')}"
    hit = limit_turn(paths, station)
    return f"the account's limit ended its turn {hit['turn']}" if hit else ""


def _when(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def reset_time(reading, now: float) -> float | None:
    """When the limits that are full now reset (the latest of them), from the windows of the reading, the ones
    the pace does not know too: a turn may have been ended by a limit of one model. None when the reading
    names no full window with a reset time ahead."""
    raw = reading.raw if isinstance(getattr(reading, "raw", None), dict) else {}
    ends = []
    for x in raw.get("limits") if isinstance(raw.get("limits"), list) else []:
        if not isinstance(x, dict) or x.get("reset") is True:
            continue
        p, t = x.get("percent"), _when(x.get("resets_at"))
        if isinstance(p, (int, float)) and not isinstance(p, bool) and p >= 100 and t is not None and t > now:
            ends.append(t)
    return max(ends) if ends else None


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@locks.scoped   # the station's lock goes with the prompt it is handed, as in pace.send
def limit_step(client: TaresClient, paths: CrewPaths, s: str, reading, *, claude_bin=None, now=None) -> bool:
    """One round for one live Codex station (see the module's note). True when the station is held for a limit
    that ended its turn, or was just carried on after it: the caller then tells it nothing else this round."""
    from . import lifecycle, proc
    if reading.state == "off":
        _mark_drop(paths, s)
        return False
    if not proc.at_rest(s, claude_bin, paths):
        return False   # in a turn: whatever the last one was, it has been carried on
    now = state.now_s() if now is None else now
    mark = mark_read(paths, s)
    try:
        hit = limit_of(_newest_turn(paths, s))
    except _Unasked:
        return mark is not None   # it cannot be asked now: a mark stays, and nothing is decided
    if hit is None:
        _mark_drop(paths, s)   # its newest turn is not one the limit ended: it has been carried on
        return False
    until = reset_time(reading, now) if reading.state == "active" else None
    if mark is None or mark.get("turn") != hit["turn"]:
        mark = {"turn": hit["turn"], "at": now, "until": until, "said": False}
        _mark_write(paths, s, mark)
        when = f", which resets {_stamp(until)}" if until else ", and no reading says when it resets"
        state.event("limit", s, f"turn {hit['turn']} ended at the account's limit{when}: {hit['message']}"[:400],
                    paths=paths)
        _say(f"the account's limit ended a turn of {s}{when}; its work is kept, and it is carried on once after the reset"
             if until else
             f"the account's limit ended a turn of {s}{when}; its work is kept")
    elif until is not None and (mark.get("until") is None or until > mark["until"]):
        mark["until"] = until   # the limit that holds it resets later than was known
        _mark_write(paths, s, mark)
    if reading.state != "active" or reading.closed:
        return True   # no reading to go by, or a limit is still full: it waits
    if mark.get("until") is None:
        if not mark.get("said"):
            mark["said"] = True
            _mark_write(paths, s, mark)
            _say(f"{s} is not carried on by the watch: no reading said when the limit that ended its turn resets. "
                 f"`{ME} restart {s} \"<prompt>\"` hands it its next prompt")
        return True
    if now < mark["until"] or state.halted(paths):
        return True
    fresh = TaresClient(paths, client.env, client.say)
    try:
        client.say(f"pacing: the account's limit has reset; carrying {s} on in the same conversation")
        try:
            lifecycle.restart(fresh, paths, [s, limit_prompt(fresh)], claude_bin=claude_bin, only_between_turns=True,
                              prompt_wait=math.inf, past_limit=True)
        except NotBetweenTurnsError:
            _mark_drop(paths, s)   # something else carried it on first
            return True
        except (CrewError, OSError) as e:
            sys.stdout.flush()
            print(e.line if isinstance(e, CrewError) else e, file=sys.stderr)
            client.say(f"could not carry {s} on after the limit's reset; will try again next round")
            return True
    finally:
        fresh.close()
    _mark_drop(paths, s)
    state.event("pace", s, "resumed after the limit's reset", paths=paths)
    return True
