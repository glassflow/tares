"""The goal-first project page: a project's goal, how it works in plain words, what its agents
found, and whether it is working.

Everything here is deterministic and reads only what is stored: no model call. The outline is
built from the configuration (sources, triggers, agents, handoffs, skills), the results from the
project timeline (tares/timeline.py: one result per thread, its last concluding run), and the
health from the runtime's source health plus the run log.

Every string here is shown to a person: plain language, no em dashes.
"""
from __future__ import annotations

import re
import statistics
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

from .config import agent_url, parse_duration, trigger_entity_label
from .connectors import SPECS
from . import timeline as _timeline

GOAL_MAX = 200
HEADLINE_MAX = 100
NEXT_STEP_MAX = 300
SUMMARY_MAX = 400
# A verdict that says nothing needs doing: its result is shown as "no action" whatever the note.
NO_ACTION_VERDICTS = {"ignore", "ok", "benign", "resolved", "no_action", "noise", "none", "fine"}
RESULTS_PAGE = 100       # threads read per timeline page while collecting results
RESULTS_SCAN_CAP = 2000  # threads examined per request before handing back a cursor
# A source is silent when nothing arrived for longer than this many times its usual gap, and
# longer than SILENT_MIN_S. See _silent().
SILENT_FACTOR = 10
SILENT_MIN_S = 30 * 60
# Only a source with a steady rhythm can go silent: events in at least this many different hours
# of that day. A source that speaks in bursts (errors, alerts) is quiet when things are fine, and
# its quiet is good news, not a problem.
SILENT_MIN_HOURS = 6


# ── goal ─────────────────────────────────────────────────────────────────────
def normalize_goal(goal) -> str | None:
    """A goal is one line: whitespace (new lines included) folds to single spaces and the ends
    are trimmed. Empty is None. Raises ValueError past GOAL_MAX characters."""
    if goal is None:
        return None
    text = " ".join(str(goal).split())
    if not text:
        return None
    if len(text) > GOAL_MAX:
        raise ValueError(f"a goal is at most {GOAL_MAX} characters (this one has {len(text)})")
    return text


