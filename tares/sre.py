"""The services an AI SRE cell knows (TR-742).

A service is not a record somebody keeps: it is a value of a source's service label (the label
named `service`, else the source's primary label), counted over the last 24 hours. What a person
changes about one (display name, hidden, merged into another, repos whose deploys belong to it)
lives in the `services` table (tares/sre_store.py) and is applied here, in Python, over the counts.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import timedelta, timezone

from .config import FINDINGS_SOURCE, primary_label
from .envelope import now_utc

LABELS_SETTING = "sre.service_labels"   # JSON {source name: label name, or null for not a service}
NOT_SERVICES = {"finding", "memory", "claude_code", "reference", "github", "github_app",
                "github_issues", "linear"}
DEPLOY_CONNECTORS = ("github", "github_app")
MAX_HOPS = 5
MAX_LABELS = 20
CACHE_SECONDS = 30
cache_clock = time.monotonic   # the cache's clock; a test replaces it instead of sleeping


def service_label(src, overrides: dict | None = None) -> str | None:
    """The label that names a service in source `src`, or None when the source is not one that
    holds services. `overrides` is the sre.service_labels setting: a source's entry wins, and a
    null entry says it is not a service source."""
    overrides = overrides or {}
    if src.name in overrides:
        v = overrides[src.name]
        return str(v) if v else None
    if src.connector in NOT_SERVICES:
        return None
    cfg = src.config if isinstance(src.config, dict) else {}
    for spec in cfg.get("labels") or []:
        if isinstance(spec, dict) and spec.get("name") == "service":
            return "service"
    return primary_label(src)


def signal_kind(connector: str, event_type: str | None = None, metric_type: str | None = None) -> str:
    """What kind of signal a source's event is: logs, metrics, traces, alerts, deploys or events."""
    if connector == "otlp":
        if event_type == "span":
            return "traces"
        return "metrics" if metric_type else "logs"
    if connector == "prometheus":
        return "metrics"
    if connector in ("alertmanager", "prometheus_alerts"):
        return "alerts"
    if connector in ("docker_logs", "loki", "vercel"):
        return "logs"
    if connector in DEPLOY_CONNECTORS:
        return "deploys"
    return "events"


def canonical(name: str, settings: dict) -> str:
    """The service `name` is merged into: follow merged_into up to MAX_HOPS hops and stop at the
    last service reached, also when a merge loops back on itself."""
    seen, cur = {name}, name
    for _ in range(MAX_HOPS):
        nxt = (settings.get(cur) or {}).get("merged_into")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
    return cur


# ── what counts as an error, and the charts built on it (TR-767) ─────────────
ERROR_LEVELS = ("error", "fatal", "critical")
LEVEL_LABELS = ("level", "severity", "severity_text")
# OTLP severityText lands in event_type: ERROR2 to ERROR4 and FATAL2 to FATAL4 are the finer steps
ERROR_TYPES = ("ERROR", "ERROR2", "ERROR3", "ERROR4", "FATAL", "FATAL2", "FATAL3", "FATAL4",
               "CRITICAL")
STATUS_LABELS = ("status_code", "status", "nginx_status_code", "http_status")
ERROR_STATUS = 500
ERROR_SEVERITY_NUMBER = 17   # OTLP: 17 and up is ERROR or FATAL, whatever the severityText says
# OTLP over gRPC writes severityNumber as its enum name, not the number
ERROR_SEVERITY_NAMES = tuple(f"SEVERITY_NUMBER_{n}" for n in (
    "ERROR", "ERROR2", "ERROR3", "ERROR4", "FATAL", "FATAL2", "FATAL3", "FATAL4"))
SPAN_ERROR_CODES = ("2", "STATUS_CODE_ERROR")   # payload.status.code, as a number or its name
WHOLE = r"[0-9]{1,6}"   # what a status or severity number is: plain digits, nothing else
WINDOWS = {"10m": (timedelta(minutes=10), 1), "1h": (timedelta(hours=1), 60),
           "2h": (timedelta(hours=2), 60), "24h": (timedelta(hours=24), 60),
           "7d": (timedelta(days=7), 84)}   # window -> (how long, buckets by default)


