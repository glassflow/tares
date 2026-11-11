"""The watch's idle-wake step: wake an idle station only when Tares has work for it (TR-961).

Ports these functions of `claude-plugin/bin/factory`: wake_base_min, iw_scan_reviewer, iw_scan_builder,
iw_scan_releaser, iw_scan_orchestrator, iw_work_prompt, idle_wake_plan and idle_wake_send. The wake file
helpers (wake_read, wake_update) live in `state`, `idle_wake_state` and `iw_launch_note` in `launch`,
`station_updated` in `claude`, and `rearm_text` in `lifecycle`.

With the crew setting idle_wake on, a station holds no tick of its own. Each round the watch looks at Tares
and wakes an idle station with one message when it has something to do, and sends a liveness wake when nothing
has happened for a while: after 15 minutes (reviewer), 20 (orchestrator) or 30 (builder, releaser), then 30,
then 45 and every 45 after. A wake is a restart in the same conversation. What a wake has already told a
station lives in stations/<station>.wake. With the setting off none of this runs and no file is written,
except for a station that was told to drop its tick: it is told once to arm it again.

Differences from bash
  * Bash decides what to do for a station in one `elif` chain (stuck, regrant, idle wake, pace, move onto an
    updated plugin), so a station gets at most one restart a round. Here every step is its own pass over the
    stations, in the order of `watch.STEP_NAMES`. A station that an earlier step restarted is not between turns,
    so this step leaves it alone: the one restart a station a round holds.
  * Bash re-reads Tares and the settings for each send (it clears TARES_STATE and SETTINGS); so does this, with
    a client of its own.

Standard library plus the crew modules.
"""
from __future__ import annotations

import os
import re
import sys

from . import claude, launch, lifecycle, locks, pace, state
from .claude import SessionListUnreadableError
from .client import TaresClient, TaresHTTPError, cksum, uri
from .errors import CrewError
from .paths import CrewPaths

ROLES = ("orchestrator", "reviewer", "releaser", "builder")
DEFAULT_SETTLE = 600   # seconds a session may be active after a wake before the clock restarts (FACTORY_WAKE_SETTLE)

_RELEASE = re.compile(r"(^|[^A-Za-z])RELEASE([^A-Za-z]|$)")
_CLOSED_AT = re.compile(r"^(?P<t>[0-9T:-]+)(\.[0-9]+)?(?P<z>Z|[+-][0-9]{2}:[0-9]{2})?$")
_SPACE = " \t\r\n\v\f"   # sed's [[:space:]]


class ScanError(Exception):
    """Tares gave an answer a scan cannot read: the scan fails and the station is not woken."""


def wake_base_min(role: str) -> int:
    """Minutes of quiet before the first liveness wake."""
    return {"reviewer": 15, "orchestrator": 20}.get(role, 30)


# ---------- reading what Tares says, the way jq would ----------


def _alt(*values):
    """jq's `a // b // c`: the first value that is neither null nor false."""
    for v in values:
        if v is not None and v is not False:
            return v
    return values[-1]


def _get(obj, key):
    """jq's `.key`: null for null, an error for anything that is not an object."""
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise ScanError(f"cannot index {type(obj).__name__} with {key!r}")
    return obj.get(key)


def _list(value) -> list:
    """jq's `(x // [])[]`: null and false are an empty list; anything else must be an array."""
    value = _alt(value, [])
    if not isinstance(value, list):
        raise ScanError("expected an array")
    return value


def _text(value) -> str:
    """What a jq string interpolation makes of a value."""
    return value if isinstance(value, str) else state.dumps_compact(value)


def _json(client: TaresClient, path: str):
    try:
        return state.loads_json(client.tares_http("GET", path))
    except (TaresHTTPError, ValueError) as e:
        raise ScanError(str(e)) from None


def _head7(head) -> str:
    if not isinstance(head, str):
        raise ScanError("head is not text")
    return head[:7]


# ---------- what each role has to do ----------
# Each scan gives {pending: [{key, text}], live: bool}; the orchestrator's also carries the baseline it compares
# with. A key names one thing to do once. A scan raises ScanError when Tares cannot be read.


