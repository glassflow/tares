"""The services an AI SRE cell knows (TR-742).

A service is not a record somebody keeps: it is a value of a source's service label (the label
named `service`, else the source's primary label), counted over the last 24 hours. What a person
changes about one (display name, hidden, merged into another, repos whose deploys belong to it)
lives in the `services` table (tares/sre_store.py) and is applied here, in Python, over the counts.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta, timezone

from .config import primary_label
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