def _whole(value):
    """`value` as an int when it is plain digits (a number or its text), else None."""
    text = str(value)
    return int(text) if re.fullmatch(WHOLE, text) else None


def _whole_sql(expr: str) -> str:
    """The SQL twin of _whole: the integer in `expr`, NULL when it is not plain digits."""
    return f"CASE WHEN regexp_full_match({expr}, '{WHOLE}') THEN CAST({expr} AS BIGINT) END"


def is_error_where(spec: dict[str, str]) -> tuple[str, list]:
    """One SQL condition (and its params) that is true for an error event of the sources in
    `spec` (source -> the label naming a service in it): a level label of error, fatal or
    critical in any case; an event type of ERROR, ERROR2 to ERROR4, FATAL, FATAL2 to FATAL4 or
    CRITICAL (OTLP severityText); a payload severityNumber of 17 or more (or its gRPC enum name, SEVERITY_NUMBER_ERROR to
    SEVERITY_NUMBER_FATAL4); a status code label of
    500 or more (a value that is not plain digits never matches); or a span whose
    payload.status.code is 2 or STATUS_CODE_ERROR. is_error() below is the same rule for one
    event in Python."""
    if not spec:
        return "FALSE", []
    from .store import _label_expr
    levels = ", ".join(f"'{v}'" for v in ERROR_LEVELS)
    types = ", ".join(f"'{v}'" for v in ERROR_TYPES)
    codes = ", ".join(f"'{v}'" for v in SPAN_ERROR_CODES)
    parts = [f"lower({_label_expr(n)}) IN ({levels})" for n in LEVEL_LABELS]
    parts.append(f"upper(event_type) IN ({types})")
    parts += [f"{_whole_sql(_label_expr(n))} >= {ERROR_STATUS}" for n in STATUS_LABELS]
    severity = "json_extract_string(payload, '$.severityNumber')"
    names = ", ".join(f"'{v}'" for v in ERROR_SEVERITY_NAMES)
    parts.append(f"({_whole_sql(severity)} >= {ERROR_SEVERITY_NUMBER} OR {severity} IN ({names}))")
    parts.append(f"(event_type = 'span' AND json_extract_string(payload, '$.status.code') "
                 f"IN ({codes}))")
    sources = sorted(spec)
    ph = ", ".join(["?"] * len(sources))
    return f"(source IN ({ph}) AND ({' OR '.join(parts)}))", sources


def is_error(event: dict) -> bool:
    """Whether one event is an error: the rule of is_error_where. `event` has a `labels` dict
    and may have an `event_type` and a `payload` dict."""
    labels = event.get("labels") or {}
    payload = event.get("payload") or {}
    etype = str(event.get("event_type") or "")
    if etype.upper() in ERROR_TYPES:
        return True
    if any(str(labels.get(n)).lower() in ERROR_LEVELS for n in LEVEL_LABELS if n in labels):
        return True
    if any((_whole(labels[n]) or 0) >= ERROR_STATUS for n in STATUS_LABELS if n in labels):
        return True
    if "severityNumber" in payload and (
            (_whole(payload["severityNumber"]) or 0) >= ERROR_SEVERITY_NUMBER
            or str(payload["severityNumber"]) in ERROR_SEVERITY_NAMES):
        return True
    status = payload.get("status")
    return etype == "span" and isinstance(status, dict) \
        and str(status.get("code")) in SPAN_ERROR_CODES


def series_for(window: str, now, points: int | None = None) -> tuple:
    """(since, until, bucket_s) for a chart over `window` (10m, 1h, 2h, 24h or 7d) ending at
    `now`. `points` asks for that many buckets instead of the window's own (24h with 20 points
    is 20 buckets of 72 minutes); a count that does not divide the window gives fewer."""
    if window not in WINDOWS:
        raise ValueError(f"no window {window!r}: use one of {', '.join(WINDOWS)}")
    span, default = WINDOWS[window]
    n = default if points is None else points
    if not isinstance(n, int) or n < 1:
        raise ValueError("points must be a whole number of 1 or more")
    return now - span, now, math.ceil(span.total_seconds() / n)