# ── words ────────────────────────────────────────────────────────────────────
def _num(v) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def duration_words(seconds: float) -> str:
    """600 -> "10 minutes", 3600 -> "1 hour", 90 -> "90 seconds"."""
    s = int(round(seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size and s % size == 0:
            return _plural(s // size, unit)
    return _plural(s, "second")


def _window_words(window: str) -> str:
    try:
        return duration_words(parse_duration(window))
    except (ValueError, KeyError, IndexError):
        return str(window)


def span_words(seconds: float) -> str:
    """A rough length of time: "2 hours", "45 minutes", "3 days", "less than a minute"."""
    s = max(0, int(seconds))
    if s < 60:
        return "less than a minute"
    if s < 3600:
        return _plural(s // 60, "minute")
    if s < 86400:
        return _plural(s // 3600, "hour")
    return _plural(s // 86400, "day")


def age_words(seconds: float) -> str:
    return "just now" if seconds < 60 else f"{span_words(seconds)} ago"


def _short_duration(ms) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return "under 1 s"
    s = int(round(ms / 1000))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    return f"{m} min {s} s" if s else f"{m} min"


def join_words(items: list) -> str:
    items = [str(i) for i in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _aware(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt is not None else None


# ── trigger phrasing ─────────────────────────────────────────────────────────
_FILTER_WORDS = {"eq": "=", "neq": "is not", "gt": "above", "lt": "below", "gte": "at least",
                 "lte": "at most", "contains": "containing", "in": "one of"}
_COUNT_WORDS = {">": "more than", ">=": "at least", "<": "fewer than", "<=": "at most",
                "==": "exactly"}
_AGG_WORDS = {"avg": "average", "sum": "total", "max": "highest", "min": "lowest",
              "any": "highest", "count": "number of"}
_AGG_VERBS = {">": "goes above", ">=": "reaches", "<": "drops below", "<=": "drops to",
              "==": "is"}
_STEP_COMPARE = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below",
                 "==": "matching"}


def _predicate(pred: str) -> tuple[str, float] | None:
    p = str(pred or "").strip()
    for sym in (">=", "<=", "==", ">", "<"):
        if p.startswith(sym):
            try:
                return sym, float(p[len(sym):].strip())
            except ValueError:
                return None
    return None


def filters_words(filters) -> str:
    """" matching level = error and status above 499", or "" without filters."""
    parts = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        value = f.get("value")
        if f.get("op") == "in" and isinstance(value, list):
            shown = ", ".join(str(v) for v in value[:3]) + (f" and {len(value) - 3} more"
                                                            if len(value) > 3 else "")
            parts.append(f"{f.get('field')} one of {shown}")
            continue
        parts.append(f"{f.get('field')} {_FILTER_WORDS.get(f.get('op'), f.get('op'))} {value}")
    return (" matching " + " and ".join(parts)) if parts else ""


# what several sources of one kind are, in a sentence ("any of 5 GitHub repos")
_KIND_NOUNS = {"github": "GitHub repos", "webhook": "webhooks", "loki": "Loki streams",
               "prometheus": "Prometheus servers", "prometheus_alerts": "alert feeds",
               "alertmanager": "alert feeds", "otlp": "OpenTelemetry feeds",
               "vercel": "Vercel projects", "http_poll": "HTTP APIs", "postgres": "Postgres tables",
               "docker": "container log streams", "claude_code": "Claude Code session feeds"}


def agent_words(name: str) -> str:
    """How a sentence names an agent: its name, or for a long generated one
    (ctx_glassflow_tares_context_maintainer) its last word, "the maintainer agent"."""
    name = str(name or "")
    if len(name) <= 24:
        return name
    last = re.split(r"[_\-]+", name.strip("_-"))[-1] or name
    return f"the {last} agent"


def _cap(s: str) -> str:
    """A sentence that opens with "the maintainer agent" starts upper case; one that opens with an
    agent's own name keeps it as written."""
    return "The" + s[3:] if s.startswith("the ") else s


_GH_EVENTS = {"pull_request": "a pull request", "push": "a push", "commit": "a commit",
              "issues": "an issue", "issue_comment": "a comment",
              "pull_request_review": "a pull request review",
              "pull_request_review_comment": "a review comment", "release": "a release",
              "workflow_run": "a CI run"}
_GH_ACTIONS = {"opened": "is opened", "merged": "is merged", "closed": "is closed",
               "reopened": "is reopened", "synchronize": "gets new commits",
               "published": "is published", "completed": "finishes", "created": "is written",
               "submitted": "is submitted", "pushed": "lands"}


def _github_event_words(filters) -> tuple[str, list] | None:
    """A GitHub trigger said by its event: ("a pull request is merged", remaining filters), from an
    `event_type` eq filter and an optional `action` eq filter (the GitHub event contract). None
    when the trigger does not name an event type."""
    fs = [f for f in filters or [] if isinstance(f, dict)]
    ev = next((f for f in fs if f.get("field") == "event_type" and f.get("op") == "eq"), None)
    if ev is None or str(ev.get("value")) not in _GH_EVENTS:
        return None
    act = next((f for f in fs if f.get("field") == "action" and f.get("op") == "eq"), None)
    what = _GH_EVENTS[str(ev["value"])]
    verb = _GH_ACTIONS.get(str(act.get("value")), f"is {act.get('value')}") if act else "happens"
    rest = [f for f in fs if f is not ev and f is not act]
    return f"{what} {verb}", rest


def source_title(cfg) -> str:
    """What a person calls a source: the repo it watches, the host it reads, the table; its
    name when nothing better is known (internal names like ctx_org_repo stay on the setup page)."""
    c = getattr(cfg, "config", None) or {}
    if getattr(cfg, "connector", "") == "github_app":
        return "GitHub (every repository of the App)"
    for key in ("repo", "repository", "table", "container", "project"):
        v = c.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    url = c.get("url") or c.get("base_url")
    if isinstance(url, str) and "://" in url:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
        if host:
            return host
    return cfg.name


def sources_words(names: list, sources: dict) -> str:
    """The sources a sentence names: one or two short names as they are; more, or long ones,
    counted by kind ("any of 5 GitHub repos"), since the names belong on the setup page."""
    names = [str(n) for n in names]
    if not names:
        return ""
    if len(names) <= 2 and all(len(n) <= 24 for n in names):
        return join_words(names)
    kinds = {getattr(sources.get(n), "connector", "") for n in names}
    if len(kinds) == 1:
        k = kinds.pop()
        noun = _KIND_NOUNS.get(k) or f"{(SPECS.get(k, {}).get('label') or k or 'source')} sources"
        return f"its {noun[:-1] if noun.endswith('s') else noun}" if len(names) == 1 else \
            f"any of its {len(names)} {noun}"
    return f"any of its {len(names)} sources"


def entity_label(trig, sources: dict) -> str | None:
    """What one firing is about: the trigger's grouping labels, else its entity label."""
    group_by = trig.condition.group_by or ["key_value"]
    group_by = [group_by] if isinstance(group_by, str) else list(group_by)
    if group_by != ["key_value"]:
        return join_words(group_by)
    return trigger_entity_label(trig, sources)


def _is_schedule(trig) -> bool:
    return bool(getattr(trig.condition, "every", None))


def condition_clause(trig, sources: dict) -> str:
    """The part after "When": "checkout-errors gets more than 5 events in 5 minutes for one
    service". For a schedule, "" (see schedule_words)."""
    c = trig.condition
    names = list(trig.sources or [])
    srcs = sources_words(names, sources) or "the project's sources"
    one = len(names) == 1 or srcs.startswith("any of")
    filt = filters_words(trig.filters)
    label = entity_label(trig, sources)
    per = f" for one {label}" if label and label not in srcs else ""
    window = _window_words(c.window)
    kinds = {getattr(sources.get(n), "connector", "") for n in names}
    pred = _predicate(c.predicate)
    if pred is None:
        return f"{srcs} {'meets' if one else 'meet'} {c.predicate} over {window}{per}"
    op, n = pred
    if c.aggregate == "count":
        if op == ">" and n == 0:
            if kinds and kinds <= {"github", "github_app"}:
                said = _github_event_words(trig.filters)
                if said:
                    what, rest = said
                    return f"{what} in {srcs}{filters_words(rest)}{per}"
                if kinds == {"github"}:
                    return f"a commit{filt} lands in {srcs}{per}"
            return f"an event{filt} arrives on {srcs}{per}"
        if op == "==" and n == 0:
            what = f"no events{filt}"
        else:
            what = f"{_COUNT_WORDS[op]} {_num(n)} {'event' if n == 1 else 'events'}{filt}"
        verb = "gets" if one else "get"
        return f"{srcs} {verb} {what} in {window}{per}"
    agg = _AGG_WORDS.get(c.aggregate, c.aggregate)
    # "the total alert_active across any of its 3 sources", "the average latency_ms on api";
    # with filters, "the average latency_ms of events matching level = error on api"
    prep = "on" if len(names) == 1 else "across"
    what = f"the {agg} {c.field or 'value'}" + (f" of events{filt}" if filt else "")
    span = "over" if op == "==" else "in"   # "is 0 over 1 hour", "goes above 5 in 1 minute"
    return f"{what} {prep} {srcs} {_AGG_VERBS[op]} {_num(n)} {span} {window}{per}"


def schedule_words(trig) -> str:
    """"every 10 minutes", with ", for events matching ..." when the trigger has filters."""
    filt = filters_words(trig.filters)
    return (f"every {duration_words(trig.condition.every)}"
            + (f", for events{filt}" if filt else ""))


def described(trig) -> str:
    """The trigger's own plain words for what wakes it ("an alert fires in the demo service"),
    or "" when it has none and the sentence is said from the rule."""
    return str(getattr(trig, "description", "") or "").strip().rstrip(".").strip()


def wake_sentence(trig, sources: dict) -> str:
    d = described(trig)
    if d:
        return f"Wakes when {d}."
    if _is_schedule(trig):
        return f"Wakes {schedule_words(trig)}."
    return f"Wakes when {condition_clause(trig, sources)}."


def cooldown_sentence(trig, sources: dict) -> str | None:
    if _is_schedule(trig) or not trig.cooldown_seconds:
        return None
    label = entity_label(trig, sources)
    return (f"Then waits {duration_words(trig.cooldown_seconds)} before waking again"
            + (f" for the same {label}." if label else "."))


def _lead(trig, sources: dict) -> str:
    d = described(trig)
    if d:
        return f"When {d}"
    if _is_schedule(trig):
        w = schedule_words(trig)
        return w[0].upper() + w[1:]
    return f"When {condition_clause(trig, sources)}"


# ── the project's parts ──────────────────────────────────────────────────────
def _internal(connector: str) -> bool:
    return bool(SPECS.get(connector, {}).get("internal"))


def _parts(store, catalog, uid: str) -> dict:
    """The project's triggers (config objects), agents (catalog rows with `enabled`), sources
    (names, internal ones left out) and whether outside agents joined it."""
    # the parts the project uses and its own wiring of them (P-TR-216): an agent's trigger,
    # handoffs and on/off are this project's
    triggers = sorted((t for t in catalog.triggers
                       if uid in store.projects_using("trigger", t.name)), key=lambda t: t.name)
    used = {o["name"] for o in store.list_project_objects(uid) if o["kind"] == "agent"}
    agents = [a for a in store.list_catalog_agents(uid) if a["name"] in used]
    names = [n for n in store.project_sources(uid)
             if n in catalog.sources and not _internal(catalog.sources[n].connector)]
    targets: dict[str, list] = {}
    for a in agents:
        for h in a.get("handoffs") or []:
            targets.setdefault(h.get("agent"), []).append((a, h))
    outside = outside_agents(store, uid)
    return {"triggers": triggers, "agents": agents, "sources": names, "targets": targets,
            "outside": outside, "external": bool(outside)}


def outside_agents(store, uid: str) -> list[dict]:
    """The agents outside Tares that work for the project: each live project key ({name,
    subscribed: it gets a webhook, joined: it used its key}), and a webhook subscribed without
    one. An agent that checks in with its key never subscribes: it counts all the same."""
    subs = store.list_project_subscriptions(uid)
    out = []
    for k in store.list_api_keys(project=uid):
        if k.get("revoked_at"):
            continue
        mine = [s for s in subs if s.get("created_by") == f"key:{k['id']}"]
        out.append({"name": k["name"], "subscribed": bool(mine),
                    "joined": k.get("last_used_at") is not None})
    keyed = {f"key:{k['id']}" for k in store.list_api_keys(project=uid)}
    for s in subs:
        if s.get("created_by") not in keyed:
            host = urlparse(str(s.get("url") or "")).hostname or "a webhook"
            out.append({"name": host, "subscribed": True, "joined": True})
    return out


def outside_words(outside: list[dict]) -> str:
    """ "claude-code is told" / "claude-code sees it when it checks in", for the agents outside
    Tares, joined with "and"."""
    told = [o["name"] for o in outside if o["subscribed"]]
    checks = [o["name"] for o in outside if not o["subscribed"]]
    parts = []
    if told:
        parts.append(f"{join_words(told)} {'is' if len(told) == 1 else 'are'} told")
    if checks:
        parts.append(f"{join_words(checks)} {'sees' if len(checks) == 1 else 'see'} it when "
                     f"{'it checks' if len(checks) == 1 else 'they check'} in")
    return " and ".join(parts)


def outside_sentence(o: dict) -> str:
    """One outside agent in the Setup page's Agents step."""
    if o["subscribed"]:
        return f"{o['name']}, your own agent, is told at its webhook when the project wakes."
    if o["joined"]:
        return f"{o['name']}, your own agent, checks the project for what happened."
    return f"{o['name']}, your own agent, has not connected with its key yet."


def _handoff_cooldown(h: dict) -> str:
    return str(h.get("cooldown") or "30m")


def _agent_sentence(a: dict, parts: dict, sources: dict) -> tuple[str, str]:
    """(sentence, runs_on) for one agent."""
    by = parts["targets"].get(a["name"]) or []
    if a["enabled"] or not by:
        if a["enabled"]:
            return _cap(f"{agent_words(a['name'])} looks first."), "trigger"
        return _cap(f"{agent_words(a['name'])} is turned off."), "trigger"
    trig_by_name = {t.name: t for t in parts["triggers"]}
    clauses = []
    for frm, h in by:
        trig = trig_by_name.get(frm.get("trigger"))
        label = entity_label(trig, sources) if trig is not None else None
        try:
            cd = duration_words(parse_duration(_handoff_cooldown(h)))
        except (ValueError, KeyError, IndexError):
            cd = _handoff_cooldown(h)
        per = f"at most once per {label} every {cd}" if label else f"at most once every {cd}"
        clauses.append(f"when {agent_words(frm['name'])} concludes {h.get('verdict')}, {per}")
    return _cap(f"{agent_words(a['name'])} digs in {' or '.join(clauses)}."), "handoff"


def outline_sentence(parts: dict, sources: dict) -> str:
    triggers = [t for t in parts["triggers"] if not t.paused] or parts["triggers"]
    if not triggers:
        return "Nothing wakes this project yet: it has no trigger."
    by_trigger: dict[str, list] = {}
    for a in parts["agents"]:
        if a["enabled"]:
            by_trigger.setdefault(a["trigger"], []).append(a)
    agents_by_name = {a["name"]: a for a in parts["agents"]}
    out = []
    for t in triggers:
        first = by_trigger.get(t.name) or []
        lead = _lead(t, sources)
        if first:
            names = [agent_words(a["name"]) for a in first]
            s = f"{lead}, {join_words(names)} {'looks' if len(names) == 1 else 'look'} first."
        elif parts["external"]:
            s = f"{lead}, {outside_words(parts['outside'])}."
        else:
            s = f"{lead}, no agent is turned on to look yet."
        # the handoff chain, breadth first; "it" when one agent looked first
        seen = set()
        queue = [(a, 1) for a in first]
        while queue:
            a, depth = queue.pop(0)
            for h in a.get("handoffs") or []:
                edge = (a["name"], h.get("agent"))
                if edge in seen:
                    continue
                seen.add(edge)
                subject = "it" if depth == 1 and len(first) == 1 else agent_words(a["name"])
                s += f" If {subject} concludes {h.get('verdict')}, {agent_words(h.get('agent'))} digs in."
                nxt = agents_by_name.get(h.get("agent"))
                if nxt is not None:
                    queue.append((nxt, depth + 1))
        out.append(s)
    return " ".join(out)


# ── source state (shared by the outline and health) ──────────────────────────
def _plain_error(connector: str, error: str) -> tuple[str, str, str]:
    """(what, reason, fix) for a source's last error, in plain words."""
    e = str(error or "")
    low = e.lower()
    what = "can't reach GitHub" if connector == "github" else "can't collect events"
    if any(k in low for k in ("401", "403", "bad credentials", "unauthorized", "forbidden",
                              "authentication")):
        if connector == "github":
            return what, "the token was rejected", "Update the token"
        return what, "the credentials were rejected", "Update the credentials"
    if any(k in low for k in ("connecterror", "connection refused", "name or service not known",
                              "nodename nor servname", "timed out", "timeout",
                              "temporary failure in name resolution", "unreachable")):
        return what, "the address cannot be reached", "Check the address"
    if "404" in low or "not found" in low:
        return what, "the address was not found", "Check the address"
    reason = re.sub(r"^[A-Za-z_.]+(Error|Exception|Timeout)?:\s*", "", e).strip() or "an error"
    reason = " ".join(reason.split())
    if len(reason) > 160:
        reason = reason[:159].rstrip() + "…"
    return what, reason, "Check the source's settings"


def _silent(store, name: str, last, now) -> tuple[bool, float]:
    """(silent, seconds since the last event). Silent means nothing arrived for longer than
    SILENT_FACTOR times the source's usual gap and longer than SILENT_MIN_S. The usual gap is the
    median time between distinct ingests over the day of activity before its last event (not the
    last day by the clock: a busy source that stopped yesterday still has a rhythm to compare
    with). Fewer than two distinct ingests in that day: no rhythm, never silent."""
    age = (now - last).total_seconds()
    if age <= SILENT_MIN_S:
        return False, age
    since = last - timedelta(days=1)
    gaps = [g for g in store.recent_ingest_gaps(name, since) if g > 0]
    if not gaps or len(store.recent_ingest_hours(name, since)) < SILENT_MIN_HOURS:
        return False, age
    return age > SILENT_FACTOR * statistics.median(gaps), age


def source_state(store, cfg, health: dict | None, now) -> dict:
    """{state, last_event_at, detail, error?} for one source."""
    h = health or {}
    last = _aware(h.get("last_ingest"))
    base = {"last_event_at": _iso(last)}
    if cfg.paused or h.get("status") == "paused":
        return {**base, "state": "paused", "detail": "Paused"}
    if h.get("status") == "error" and h.get("last_error"):
        what, reason, _fix = _plain_error(cfg.connector, h["last_error"])
        return {**base, "state": "error", "detail": f"It {what}: {reason}",
                "error": h["last_error"]}
    if last is None:
        return {**base, "state": "waiting", "detail": "Waiting for its first event"}
    silent, age = _silent(store, cfg.name, last, now)
    if silent:
        return {**base, "state": "silent", "detail": f"Nothing for {span_words(age)}"}
    return {**base, "state": "receiving", "detail": f"Last event {age_words(age)}"}


def _health_for(runtime_health: dict, store, names: list) -> dict:
    """Runtime health per source, with the stored last ingest for a source the runtime has no
    entry for (not started yet)."""
    out = dict(runtime_health or {})
    missing = [n for n in names if n not in out]
    if missing:
        stats = {s["source"]: s for s in store.event_stats()}
        for n in missing:
            s = stats.get(n) or {}
            out[n] = {"status": "", "last_ingest": s.get("last_ingest"),
                      "events_total": s.get("events", 0)}
    return out


# ── outline ──────────────────────────────────────────────────────────────────
def _chain_order(agents: list[dict]) -> list[dict]:
    """In the order the work happens: the agents a trigger wakes first, each followed by the
    agents it hands off to (depth first), then any left over."""
    by_name = {a["name"]: a for a in agents}
    out: list[dict] = []

    def visit(a):
        if a in out:
            return
        out.append(a)
        for h in a["handoffs"]:
            if h["agent"] in by_name:
                visit(by_name[h["agent"]])

    for a in agents:
        if a["runs_on"] == "trigger":
            visit(a)
    for a in agents:
        visit(a)
    return out


def outline(store, catalog, uid: str, runtime_health: dict | None = None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    sources = catalog.sources
    parts = _parts(store, catalog, uid)
    health = _health_for(runtime_health, store, parts["sources"])
    watches = []
    for n in parts["sources"]:
        cfg = sources[n]
        st = source_state(store, cfg, health.get(n), now)
        watches.append({"source": n, "title": source_title(cfg), "connector": cfg.connector,
                        "description": SPECS.get(cfg.connector, {}).get("label") or cfg.connector,
                        "state": st["state"], "last_event_at": st["last_event_at"],
                        "detail": st["detail"]})
    wakes = [{"trigger": t.name, "sentence": wake_sentence(t, sources),
              "cooldown_sentence": cooldown_sentence(t, sources), "paused": bool(t.paused)}
             for t in parts["triggers"]]
    agents = []
    for a in parts["agents"]:
        sentence, runs_on = _agent_sentence(a, parts, sources)
        agents.append({"name": a["name"], "sentence": sentence, "enabled": a["enabled"],
                       "runs_on": runs_on,
                       "handoffs": [{"verdict": h.get("verdict"), "agent": h.get("agent"),
                                     "cooldown": _handoff_cooldown(h)}
                                    for h in a.get("handoffs") or []]})
    agents = _chain_order(agents)
    loads = store.skill_loads([a["name"] for a in parts["agents"]], days=7)
    skills = [{"name": sk["name"], "description": sk["description"],
               "loaded_by": loads.get(sk["name"], [])} for sk in store.list_skills(uid)]
    outside = [{"name": o["name"], "sentence": outside_sentence(o), "joined": o["joined"]}
               for o in parts["outside"]]
    return {"sentence": outline_sentence(parts, sources), "watches": watches, "wakes": wakes,
            "agents": agents, "outside": outside, "skills": skills}


# ── headline, next step, summary ─────────────────────────────────────────────
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_LEAD = re.compile(r"^\s*(?:[-*>]\s+)?(\*\*|__)(.+?)\1\s*(.*)$")
_NEXT_LABEL = re.compile(
    r"(?:^|(?<=[.!?])\s+)[\s>*_#\-]*(?:\d+[.)]\s*)?[*_]*"
    r"(next steps?|next|recommendations?|recommended action|suggested next action)"
    r"[*_]*\s*:[*_]*\s*", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+(next steps?|recommendations?|recommended action|suggested next action)"
    r"\s*:?\s*$", re.IGNORECASE | re.MULTILINE)


def plain(text: str) -> str:
    """Markdown stripped to plain text, whitespace folded to single spaces."""
    t = str(text or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)          # links and images: the text
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<![\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])", r"\1", t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*(?:>\s*)+", "", t)
    t = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", t)
    return " ".join(t.split())


def _cut(text: str, limit: int) -> str | None:
    t = " ".join(str(text or "").split())
    if not t:
        return None
    if len(t) <= limit:
        return t
    cut = t[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.") + "…"


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]


# An agent's narration before the finding ("I have a complete picture.", "Here's the full
# analysis:"): never a headline or a summary.
_FILLER = re.compile(r"^(i have (a|the|all|enough|sufficient|everything)|i now have|i've (got|gathered|"
                     r"collected|confirmed)|i'll|i will|i can now|let me|now (let me|i)|here's|"
                     r"here (is|are)|okay|alright|great|perfect)\b", re.IGNORECASE)
FILLER_MAX = 120


def _filler(sentence: str, after_filler: bool) -> bool:
    """Narration: a known opener, short; or, right after one, the line that introduces what
    follows ("Here's the full analysis:")."""
    t = plain(sentence).strip()
    return bool(t) and ((bool(_FILLER.match(t)) and len(t) <= FILLER_MAX)
                        or (after_filler and t.endswith(":")))


def strip_filler(note: str | None) -> str:
    """The note without the narration sentences it opens with."""
    text = str(note or "").lstrip()
    while text:
        block, sep, rest = text.partition("\n\n")
        if block.lstrip().startswith(("#", "|", "-", "*", "1.")):
            break
        sentences = re.split(r"(?<=[.!?:])\s+", block.strip())
        keep = 0
        while keep < len(sentences) and _filler(sentences[keep], keep > 0):
            keep += 1
        if keep == 0:
            break
        if keep < len(sentences):
            return (" ".join(sentences[keep:]) + sep + rest).lstrip()
        text = rest.lstrip()
    # a note that is all narration keeps it: a weak headline beats none
    return text or str(note or "").strip()


def derive_headline(note: str | None) -> str | None:
    """For a run whose agent gave no headline: the note's first line when it is a markdown
    heading, else its bold lead (the text after it when the bold part is a label such as
    "Conclusion:"), else its first sentence; plain text, at most HEADLINE_MAX characters. The
    narration an agent opens with is skipped."""
    note = strip_filler(note)
    lines = [l for l in str(note or "").splitlines() if l.strip()]
    if not lines:
        return None
    first = lines[0]
    m = _HEADING.match(first)
    if m and plain(m.group(1)):
        return _cut(plain(m.group(1)), HEADLINE_MAX)
    m = _BOLD_LEAD.match(first)
    if m:
        lead, rest = plain(m.group(2)), m.group(3)
        if lead.endswith(":") and plain(rest):
            return _cut(_first_sentence(plain(rest)), HEADLINE_MAX)
        if lead:
            return _cut(lead.rstrip(":"), HEADLINE_MAX)
    para = _paragraphs(note)
    return _cut(_first_sentence(para[0]), HEADLINE_MAX) if para else None


def _tidy(text: str | None) -> str | None:
    """A derived line without emphasis marks left over from markdown around a label, starting
    with a capital."""
    t = (text or "").strip().strip("*_ ").strip()
    return (t[:1].upper() + t[1:]) if t else None


def derive_next_step(note: str | None) -> str | None:
    """The text after a "Next step:", "Next:" or "Recommendation:" label (or under a heading
    of that name), to the end of its paragraph; None when the note has no such label."""
    text = str(note or "")
    m = _NEXT_HEADING.search(text)
    if m:
        rest = text[m.end():].lstrip("\n")
        body = re.split(r"\n\s*\n|\n\s{0,3}#{1,6}\s", rest, maxsplit=1)[0]
        return _tidy(_cut(plain(body), NEXT_STEP_MAX))
    m = _NEXT_LABEL.search(text)
    if not m:
        return None
    rest = text[m.end():]
    body = re.split(r"\n\s*\n", rest, maxsplit=1)[0]
    return _tidy(_cut(plain(body), NEXT_STEP_MAX))


def _paragraphs(note: str | None) -> list[str]:
    """The note's paragraphs as plain text, headings left out."""
    out = []
    for block in re.split(r"\n\s*\n", str(note or "")):
        lines = [l for l in block.splitlines() if l.strip() and not _HEADING.match(l)]
        text = plain("\n".join(lines))
        if text:
            out.append(text)
    return out


def summary_of(note: str | None, headline: str | None = None) -> str | None:
    """The note's first paragraph as plain text, without what the card already shows: the
    headline when the paragraph opens with it, and a next step written into the paragraph."""
    text = strip_filler(note)
    m = _NEXT_LABEL.search(text)
    if m:
        text = text[:m.start()]
    para = _paragraphs(text)
    if not para:
        return None
    first = para[0]
    head = (headline or "").rstrip(". ")
    if head and first.startswith(head):
        first = first[len(head):].lstrip(" .:")
        if not first and len(para) > 1:
            first = para[1]
    return _cut(first, SUMMARY_MAX)


_DASH = re.compile(r"\s*[\u2014\u2013]\s*")


def _no_dash(text: str | None) -> str | None:
    """A model's line with its em and en dashes turned into commas: the console shows no dashes."""
    return _DASH.sub(", ", text).strip(", ") if text else text


def headline_and_next(run: dict) -> tuple[str | None, str | None]:
    """What the agent gave; when it gave neither (older runs, small models), derived from the
    note."""
    if run.get("headline") or run.get("next_step"):
        return _no_dash(run.get("headline")), _no_dash(run.get("next_step"))
    note = run.get("finding")
    return _no_dash(derive_headline(note)), _no_dash(derive_next_step(note))


# ── results ──────────────────────────────────────────────────────────────────
def _concluding(r: dict) -> bool:
    if r.get("status") != "ok":
        return False
    outcome = r.get("outcome")
    if outcome == "no_op":
        return bool((r.get("finding") or "").strip())   # only when it left a note
    return bool(r.get("finding")) or outcome == "finding"


def _walk(thread: dict) -> tuple[list, dict]:
    """(every run of the thread, {run id: parent run or None})."""
    runs, parent = [], {}
    stack = [(r, None) for r in thread.get("runs") or []]
    while stack:
        r, p = stack.pop()
        runs.append(r)
        parent[r["id"]] = p
        stack.extend((c, r) for c in r.get("children") or [])
        for t in r.get("firings") or []:
            stack.extend((c, r) for c in t.get("runs") or [])
    return runs, parent


def _started(r: dict):
    return _aware(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)


def kind_of(run: dict, next_step: str | None) -> str:
    if run.get("outcome") == "no_op":
        return "no_action"
    if not next_step or str(run.get("verdict") or "").strip().lower() in NO_ACTION_VERDICTS:
        return "no_action"
    # a next step that says there is nothing to do ("No action required. Monitor ...")
    if _NOTHING_TO_DO.match(next_step):
        return "no_action"
    return "action"


_NOTHING_TO_DO = re.compile(r"\s*(no action|nothing to do|no further action|none needed|no need to)",
                            re.IGNORECASE)


def result_for(thread: dict, run: dict, parent: dict) -> dict:
    path = []
    cur = run
    while cur is not None and len(path) < 50:
        path.append(cur)
        cur = parent.get(cur["id"])
    path.reverse()
    chain = []
    for r in path:
        if not chain or chain[-1] != r["agent"]:
            chain.append(r["agent"])
    headline, next_step = headline_and_next(run)
    handled = ({"at": _iso(run["handled_at"]), "by": run.get("handled_by")}
               if run.get("handled_at") else None)
    return {"id": run["id"], "thread": thread["id"], "at": _iso(thread.get("at")),
            "entity": run.get("key") or thread.get("entity"),
            "kind": kind_of(run, next_step),
            "headline": headline, "summary": summary_of(run.get("finding"), headline),
            "next_step": next_step, "verdict": run.get("verdict"), "chain": chain,
            "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in path), 6),
            "duration_ms": sum(int(r.get("duration_ms") or 0) for r in path),
            "handled": handled, "external": run.get("woken_by") == "external",
            # a run the guided setup's "Try it" started, or what it led to: shown, never counted
            "practice": any(bool(r.get("practice")) for r in path)}


def thread_result(thread: dict) -> dict | None:
    """The one result of a thread: its last concluding run (a triage that handed off is not a
    result, the run it handed to is). None while a run of the thread is still working, and when
    no run concluded."""
    runs, parent = _walk(thread)
    if any(r.get("status") == "running" for r in runs):
        return None
    done = [r for r in runs if _concluding(r)]
    if not done:
        return None
    return result_for(thread, max(done, key=_started), parent)


def _threads(store, uid: str, before, scheduled: set):
    """Every thread of the project, newest first, older than `before`, read page by page."""
    cursor = before
    while True:
        page = _timeline.project_timeline(store, uid, limit=RESULTS_PAGE, before=cursor,
                                          scheduled=scheduled)
        for t in page["threads"]:
            yield t
        if not page["next_before"]:
            return
        cursor = _aware(page["next_before"])


def utc_midnight(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def project_results(store, uid: str, limit: int = 20, before=None, scheduled: set | None = None,
                    now=None) -> dict:
    """One page of results. The first page (no `before`) also counts today's in the same walk:
    it reads on past the page while threads are from today, so the overview's poll walks the
    project once. Older pages leave `found` at 0; the console reads today from the first page."""
    limit = max(1, min(int(limit), 100))
    scheduled = scheduled or set()
    # "today" is since midnight UTC: the cell does not know the viewer's time zone
    midnight = utc_midnight(now)
    first_page = before is None
    results, next_before, scanned, found, full = [], None, 0, 0, False
    for t in _threads(store, uid, before, scheduled):
        scanned += 1
        today_thread = first_page and _aware(t["at"]) >= midnight
        if full and not today_thread:
            break
        r = thread_result(t)
        if r is not None:
            if today_thread and r["kind"] == "action" and not r["practice"]:
                found += 1
            if not full:
                results.append(r)
                if len(results) >= limit:
                    next_before, full = _iso(t["at"]), True
        if scanned >= RESULTS_SCAN_CAP:
            if not full:
                next_before = _iso(t["at"])
            break
    today = {"looked_at": store.project_threads_since(uid, midnight), "found": found,
             "spent_usd": round(store.project_spend_since(uid, midnight), 6)}
    return {"results": results, "next_before": next_before, "today": today}


def _root_of(store, run: dict, uid: str | None = None) -> tuple | None:
    """The (kind, id, at) root of the timeline thread a run sits in, in project `uid`, by the
    same rules as store.timeline_roots (a parent counts when it belongs to the project too)."""
    def mine(r):
        return (uid in store.run_projects(r["id"])) if uid else True
    cur, seen = run, set()
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        pid = cur.get("parent_run_id")
        if pid:
            p = store.get_agent_run(pid)
            if p is not None and mine(p):
                cur = p
                continue
        if cur.get("dispatch_id"):
            ds = store.dispatches_where("dispatch_id", [cur["dispatch_id"]])
            if not ds:
                return ("run", cur["id"], cur["started_at"])
            d = ds[0]
            if d.get("parent_run_id"):
                p = store.get_agent_run(d["parent_run_id"])
                if p is not None and mine(p):
                    cur = p
                    continue
            return ("firing", d["dispatch_id"], d["fired_at"])
        return ("run", cur["id"], cur["started_at"])
    return None


def _firing_value(store, trig, fired: dict):
    """The condition's value for the firing's entity over the window that ended when it fired,
    or None when it cannot be told (the trigger changed, the events are gone)."""
    from .triggers import _fire_key, _group_by
    c = trig.condition
    try:
        at = _aware(fired["fired_at"])
        since = at - timedelta(seconds=parse_duration(c.window))
        group_by = _group_by(c)
        per = store.aggregate(trig.sources, c.field, c.aggregate, since, filters=trig.filters,
                              group_by=group_by, until=at + timedelta(seconds=1))
    except Exception:
        return None
    for grp, value in per.items():
        grp = grp if isinstance(grp, tuple) else (grp,)
        if _fire_key(group_by, list(grp)) == fired.get("key"):
            return value
    return None


def _firing_text(store, catalog, t: dict, nested: bool) -> str:
    trig = next((x for x in catalog.triggers if x.name == t.get("trigger")), None)
    entity = t.get("entity") or (t.get("fired") or {}).get("key")
    if nested:
        return f"The finding on {entity} woke {t.get('trigger')}"
    if trig is None:
        return f"{t.get('trigger')} woke the project for {entity}"
    if _is_schedule(trig):
        return f"The scheduled check ran, as it does every {duration_words(trig.condition.every)}"
    c = trig.condition
    pred = _predicate(c.predicate)
    value = _firing_value(store, trig, {"fired_at": t.get("at"), "key": entity})
    window = _window_words(c.window)
    if pred is None or value is None:
        d = described(trig)
        if d:
            return f"Woke when {d}, for {entity}"
        return f"{t.get('trigger')} woke the project for {entity}"
    op, n = pred
    if c.aggregate == "count":
        v = int(value)
        return (f"{_plural(v, 'event')} from {entity}, {_COUNT_WORDS[op]} the {_num(n)} in "
                f"{window} that wakes the project")
    agg = _AGG_WORDS.get(c.aggregate, c.aggregate)
    return (f"The {agg} {c.field or 'value'} for {entity} was {_num(round(float(value), 2))}, "
            f"{_STEP_COMPARE[op]} the {_num(n)} in {window} that wakes the project")


def _run_text(r: dict, is_result: bool) -> str:
    name = r.get("agent")
    if r.get("woken_by") == "external":
        return f"{name}, an outside agent, recorded {'this' if is_result else 'a'} finding"
    verb = {"handoff": "dug in", "rerun": "looked again"}.get(r.get("woken_by"), "looked")
    bits = [b for b in (_short_duration(r.get("duration_ms")),
                        ("used " + join_words(r["skills"])) if r.get("skills") else "") if b]
    paren = f" ({', '.join(bits)})" if bits else ""
    status = r.get("status")
    if status == "running":
        return f"{name} is still working"
    if status == "failed":
        return f"{name} could not finish: {_cut(r.get('error') or 'it failed', 160)}"
    if status == "capped":
        return f"{name} did not run: {_cut(r.get('error') or 'a limit was reached', 160)}"
    if status in ("empty", "exhausted"):
        return f"{name} {verb}{paren} but did not reach a conclusion"
    if r.get("outcome") == "no_op":
        return f"{name} {verb}{paren} and found nothing that needs action"
    if is_result:
        return f"{name} {verb}{paren} and wrote this finding"
    if r.get("verdict"):
        return f"{name} {verb}{paren} and concluded {r['verdict']}"
    return f"{name} {verb}{paren} and wrote a finding"


def steps_of(store, catalog, thread: dict, result_run_id: str) -> list[dict]:
    """The plain story of a thread, oldest first: what woke it and what each run did."""
    items = []   # (at, order, text)

    def add_firing(t, nested):
        items.append((_aware(t.get("at")), 0, _firing_text(store, catalog, t, nested)))
        for r in t.get("runs") or []:
            add_run(r)

    def add_run(r):
        items.append((_started(r), 1, _run_text(r, r["id"] == result_run_id)))
        for c in r.get("children") or []:
            add_run(c)
        for t in r.get("firings") or []:
            add_firing(t, True)

    if thread.get("kind") == "firing":
        add_firing(thread, False)
    else:
        for r in thread.get("runs") or []:
            add_run(r)
    items.sort(key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc), x[1]))
    return [{"at": _iso(at), "text": text} for at, _o, text in items]


def result_detail(store, catalog, uid: str, run_id: str, scheduled: set | None = None) -> dict:
    """The result a run wrote, with the full note and the steps. KeyError when the run is not
    in the project or wrote no result."""
    run = store.get_agent_run(run_id)
    if run is None or uid not in store.run_projects(run_id):
        raise KeyError(f"project has no run {run_id!r}")
    if not _concluding(run):
        raise KeyError(f"run {run_id!r} wrote no result")
    root = _root_of(store, run, uid)
    threads = _timeline._build(store, uid, [root], scheduled or set()) if root else []
    thread = threads[0] if threads else {"id": run_id, "kind": "run", "at": run["started_at"],
                                         "entity": run.get("key"), "runs": []}
    runs, parent = _walk(thread)
    target = next((r for r in runs if r["id"] == run_id), None)
    if target is None:
        target = {**run, "children": [], "firings": []}
        parent = {run_id: None}
    out = result_for(thread, {**target, "finding": run.get("finding")}, parent)
    out["note"] = run.get("finding") or ""
    out["steps"] = steps_of(store, catalog, thread, run_id)
    return out


# ── health ───────────────────────────────────────────────────────────────────
def project_health(store, catalog, uid: str, project: dict, runtime_health: dict | None = None,
                   now=None) -> dict:
    from .builtin_agents import effective_daily_cap
    from . import providers
    now = now or datetime.now(timezone.utc)
    parts = _parts(store, catalog, uid)
    health = _health_for(runtime_health, store, parts["sources"])
    sources = catalog.sources
    errors, warnings = [], []

    # sources: in error (grouped by the same plain error), silent, never received
    states = {n: source_state(store, sources[n], health.get(n), now) for n in parts["sources"]}
    groups: dict[tuple, list] = {}
    for n, st in states.items():
        if st["state"] == "error":
            what, reason, fix = _plain_error(sources[n].connector, st.get("error") or "")
            groups.setdefault((what, reason, fix), []).append(n)
    # the error that stops the most sources first
    for (what, reason, fix), names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        who = names[0] if len(names) == 1 else f"{len(names)} sources"
        view = ("settings:github" if what == "can't reach GitHub" and fix == "Update the token"
                else f"source:{names[0]}")
        errors.append({"severity": "error", "message": f"{who} {what}: {reason}.", "fix": fix,
                       "view": view})

    # agents: can one run at all, is there a model to run it, did one hit a limit
    can_run = [a for a in parts["agents"]
               if a["enabled"] and any(t.name == a["trigger"] for t in parts["triggers"])]
    ready = can_run + [a for a in parts["agents"]
                       if a not in can_run and a["name"] in parts["targets"]]
    if not can_run and not parts["external"] and project.get("status") != "paused":
        if not parts["agents"]:
            errors.append({"severity": "error",
                           "message": "No agent is set up, so nothing looks at what arrives.",
                           "fix": "Add an agent", "view": "how"})
        else:
            errors.append({"severity": "error",
                           "message": "No agent is turned on, so nothing looks at what arrives.",
                           "fix": "Turn on an agent", "view": f"agent:{parts['agents'][0]['name']}"})
    if (not can_run and parts["outside"] and not any(o["joined"] for o in parts["outside"])
            and project.get("status") != "paused"):
        o = parts["outside"][0]
        warnings.append({"severity": "warning",
                         "message": f"Your agent {o['name']} has not connected with its key yet.",
                         "fix": "See its key", "view": "settings:keys"})
    if ready and not any(providers.resolve_provider(store, a.get("provider") or None)[0]
                         for a in ready):
        errors.append({"severity": "error",
                       "message": "No model provider is set up, so the agents cannot run.",
                       "fix": "Add a model provider", "view": "settings:providers"})
    for n, st in states.items():
        if st["state"] == "silent":
            age = (now - _aware(st["last_event_at"])).total_seconds()
            errors.append({"severity": "warning",
                           "message": f"Nothing has arrived on {n} for {span_words(age)}.",
                           "fix": "Check the connection", "view": f"source:{n}"})
    capped = []
    for a in ready:
        cap, _src = effective_daily_cap(a, store)
        if store.agent_runs_today(a["name"], include_practice=False) >= cap:
            capped.append(a["name"])
            warnings.append({"severity": "warning",
                             "message": f"{a['name']} reached its limit of {_plural(cap, 'run')} "
                                        "a day, so it does not run until its earlier runs are a "
                                        "day old.",
                             "fix": "Raise the limit", "view": f"agent:{a['name']}"})
            continue
        budget = a.get("budget_usd")
        if budget and store.agent_cost_total(a["name"]) >= float(budget):
            capped.append(a["name"])
            warnings.append({"severity": "warning",
                             "message": f"{a['name']} used its budget of ${float(budget):.2f}, so "
                                        "it does not run until the budget is raised.",
                             "fix": "Raise the budget", "view": f"agent:{a['name']}"})
    for n, st in states.items():
        mode = SPECS.get(sources[n].connector, {}).get("mode")
        if st["state"] == "waiting" and mode == "push":
            warnings.append({"severity": "warning",
                             "message": f"{n} is waiting for its first event.",
                             "fix": "See how to send events", "view": f"source:{n}"})
    issues = errors + warnings

    if project.get("status") == "paused":
        srcs = ("Its sources are paused too." if (project.get("params") or {}).get("paused_sources")
                else "Sources keep collecting.")
        return {"state": "paused",
                "message": f"Triggers are off and agents do not run. {srcs}",
                "issues": issues}
    if not parts["sources"]:
        return {"state": "setting_up", "message": "Add a source so this project has something "
                                                  "to watch.", "issues": issues}
    if errors:   # something is broken: that leads, even before the first event
        return {"state": "attention", "message": errors[0]["message"], "issues": issues}
    if all(st["last_event_at"] is None for st in states.values()):
        waiting = [n for n in parts["sources"]
                   if SPECS.get(sources[n].connector, {}).get("mode") == "push"] or parts["sources"]
        where = sources_words(waiting, sources).replace("any of its ", "its ")
        return {"state": "setting_up",
                "message": f"Waiting for the first event from {where}.",
                "issues": issues}
    if issues:
        return {"state": "attention", "message": issues[0]["message"], "issues": issues}
    receiving = [n for n, st in states.items() if st["state"] == "receiving"]
    last = max((_aware(st["last_event_at"]) for st in states.values() if st["last_event_at"]),
               default=None)
    n_ready = len([a for a in ready if a["name"] not in capped])
    msg = f"Receiving from {_plural(len(receiving), 'source')}"
    if last is not None:
        msg += f", last event {age_words((now - last).total_seconds())}"
    msg += "."
    if n_ready:
        msg += f" {_plural(n_ready, 'agent')} ready."
    elif parts["external"]:
        joined = [o for o in parts["outside"] if o["joined"]]
        if len(joined) == 1:
            msg += f" Your agent {joined[0]['name']} is connected."
        elif joined:
            msg += f" {len(joined)} of your agents are connected."
    return {"state": "working", "message": msg, "issues": issues}
