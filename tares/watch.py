"""Whether anybody is watching each service, and telling Slack when that changes (TR-743).

A service is watched while at least one watcher covers it and can run now. A watcher is an agent
woken by a scheduled trigger (tares/sre.py roles() and covers() say who, from the configuration);
whether it can run is answered here from the same facts the runner refuses a run on: the project
is paused, the wake is off, the budget is used (TR-770), the daily limit is reached, no model is
set up, or the schedule has stopped ticking.

`compute` is the answer now. `sync` compares it with the `watch_state` table and keeps the table
current, returning the services that changed. `post` tells Slack about each change once, on a cell
whose edition is sre; every other cell keeps the rows and says nothing. `run` is the daemon loop.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import timedelta, timezone

import httpx

from . import edition as edition_mod
from . import goal, providers, sre
from . import slack as slack_mod
from .builtin_agents import effective_daily_cap
from .config import slack_channel_from_url
from .envelope import now_utc
from .schedule import is_scheduled

SYSTEM_NAME_SETTING = "sre.system_name"
CHANNEL_SETTING = "sre.watch_channel"   # where "no watcher is set up for it" goes
RETRY_SECONDS = 600                     # a post that failed is tried again after this long
SCHEDULE_MISSES = 3                     # ticks a schedule may miss before it counts as stopped

REASONS = ("no_agent", "project_paused", "agent_off", "budget_used", "daily_cap", "no_model",
           "schedule_not_firing")

_SYNC_LOCK = threading.Lock()
_POST_LOCK = threading.Lock()
_ATTEMPTS: dict[str, float] = {}   # service -> when a post for it last failed or was tried
clock = time.monotonic             # the retry clock; a test replaces it instead of sleeping
EVERY_TICK: list = []              # hook(store, catalog, changes), called after each sync


def _aware(dt):
    """A time in UTC, whatever zone the database handed it back in (messages show UTC)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def system_name(store) -> str:
    """What the cell is called in messages: the sre.system_name setting, else the instance name,
    else "your services"."""
    return ((store.get_setting(SYSTEM_NAME_SETTING) or "").strip()
            or os.getenv("TARES_INSTANCE_NAME", "").strip() or "your services")


# ── why a watcher cannot run ─────────────────────────────────────────────────
def _paused_at(store, project, now):
    """When the project was paused: its newest `paused` log entry, else now."""
    for e in store.list_project_log(project, limit=100):
        if e["action"] == "paused":
            return _aware(e["at"])
    return now


def _budget_reached_at(store, agent: dict, now):
    """The start of the run after which the spend over the budget's period first reached the
    budget (summed in Python, oldest first), else now."""
    month = agent.get("budget_period") == "month"
    where, params = ("started_at >= ?", [store.month_start(now)]) if month else ("", [])
    runs = store.list_agent_runs(agent["name"], limit=100000, where_sql=where,
                                 where_params=params)
    total = 0.0
    for r in sorted(runs, key=lambda r: _aware(r["started_at"])):
        total += float(r["cost_usd"] or 0)
        if total >= float(agent["budget_usd"]):
            return _aware(r["started_at"])
    return now


def _cap_reached_at(store, agent_name: str, now):
    """The start of the first run the limit refused in the last 24 hours, else now."""
    runs = store.list_agent_runs(agent_name, limit=1000, status="capped",
                                 where_sql="started_at > ?", where_params=[now - timedelta(days=1)])
    return _aware(min((r["started_at"] for r in runs), key=_aware)) if runs else now


def _schedule_stalled(store, trig, now):
    """When the schedule should next have ticked, if it has missed SCHEDULE_MISSES ticks in a
    row (and the trigger is older than that); else None."""
    every = float(trig.condition.every)
    last = _aware(store.last_fired_any(trig.name))
    if last is None:
        last = _aware(store.trigger_created_at(trig.name))
        if last is None:
            return None
    if (now - last).total_seconds() > SCHEDULE_MISSES * every:
        return last + timedelta(seconds=every)
    return None