def _overrides(store) -> dict:
    """The stored label setting. An entry that is not a label name or null (a setting written by
    hand, or before the PUT checked it) is left out, so that source keeps its default instead of
    the whole list failing on a name the event query refuses."""
    from .store import _FIELD_RE
    try:
        v = json.loads(store.get_setting(LABELS_SETTING) or "{}")
    except ValueError:
        return {}
    if not isinstance(v, dict):
        return {}
    return {k: lab for k, lab in v.items()
            if lab is None or (isinstance(lab, str) and _FIELD_RE.match(lab))}


def _iso(t) -> str | None:
    if t is None:
        return None
    t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    return t.isoformat()


def _later(a, b):
    return b if a is None or (b is not None and b > a) else a


def _compute(store, catalog, now) -> dict:
    """Every service with its feeds and the numbers behind them: name -> a working record."""
    now = now or now_utc()
    since_10m, since_24h = now - timedelta(minutes=10), now - timedelta(hours=24)
    settings = store.service_settings()
    overrides = _overrides(store)
    spec, svc_sources = {}, {}
    for s in catalog.sources.values():
        label = service_label(s, overrides)
        if label:
            spec[s.name] = svc_sources[s.name] = label
    # a repo's deploys belong to the service that lists it
    repo_owner = {}
    for raw, st in settings.items():
        for repo in st["repos"]:
            repo_owner[repo] = canonical(raw, settings)
    deploy_sources = {}
    if repo_owner:
        for s in catalog.sources.values():
            if s.connector in DEPLOY_CONNECTORS and s.name not in spec:
                deploy_sources[s.name] = primary_label(s) or "repo"
    spec.update(deploy_sources)
    rows = store.service_window_counts(spec, since_10m, since_24h)

    recs: dict[str, dict] = {}

    def rec(name):
        return recs.setdefault(name, {"feeds": {}, "ever": set(), "labels": {}})

    def feed(name, source, kind, n10, n24, last):
        f = rec(name)["feeds"].setdefault((source, kind), [0, 0, None])
        f[0] += n10
        f[1] += n24
        f[2] = _later(f[2], last)

    for r in rows:
        src = catalog.sources[r["source"]]
        if r["source"] in deploy_sources:
            owner = repo_owner.get(r["value"])
            if owner:
                feed(owner, r["source"], "deploys", r["n10"], r["n24"], r["last"])
            continue
        kind = signal_kind(src.connector, r["event_type"], "metric" if r["is_metric"] else None)
        feed(canonical(r["value"], settings), r["source"], kind, r["n10"], r["n24"], r["last"])
    # services silent for a day: last seen, and what feeds them, from the maintained counters
    for sname, label in spec.items():
        src = catalog.sources[sname]
        is_deploy = sname in deploy_sources
        for e in store.list_entities(label, sources=[sname], limit=10000):
            if is_deploy:
                name = repo_owner.get(e["value"])
                if not name:
                    continue
                kind = "deploys"
            else:
                name = canonical(e["value"], settings)
                kind = signal_kind(src.connector)
            rec(name)["ever"].add((sname, kind, e["last_ingest"]))
    return {"recs": recs, "settings": settings, "since_24h": since_24h,
            "svc_sources": svc_sources}


def _compute_cached(store, catalog, now) -> dict:
    """_compute, kept for CACHE_SECONDS so a busy cell does not scan its events on every page
    load. One entry per store serves the list (hidden or not) and every single service, because
    hiding is applied afterwards. It is dropped by every service write (Store.set_service) and
    is keyed by which sources hold services under which label, so a source added, removed or
    relabelled, or a changed sre.service_labels setting, shows at once. A call with a `now` of
    its own is never cached: that is a test or a replay asking about another moment."""
    if now is not None:
        return _compute(store, catalog, now)
    overrides = _overrides(store)
    key = tuple(sorted((s.name, s.connector, service_label(s, overrides) or "")
                       for s in catalog.sources.values()))
    cache = store.__dict__.setdefault("services_cache", {})
    hit = cache.get("entry")
    if hit and hit["key"] == key and cache_clock() - hit["at"] < CACHE_SECONDS:
        return hit["value"]
    gen = store.__dict__.get("services_gen", 0)
    value = _compute(store, catalog, None)
    if store.__dict__.get("services_gen", 0) == gen:   # no write while it ran
        store.__dict__.setdefault("services_cache", {})["entry"] = {
            "key": key, "at": cache_clock(), "value": value}
    return value


