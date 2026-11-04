"""Incidents with an owner and an end (TR-744).

Every note the investigator writes belongs to an incident: a run of notes about one service while
it was still the same trouble. An incident is open (nobody on it), on_it (somebody took it) or
resolved (a person closed it, it resolved itself, or it is from before incidents were tracked).

Everything here only writes the incident tables, so it runs on every cell. The one thing that
changes what an existing cell does, a joined note posted as a Slack thread reply, lives in
tares/builtin_agents.py and runs on AI SRE cells only.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import goal
from .envelope import now_utc

BACKFILL_SETTING = "sre.incidents_backfilled"
BACKFILL_VERSION = "2"        # bump when the rule for "is a note" or for joining changes
SELF_RESOLVE_AFTER = timedelta(hours=24)
BACKFILL_PAGE = 2000           # runs the backfill reads at a time
SWEEP_PAGE = 500              # open incidents the sweep reads at a time
BEFORE_TRACKING_AFTER = timedelta(days=7)
DEFAULT_COOLDOWN = 3600.0     # seconds, when neither a handoff nor a trigger says
CARRIER_WINDOW = timedelta(hours=1)

clock = now_utc               # the notes' clock; a test replaces it instead of sleeping
ON_CHANGE: list = []          # hook(store, incident, via), called after every state change
_LOCK = threading.Lock()


def _aware(dt):
    return goal._aware(dt)


# ── what is a note, and what service is it about ─────────────────────────────
def watcher_triggers(store, catalog, agent_name: str, roles: dict | None = None) -> set:
    """The triggers on which `agent_name` is a watcher (TR-743 roles), paused ones included."""
    from . import sre
    roles = roles or sre.roles(store, catalog, include_paused=True)
    return {w["trigger"] for w in roles["watchers"] if w["agent"] == agent_name}


def is_note(store, catalog, agent: dict, run: dict, roles: dict | None = None) -> bool:
    """Whether `run` is an incident note: it concluded a finding, it is not practice and
    goal.outcome_of (the one rule) calls it note_written. A watcher's finding is "raised" and a
    finding whose verdict says there is nothing to do is "investigated", neither is a note."""
    if run.get("practice") or run.get("status") != "ok" or run.get("outcome") != "finding":
        return False
    if not str(run.get("finding") or "").strip():
        return False
    triggers = watcher_triggers(store, catalog, agent.get("name") or run.get("agent") or "", roles)
    return goal.outcome_of(run, goal.is_watcher_run(run, triggers)) == "note_written"


def service_of(store, catalog, key: str, label: str | None, at=None) -> str:
    """The service a note is about. A key that is a value of a service label is the service
    (followed through its merges); a key that is a value of another label (a path, a host) is
    the service whose events carried that value most in the last hour; else the key itself."""
    from . import sre
    key = str(key or "").strip()
    settings = store.service_settings()
    if not label:
        return sre.canonical(key, settings)
    overrides = sre._overrides(store)
    spec = {}
    for src in catalog.sources.values():
        svc_label = sre.service_label(src, overrides)
        if svc_label:
            spec[src.name] = svc_label
    if label in spec.values():
        return sre.canonical(key, settings)
    try:
        since = _aware(at or clock()) - CARRIER_WINDOW
        found = store.services_carrying(spec, label, key, since)
    except Exception as e:   # a label the event query refuses is no service
        print(f"[incidents] service lookup: {type(e).__name__}: {e}")
        found = []
    return sre.canonical(found[0][0], settings) if found else sre.canonical(key, settings)


def cooldown_for(store, catalog, run: dict, known: dict | None = None) -> float:
    """Seconds a later note on the service still joins this note's incident: the cooldown of the
    handoff that started the run, else its trigger's cooldown, else an hour. `known` carries what
    a caller that files many notes has read in bulk ("parents": run id -> agent, "projects": run
    id -> projects, "handoffs": a dict this fills), so each note costs no query of its own."""
    from .config import parse_duration
    known = known or {}
    try:
        if run.get("woken_by") == "handoff" and run.get("parent_run_id"):
            if "parents" in known:
                parent_agent = known["parents"].get(run["parent_run_id"])
            else:
                parent = store.get_agent_run(run["parent_run_id"])
                parent_agent = parent["agent"] if parent else None
            if parent_agent:
                if "projects" in known:
                    own = known["projects"].get(run.get("id"), []) if run.get("id") else []
                else:
                    own = store.run_projects(run["id"]) if run.get("id") else []
                projects = own or [run.get("project")]
                memo = known.setdefault("handoffs", {})
                found = []
                for p in projects:
                    if not p:
                        continue
                    if (p, parent_agent) not in memo:
                        memo[(p, parent_agent)] = store.list_handoffs(project=p,
                                                                      from_agent=parent_agent)
                    found += [h for h in memo[(p, parent_agent)] if h["agent"] == run.get("agent")]
                if found:
                    return max(parse_duration(h.get("cooldown") or "30m") for h in found)
        trig = next((t for t in catalog.triggers if t.name == run.get("trigger")), None)
        if trig is not None:
            return float(trig.cooldown_seconds)
    except Exception as e:
        print(f"[incidents] cooldown: {type(e).__name__}: {e}")
    return DEFAULT_COOLDOWN


# ── a note arrives ───────────────────────────────────────────────────────────
def on_note(store, catalog, agent: dict, run_id: str, key: str, label: str | None, *,
            run: dict | None = None, at=None, via: str = "runner") -> dict:
    """File the note of run `run_id` under the open incident of its service that had a note
    inside the cooldown, or under a new one. The state of a joined incident stays as it is:
    one somebody is on stays theirs. Returns the incident as stored."""
    at = _aware(at or clock())
    run = run or store.get_agent_run(run_id) or {}
    service = service_of(store, catalog, key, label, at)
    cooldown = cooldown_for(store, catalog, {**run, "id": run_id})
    who = agent.get("name")
    with _LOCK:
        existing = store.incident_for_run(run_id)
        if existing:
            return existing
        inc = store.open_incident_for(service, at - timedelta(seconds=cooldown))
        if inc:
            store.add_incident_note(inc["id"], run_id, at)
            store.log_incident(inc["id"], "note_joined", None, who, via, at)
        else:
            inc = store.create_incident(service, run.get("project") or agent.get("owned_by"), at)
            store.add_incident_note(inc["id"], run_id, at)
            store.log_incident(inc["id"], "opened", None, who, via, at)
    return store.get_incident(inc["id"])


# ── people change the state ──────────────────────────────────────────────────
def _changed(store, incident: dict | None, via: str) -> None:
    for hook in list(ON_CHANGE):
        try:
            hook(store, incident, via)
        except Exception as e:   # a hook must never fail the change it follows
            print(f"[incidents] hook failed: {type(e).__name__}: {e}")


class Held(Exception):
    """Somebody else is on the incident; the message names them."""

    def __init__(self, inc: dict):
        self.name = inc.get("owner_name") or inc.get("owner") or "Someone"
        super().__init__(f"{self.name} is on it.")


def take(store, incident_id: str, by_id: str | None, by_name: str | None,
         via: str = "console") -> dict | None:
    """Somebody is on it. Taking an incident somebody else is on changes nothing and raises Held.
    The write is conditional on the state that was read, so of two people who take a free incident
    at the same moment one holds it and the other gets Held."""
    inc = store.get_incident(incident_id)
    for _ in range(5):
        if inc is None:
            return None
        if inc["state"] == "on_it":
            if inc["owner"] == by_id and inc["owner_name"] == by_name:
                return inc
            raise Held(inc)
        out = store.set_incident_state(incident_id, "on_it", by_id, by_name, via,
                                       expect=inc["state"])
        if out is not None:
            _changed(store, out, via)
            return out
        inc = store.get_incident(incident_id)   # it moved between the read and the write: look again
    return inc


def resolve(store, incident_id: str, by_id: str | None, by_name: str | None,
            via: str = "console") -> dict | None:
    """A person closes it, and the notes it holds count as handled by them."""
    inc = store.get_incident(incident_id)
    if inc is None:
        return None
    if inc["state"] == "resolved":
        return inc
    out = store.set_incident_state(incident_id, "resolved", by_id, by_name, via,
                                   resolution="person")
    for note in store.incident_notes(incident_id):
        store.set_run_handled(note["run_id"], by_name or by_id or "console")
    _changed(store, out, via)
    return out


def reopen(store, incident_id: str, by_id: str | None, by_name: str | None,
           via: str = "console") -> dict | None:
    """Open again, with nobody on it. The sweep counts its quiet period from now, not from the
    last note, so a reopen lasts."""
    inc = store.get_incident(incident_id)
    if inc is None:
        return None
    if inc["state"] == "open":
        return inc
    out = store.set_incident_state(incident_id, "open", by_id, by_name, via, at=_aware(clock()))
    _changed(store, out, via)
    return out


# ── resolved itself ──────────────────────────────────────────────────────────
def _names_of(settings: dict, service: str) -> list[str]:
    """The service and every name merged into it: the keys its notes may carry."""
    from . import sre
    return [service] + sorted(n for n in settings
                              if n != service and sre.canonical(n, settings) == service)


def _said_all_clear(store, catalog, inc: dict, settings: dict, roles: dict,
                    reopened=None) -> bool:
    """Whether the investigator's latest finding on the service, written after the incident's
    last note (and after a person last reopened it), says there is nothing to do (a verdict such
    as resolved). A note itself never does, so this is the only way an incident learns the
    trouble went away."""
    notes = store.incident_notes(inc["id"])
    last_run = store.get_agent_run(notes[-1]["run_id"]) if notes else None
    after = _aware((last_run or {}).get("started_at") or inc["last_note_at"])
    if reopened is not None:
        after = max(after, _aware(reopened))
    names = _names_of(settings, inc["service"])
    ph = ", ".join(["?"] * len(names))
    where, params = f"key_value IN ({ph}) AND outcome = 'finding' AND status = 'ok'", list(names)
    # a watcher's finding is a flag, not the investigator's word: leave those runs out here, or
    # enough newer ones would push the investigator's word out of the rows read
    pairs = sorted({(w["agent"], w["trigger"]) for w in roles["watchers"]})
    if pairs:   # goal.is_watcher_run, in SQL: its trigger is a scheduled one and no handoff woke it
        where += " AND NOT (" + " OR ".join(
            ["(agent = ? AND trigger = ? AND (woken_by IS NULL OR woken_by <> 'handoff'))"]
            * len(pairs)) + ")"
        params += [x for pair in pairs for x in pair]
    rows = store.list_agent_runs(None, limit=1, where_sql=where, where_params=params)
    for r in rows:
        if _aware(r["started_at"]) <= after:
            return False
        return str(r.get("verdict") or "").strip().lower() in goal.NO_ACTION_VERDICTS
    return False


def sweep(store, catalog, now=None) -> list[dict]:
    """Close the open incidents that went quiet: nobody on it, no note (and no reopening by a
    person) for 24 hours and the investigator's latest word on the service says there is nothing
    to do. One somebody is on is never closed here. Returns the incidents it resolved."""
    from . import sre
    now = _aware(now or clock())
    cutoff = now - SELF_RESOLVE_AFTER
    settings = store.service_settings()
    roles = None
    closed, offset = [], 0
    while True:   # a page at a time: the ones that stay open must not hide the ones after them
        stale = store.list_incidents(state="open", before=cutoff, limit=SWEEP_PAGE, offset=offset)
        if not stale:
            break
        if roles is None:
            roles = sre.roles(store, catalog, include_paused=True)
        reopened = store.last_reopened([i["id"] for i in stale])
        kept = 0
        for inc in stale:
            try:
                again = reopened.get(inc["id"])
                if again is not None and _aware(again) > cutoff:
                    kept += 1
                    continue   # a person reopened it lately: its quiet period starts there
                if not _said_all_clear(store, catalog, inc, settings, roles, again):
                    kept += 1
                    continue
                out = store.set_incident_state(inc["id"], "resolved", None, None, "sweep",
                                               at=now, resolution="itself", expect="open")
            except Exception as e:
                print(f"[incidents] sweep {inc['id']}: {type(e).__name__}: {e}")
                kept += 1
                continue
            if out is not None:
                closed.append(out)
                _changed(store, out, "sweep")
        if len(stale) < SWEEP_PAGE:
            break
        offset += kept   # the closed ones left the open list, the rest are still in it
    return closed


def sweep_hook(store, catalog, changes) -> None:
    """The watch's every-minute hook (watch.EVERY_TICK)."""
    sweep(store, catalog)