def scan_reviewer(client: TaresClient, paths: CrewPaths, s: str) -> dict:
    """The review queue's PRs without a current verdict, once per head."""
    q = _json(client, "/api/review-queue?mine=1")
    pending = []
    for item in _list(_get(q, "queue")):
        verdict = _get(item, "verdict")
        if verdict is None or not _alt(_get(verdict, "current"), False):
            head = _alt(_get(item, "head"), "")
            project = _get(item, "project")
            pending.append({
                "key": f"rv:{_text(project)}/{_text(_get(item, 'ticket'))}@{_text(head)}",
                "text": f"{_text(_alt(_get(item, 'project_name'), project))} {_text(_get(item, 'ticket'))} "
                        f"at {_head7(head)}"})
    return {"pending": pending, "live": True}


def scan_builder(client: TaresClient, paths: CrewPaths, s: str) -> dict:
    """The tickets it holds that need it: changes asked for, a verdict, or failed CI."""
    project = state.stations_get(s, "project", paths=paths)
    uid = ""
    for p in _list(_get(_json(client, "/api/crew/projects"), "projects")):
        if _get(p, "name") == project or _get(p, "id") == project:
            found = _get(p, "id")
            uid = "" if found is None or found is False else state.jq_raw(found)
            break
    if not uid:
        return {"pending": [], "live": False}
    doc = _json(client, f"/api/projects/{uri(uid)}/tickets")
    mine = [t for t in _list(_get(doc, "tickets"))
            if _get(t, "holder") == s and _get(t, "stage") in ("review", "changes")]
    pending = []
    for t in mine:
        pr = _alt(_get(t, "pr"), {})
        verdict = _get(t, "verdict")
        vd = ""
        if verdict is not None and _alt(_get(verdict, "current"), False) and _get(verdict, "verdict") in ("pass", "changes", "block"):
            vd = verdict["verdict"]
        ci = _get(pr, "ci")
        if not (_get(t, "stage") == "changes" or vd != "" or ci == "failure"):
            continue
        label = _text(_get(t, "label"))
        if vd != "":
            why = f"verdict {vd}"
        elif ci == "failure":
            why = "CI failed"
        else:
            why = "changes asked for"
        pending.append({"key": f"bd:{label}@{_text(_alt(_get(pr, 'head'), ''))}|{vd}|{_text(_alt(ci, ''))}",
                        "text": f"{label} ({why})"})
    return {"pending": pending, "live": bool(mine)}


def scan_releaser(client: TaresClient, paths: CrewPaths, s: str) -> dict:
    """RELEASE lines under Undelivered in the orchestrator's report, once per line."""
    f = paths.reports_dir / f"{state.crew_orchestrator(paths)}.md"
    if not f.is_file():
        return {"pending": [], "live": True}
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as e:
        raise ScanError(str(e)) from None
    pending, under = [], False
    for raw in lines:
        if raw.startswith("## "):
            under = raw.startswith("## Undelivered")
        if not (under and _RELEASE.search(raw)):
            continue
        line = raw.strip(_SPACE)
        if line:
            pending.append({"key": f"rl:{cksum(line.encode('utf-8'))}", "text": line})
    return {"pending": pending, "live": True}


def _secs(closed_at):
    """The epoch seconds of a closed_at time (a UTC time with an optional fraction and offset); None when
    it is not one."""
    if not isinstance(closed_at, str):
        return None
    m = _CLOSED_AT.match(closed_at)
    if not m:
        return None
    t = claude.iso_epoch(m.group("t") + "Z")
    if t is None:
        return None
    z = m.group("z") or "Z"
    if z == "Z":
        return t
    sign = -1 if z[0] == "-" else 1
    return t - sign * (int(z[1:3]) * 3600 + int(z[4:6]) * 60)


def scan_orchestrator(client: TaresClient, paths: CrewPaths, s: str) -> dict:
    """Desk answers closed, and projects handed over, since the baseline; the first look only sets it."""
    desk = _json(client, "/api/desk?state=answered&mine=1")
    projs = _json(client, "/api/crew/projects")
    orch = state.crew_orchestrator(paths)
    base = state.wake_read(orch, paths)["baseline"]
    act = claude.station_updated(orch, paths)   # an answer the orchestrator itself recorded is not news to it
    answers = [a for a in _list(_get(desk, "desk"))
               if _get(a, "command") is None and _get(a, "closed_at") is not None]
    dmax = max((a["closed_at"] for a in answers), default="")
    project_rows = _list(_get(projs, "projects"))
    ids = [_get(p, "id") for p in project_rows]
    try:
        if base is None:
            return {"first": True, "pending": [], "live": True, "baseline": {"desk": dmax, "projects": ids}}
        base_desk = _alt(_get(base, "desk"), "")
        known = _alt(_get(base, "projects"), [])
        if not isinstance(known, list):
            raise ScanError("baseline projects is not an array")
        pending = []
        for a in answers:
            if not a["closed_at"] > base_desk:
                continue
            if act is not None:
                when = _secs(a["closed_at"])
                if (act + 1 if when is None else when) <= act:
                    continue
            pending.append({"key": f"dk:{_text(_get(a, 'id'))}",
                            "text": f"desk answer {_text(_alt(_get(a, 'label'), _get(a, 'id')))} is closed"})
        for p in project_rows:
            if _get(p, "id") not in known:
                pending.append({"key": f"ph:{_text(_get(p, 'id'))}",
                                "text": f"project {_text(_alt(_get(p, 'name'), _get(p, 'id')))} was handed over"})
        return {"live": True, "pending": pending,
                "baseline": {"desk": max(dmax, base_desk), "projects": sorted(set(known + ids), key=state.dumps_compact)}}
    except TypeError as e:   # a time that is not text, an id that cannot be ordered
        raise ScanError(str(e)) from None


