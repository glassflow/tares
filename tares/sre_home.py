"""GET /api/home on an AI SRE cell (TR-748): everything the home page shows, in one read.

build() reads what the cell already knows (services, the watch state, incidents, the findings
list, the agents' spend) and words it. It adds no state of its own. service_row() is the one place
that decides a service's state, so the services page and this page cannot disagree.

cached() keeps a body for CACHE_SECONDS per credential, so a page open in ten tabs reads the
database once. Anything that changes the answer on purpose (the raise_budget action) calls
clear_cache() first.
"""
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from . import feedback, goal, incidents, providers, sre, start_steps, watch
from . import slack as slack_mod
from .envelope import now_utc
from .store import day_month_words

CACHE_SECONDS = 5
SERIES_SECONDS = 30          # the 24 hour error series, the dearest read of the page, as long as the services
SPARK_POINTS = 24            # the sparkline of a service: its errors over 24 hours, one point an hour
UNUSUAL_BUCKET_S = 600       # "unusual" compares the last 10 minutes with the 143 before them
UNUSUAL_TIMES = 3
UNUSUAL_MIN = 5
INCIDENTS_SHOWN = 3
RAISED_SHOWN = 5
RAISE_STEP_USD = 25
CHART_LABEL = "Errors, 2 hours around when it opened"
RAISED_OFF_NOTE = "The checks before the watcher stopped all came back quiet."
NOTES_PLACE = {"connect": "/settings#tools", "link_repo": "/settings#tools",
               "service": "/services", "rule": "/settings#watching"}
_SIGNAL_WORDS = {"logs": "logs", "traces": "traces", "metrics": "metrics", "deploys": "deploys",
                 "alerts": "alerts", "events": "events"}
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")

_LOCK = threading.Lock()
_CACHE: dict = {}
_GEN = [0]      # bumped by clear_cache, so a read that was building then does not store
_SERIES: dict = {}


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _SERIES.clear()
        _GEN[0] += 1


def _errors_24h(store, names: list, t, now) -> dict:
    """Errors per 10 minutes over the 24 hours before `t` for each service, one scan. On the real
    clock (`now` None) it is kept for SERIES_SECONDS, like the services in tares/sre.py, because
    the scan is most of what a build costs; a call with a `now` of its own always scans."""
    if not names:
        return {}
    key = (id(store), tuple(names))
    if now is None:
        with _LOCK:
            hit = _SERIES.get(key)
            gen = _GEN[0]
        if hit and time.monotonic() - hit[0] < SERIES_SECONDS:
            return hit[1]
    out = store.event_series_many(names, t - timedelta(hours=24), t, UNUSUAL_BUCKET_S,
                                  errors_only=True)
    if now is None:
        with _LOCK:
            if gen == _GEN[0]:
                _SERIES[key] = (time.monotonic(), out)
    return out


def cached(store, catalog, health, by, start_who=None) -> dict:
    """build(), kept for CACHE_SECONDS per store and credential. `health` may be the
    runtime's health_snapshot (a function), which a cache hit never calls."""
    key = (id(store), by or "open", start_who or "")
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        gen = _GEN[0]
    body = build(store, catalog, health() if callable(health) else health, person=by or None,
                 start_who=start_who)
    with _LOCK:
        # a clear_cache() that landed while this was building means the body may be stale
        if gen == _GEN[0]:
            _CACHE[key] = (time.monotonic(), body)
    return body


# ── small words ──────────────────────────────────────────────────────────────
def _up(text: str) -> str:
    return text[:1].upper() + text[1:]


def _nd(text):
    return goal._no_dash(text) if text else text


def _plural(n: int, word: str) -> str:
    return goal._plural(n, word)


def _utc(when):
    """A time in UTC from a datetime or the ISO string a watcher block holds."""
    return goal._aware(when).astimezone(timezone.utc) if when else None