def judge(store, catalog, now=None, watchers: list | None = None) -> dict:
    """(agent, project, trigger) -> {reason, since} for each watcher: why it cannot run (the first
    of project_paused, agent_off, budget_used, daily_cap, no_model, schedule_not_firing that
    applies) and since when, or reason None when it can. `watchers` is a roles() list, else all
    of them, paused ones included."""
    t = now or now_utc()
    if watchers is None:
        watchers = sre.roles(store, catalog, include_paused=True)["watchers"]
    agents = {a["name"]: a for a in store.list_catalog_agents()}
    triggers = {x.name: x for x in catalog.triggers}
    rows = store.watch_rows()
    by_agent: dict[str, tuple] = {}   # an agent's own limits, looked at once
    out = {}

    def first_seen(agent, reason):
        # what is not dated by an event is dated by when it was first seen
        seen = [r["since"] for r in rows.values()
                if r["agent"] == agent and r["reason"] == reason and not r["watched"] and r["since"]]
        return _aware(min(seen, key=_aware)) if seen else t

    def limits(name):
        if name not in by_agent:
            a = agents[name]
            res = (None, None)
            budget = a.get("budget_usd")
            if budget and store.agent_budget_spend(a, t) >= float(budget):
                res = ("budget_used", _budget_reached_at(store, a, t))
            else:
                cap, _src = effective_daily_cap(a, store)
                if store.agent_runs_today(name, include_practice=False) >= cap:
                    res = ("daily_cap", _cap_reached_at(store, name, t))
                elif providers.resolve_for_agent(store, a)[0] is None:
                    res = ("no_model", first_seen(name, "no_model"))
            by_agent[name] = res
        return by_agent[name]

    for w in watchers:
        key = (w["agent"], w["project"], w["trigger"])
        if w["agent"] not in agents:
            continue
        if w["paused"]:
            out[key] = {"reason": "project_paused", "since": _paused_at(store, w["project"], t)}
        elif not w["enabled"] or w["trigger_paused"]:
            out[key] = {"reason": "agent_off", "since": first_seen(w["agent"], "agent_off")}
        else:
            reason, since = limits(w["agent"])
            if reason is None:
                trig = triggers.get(w["trigger"])
                stalled = _schedule_stalled(store, trig, t) if trig and is_scheduled(trig) else None
                if stalled is not None:
                    reason, since = "schedule_not_firing", stalled
            out[key] = {"reason": reason, "since": since}
    return out


def compute(store, catalog, now=None) -> dict[str, dict]:
    """service -> {watched, reason, since, agent, project} as things stand. Hidden services are
    left out. A watched service names the watcher that covers it (the first); an unwatched one
    names the watcher that stopped last, or has no watcher and the reason no_agent."""
    t = now or now_utc()
    rows = store.watch_rows()
    found = sre.roles(store, catalog, include_paused=True)["watchers"]
    why = judge(store, catalog, t, found)
    out = {}
    for item in sre.list_services(store, catalog, now):
        name = item["name"]
        prev = rows.get(name)
        cover = [w for w in sre.covers(store, catalog, item, found)
                 if (w["agent"], w["project"], w["trigger"]) in why]
        running = [w for w in cover if why[(w["agent"], w["project"], w["trigger"])]["reason"] is None]
        if running:
            w = running[0]
            since = _aware(prev["since"]) if prev and prev["watched"] and prev["since"] else t
            out[name] = {"watched": True, "reason": None, "since": since,
                         "agent": w["agent"], "project": w["project"]}
        elif cover:
            w = max(cover, key=lambda w: _aware(why[(w["agent"], w["project"], w["trigger"])]["since"]))
            j = why[(w["agent"], w["project"], w["trigger"])]
            out[name] = {"watched": False, "reason": j["reason"], "since": j["since"],
                         "agent": w["agent"], "project": w["project"]}
        else:
            since = (_aware(prev["since"]) if prev and not prev["watched"]
                     and prev["reason"] == "no_agent" and prev["since"] else t)
            out[name] = {"watched": False, "reason": "no_agent", "since": since,
                         "agent": None, "project": None}
    return out