def _item(name: str, r: dict, settings: dict, include_hidden: bool) -> dict | None:
    st = settings.get(name) or {}
    if st.get("hidden") and not include_hidden:
        return None
    feeds = r["feeds"]
    n10 = sum(f[0] for f in feeds.values())
    n24 = sum(f[1] for f in feeds.values())
    last = None
    for f in feeds.values():
        last = _later(last, f[2])
    for _s, _k, t in r["ever"]:
        last = _later(last, t)
    if feeds:
        pairs = sorted((s, k) for (s, k), f in feeds.items() if f[1] > 0)
    else:
        pairs = []
    if not pairs:
        pairs = sorted({(s, k) for s, k, _t in r["ever"]})
    merged = sorted(n for n in settings if n != name and settings[n].get("merged_into")
                    and canonical(n, settings) == name)
    return {"name": name, "display": st.get("display") or name, "hidden": bool(st.get("hidden")),
            "merged": merged, "signals": sorted({k for _s, k in pairs}),
            "feeds": [{"name": s, "kind": k} for s, k in pairs],
            "events_10m": n10, "events_24h": n24, "last_seen": _iso(last)}


def list_services(store, catalog, now=None, include_hidden: bool = False) -> list[dict]:
    """The services the cell receives data for, busiest first (events in 24 hours, then name)."""
    c = _compute_cached(store, catalog, now)
    out = [i for n, r in c["recs"].items()
           if (i := _item(n, r, c["settings"], include_hidden)) is not None]
    out.sort(key=lambda i: (-i["events_24h"], i["name"]))
    found = roles(store, catalog)["watchers"]
    for i in out:
        _watched(store, catalog, i, found)
    return out


def get_service(store, catalog, name: str, now=None) -> dict | None:
    """One service (hidden ones too, and a merged name resolves to what it is merged into): the
    list item plus by_feed (the numbers per feed) and the label names seen on its events in 24
    hours. None when the cell knows no such service."""
    c = _compute_cached(store, catalog, now)
    name = canonical(name, c["settings"])
    r = c["recs"].get(name)
    if r is None:
        return None
    item = _item(name, r, c["settings"], True)
    _watched(store, catalog, item)
    ever = {}
    for sname, kind, t in r["ever"]:
        ever[(sname, kind)] = _later(ever.get((sname, kind)), t)
    item["by_feed"] = []
    for f in item["feeds"]:
        key = (f["name"], f["kind"])
        n = r["feeds"].get(key)
        last = n[2] if n else ever.get(key)
        item["by_feed"].append({**f, "events_10m": n[0] if n else 0,
                                "events_24h": n[1] if n else 0, "last_seen": _iso(last)})
    scope = {s: (label, [name, *item["merged"]]) for s, label in c["svc_sources"].items()}
    item["labels"] = store.service_label_names(scope, c["since_24h"], MAX_LABELS)
    return item


# ── who is watching (TR-743) ─────────────────────────────────────────────────
def _paused_projects(store) -> dict:
    return {p["id"]: p.get("status") == "paused" for p in store.list_projects()}