def _since_phrase(when, now, clock: bool = True) -> str:
    """When something began: "14:09" today, "yesterday, 00:05", "27 Oct, 00:05". Without the
    clock a day before today is only the day: "yesterday", "27 Oct"."""
    when = goal._aware(when).astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    hm = when.strftime("%H:%M")
    if when.date() == now.date():
        return hm
    day = "yesterday" if when.date() == (now - timedelta(days=1)).date() else day_month_words(when)
    return f"{day}, {hm}" if clock else day


def _when_words(at, now) -> str:
    """"14:09" for something today, "29 Sep" for an earlier day."""
    at = goal._aware(at).astimezone(timezone.utc)
    return at.strftime("%H:%M") if at.date() == now.astimezone(timezone.utc).date() \
        else day_month_words(at)


def _channel_words(channel) -> str | None:
    ch = str(channel or "").strip().lstrip("#")
    if not ch:
        return None
    return "a Slack channel" if _CHANNEL_ID.match(ch) else f"#{ch}"


def _signals_words(item: dict) -> str:
    if item.get("seen_via") and not item.get("signals"):
        return "status codes only"
    kinds = [_SIGNAL_WORDS.get(k, k) for k in item.get("signals") or []]
    return goal.join_words(kinds) if kinds else "no signals yet"


def _model_words(store, agent: dict) -> str:
    model = agent.get("model") or providers.default_model_for(store, agent.get("provider") or None)
    low = (model or "").lower()
    for family in ("haiku", "sonnet", "opus"):
        if family in low:
            return family.capitalize()
    return model or "No model set up"


def _spent_words(store, agent: dict, now) -> str:
    """"Spent $4.20 of $25 this month": the same sentence the watch block gives."""
    spent = store.agent_budget_spend(agent, now)
    budget = agent.get("budget_usd")
    return (f"Spent ${spent:.2f}" + (f" of {watch._money(float(budget))}" if budget else "")
            + (" this month" if agent.get("budget_period") == "month" else ""))


# ── one service ──────────────────────────────────────────────────────────────
def _numbers(series) -> list:
    return [(p[1] if isinstance(p, (list, tuple)) else p) for p in series or []]