# ── the words ────────────────────────────────────────────────────────────────
def _clock_words(when, now) -> str:
    from .store import day_month_words
    when = _aware(when)
    hm = when.strftime("%H:%M")
    return hm if when.date() == now.date() else f"{day_month_words(when)}, {hm}"


def reason_words(reason: str, agent_row: dict | None, store, now=None, trigger: str | None = None) -> str:
    """Why nobody is watching, in plain words: no agent, trigger or run in it."""
    from .store import Store, day_month_words, month_name
    t = now or now_utc()
    a = agent_row or {}
    if reason == "budget_used":
        budget = f"${float(a.get('budget_usd') or 0):.2f}"
        if a.get("budget_period") == "month":
            return (f"the watcher used its {budget} budget for {month_name(t)}; it starts again "
                    f"on {day_month_words(Store.budget_resets_on(a, t))}")
        return f"the watcher used its {budget} budget"
    if reason == "daily_cap":
        cap, _src = effective_daily_cap(a, store)
        text = f"the watcher reached its limit of {cap} checks a day"
        frees = store.agent_cap_frees_at(a.get("name", ""), cap, include_practice=False)
        return text + (f"; it starts again in {goal.span_words((_aware(frees) - t).total_seconds())}"
                       if frees else "")
    if reason == "agent_off":
        return "the watcher is turned off"
    if reason == "project_paused":
        return "watching is paused"
    if reason == "schedule_not_firing":
        last = store.last_fired_any(trigger or a.get("trigger") or "")
        return ("the scheduled check has not happened since " + _clock_words(last, t) if last
                else "the scheduled check has not happened yet")
    if reason == "no_model":
        return "no AI model is set up"
    return "no watcher is set up for it"


# ── keeping the table current ────────────────────────────────────────────────
def sync(store, catalog, now=None) -> list[dict]:
    """Bring `watch_state` up to date and return what changed: one {service, watched, reason,
    since, agent, project} for each service whose watched or reason changed. A service seen for
    the first time is a change only when nobody is watching it. A service that is gone has its
    row removed, with nothing said. A row nobody needs to hear about (a service first seen
    watched, or watched again when its stop was never announced) is written as told."""
    with _SYNC_LOCK:
        t = now or now_utc()
        state = compute(store, catalog, now)
        rows = store.watch_rows()
        changes = []
        for name, st in state.items():
            prev = rows.get(name)
            if prev is not None and (prev["watched"], prev["reason"]) == (st["watched"], st["reason"]):
                if (prev["agent"], prev["project"]) != (st["agent"], st["project"]):
                    store.set_watch_row(name, agent=st["agent"], project=st["project"])
                continue
            quiet = st["watched"] and (prev is None or prev["told_at"] is None)
            store.set_watch_row(name, watched=st["watched"], reason=st["reason"], since=st["since"],
                                agent=st["agent"], project=st["project"],
                                told_at=t if quiet else None, told_where=None)
            _ATTEMPTS.pop(name, None)
            if not quiet:
                changes.append({"service": name, **st})
        store.delete_watch_rows([s for s in rows if s not in state])
        return changes


# ── telling Slack ────────────────────────────────────────────────────────────
def channel_for(store, catalog, agent: str | None, project: str | None) -> str | None:
    """The Slack channel a message about this watcher's project goes to: the investigator's
    channel, else the watcher's own, else the first channel the project is subscribed to. For a
    service nobody watches (no project) the sre.watch_channel setting, else None."""
    if not project:
        return (store.get_setting(CHANNEL_SETTING) or "").strip().lstrip("#") or None
    named = {a["name"]: (a.get("slack_channel") or "").strip() for a in store.list_catalog_agents()}
    for i in sre.roles(store, catalog, include_paused=True)["investigators"]:
        if i["project"] == project and named.get(i["agent"]):
            return named[i["agent"]].lstrip("#")
    if agent and named.get(agent):
        return named[agent].lstrip("#")
    for s in store.list_project_subscriptions(project):
        chan = slack_channel_from_url(s.get("url") or "")
        if chan:
            return chan
    return None