SCANS = {"reviewer": scan_reviewer, "builder": scan_builder, "releaser": scan_releaser,
         "orchestrator": scan_orchestrator}


def work_prompt(role: str, items: str) -> str:
    if role == "reviewer":
        return f"There is work in the review queue: {items} have no current verdict. Work the queue."
    if role == "builder":
        return (f"Your tickets changed: {items}. Read the reviewer's verdict and CI on the current head and act "
                "on it, as your manual says.")
    if role == "releaser":
        return (f"A RELEASE is waiting for you in the orchestrator's report under Undelivered: {items}. Take it "
                "as your manual says.")
    return f"Something is waiting for you: {items}. Follow the Tick section of your manual."


# ---------- deciding and waking ----------


def pace_level(paths: CrewPaths, env=None) -> int:
    """The pace level of the plan; 0 while nothing is paced or the pace is off or unread. This is the reading of
    the round (`pace.current`, the one the revive step uses): bash reads the pace once at the top of a round, and
    its PACE_LEVEL stays 0 unless the reading is active."""
    reading = pace.current(paths, env)
    return reading.level if reading.state == "active" else 0


def _settle(env) -> int:
    text = env.get("FACTORY_WAKE_SETTLE") or str(DEFAULT_SETTLE)
    try:
        return int(float(text))
    except ValueError:
        raise ScanError(f"FACTORY_WAKE_SETTLE is not a number: {text}") from None


def _after(n: int, base: int, level: int) -> int:
    """Seconds of quiet before the n-th liveness wake; pacing waits the longest at once."""
    if level >= 1:
        return 2700
    if n == 0:
        return base * 60
    return 1800 if n == 1 else 2700


def plan(client: TaresClient, paths: CrewPaths, s: str, *, claude_bin=None, now=None):
    """What to tell station `s` now, or None when it is not to be woken. A dict with the station, the state
    of the setting it was decided under, the time, the reason, the prompt, the keys of what is pending, the
    baseline, the wake count and whether it is a work wake."""
    role = state.stations_get(s, "role", paths=paths)
    if role not in ROLES:
        return None
    if state.halted(paths) or not state.stations_get(s, "sessionId", paths=paths):
        return None
    st = launch.idle_wake_state(client)
    if st == "off":
        if state.wake_read(s, paths)["released"] is not True:
            return None
    elif st != "on":
        return None
    if claude.attached(s, paths) or not claude.between_turns(s, claude_bin, paths):
        return None
    now = state.now_s() if now is None else now
    out = {"station": s, "state": st, "now": now, "keys": [], "baseline": None, "n": 0, "work": False}
    if st == "off":
        out.update(reason="rearm", prompt="idle_wake is off again: re-arm your tick as your manual says.")
        return out

    w = state.wake_read(s, paths)
    try:
        scan = SCANS[role](client, paths, s)
        settle = _settle(client.env)
    except ScanError:
        return None
    upd = claude.station_updated(s, paths)
    level = pace_level(paths, client.env)
    hold = role == "builder" and level >= 2   # a builder told to hold gets no work wake
    c0 = _alt(w["clock"], now)
    if upd is not None and upd > _alt(w["last_wake"], c0) + settle:
        n, clock = 0, upd
    else:
        n, clock = w["n"], c0
    if not scan["live"]:
        n, clock = 0, now
    pending = scan["pending"]
    seen = w["seen"] if isinstance(w["seen"], list) else []
    new = [] if hold else [p for p in pending if p["key"] not in seen]
    keys = [p["key"] for p in pending]
    due = scan["live"] and now - clock >= _after(n, wake_base_min(role), level)
    # A builder with no PR holds no tick, so it counts as released.
    update = {"clock": clock, "n": n, "seen": [k for k in seen if k in keys]}
    if scan.get("first"):
        update["baseline"] = scan["baseline"]
    if not scan["live"]:
        update["released"] = True
    try:
        state.wake_update(s, update, paths)
    except OSError:
        pass
    out.update(keys=keys, baseline=scan.get("baseline"), n=n)
    note = launch.idle_wake_note()
    if new:
        texts = "; ".join(p["text"] for p in new)
        out.update(work=True, n=0, reason=f"work: {texts}",
                   prompt=f"{work_prompt(role, texts)} {note} Carry on from your report.")
    elif scan["live"] and w["released"] is not True:
        # Switched on under a running station: it still holds the tick it armed itself, so tell it now.
        out.update(n=0, reason="release: idle_wake was switched on, telling the station to drop its tick",
                   prompt=f"idle_wake was switched on. {note} Carry on from your report.")
    elif due:
        out.update(n=n + 1, reason=f"liveness: wake {n + 1} with nothing pending",
                   prompt=("Liveness check, nothing is known to be pending: check your queue once, then end the "
                           f"turn. {note} Carry on from your report."))
    else:
        return None
    return out