# ── notes from before incidents were tracked ─────────────────────────────────
def backfill(store, catalog=None, now=None) -> int:
    """Give every past note an incident, with the join rule live notes follow, in time order.
    Incidents whose last note is older than 7 days are stored resolved as before_tracking, newer
    ones stay open. Runs when the stored rule version is not BACKFILL_VERSION; running it again
    does nothing. Writes the incident tables only and uses no model. Returns the notes filed.

    It runs at the start of a cell, so it reads the runs it needs in a few queries, decides every
    join in memory and writes the result in batches (TR-897): a cell with thousands of findings
    starts in seconds, where one write per note took minutes."""
    from . import sre
    from .config import catalog_from_db, trigger_entity_label
    if store.get_setting(BACKFILL_SETTING) == BACKFILL_VERSION:
        return 0
    catalog = catalog or catalog_from_db(store)
    now = _aware(now or clock())
    store.delete_backfilled_incidents()
    roles = sre.roles(store, catalog, include_paused=True)
    triggers = {t.name: t for t in catalog.triggers}
    # only runs that can be a note: the same first checks as is_note, in SQL
    maybe = ("status = 'ok' AND outcome = 'finding' AND COALESCE(practice, FALSE) = FALSE "
             "AND finding IS NOT NULL AND finding <> ''")
    notes, offset = [], 0
    while True:   # newest first, a page at a time
        page = store.list_agent_runs(None, limit=BACKFILL_PAGE, offset=offset, where_sql=maybe)
        if not page:
            break
        offset += len(page)
        for r in page:
            if r.get("started_at") is not None and is_note(store, catalog, {"name": r["agent"]},
                                                           r, roles):
                notes.append(r)
        if len(page) < BACKFILL_PAGE:
            break
    notes.sort(key=lambda r: (_aware(r["started_at"]), r["id"]))
    filed_already = store.filed_run_ids()
    notes = [r for r in notes if r["id"] not in filed_already]   # filed by a pass that is kept
    known = {"parents": store.agents_of_runs(r["parent_run_id"] for r in notes
                                             if r.get("woken_by") == "handoff"),
             "projects": store.projects_of_runs(r["id"] for r in notes)}
    # incidents a note may join, per service: the kept ones (a live one, a person's), apart,
    # and the ones this pass makes, in the order of their last note
    kept: dict[str, list[dict]] = {}
    for inc in store.kept_open_incidents():
        inc = {**inc, "opened_at": _aware(inc["opened_at"]), "last_note_at": _aware(inc["last_note_at"]),
               "bumped": False}
        kept.setdefault(inc["service"], []).append(inc)
    fresh: dict[str, dict[str, dict]] = {}    # service -> {incident id: incident}, oldest touch first
    made, joined, log, seen = [], [], [], {}
    for r in notes:
        trig = triggers.get(r.get("trigger"))
        label = trigger_entity_label(trig, catalog.sources) if trig is not None else None
        at = _aware(r["started_at"])
        memo = (label, r["key"])
        if memo not in seen:
            seen[memo] = service_of(store, catalog, r["key"], label, now)
        service = seen[memo]
        since = at - timedelta(seconds=cooldown_for(store, catalog, r, known))
        # the newest incident of the service with a note inside the cooldown that opened no later
        # than this note (one that opened after it is a live one, on a rerun: not its incident)
        best = None
        for inc in kept.get(service, ()):
            if inc["last_note_at"] >= since and inc["opened_at"] <= at \
                    and (best is None or inc["last_note_at"] > best["last_note_at"]):
                best = inc
        if fresh.get(service):   # touched last = newest last note, so only that one can win
            newest = next(reversed(fresh[service].values()))
            if newest["last_note_at"] >= since and (
                    best is None or newest["last_note_at"] > best["last_note_at"]):
                best = newest
        if best is not None:
            if best["last_note_at"] < at:
                best["last_note_at"] = at
                best["bumped"] = True
                if best["id"] in fresh.get(service, {}):
                    fresh[service].pop(best["id"])
                    fresh[service][best["id"]] = best      # touched last
            joined.append((r["id"], best["id"], at))
            log.append((best["id"], at, "note_joined", "backfill", r["agent"]))
        else:
            inc = {"id": "inc_" + uuid.uuid4().hex[:10], "service": service,
                   "project": r.get("project"), "state": "open", "opened_at": at,
                   "last_note_at": at, "resolved_at": None, "resolution": None, "bumped": False}
            made.append(inc)
            fresh.setdefault(service, {})[inc["id"]] = inc
            joined.append((r["id"], inc["id"], at))
            log.append((inc["id"], at, "opened", "backfill", r["agent"]))
    for inc in made:    # long quiet ones are from before incidents were tracked
        if inc["last_note_at"] < now - BEFORE_TRACKING_AFTER:
            inc.update(state="resolved", resolved_at=inc["last_note_at"],
                       resolution="before_tracking")
            log.append((inc["id"], inc["last_note_at"], "resolved", "backfill", None))
    bumped = [(i["id"], i["last_note_at"]) for lst in kept.values() for i in lst if i["bumped"]]
    store.write_backfill(made, joined, log, bumped)
    store.set_setting(BACKFILL_SETTING, BACKFILL_VERSION)
    return len(joined)