def _send(store, channel: str, message: dict) -> bool:
    """One attempt at chat.postMessage; the verdict is slack.classify's, a failure is one line."""
    token, _origin = slack_mod.resolve_token(store)
    if not token:
        print("taresd: watch: no Slack bot token is set, so nobody was told", flush=True)
        return False
    try:
        r = httpx.post(f"{slack_mod.API_BASE}/chat.postMessage", json={"channel": channel, **message},
                       headers={"authorization": f"Bearer {token}"}, timeout=15)
        try:
            data = r.json()
        except Exception:
            data = None
        ok, error, _retry = slack_mod.classify(r.status_code, data)
    except Exception as e:   # a notification failing must never stop the loop
        ok, error = False, f"{type(e).__name__}: {e}"
    if not ok:
        print(f"taresd: watch: could not tell {channel}: {error}", flush=True)
    return ok


def post(store, catalog, changes: list[dict] | None = None, now=None) -> list[dict]:
    """Tell Slack, one message per channel, about the rows nobody has been told about: a service
    nobody watches, or one watched again after a stop that was announced. Only a cell whose
    edition is sre posts; it returns at once on any other. A post that fails leaves the row
    untold and is tried again on a later call, at most once per RETRY_SECONDS per service, which
    is also how a cell switched to sre later hears about a stop that was already there. Returns
    what was posted: [{channel, stopped, started}]. `changes` (from sync) is not needed: the
    rows say what is left to tell, so two callers cannot both post the same one."""
    if edition_mod.edition(store)[0] != "sre":
        return []
    t = now or now_utc()
    posted = []
    with _POST_LOCK:
        rows = store.watch_rows()
        agents = {a["name"]: a for a in store.list_catalog_agents()}
        settings = store.service_settings()
        groups: dict[str, dict] = {}
        for name, r in sorted(rows.items()):
            if r["told_at"] is not None:
                continue
            channel = channel_for(store, catalog, r["agent"], r["project"])
            if not channel:
                continue
            last = _ATTEMPTS.get(name)
            if last is not None and clock() - last < RETRY_SECONDS:
                continue
            entry = {"service": name, "display": (settings.get(name) or {}).get("display") or name,
                     "since": _aware(r["since"]) or t,
                     "reason_words": reason_words(r["reason"], agents.get(r["agent"]), store, t)
                     if not r["watched"] else ""}
            groups.setdefault(channel, {"stopped": [], "started": []})[
                "started" if r["watched"] else "stopped"].append(entry)
        for channel, g in groups.items():
            message = slack_mod.build_watch_message(g["stopped"], g["started"], system_name(store))
            names = [e["service"] for e in g["stopped"] + g["started"]]
            for n in names:
                _ATTEMPTS[n] = clock()
            if _send(store, channel, message):
                done = store.mark_told(names, t, channel)
                for n in done:
                    _ATTEMPTS.pop(n, None)
                posted.append({"channel": channel, "stopped": [e["service"] for e in g["stopped"]],
                               "started": [e["service"] for e in g["started"]]})
    return posted


# ── the screen ───────────────────────────────────────────────────────────────
def _every_words(trig) -> str | None:
    if trig is None or not is_scheduled(trig):
        return None
    words = goal.duration_words(trig.condition.every)
    return "every " + (words[2:] if words.startswith("1 ") else words)


def _money(v: float) -> str:
    return f"${v:.0f}" if float(v).is_integer() else f"${v:.2f}"


