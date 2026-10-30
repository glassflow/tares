"""The services an AI SRE cell knows (TR-742).

A service is not a record somebody keeps: it is a value of a source's service label (the label
named `service`, else the source's primary label), counted over the last 24 hours. What a person
changes about one (display name, hidden, merged into another, repos whose deploys belong to it)
lives in the `services` table (tares/sre_store.py) and is applied here, in Python, over the counts.
"""
from __future__ import annotations

import json
from datetime import timedelta, timezone

from .config import primary_label
from .envelope import now_utc

LABELS_SETTING = "sre.service_labels"   # JSON {source name: label name, or null for not a service}
NOT_SERVICES = {"finding", "memory", "claude_code", "reference", "github", "github_app",
                "github_issues", "linear"}
DEPLOY_CONNECTORS = ("github", "github_app")
MAX_HOPS = 5
MAX_LABELS = 20


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


def _overrides(store) -> dict:
    try:
        v = json.loads(store.get_setting(LABELS_SETTING) or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


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
    c = _compute(store, catalog, now)
    out = [i for n, r in c["recs"].items()
           if (i := _item(n, r, c["settings"], include_hidden)) is not None]
    out.sort(key=lambda i: (-i["events_24h"], i["name"]))
    return out


def get_service(store, catalog, name: str, now=None) -> dict | None:
    """One service (hidden ones too, and a merged name resolves to what it is merged into): the
    list item plus by_feed (the numbers per feed) and the label names seen on its events in 24
    hours. None when the cell knows no such service."""
    c = _compute(store, catalog, now)
    name = canonical(name, c["settings"])
    r = c["recs"].get(name)
    if r is None:
        return None
    item = _item(name, r, c["settings"], True)
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
