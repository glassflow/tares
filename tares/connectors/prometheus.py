"""Prometheus connector — polls the instant-query API, one Envelope per result series.

Stores every non-null sample (lossless); the view and triggers decide what's interesting.

`discover()` is deterministic (no LLM) and drives a pick-don't-write flow: the user chooses *which
metrics* to ingest (by name pattern or by label) and *which labels* become Tares labels — never
writing PromQL. discover has three modes, keyed off its input:
  - catalog (default): the metric-name list + the label-name list, for the pickers.
  - for_label="<label>": the metric names that carry that label (the by-label picker).
  - selected=[names]: finalize — sample the picked metrics and propose a ready config.
The proposed config's poll query is a single `{__name__=~"(a|b|…)"}` selector with `by_name`, so one
query ingests the whole basket, and the metric value rides along as a number-typed `value` label so
triggers can aggregate it.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from ..envelope import Envelope, now_utc
from .base import Connector

_BAD = ("NaN", "+Inf", "-Inf")

# Prometheus/runtime internals hidden from the catalog by default (still ingestable if typed).
_INTERNAL = re.compile(r"^(go_|process_|prometheus_|scrape_|net_|promhttp_|python_)")
# Labels that make a good entity key, best first. The first present with >1 value wins.
_KEY_PRIORITY = ["service", "tenant", "tenant_id", "namespace", "pod", "app", "instance", "job"]
# Structural labels we don't propose as Tares labels, and never as a key.
_LABEL_SKIP = {"le", "instance", "job", "__name__"}

# Finalize samples at most this many of the picked metrics (concurrently) to learn their label set —
# the basket shares its label keys, so a few dozen reveal the union without sampling all of them.
_SAMPLE_CAP = 40
_SAMPLE_CONCURRENCY = 12
_TOPK = 5   # topk(N) bounds each sample's response to N series (label keys are shared across series)


def _client_kwargs(config: dict) -> dict:
    """httpx.AsyncClient kwargs for the endpoint's auth: a bearer token (Authorization header) and/or
    HTTP basic auth. Both are stored as secrets — redacted in the API and omitted from exports."""
    kw: dict = {}
    if config.get("bearer_token"):
        kw["headers"] = {"Authorization": f"Bearer {config['bearer_token']}"}
    if config.get("username"):
        kw["auth"] = (config["username"], config.get("password") or "")
    return kw


class PrometheusConnector(Connector):
    # The authoritative config schema — the source of truth every entry path (form, discover,
    # YAML, API) normalizes to. `SPECS["prometheus"].fields` is generated from it.
    CONFIG_SCHEMA = {
        "url": {"type": "string", "required": True, "format": "url", "discover_input": True,
                "help": "Prometheus base URL, e.g. http://localhost:9090"},
        "bearer_token": {"type": "string", "secret": True, "discover_input": True,
                         "help": "optional Authorization: Bearer token; for managed Prometheus "
                                 "(Thanos/Cortex/Mimir) or one behind an auth proxy"},
        "username": {"type": "string", "discover_input": True,
                     "help": "optional HTTP basic-auth username (e.g. a Grafana Cloud instance id)"},
        "password": {"type": "string", "secret": True, "discover_input": True,
                     "help": "optional HTTP basic-auth password / API token, paired with username"},
        "default_key": {"type": "string", "default": "unknown",
                        "help": "entity key for series that carry no key label"},
        "queries": {
            "type": "list", "required": True,
            "help": "metrics to ingest; each a PromQL query mapped into the envelope",
            "item": {
                "promql": {"type": "string", "required": True,
                           "help": "PromQL (a bare metric name = raw, lossless ingest)"},
                "event_type": {"type": "string", "default": "metric"},
                "field": {"type": "string", "default": "value",
                          "help": "fields.<name> the numeric value is stored under"},
                "text": {"type": "string", "default": "{key} {field}={val}"},
                "key_label": {"type": "string",
                              "help": "series label to read the entity key from, e.g. service"},
                "by_name": {"type": "bool",
                            "help": "whole-basket query ({__name__=~\"(a|b)\"}): derive event_type "
                                    "& field from each series' own metric name"},
                "exclude": {"type": "string",
                            "help": "comma-separated substrings to drop by metric name (e.g. _bucket)"},
            },
        },
    }

    async def poll(self):
        url = self.cfg.config["url"].rstrip("/") + "/api/v1/query"
        default_key = self.cfg.config.get("default_key", "unknown")
        out = []
        async with httpx.AsyncClient(timeout=15, **_client_kwargs(self.cfg.config)) as cx:
            for q in self.cfg.config.get("queries", []):
                try:
                    # POST (not GET): a whole-basket selector can be a long regex — past URL limits.
                    resp = await cx.post(url, data={"query": q["promql"]})
                    series = resp.json().get("data", {}).get("result", [])
                except Exception:
                    continue
                key_label = q.get("key_label")
                by_name = q.get("by_name", False)
                excludes = [e.strip() for e in str(q.get("exclude", "")).split(",") if e.strip()]
                for s in series:
                    raw = s.get("value", [None, None])[1]
                    if raw in _BAD or raw is None:
                        continue
                    val = float(raw)
                    metric = s.get("metric", {})
                    mname = metric.get("__name__", "")
                    # whole-basket query: event_type & field come from each series' own metric name,
                    # so one selector ingests the whole basket with per-metric names.
                    if by_name:
                        if any(x in mname for x in excludes):
                            continue
                        event_type = mname or q.get("event_type", "metric")
                        field = mname or q.get("field", "value")
                    else:
                        event_type = q.get("event_type", "metric")
                        field = q.get("field", "value")
                    fallback = metric.get(key_label, default_key) if key_label else default_key
                    payload = {"metric": metric, "value": val, "promql": q["promql"]}
                    # extract labels from the same context retroactive relabel uses (label_context),
                    # so a `metric.<label>` field — as the field profile shows it — resolves both now
                    # and on backfill. The numeric `value` label is read from payload.value here.
                    labels, key = self.keyed(self.label_context(payload), fallback=fallback)
                    try:
                        text = q.get("text", "{key} {field}={val}").format(
                            key=key, field=field, val=round(val, 3), event_type=event_type, **metric
                        )
                    except (KeyError, IndexError):
                        text = f"{key} {field}={round(val, 3)}"
                    out.append(Envelope(
                        source=self.cfg.name, source_type=self.cfg.type, key_value=key,
                        event_type=event_type, text=text, event_time=now_utc(),
                        payload=payload, labels=labels,  # the series' labels; the primary one is the key
                    ))
        return out

    def label_context(self, payload: dict | None) -> dict:
        """Prometheus labels are declared against the field profile, which shows series labels nested
        as `metric.<label>` (e.g. metric.service, metric.table) plus a `value`. Expose the payload so
        a dotted `metric.<label>` field and the bare `value` field both resolve, and merge the bare
        series labels on top so an older source that declared `field: service` (bare) keeps working.
        Used at ingest AND on relabel, so both reproduce identical labels."""
        p = payload or {}
        metric = p.get("metric") if isinstance(p.get("metric"), dict) else {}
        return {**(metric or {}), **p}

    # ── discover (deterministic; three modes off its input) ───────────────────
    @classmethod
    async def discover(cls, config: dict) -> dict:
        base = config["url"].rstrip("/")
        for_label = str(config.get("for_label") or "").strip()
        selected = [str(m) for m in (config.get("selected") or []) if str(m).strip()]
        async with httpx.AsyncClient(timeout=20, **_client_kwargs(config)) as cx:
            async def api(path, **params):
                return (await cx.get(f"{base}{path}", params=params)).json().get("data", {})

            async def query(promql):
                r = await cx.post(f"{base}/api/v1/query", data={"query": promql})
                return r.json().get("data", {}).get("result", [])

            # MODE: metrics that carry one label — the by-label picker. One cheap query.
            if for_label:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", for_label):
                    raise ValueError(f"bad label name {for_label!r}")
                res = await query(f'group by (__name__) ({{{for_label}!=""}})')
                names = sorted({s.get("metric", {}).get("__name__", "") for s in res} - {""})
                return {"connector": "prometheus", "for_label": for_label, "metrics_for_label": names}

            # MODE: finalize — sample the picked metrics and propose a ready config.
            if selected:
                return await cls._finalize(base, cx, api, query, config, selected)

            # MODE: catalog (default) — the metric-name list + the label-name list for the pickers.
            try:
                metrics = await api("/api/v1/label/__name__/values") or []
                labels = await api("/api/v1/labels") or []
            except Exception as e:
                raise ValueError(f"could not reach Prometheus at {base}: {e}")
            # drop recording rules (`:`) — a {__name__=~"…"} query can't match them anyway.
            metrics = sorted(m for m in metrics if ":" not in m)
            labels = sorted(l for l in labels if ":" not in l and l != "__name__")
            return {"connector": "prometheus", "catalog": {"metrics": metrics, "labels": labels}}

    @classmethod
    async def _finalize(cls, base, cx, api, query, config, selected: list) -> dict:
        """Sample the picked metrics for their label union + a suggested key, and build a ready
        config: one whole-basket selector, the promoted labels, and the numeric `value` measurement."""
        sem = asyncio.Semaphore(_SAMPLE_CONCURRENCY)

        async def sample(n):
            async with sem:
                try:
                    r = await cx.post(f"{base}/api/v1/query", data={"query": f"topk({_TOPK}, {n})"})
                    res = r.json().get("data", {}).get("result", [])
                except Exception:
                    return [], None, "gauge"
            labels = sorted({k for s in res for k in s.get("metric", {}) if k != "__name__"})
            val = res[0]["value"][1] if res else None
            mtype = "counter" if n.endswith("_total") else ("histogram" if n.endswith("_bucket") else "gauge")
            return labels, val, mtype

        sampled = selected[:_SAMPLE_CAP]
        results = await asyncio.gather(*(sample(n) for n in sampled))
        label_union = set()
        preview = []
        for n, (labels, val, mtype) in zip(sampled, results):
            label_union |= set(labels)
            preview.append({"name": n, "type": mtype, "sample": val, "labels": labels})

        # suggested key: first priority label present, preferring one with >1 distinct value
        present = [l for l in _KEY_PRIORITY if l in label_union]
        key, key_card, key_vals = None, 0, []
        for l in present:
            vals = await api(f"/api/v1/label/{l}/values") or []
            if key is None or (len(vals) > 1 and key_card <= 1):
                key, key_card, key_vals = l, len(vals), vals
            if len(vals) > 1:
                break
        key = key or "instance"
        default_key = key_vals[0] if key_vals else "unknown"
        proposed_labels = sorted(label_union - _LABEL_SKIP - {key})

        # one whole-basket query: an anchored alternation of the picked names, by_name so each
        # series is named by its own metric. Metric names are [A-Za-z_:][A-Za-z0-9_:]* — no regex
        # metachars — so a bare join is safe; still guard against anything unexpected.
        safe = [n for n in selected if re.match(r"^[A-Za-z_:][A-Za-z0-9_:]*$", n)]
        selector = '{__name__=~"(' + "|".join(safe) + ')"}'
        proposed_config = {
            "url": base, "default_key": default_key,
            "queries": [{"promql": selector, "by_name": True}],
            # labels reference the field profile's nested path (metric.<label>), matching what the
            # field picker shows; `value` is a number-typed measurement (aggregatable, not faceted).
            "labels": [{"name": key, "field": f"metric.{key}", "primary": True}]
                      + [{"name": l, "field": f"metric.{l}"} for l in proposed_labels]
                      + [{"name": "value", "field": "value", "type": "number"}],
        }
        return {
            "connector": "prometheus",
            "selected_count": len(selected),
            "suggested_key": {"name": key, "cardinality": key_card,
                              "values_preview": key_vals[:8],
                              "alternatives": [l for l in present if l != key]},
            "proposed_labels": proposed_labels,
            "preview": preview,
            "proposed_config": proposed_config,
        }
