"""The first run of a new factory cell (TR-1312): what the home's "Get your crew running" panel shows.

Two choices are made in the console (how the crew works, where tickets live) and four commands are run
in a terminal. Each step ticks from what the cell knows: the crew's settings, the GitHub and Linear
connections, a machine report the command line posts after `tares login` (this module), and the crew's
live sessions. `payload()` is what GET /api/home carries as `onboarding`; tares/start_steps.py words
the steps from the same facts.

Nothing here is a secret: the machine report is a name and a version.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

MACHINE_SETTING = "crew_machine"     # {"machine", "version", "at"}: the last `tares login` or `crew up`;
                                     # `crew_machine:<person>` for a person's own (machine_key)
TRACKER_SETTING = "crew_tracker"     # where tickets live, chosen in step 2
TRACKERS = ("tares", "linear", "github_issues")

# the sessions that count as up: a quiet one was heard from and has not ended
UP = ("working", "idle", "waiting", "quiet")
RUNNING = ("working", "idle", "waiting")
AUTONOMY = ("L3-review", "L4-ship")
DEFAULT_AUTONOMY = "L3-review"      # a new crew starts on your yes for each merge (the crew's own default is L4-ship)


def _clean(text, limit: int) -> str:
    """A short plain-text field: printable characters (a curly apostrophe stays), no control
    characters, trimmed. The report is shown as text, never run."""
    return "".join(c for c in str(text or "") if c.isprintable()).strip()[:limit]


def machine_key(person_id: str | None = None) -> str:
    """Where a machine report is kept: the cell's own, or one person's (a person's crew is theirs)."""
    return f"{MACHINE_SETTING}:{person_id}" if person_id else MACHINE_SETTING


def save_machine(store, machine, version, now: datetime | None = None, key: str = MACHINE_SETTING) -> dict:
    """Keep the machine report under `key`. Either field may be empty (`crew up` knows the version,
    `login` both); an empty one keeps what was reported before. A report with both empty changes
    nothing, not even the time."""
    old = machine_report(store, key) or {}
    m, v = _clean(machine, 80), _clean(version, 40)
    if not m and not v:
        return {"machine": old.get("machine", ""), "version": old.get("version", ""), "at": old.get("at")}
    out = {"machine": m or old.get("machine", ""), "version": v or old.get("version", ""),
           "at": (now or datetime.now(timezone.utc)).isoformat()}
    store.set_setting(key, json.dumps(out))
    return out


def machine_report(store, key: str | None = MACHINE_SETTING) -> dict | None:
    """The report kept under `key`; None for no key (a credential with no person has no report)."""
    if not key:
        return None
    raw = store.get_setting(key)
    try:
        out = json.loads(raw) if raw else None
    except ValueError:
        return None
    return out if isinstance(out, dict) and (out.get("machine") or out.get("version")) else None


def saved_tracker(store) -> str | None:
    v = store.get_setting(TRACKER_SETTING)
    return v if v in TRACKERS else None


def save_tracker(store, value: str) -> str:
    if value not in TRACKERS:
        raise ValueError("tracker must be one of " + ", ".join(TRACKERS))
    store.set_setting(TRACKER_SETTING, value)
    return value


def defaults() -> dict:
    """What the first-run form starts on. Who merges starts on the person's own yes (L3-review): the
    crew merging on its own is asked about before it is saved. Releases and builders are the crew's
    own defaults, unchanged (`settings_defaults()` itself is not touched). Saved only on confirm."""
    from .crew.client import settings_defaults
    d = settings_defaults()
    return {"autonomy": DEFAULT_AUTONOMY, "releaser": d["releaser"], "builders_max": d["builders_max"]}


def choices(settings: dict) -> dict:
    """The step 1 choices as the cell holds them. `chosen` once autonomy, releases and builders
    are all set on the cell (the crew asks nothing it already has)."""
    a, r, b = settings.get("autonomy"), settings.get("releaser"), settings.get("builders_max")
    chosen = a in AUTONOMY and isinstance(r, bool) and isinstance(b, int) and not isinstance(b, bool) and b >= 1
    return {"chosen": chosen, "autonomy": a if a in AUTONOMY else None,
            "releaser": r if isinstance(r, bool) else None,
            "builders_max": b if isinstance(b, int) and not isinstance(b, bool) else None,
            "defaults": defaults()}


def choices_text(c: dict) -> str:
    """The one line a finished step 1 shows."""
    merges = "You approve each merge" if c["autonomy"] == "L3-review" else "The crew merges on its own"
    n = c["builders_max"] or 0
    builders = f"{n} builder" + ("" if n == 1 else "s")
    return f"{merges}, {builders}, " + ("releases on" if c["releaser"] else "no releases")


def tracker(store, linear: bool, issues: bool, crew_up: bool) -> str | None:
    """Where tickets live: the saved choice; else what is connected; else Tares itself for a cell whose
    crew already runs."""
    return (saved_tracker(store) or ("linear" if linear else "github_issues" if issues
                                    else "tares" if crew_up else None))


def tracker_done(chosen: str | None, github: bool, linear: bool) -> bool:
    """Step 2 is done with GitHub connected and the tickets' place settled: nothing to connect for
    Tares, Linear connected for Linear, GitHub Issues uses the GitHub connection."""
    if not github or chosen is None:
        return False
    return linear if chosen == "linear" else True


def github_text(store) -> str:
    """"Connected as example-org" from the saved credentials, or "" when there is none."""
    rows = store.list_github_credentials() or []
    names = sorted({str(r.get("account") or "").strip() for r in rows} - {""})
    if not names:
        return "GitHub connected" if rows else ""
    return "GitHub connected as " + ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def _role(stations: list[dict], role: str) -> dict | None:
    rows = [s for s in stations if s.get("role") == role]
    live = [s for s in rows if s.get("state") in UP]
    return (live or rows or [None])[0]


def member(stations: list[dict], role: str, others_up: bool) -> dict:
    """One always-on member of the crew: running, quiet, stopped, or starting (nothing from it yet
    while another member is up) or none."""
    s = _role(stations, role)
    if s is None:
        return {"state": "starting" if others_up else "none", "last_at": None}
    state = s.get("state")
    shown = "running" if state in RUNNING else "quiet" if state == "quiet" else "stopped"
    last = s.get("last_at")
    return {"state": shown, "last_at": last.isoformat() if isinstance(last, datetime) else last}


def crew_view(stations: list[dict], settings: dict, machine: str) -> dict:
    """The "Your crew" panel: the machine, the orchestrator and the reviewer, and the builders."""
    o_row, r_row = _role(stations, "orchestrator"), _role(stations, "reviewer")
    up = lambda s: s is not None and s.get("state") in RUNNING   # noqa: E731
    orch = member(stations, "orchestrator", up(r_row))
    rev = member(stations, "reviewer", up(o_row))
    builders = [s for s in stations if s.get("role") == "builder" and s.get("state") in RUNNING]
    cap = settings.get("builders_max")
    return {"machine": machine, "orchestrator": orch, "reviewer": rev,
            "builders": {"live": len(builders),
                         "max": cap if isinstance(cap, int) and not isinstance(cap, bool) else
                         defaults()["builders_max"]},
            "up": o_row is not None and r_row is not None
                  and o_row.get("state") in UP and r_row.get("state") in UP}


def payload(store, stations: list[dict], settings: dict, linear: bool, issues: bool,
            key: str | None = MACHINE_SETTING, person_id: str | None = None) -> dict:
    """The `onboarding` block of the home, for the caller: `key` is where their machine report is
    kept (machine_key) and `person_id` is whose crew may name the machine when no report came (a
    crew that ran before the report existed). Another person's crew never does."""
    report = machine_report(store, key) or {}
    crews = [c for c in store.list_crews() if c.get("machine") and person_id and c.get("person_id") == person_id]
    machine = report.get("machine") or (crews[0]["machine"] if crews else "")
    c = choices(settings)
    crew = crew_view(stations, settings, machine)
    return {"choices": c,
            "tracker": tracker(store, linear, issues, crew["up"]),
            "github": {"connected": bool(store.list_github_credentials()), "text": github_text(store)},
            "linear": {"connected": bool(linear)},
            "machine": {"name": machine, "version": report.get("version") or "",
                        "at": report.get("at")},
            "crew": crew}