# ── how an incident reads ────────────────────────────────────────────────────
def _span(a, b) -> str:
    return goal.span_words((_aware(b) - _aware(a)).total_seconds())


def state_words(inc: dict, now=None) -> str:
    """The state in a sentence: "Open for 38 minutes, nobody on it", "Alex has been on it for 12
    minutes", "Resolved by Kim after 2 hours", "Resolved itself after 2 hours"."""
    now = _aware(now or clock())
    if inc["state"] == "on_it":
        who = inc.get("owner_name") or "Someone"
        return f"{who} has been on it for {_span(inc['owner_at'] or inc['opened_at'], now)}"
    if inc["state"] == "resolved":
        how = inc.get("resolution")
        if how == "before_tracking":
            return "Closed: from before incidents were tracked"
        after = _span(inc["opened_at"], inc["resolved_at"] or now)
        if how == "itself":
            return f"Resolved itself after {after}"
        by = inc.get("resolved_by_name")
        return f"Resolved by {by} after {after}" if by else f"Resolved after {after}"
    return f"Open for {_span(inc['opened_at'], now)}, nobody on it"


def title_of(store, first_run: dict | None, inc: dict, settings: dict, now) -> str:
    """goal.title of the incident's first note; the service alone when that note is gone."""
    if not first_run:
        return f"{goal.service_name(inc['service'], settings)}: incident"
    parts = goal.title_parts(first_run, inc["opened_at"], settings)
    parts["service"] = parts["service"] or goal.service_name(inc["service"], settings)
    return goal.title(parts, now)