def roles(store, catalog, include_paused: bool = False) -> dict:
    """The jobs the agents of this cell do, from how they are wired.

    watchers: [{agent, project, trigger, enabled, paused, trigger_paused}], one per wake of an
    agent by a scheduled trigger. By default only the ones that can run: the wake is on and neither
    its trigger nor its project is paused. With `include_paused` the ones that cannot are listed
    too, and the three flags say why (`paused` is the project's).
    investigators: [{agent, project}], the agents a watcher hands a verdict to, and the agents
    woken by a trigger that reads only findings."""
    from .schedule import is_scheduled
    triggers = {t.name: t for t in catalog.triggers}
    agents = {a["name"] for a in store.list_catalog_agents()}
    paused = _paused_projects(store)
    watchers, investigators = [], []

    def add_investigator(agent, project):
        if agent in agents and not any(i["agent"] == agent and i["project"] == project
                                       for i in investigators):
            investigators.append({"agent": agent, "project": project})

    for w in store.list_wakes():
        trig = triggers.get(w["trigger"])
        if trig is None or w["agent"] not in agents:
            continue
        project_paused = bool(paused.get(w["project"]))
        runnable = w["enabled"] and not trig.paused and not project_paused
        if not (runnable or include_paused):
            continue
        if is_scheduled(trig) and trig.sources:
            watchers.append({"agent": w["agent"], "project": w["project"], "trigger": trig.name,
                             "enabled": bool(w["enabled"]), "paused": project_paused,
                             "trigger_paused": bool(trig.paused)})
        elif list(trig.sources) == [FINDINGS_SOURCE]:
            add_investigator(w["agent"], w["project"])
    for w in list(watchers):
        for h in store.list_handoffs(project=w["project"], from_agent=w["agent"]):
            add_investigator(h["agent"], w["project"])
    # an investigator whose own wake of the schedule is off is wiring left over, not a watcher
    # that was turned off
    inv = {(i["agent"], i["project"]) for i in investigators}
    watchers = [w for w in watchers if w["enabled"] or (w["agent"], w["project"]) not in inv]
    return {"watchers": watchers, "investigators": investigators}


def _excluded(trig, src, names: list[str], overrides: dict) -> bool:
    """Whether a filter of `trig` leaves out the service whose names (itself and what is merged
    into it) are `names`, when it is fed by source `src`. Only a filter on the label that names
    services in that source says anything about a service: eq or in with other values leave it
    out, and so does neq with this service's own name."""
    label = service_label(src, overrides)
    for f in trig.filters or []:
        if not label or f.get("field") != label:
            continue
        op, val = f.get("op"), f.get("value")
        if op == "eq" and str(val) not in names:
            return True
        if op == "neq" and str(val) == names[0]:
            return True
        # the runtime matches `in` and `contains` without regard to case
        if op == "in" and isinstance(val, list) and not {str(v).lower() for v in val} & {n.lower() for n in names}:
            return True
        if op == "contains" and not any(str(val).lower() in n.lower() for n in names):
            return True
    return False


def covers(store, catalog, service_item: dict, watchers: list | None = None) -> list[dict]:
    """The watchers whose trigger reads a feed of this service and does not filter it out, from
    how they are set up (configuration only: whether they can run is tares/watch.py's question).
    `watchers` is a roles() list when the caller has one, else the ones that can run."""
    if watchers is None:
        watchers = roles(store, catalog)["watchers"]
    triggers = {t.name: t for t in catalog.triggers}
    overrides = _overrides(store)
    feeds = {f["name"] for f in service_item.get("feeds") or []}
    names = [service_item["name"], *(service_item.get("merged") or [])]
    out = []
    for w in watchers:
        trig = triggers.get(w["trigger"])
        if trig is None:
            continue
        for sname in trig.sources:
            src = catalog.sources.get(sname)
            if sname in feeds and src is not None and not _excluded(trig, src, names, overrides):
                out.append(w)
                break
    return out


def _watched(store, catalog, item: dict, watchers: list | None = None) -> None:
    """Add `watched` and `watched_by` to a service item."""
    by = covers(store, catalog, item, watchers)
    seen = []
    for w in by:
        pair = {"agent": w["agent"], "project": w["project"]}
        if pair not in seen:
            seen.append(pair)
    item["watched_by"] = seen
    item["watched"] = bool(seen)