def _spark(errors_series) -> list:
    """The 10-minute error counts of 24 hours as one count an hour, so a service costs one query."""
    values = _numbers(errors_series)
    step = max(1, len(values) // SPARK_POINTS)
    return [sum(values[i:i + step]) for i in range(0, len(values), step)]


def _is_unusual(errors_series) -> bool:
    """The last 10 minutes hold at least 5 errors and at least 3 times the median of the
    10-minute buckets before them."""
    values = _numbers(errors_series)
    if len(values) < 2:
        return False
    last, rest = values[-1], values[:-1]
    return last >= UNUSUAL_MIN and last >= UNUSUAL_TIMES * statistics.median(rest)


def service_row(item, watch_row, blind_spots, incidents, errors_series, spark, now) -> dict:
    """One line of the services list. `state`, first match wins: incident (one open or being
    worked on), not_watched, silent, no_own_signal, unusual, normal. `watch_row` is the service's
    row of the watch state (plus last_check_at), None for a service nobody could watch because
    it has no events of its own. `blind_spots` and `incidents` may hold other services' entries;
    this reads its own. `errors_series` is the last 24 hours in 10-minute buckets, `spark` the
    same in 24, one an hour (`_spark` makes it from the first)."""
    name = item["name"]
    spots = [s for s in blind_spots or [] if s.get("service") == name]
    mine = [i for i in incidents or []
            if i.get("service") == name and i.get("state") in ("open", "on_it")]
    kinds = {s["kind"] for s in spots}
    silent = next((s for s in spots if s["kind"] == "silent"), None)
    state = words = None
    if mine:
        state = "incident"
        on_it = next((i for i in mine if i["state"] == "on_it"), None)
        words = (f"Incident, {(on_it.get('who') or {}).get('name') or 'someone'} is on it"
                 if on_it and not any(i["state"] == "open" for i in mine) else "Incident open")
    elif watch_row is not None and not watch_row.get("watched"):
        state = "not_watched"
        since = watch_row.get("since")
        words = "Not watched" if not since else \
            "Not watched since " + _since_phrase(since, now, clock=False)
    elif silent is not None:
        state = "silent"
        since = goal._aware(silent["since"]) if silent.get("since") else None
        words = "Sent nothing for " + goal.span_words((now - since).total_seconds()) \
            if since else "Sent nothing lately"
    elif "seen_via_other" in kinds or item.get("seen_via"):
        state = "no_own_signal"
        words = "Seen only through " + goal.join_words(item.get("seen_via") or ["another service"])
    elif _is_unusual(errors_series):
        state, words = "unusual", "More errors than usual"
    else:
        state, words = "normal", "Normal"
    checked = (watch_row or {}).get("last_check_at")
    if watch_row is None:
        last = "not checked, it has no events of its own"
    elif checked:
        last = "checked " + goal.age_words((now - goal._aware(checked)).total_seconds())
    else:
        last = "not checked yet"
    display = item.get("display")
    return {"name": name, "about": display if display and display != name else None,
            "state": state, "state_words": words, "signals_words": _signals_words(item),
            "last_words": last, "spark": _numbers(spark)}


# ── the pieces of the page ───────────────────────────────────────────────────
def _watchers(store, catalog, roles, why, t) -> list:
    """[(watcher entry of roles(), its watch block)], one per agent and project."""
    agents = {a["name"]: a for a in store.list_catalog_agents()}
    triggers = {x.name: x for x in catalog.triggers}
    out, seen = [], set()
    for w in roles["watchers"]:
        key = (w["agent"], w["project"])
        jk = (w["agent"], w["project"], w["trigger"])
        if key in seen or jk not in why or w["agent"] not in agents:
            continue
        seen.add(key)
        out.append((w, watch._watcher_block(store, w, why[jk], triggers.get(w["trigger"]),
                                            agents[w["agent"]], t)))
    return out


def _watch_part(store, catalog, state, rows, pairs, t) -> tuple:
    """(the "watch" object, when it stopped, the watcher it follows) for the headline and the page."""
    unwatched = [(n, r) for n, r in state.items() if not r["watched"]]
    on = any(r["watched"] for r in state.values()) if state else \
        any(b["state"] == "running" for _w, b in pairs)
    triggers = {x.name: x for x in catalog.triggers}
    reason = since = agent = project = None
    if not on:
        pick = next(((n, r) for n, r in unwatched if r.get("agent")), unwatched[0] if unwatched else None)
        if pick:
            reason, since, agent, project = (pick[1]["reason"], pick[1]["since"],
                                             pick[1]["agent"], pick[1]["project"])
        else:
            stopped = next(((w, b) for w, b in pairs if b["state"] == "stopped"), None)
            if stopped:
                reason, since = stopped[1]["reason"], _utc(stopped[1]["since"])
                agent, project = stopped[0]["agent"], stopped[0]["project"]
            else:
                reason = "no_agent"
    main = next(((w, b) for w, b in pairs if (w["agent"], w["project"]) == (agent, project)), None)
    if main is None:
        main = next(((w, b) for w, b in pairs if b["state"] == "running"), pairs[0] if pairs else None)
    block = main[1] if main else None
    agent_row = next((a for a in store.list_catalog_agents() if block and a["name"] == block["agent"]), None)
    trig = triggers.get(main[0]["trigger"]) if main else None
    out = {"on": on, "reason": reason, "reason_words": None, "since": None, "off_for_words": None,
           "missed_checks": None, "every_words": block["every_words"] if block else None,
           "last_check": None, "cost_per_check_usd": block["cost_per_check_usd"] if block else None,
           "spent_words": block["spent_words"] if block and block["budget_usd"] else None,
           "resets_on": day_month_words(_utc(block["resets_on"])) if block and block["resets_on"] else None, "told": None, "ways_out": []}
    if block and block["last_check_at"]:
        out["last_check"] = {"at": block["last_check_at"], "said": block["last_check_words"]}
    if not on:
        out["reason_words"] = watch.reason_words(reason, agent_row, store, t,
                                                 main[0]["trigger"] if main else None)
        if since is not None:
            since = _utc(since)
            out["since"] = sre._iso(since)
            out["off_for_words"] = goal.span_words((t - since).total_seconds())
            every = getattr(getattr(trig, "condition", None), "every", None) if trig else None
            if every:
                out["missed_checks"] = max(0, int((t - since).total_seconds() // float(every)))
        told = next((rows[n] for n, _r in unwatched
                     if n in rows and rows[n].get("told_at") and rows[n].get("told_where")), None)
        if told:
            out["told"] = {"where": _channel_words(told["told_where"]) or "a Slack channel",
                           "at": sre._iso(_utc(told["told_at"]))}
        if reason == "budget_used":
            out["ways_out"] = [{"action": "raise_budget",
                                "label": f"Add {watch._money(RAISE_STEP_USD)} and restart",
                                "amount_usd": RAISE_STEP_USD, "primary": True}]
    return out, since, block


def _raised(store, catalog, person, on, t) -> tuple:
    scheduled = {x.name for x in catalog.triggers if getattr(x.condition, "every", None)}
    worth = goal.cell_results(store, scheduled, limit=RAISED_SHOWN, show="worth", now=t)["findings"]
    feedback.with_answers(store, worth, person)
    newest = goal.cell_results(store, scheduled, limit=1, show="all", now=t)["findings"]
    items = [{"id": r["id"], "at": sre._iso(_utc(r["at"])), "when_words": _when_words(r["at"], t),
              "service": r["title_parts"]["service"], "what": r["title_parts"]["what"],
              "outcome_words": r["outcome_words"], "worth": (r.get("worth") or {}).get("mine"),
              "can_answer": r["can_answer"]} for r in worth]
    note = None
    if newest and newest[0]["outcome"] == "quiet":
        note = RAISED_OFF_NOTE if not on else \
            ("Every check since then came back quiet." if items else "Every check so far came back quiet.")
    return items, note


def _today(store, agents_watch: list, agents_inv: list, t) -> dict:
    midnight = goal.utc_midnight(t)

    def runs(agent):
        return [r for r in store.list_agent_runs(agent, limit=1000, status="ok",
                                                 where_sql="started_at >= ?", where_params=[midnight])
                if not r.get("practice")]
    checks = [r for a in agents_watch for r in runs(a)]
    inv = [r for a in agents_inv for r in runs(a)]
    spent = sum(float(r["cost_usd"] or 0) for r in checks + inv)
    return {"checks": len(checks), "quiet": sum(1 for r in checks if r.get("outcome") == "no_op"),
            "investigated": len(inv), "spent_usd": round(spent, 4)}


def _deploy_label(d: dict) -> str:
    text = _nd(d.get("text") or "") or d.get("kind") or "deploy"
    repo = d.get("repo") or ""
    if d.get("kind") == "push" and text.lower().startswith("push to ") and repo:
        return f"{repo} pushed to {text[8:]}"
    if d.get("kind") == "merge":
        return "Merged " + (text[6:] if text.lower().startswith("merge ") else text)
    return text


def _incident_out(store, catalog, inc, notes, settings, items_by_name, t) -> dict:
    v = incidents.view(store, inc, notes=notes, settings=settings, now=t)
    opened = goal._aware(inc["opened_at"])
    run = store.get_agent_run(notes[-1]["run_id"]) if notes else None
    failing = cause = next_step = None
    if run:
        parts = goal.derive_parts(run.get("finding"))
        failing = _nd(run.get("failing") or parts["failing"])
        cause = _nd(run.get("likely_cause") or parts["likely_cause"])
        next_step = goal.headline_and_next(run)[1] or _nd(parts["next_step"])
    service = inc["service"]
    window = (opened - timedelta(hours=1), opened + timedelta(hours=1))
    series = store.event_series(service, window[0], window[1], 120, errors_only=True)
    deploys = [d for d in sre.deploys(store, catalog, service, window[0])
               if goal._aware(d["at"]) <= window[1]]
    chart = None
    if any(n for _a, n in series) or deploys:
        chart = {"label": CHART_LABEL, "points": [[a, n] for a, n in series],
                 "deploys": [{"at": d["at"], "label": _deploy_label(d)} for d in deploys]}
    posted = None
    slack = inc.get("slack")
    if slack:
        try:
            at = datetime.fromtimestamp(float(slack.get("ts") or 0), timezone.utc)
        except (TypeError, ValueError, OSError):
            at = opened
        posted = {"where": _channel_words(slack.get("channel")) or "a Slack channel",
                  "at": sre._iso(at if slack.get("ts") else opened)}
    signals = [_SIGNAL_WORDS.get(k, k) for k in (items_by_name.get(service) or {}).get("signals") or []
               if k != "deploys"]
    if deploys:
        signals.append("the latest deploy")
    who = v["who"]
    return {"id": inc["id"], "title": v["title"], "service": service, "state": inc["state"],
            "opened_at": v["opened_at"],
            "age_words": "open " + goal.span_words((t - opened).total_seconds()),
            "who": who, "who_words": f"{who['name']} is on it" if who else "nobody has picked it up",
            "failing": failing, "likely_cause": cause, "next_step": next_step,
            "chart": chart, "posted": posted, "based_on": goal.join_words(signals) if signals else None}


def _on_watch(store, catalog, roles, pairs, why, t) -> list:
    out = []
    agents = {a["name"]: a for a in store.list_catalog_agents()}
    triggers = {x.name: x for x in catalog.triggers}
    for w, b in pairs:
        agent = agents[w["agent"]]
        trig = triggers.get(w["trigger"])
        every = b["every_words"]
        doing = (_up(b["reason_words"]) if b["state"] == "stopped" and b["reason_words"] else
                 f"Checks {goal.join_words(list(getattr(trig, 'sources', []) or []))}"
                 + (f" {every}" if every else ""))
        out.append({"role": "watcher", "name": _up(goal.agent_words(w["agent"])),
                    "state": b["state"], "doing": doing, "model_words": _model_words(store, agent),
                    "cost_words": f"{_model_words(store, agent)}, {b['spent_words']}"})
    seen = set()
    for i in roles["investigators"]:
        agent = agents.get(i["agent"])
        if agent is None or i["agent"] in seen:
            continue
        seen.add(i["agent"])
        budget = agent.get("budget_usd")
        if budget and store.agent_budget_spend(agent, t) >= float(budget):
            state, doing = "stopped", _up(watch.reason_words("budget_used", agent, store, t))
        else:
            busy = store.list_agent_runs(i["agent"], limit=1, status="running")
            state = "running" if busy else "waiting"
            doing = "Looks into what the watcher raises"
        out.append({"role": "investigator", "name": _up(goal.agent_words(i["agent"])),
                    "state": state, "doing": doing, "model_words": _model_words(store, agent),
                    "cost_words": f"{_model_words(store, agent)}, {_spent_words(store, agent, t)}"})
    return out


def _tools(store, catalog, items, health, t) -> list:
    names = [s.name for s in catalog.sources.values()]
    got = goal._health_for(health or {}, store, names)
    state = {s.name: goal.source_state(store, s, got.get(s.name), t)["state"]
             for s in catalog.sources.values()}
    feeding = {f["name"] for i in items for f in i.get("feeds") or []}
    code = [s for s in catalog.sources.values() if s.connector in ("github", "github_app")]
    return [{"name": "Traces and logs", "ok": any(state.get(n) == "receiving" for n in feeding)},
            {"name": "Code changes", "ok": any(state[s.name] not in ("error", "paused") for s in code)},
            {"name": "Slack", "ok": bool(slack_mod.resolve_token(store)[0])}]


def _feeds_words(items) -> str:
    names = sorted({f["name"] for i in items for f in i.get("feeds") or []})
    if not names:
        return ""
    return f"from the {goal.join_words(names)} " + ("feeds" if len(names) > 1 else "feed")


# ── the Start checklist (TR-765) ─────────────────────────────────────────────
DEMO_TEMPLATE = "ai_sre_demo"


def _start_ctx(store, items, state, roles) -> start_steps.SreStartCtx:
    """What the checklist needs, from the store alone."""
    demo = next((p for p in store.list_projects() if p.get("template") == DEMO_TEMPLATE), None)
    agents = {a["name"]: a for a in store.list_catalog_agents()}
    has_channel = [i for i in roles["investigators"]
                   if (agents.get(i["agent"]) or {}).get("slack_channel")]
    demo_channel = demo is not None and any(i["project"] == demo["id"] for i in has_channel)
    return start_steps.SreStartCtx(
        model=providers.default_id(store) is not None,
        signals=any(c.get("connector") in start_steps.SRE_SIGNAL_CONNECTORS
                    for c in store.list_catalog_sources()),
        services=bool(items),
        watching=any(r["watched"] for r in state.values()),
        slack=demo_channel or (bool(has_channel) and bool(slack_mod.resolve_token(store)[0])),
        demo=demo is not None,
        demo_finding=demo is not None and bool(store.project_findings(demo["id"], limit=1)))


def start_block(store, catalog, who: str, now=None, items=None, state=None, roles=None) -> dict:
    """The checklist block of this person's home: the steps, the counts and whether to show it."""
    items = items if items is not None else sre.list_services(store, catalog, now)
    state = state if state is not None else watch.compute(store, catalog, now)
    roles = roles if roles is not None else sre.roles(store, catalog, include_paused=True)
    return start_steps.checklist(
        store, start_steps.sre_steps(_start_ctx(store, items, state, roles)), who)


# ── the page ─────────────────────────────────────────────────────────────────
def build(store, catalog, health=None, now=None, person=None, start_who=None) -> dict:
    t = now or now_utc()
    settings = store.service_settings()
    system = watch.system_name(store)
    items = sre.list_services(store, catalog, now)
    by_name = {i["name"]: i for i in items}
    rows = store.watch_rows()
    state = watch.compute(store, catalog, now)
    roles = sre.roles(store, catalog, include_paused=True)
    why = watch.judge(store, catalog, t, roles["watchers"])
    pairs = _watchers(store, catalog, roles, why, t)
    spots = sre.blind_spots(store, catalog, now)

    watch_out, off_since, main_block = _watch_part(store, catalog, state, rows, pairs, t)
    on = watch_out["on"]
    last_by_agent = {b["agent"]: b["last_check_at"] for _w, b in pairs}

    # incidents
    active = store.list_incidents("active", limit=50)
    counts = store.incident_counts()
    total = counts["open"] + counts["on_it"]
    shown = active[:INCIDENTS_SHOWN]
    notes = store.notes_of([i["id"] for i in active])
    inc_views = [{"service": i["service"], "state": i["state"],
                  "who": ({"name": i.get("owner_name")} if i["state"] == "on_it" else None)}
                 for i in active]
    incidents_out = [_incident_out(store, catalog, i, notes[i["id"]], settings, by_name, t)
                     for i in shown]

    # services
    names = [i["name"] for i in items]
    errors = _errors_24h(store, names, t, now)
    services = []
    for item in items:
        row = state.get(item["name"])
        if row is not None:
            row = {**row, "last_check_at": last_by_agent.get(row.get("agent"))}
        services.append(service_row(item, row, spots, inc_views, errors.get(item["name"]),
                                    _spark(errors.get(item["name"])), t))

    # the headline
    system_text = system
    n_services = len(items)
    inc_services = {i["service"] for i in active}
    others = [s for s in services if s["name"] not in inc_services]
    normal = sum(1 for s in others if s["state"] == "normal")
    last = watch_out["last_check"]
    checked = ("Checked " + (watch_out["every_words"] or "regularly")
               + (f", last {goal.age_words((t - goal._aware(last['at'])).total_seconds())}."
                  if last else ", no check yet."))
    reason_sentence = ("No watcher is set up yet." if watch_out["reason"] == "no_agent"
                       else _up(watch_out["reason_words"] or "") + ".")
    keep = (f"Your {n_services} services keep sending data. " if n_services != 1
            else "Your service keeps sending data. ")
    off_sub = keep + reason_sentence
    if active:
        first = goal.service_name(active[0]["service"], settings)
        text = (f"1 incident open on {first}" if total == 1 else f"{total} incidents open")
        if not on:
            text += ", and nobody is watching"
        tone = "broken"
        if not on:
            sub = off_sub
        else:
            rest = len(others)
            if not rest:
                sub = ""
            elif rest == 1:
                sub = "The other service looks normal. " if normal else "The other service needs a look. "
            elif normal == rest:
                sub = f"The other {rest} services look normal. "
            else:
                sub = f"{normal} of the other {rest} services look normal. "
            sub += checked
    elif not items:
        text, tone = "No services yet", "new"
        sub = "Connect where your logs, metrics or alerts come from."
    elif not on:
        tone = "unwatched"
        text = (f"Nobody is watching {system_text}" if watch_out["reason"] == "no_agent" or not off_since
                else f"Nobody has watched {system_text} since {_since_phrase(off_since, t)}")
        sub = off_sub
    else:
        tone, text = "quiet", "All quiet"
        watchable = len(state)
        watched = sum(1 for r in state.values() if r["watched"])
        base = (f"{_plural(watched, 'service')} watched" if watched == watchable
                else f"{watched} of {watchable} services watched")
        sub = base + (f", last check {goal.age_words((t - goal._aware(last['at'])).total_seconds())}."
                      if last else ", no check yet.")
        silent = [s["title"] for s in spots if s["kind"] == "silent"]
        if silent:
            sub += f" {silent[0]}." + (f" {len(silent) - 1} more {'service is' if len(silent) == 2 else 'services are'} silent too."
                                            if len(silent) > 1 else "")
    headline = {"text": text, "tone": tone, "sub": sub}

    raised, raised_note = _raised(store, catalog, person, on, t)
    inv_agents = [i["agent"] for i in roles["investigators"]]
    notes_channel = watch.channel_for(store, catalog, main_block["agent"] if main_block else None,
                                      main_block["project"] if main_block else None)
    return {
        "edition": "sre", "system": system, "headline": headline, "watch": watch_out,
        "incidents": incidents_out, "incidents_more": max(0, total - len(incidents_out)),
        "services": services, "services_from_words": _feeds_words(items),
        "raised": raised, "raised_note": raised_note,
        "today": _today(store, [w["agent"] for w, _b in pairs], inv_agents, t),
        "on_watch": _on_watch(store, catalog, roles, pairs, why, t),
        "notes_go_to": {"label": (f"{_channel_words(notes_channel)} in Slack"
                                  if not _CHANNEL_ID.match(notes_channel.lstrip("#"))
                                  else "a Slack channel")} if notes_channel else None,
        "blind_spots": [{"service": s.get("service") or "", "kind": s["kind"], "title": s["title"],
                         "detail": s["detail"],
                         "action": {"label": s["fix"]["label"],
                                    "to": NOTES_PLACE.get(s["fix"]["action"], "/settings")}}
                        for s in spots],
        "tools": _tools(store, catalog, items, health, t),
        "start": start_block(store, catalog, start_who or person or "console", now, items, state,
                             roles)}