def _watcher_block(store, w: dict, j: dict, trig, agent: dict, now) -> dict:
    runs = store.list_agent_runs(w["agent"], limit=1, status="ok")
    last = runs[0] if runs else None
    recent = store.list_agent_runs(w["agent"], limit=1000, status="ok", where_sql="started_at >= ?",
                                   where_params=[now - timedelta(days=7)])
    costs = [float(r["cost_usd"]) for r in recent if r["cost_usd"] is not None]
    budget = agent.get("budget_usd")
    month = agent.get("budget_period") == "month"
    spent = store.agent_budget_spend(agent, now)
    resets = store.budget_resets_on(agent, now)
    if last is None:
        check = None
    elif last.get("outcome") == "no_op":
        check = "nothing to report"
    else:
        check = goal.headline_and_next(last)[0]
    spent_words = (f"Spent ${spent:.2f}" + (f" of {_money(float(budget))}" if budget else "")
                   + (" this month" if month else ""))
    return {"agent": w["agent"], "project": w["project"],
            "state": "running" if j["reason"] is None else "stopped",
            "reason": j["reason"],
            "reason_words": reason_words(j["reason"], agent, store, now, w["trigger"])
            if j["reason"] else None,
            "since": sre._iso(j["since"]) if j["reason"] else None,
            "every_words": _every_words(trig),
            "last_check_at": sre._iso(last["started_at"]) if last else None,
            "last_check_words": check,
            "cost_per_check_usd": round(sum(costs) / len(costs), 4) if costs else None,
            "budget_usd": float(budget) if budget else None,
            "budget_period": "month" if month else "lifetime",
            "spent_usd": round(spent, 4), "spent_words": spent_words,
            "resets_on": resets.isoformat() if resets else None}


def view(store, catalog, now=None) -> dict:
    """The body of GET /api/watch: the stored state of every service and a fresh look at each
    watcher."""
    t = now or now_utc()
    rows = store.watch_rows()
    settings = store.service_settings()
    agents = {a["name"]: a for a in store.list_catalog_agents()}
    services = []
    for name in sorted(rows, key=lambda n: (rows[n]["watched"], n)):
        r = rows[name]
        since = _aware(r["since"])
        services.append({
            "service": name, "display": (settings.get(name) or {}).get("display") or name,
            "watched": r["watched"], "reason": r["reason"],
            "reason_words": reason_words(r["reason"], agents.get(r["agent"]), store, t)
            if r["reason"] else None,
            "since": sre._iso(since),
            "for_words": ("for " + goal.span_words((t - since).total_seconds())) if since else None,
            "told": {"at": sre._iso(r["told_at"]), "where": r["told_where"]}
            if r["told_at"] and r["told_where"] else None})
    found = sre.roles(store, catalog, include_paused=True)["watchers"]
    why = judge(store, catalog, t, found)
    triggers = {x.name: x for x in catalog.triggers}
    watchers, seen = [], set()
    for w in found:
        key = (w["agent"], w["project"])
        if key in seen or (w["agent"], w["project"], w["trigger"]) not in why:
            continue
        seen.add(key)
        watchers.append(_watcher_block(store, w, why[(w["agent"], w["project"], w["trigger"])],
                                       triggers.get(w["trigger"]), agents[w["agent"]], t))
    flags = [s["watched"] for s in services]
    watching = ("nothing" if not flags else "all" if all(flags) else "none" if not any(flags)
                else "some")
    return {"watching": watching, "system_name": system_name(store), "services": services,
            "watchers": watchers}


def service_state(store, rows: dict, name: str, agents: dict, now) -> dict:
    """The `watch` entry of one item of GET /api/services."""
    r = rows.get(name)
    if r is None:
        return {"watched": None, "reason": None, "reason_words": None, "since": None}
    return {"watched": r["watched"], "reason": r["reason"],
            "reason_words": reason_words(r["reason"], agents.get(r["agent"]), store, now)
            if r["reason"] else None,
            "since": sre._iso(_aware(r["since"]))}


# ── the loop ─────────────────────────────────────────────────────────────────
def _tick(store, catalog) -> None:
    changes = sync(store, catalog)
    for hook in list(EVERY_TICK):
        try:
            hook(store, catalog, changes)
        except Exception as e:
            print(f"taresd: watch hook failed: {type(e).__name__}: {e}", flush=True)
    post(store, catalog, changes)


async def run(runtime, stop: asyncio.Event) -> None:
    """The daemon's watch: every TARES_WATCH_SECONDS (60) look at who is watching, keep the table
    current and tell Slack what changed. It reads the live catalog each pass and never stops on
    an error."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(_tick, runtime.store, runtime.catalog)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"taresd: watch failed: {type(e).__name__}: {e}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=float(os.getenv("TARES_WATCH_SECONDS", "60")))
        except asyncio.TimeoutError:
            pass