# ── what a person changes ────────────────────────────────────────────────────
PATCH_FIELDS = ("display", "hidden", "merged_into")
MAX_DISPLAY = 100
MAX_NAME = 200


def check_merge(settings: dict, name: str, target: str) -> None:
    """Refuse a merge of `name` into `target` that would lose or hide data. A service can only be
    merged into one that is not merged elsewhere, and the chain it makes must stay within MAX_HOPS
    hops, or the services at its far end would show up as separate rows."""
    if target == name:
        raise ValueError(f"{name} cannot be merged into itself: pick a different service")
    onward = (settings.get(target) or {}).get("merged_into")
    if onward:
        raise ValueError(f"{target} is already merged into {onward}, so {name} cannot be merged "
                         f"into it: merge {name} into {onward} instead")
    links = {n: st["merged_into"] for n, st in settings.items() if st.get("merged_into")}
    links[name] = target
    for start in links:
        cur, hops = start, 0
        while cur in links:
            cur, hops = links[cur], hops + 1
            if hops > MAX_HOPS or cur == start:
                raise ValueError(f"merging {name} into {target} makes a chain of more than "
                                 f"{MAX_HOPS} services: merge {name} into the last one instead")


def _text(body: dict, key: str, limit: int) -> str | None:
    v = body[key]
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text or null, and it was {type(v).__name__}")
    v = v.strip()
    if len(v) > limit:
        raise ValueError(f"{key} is {len(v)} characters long and can be at most {limit}")
    return v or None


def patch_service(store, catalog, name: str, body, by: str) -> dict:
    """Change one service's display name, hidden flag or merge, and return it as get_service
    does. A field left out stays as it is, null clears it. ValueError (400) when the body is
    refused, LookupError (404) when the cell knows no such service. The body is checked before
    the name, so a refused merge says why whatever the name is."""
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object such as {\"hidden\": true}")
    unknown = sorted(set(body) - set(PATCH_FIELDS))
    if unknown:
        raise ValueError(f"cannot change {', '.join(unknown)}: a service takes "
                         + ", ".join(PATCH_FIELDS))
    if not body:
        raise ValueError("nothing to change: give one of " + ", ".join(PATCH_FIELDS))
    name = str(name or "").strip()
    if not name or len(name) > MAX_NAME:
        raise ValueError("the service name is empty or too long")
    changes: dict = {}
    if "display" in body:
        changes["display"] = _text(body, "display", MAX_DISPLAY)
    if "hidden" in body:
        if body["hidden"] is not None and not isinstance(body["hidden"], bool):
            raise ValueError("hidden must be true, false or null")
        changes["hidden"] = bool(body["hidden"])
    settings = store.service_settings()
    if "merged_into" in body:
        target = _text(body, "merged_into", MAX_NAME)
        if target is not None:
            check_merge(settings, name, target)
        changes["merged_into"] = target
    # the same test GET /api/services/{name} applies, so a name that answers 404 there does here
    if canonical(name, settings) not in _compute_cached(store, catalog, None)["recs"]:
        raise LookupError(f"no service named {name}")
    store.set_service(name, by, **changes)
    return get_service(store, catalog, name)


def set_service_labels(store, catalog, body) -> dict:
    """Store which label names services in which source: {source name: label name, or null for a
    source that holds no services}. The mapping replaces the one stored. ValueError (400) for a
    source the catalog does not have or a label name that is not letters, digits and underscores."""
    from .store import _FIELD_RE
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object such as {\"prod-otlp\": \"namespace\"}")
    unknown = sorted(s for s in body if s not in catalog.sources)
    if unknown:
        raise ValueError(f"no source named {', '.join(unknown)}: use the names of the sources "
                         "in this cell")
    out = {}
    for source, label in body.items():
        if label is None:
            out[source] = None
        elif isinstance(label, str) and _FIELD_RE.match(label):
            out[source] = label
        else:
            raise ValueError(f"the label for {source} must be a label name (letters, digits and "
                             "underscores) or null for a source that holds no services")
    store.set_setting(LABELS_SETTING, json.dumps(out))
    store.clear_services_cache()
    return out