def view(store, inc: dict, *, notes: list | None = None, settings: dict | None = None,
         now=None) -> dict:
    """The incident as the API shows it."""
    now = _aware(now or clock())
    settings = settings if settings is not None else store.service_settings()
    notes = notes if notes is not None else store.incident_notes(inc["id"])
    first = store.get_agent_run(notes[0]["run_id"]) if notes else None
    owner = inc.get("owner_name") or inc.get("owner")
    resolved = None
    if inc["state"] == "resolved":
        resolved = {"by_name": inc.get("resolved_by_name") or None,
                    "at": goal._iso(inc["resolved_at"]), "how": inc.get("resolution")}
    return {
        "id": inc["id"], "service": inc["service"], "project": inc["project"],
        "state": inc["state"], "opened_at": goal._iso(inc["opened_at"]),
        "last_note_at": goal._iso(inc["last_note_at"]), "notes": len(notes),
        "title": title_of(store, first, inc, settings, now),
        "who": ({"name": owner, "since": goal._iso(inc["owner_at"])}
                if inc["state"] == "on_it" else None),
        "resolved": resolved, "slack": inc.get("slack"),
        "state_words": state_words(inc, now),
        "age_words": goal.age_words((now - _aware(inc["opened_at"])).total_seconds()),
    }


def detail(store, inc: dict, now=None) -> dict:
    """view() plus the notes (newest last) and the log."""
    now = _aware(now or clock())
    notes = store.incident_notes(inc["id"])
    out = view(store, inc, notes=notes, now=now)
    items = []
    for n in notes:
        run = store.get_agent_run(n["run_id"]) or {}
        headline, next_step = goal.headline_and_next(run)
        items.append({
            "run_id": n["run_id"], "at": goal._iso(n["joined_at"]), "headline": headline,
            "summary": goal.summary_of(run.get("finding"), headline), "next_step": next_step,
            "verdict": run.get("verdict"),
            "slack": ({"channel": n["slack_channel"], "ts": n["slack_ts"]}
                      if n["slack_channel"] and n["slack_ts"] else None)})
    out["notes"] = items
    out["log"] = [{"at": goal._iso(e["at"]), "action": e["action"], "by_name": e["by_name"],
                   "via": e["via"]} for e in store.incident_log(inc["id"])]
    return out
