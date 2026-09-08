"""Prometheus alerts connector — polls the alerting API and turns each active alert into an event.

Prometheus evaluates its own alerting rules (a PromQL expression + a `for:` duration), so
`GET /api/v1/alerts` already holds the alerts that have *fired* — no Alertmanager, and no PromQL to
write here. We poll it and emit one event per active alert, keyed by a label (namespace / service).

State is tracked across polls (via the source cursor) so each alert fires *once* and emits a
`resolved` event when it clears — instead of re-emitting every active alert on every tick.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx

from ..envelope import Envelope, now_utc
from .base import Connector
from .prometheus import _client_kwargs


def _fingerprint(labels: dict) -> str:
    """Stable id for one alert instance = a hash of its full label set (alertname + all labels)."""
    return hashlib.sha1(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]


def _parse_time(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return now_utc()


class PrometheusAlertsConnector(Connector):
    # Authoritative config schema (SPECS fields generated from it). Auth mirrors the Prometheus
    # connector — same endpoint, same credentials.
    CONFIG_SCHEMA = {
        "url": {"type": "string", "required": True, "format": "url", "discover_input": True,
                "help": "Prometheus base URL, e.g. http://localhost:9090"},
        "bearer_token": {"type": "string", "secret": True, "discover_input": True,
                         "help": "optional Authorization: Bearer token; for managed Prometheus or "
                                 "one behind an auth proxy"},
        "username": {"type": "string", "discover_input": True,
                     "help": "optional HTTP basic-auth username"},
        "password": {"type": "string", "secret": True, "discover_input": True,
                     "help": "optional HTTP basic-auth password / API token, paired with username"},
        "severities": {"type": "string",
                       "help": "comma-separated severities to ingest (blank = all), e.g. critical,warning"},
        "include_pending": {"type": "bool",
                            "help": "also ingest alerts still `pending` (waiting out their for: duration)"},
        # Edge-case fallback key, used only when an alert lacks the primary label. Advanced (hidden
        # from the form, kept in schema so it round-trips) — the real key is the primary label.
        "default_key": {"type": "string", "default": "unknown", "advanced": True,
                        "help": "entity key for alerts whose key label is absent"},
    }

    async def poll(self):
        base = self.cfg.config["url"].rstrip("/")
        include_pending = self.cfg.config.get("include_pending", False)
        severities = [s.strip() for s in str(self.cfg.config.get("severities", "")).split(",") if s.strip()]
        try:
            async with httpx.AsyncClient(timeout=15, **_client_kwargs(self.cfg.config)) as cx:
                resp = await cx.get(f"{base}/api/v1/alerts")
                active = resp.json().get("data", {}).get("alerts", [])
        except Exception:
            return []

        # cursor state across polls: {fingerprint: {activeAt, state, labels, annotations}}. Emit each
        # alert once (on fire / on state change) and a `resolved` event when it disappears.
        prev = {}
        raw = self.store.get_cursor(self.cfg.name) if self.store else None
        if raw:
            try:
                prev = json.loads(raw)
            except (ValueError, TypeError):
                prev = {}

        current, out = {}, []
        for a in active:
            state = a.get("state", "firing")
            if state == "pending" and not include_pending:
                continue
            if state not in ("firing", "pending"):
                continue
            labels_in = a.get("labels", {}) or {}
            if severities and labels_in.get("severity") not in severities:
                continue
            anns = a.get("annotations", {}) or {}
            active_at = str(a.get("activeAt", ""))
            fp = _fingerprint(labels_in)
            current[fp] = {"activeAt": active_at, "state": state, "labels": labels_in, "annotations": anns}
            prior = prev.get(fp)
            if prior and prior.get("activeAt") == active_at and prior.get("state") == state:
                continue  # already emitted this episode — don't re-emit every tick
            out.append(self._event(labels_in, anns, state, _parse_time(active_at)))

        # resolved: was active last poll, gone now (use the labels we stored for it)
        for fp, info in prev.items():
            if fp not in current:
                out.append(self._event(info.get("labels", {}), info.get("annotations", {}),
                                       "resolved", now_utc()))

        if self.store:
            self.store.set_cursor(self.cfg.name, json.dumps(current))
        return out

    def _event(self, labels_in, anns, state, event_time) -> Envelope:
        alertname = labels_in.get("alertname", "alert")
        summary = anns.get("summary") or anns.get("description") or alertname
        payload = {"labels": labels_in, "annotations": anns, "state": state}
        labels, key = self.keyed(self.label_context(payload),
                                 fallback=self.cfg.config.get("default_key", "unknown"))
        return Envelope(
            source=self.cfg.name, source_type=self.cfg.type, key_value=key,
            event_type=alertname, text=f"{state.upper()}: {alertname} · {summary}",
            event_time=event_time, payload=payload, labels=labels,
        )

    def label_context(self, payload: dict | None) -> dict:
        """The alert's labels are the correlation axes. Expose them bare (field: namespace) and nested
        (field: labels.namespace, as the field profile shows), plus the synthetic `state`. Used at
        ingest AND on relabel, so both reproduce identical labels."""
        p = payload or {}
        labels = p.get("labels") if isinstance(p.get("labels"), dict) else {}
        return {**(labels or {}), "state": p.get("state"), **p}

    @classmethod
    async def discover(cls, config: dict) -> dict:
        """List the configured alerting rules (for transparency + the severity filter). We do NOT
        guess the entity labels: an alert's dynamic labels (namespace, service, pod) only exist while
        it's firing, so guessing from a momentary snapshot is unreliable. We propose only the labels
        guaranteed to be present — `severity` and the synthetic `state` — and leave the rest to the
        user to add once real alerts have been ingested and the field list shows what they carry."""
        base = config["url"].rstrip("/")
        active_by_name: dict = {}
        rules_out = []
        async with httpx.AsyncClient(timeout=15, **_client_kwargs(config)) as cx:
            try:
                active = (await cx.get(f"{base}/api/v1/alerts")).json().get("data", {}).get("alerts", [])
            except Exception as e:
                raise ValueError(f"could not reach Prometheus at {base}: {e}")
            for a in active:
                lbl = a.get("labels") or {}
                active_by_name[lbl.get("alertname")] = a.get("state", "firing")
            try:
                groups = (await cx.get(f"{base}/api/v1/rules")).json().get("data", {}).get("groups", [])
                for g in groups:
                    for r in g.get("rules", []):
                        if r.get("type") != "alerting":
                            continue
                        name = r.get("name", "")
                        rules_out.append({
                            "name": name, "severity": (r.get("labels") or {}).get("severity", ""),
                            "group": g.get("name", ""),
                            # live state wins (firing/pending) over the rule's resting state
                            "state": active_by_name.get(name, r.get("state", "inactive")),
                        })
            except Exception:
                pass

        # Only the guaranteed labels: severity (on every rule/alert) keyed as primary, and the
        # synthetic state. Everything else is per-alert and added by the user after ingest.
        labels = [{"name": "severity", "field": "labels.severity", "primary": True},
                  {"name": "state", "field": "state"}]
        severities = sorted({r["severity"] for r in rules_out if r["severity"]})
        firing = sum(1 for s in active_by_name.values() if s == "firing")
        return {
            "connector": "prometheus_alerts",
            "rules": sorted(rules_out, key=lambda r: (r["severity"], r["name"])),
            "severities": severities,
            "summary": f"{len(rules_out)} alert rules · {firing} firing now",
            "proposed_config": {"url": base, "default_key": "unknown", "labels": labels},
        }