@locks.scoped   # the station's lock goes with its wake, as with the end of bash's per-station subshell (TR-1171)
def send(client: TaresClient, paths: CrewPaths, p: dict, *, claude_bin: str) -> None:
    """Restart the station `plan` chose, in the same conversation. Never raises for a station that cannot be
    woken: it is said, and tried again next round."""
    s = p["station"]
    fresh = TaresClient(paths, client.env, client.say)   # Tares and the settings, read again
    try:
        if not fresh.tares_on():
            return
        if launch.idle_wake_state(fresh) != p["state"]:
            return   # the setting changed since the round began
        if (state.halted(paths) or claude.attached(s, paths)
                or not claude.between_turns(s, claude_bin, paths)):
            return   # it started something since the first check
        client.say(f"waking {s} ({p['reason'].split(':')[0]}); restarting it in the same conversation")
        try:
            lifecycle.restart(fresh, paths, [s, p["prompt"]], claude_bin=claude_bin)
        except (CrewError, OSError) as e:   # a restart that hits the disk is no different from a refused one
            sys.stdout.flush()
            print(getattr(e, "line", None) or str(e), file=sys.stderr)
            client.say(f"could not wake {s}; will try again next round")
            return
        state.event("idle_wake", s, p["reason"], paths=paths)
    finally:
        fresh.close()
    fields = {"last_wake": p["now"], "clock": p["now"], "n": p["n"], "released": p["state"] == "on"}
    if p["work"]:
        fields["seen"] = p["keys"]
    if p["baseline"] is not None:
        fields["baseline"] = p["baseline"]
    try:
        state.wake_update(s, fields, paths)
    except OSError:
        pass


# ---------- the step ----------


def step(paths: CrewPaths, *, env=None, claude_bin=None) -> None:
    """One round of the idle wake: every active, live station of a crew role that is between turns and has work
    (or is due its liveness wake) is restarted with a message. A round the revive step could not read the session
    list for does nothing here either, and a station it skipped, revived or restarted this round is left alone, as bash's
    elif chain leaves it."""
    from . import watch_revive   # the module, not `last`: the revive step rebinds `last` every round
    env = os.environ if env is None else env
    if watch_revive.last.unread:
        return
    names = [k for k, v in state.stations_load(paths).items() if isinstance(v, dict) and v.get("status") == "active"]
    if not names:
        return
    if claude_bin is None:
        claude_bin = claude.claude_bin(env)
    client = TaresClient(paths, env)
    try:
        for s in names:
            role = state.stations_get(s, "role", paths=paths)
            if role == "helper":
                continue
            if role == "builder" and paths.station_file(s, "done").is_file():
                continue
            if s in watch_revive.last.handled:
                continue
            try:
                if not claude.is_alive(s, "", claude_bin, paths):
                    continue
            except SessionListUnreadableError:
                client.say(f"skipping {s} this round")
                continue
            try:
                p = plan(client, paths, s, claude_bin=claude_bin)
                if p is not None:
                    send(client, paths, p, claude_bin=claude_bin)
            except SessionListUnreadableError:
                client.say(f"skipping {s} this round")
            except CrewError as e:
                client.say(e.message)
    finally:
        client.close()
