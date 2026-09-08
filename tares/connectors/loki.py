"""Grafana Loki connector — polls a LogQL stream selector via query_range, one Envelope per line.

The pull-based counterpart of docker_logs for logs that live in a Loki (or a Loki-compatible
endpoint): the cell polls with a timestamp cursor instead of tailing a local container, so it
works against any reachable Loki — a customer's own, Grafana Cloud, or a hosted demo stack.

Ingests all matched lines by default (lossless; reads/triggers decide what's interesting).
Optional match/drop regex filters narrow it, same semantics as docker_logs. Lines get the same
format-agnostic derived fields (level, http method/status, JSON scalars) as docker_logs, so a
view over either source sees the same shape.

Cursor is the last-seen entry timestamp in nanoseconds (Loki's native unit); the next poll asks
from cursor+1 so the boundary entry is never re-ingested.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from ..envelope import Envelope, now_utc
from .base import Connector
from .docker_logs import derive_fields

# Cap how many lines one poll ingests: after downtime the range query can return a huge backlog;
# take at most this many, advance the cursor to the last consumed entry, and drain over polls.
MAX_PER_POLL = 5000
# First poll looks back this far (mirrors docker_logs's "30s" initial --since).
INITIAL_LOOKBACK_NS = 30 * 1_000_000_000


def _client_kwargs(config: dict) -> dict:
    """httpx client kwargs for the endpoint's auth: bearer and/or basic, plus Loki's tenant
    header. Same shape as the prometheus connector so the two read identically."""
    headers: dict = {}
    if config.get("bearer_token"):
        headers["Authorization"] = f"Bearer {config['bearer_token']}"
    if config.get("tenant_id"):
        headers["X-Scope-OrgID"] = str(config["tenant_id"])
    kw: dict = {"headers": headers} if headers else {}
    if config.get("username"):
        kw["auth"] = (config["username"], config.get("password") or "")
    return kw


class LokiConnector(Connector):
    CONFIG_SCHEMA = {
        "url": {"type": "string", "required": True, "format": "url",
                "help": "Loki base URL, e.g. http://localhost:3100"},
        "query": {"type": "string", "required": True,
                  "help": 'LogQL stream selector, e.g. {service="api-server"}'},
        "bearer_token": {"type": "string", "secret": True,
                         "help": "optional Authorization: Bearer token"},
        "username": {"type": "string",
                     "help": "optional HTTP basic-auth username (e.g. a Grafana Cloud user id)"},
        "password": {"type": "string", "secret": True,
                     "help": "optional basic-auth password / API token, paired with username"},
        "tenant_id": {"type": "string",
                      "help": "optional X-Scope-OrgID header for multi-tenant Loki"},
        "match": {"type": "string",
                  "help": "keep only lines matching this regex (default: all lines), e.g. ERROR|WARN"},
        "drop": {"type": "string",
                 "help": "skip lines matching this regex, e.g. 'HTTP/1.1\"' to drop access logs"},
        "key": {"type": "string", "advanced": True,
                "help": "legacy fixed key; prefer a primary label"},
    }

    def label_context(self, payload: dict | None) -> dict:
        """A line's context from its stored payload: the stream's labels plus the same derived
        fields docker_logs extracts. Used at ingest AND on retroactive relabel, so both see the
        identical structure."""
        p = payload or {}
        stream = p.get("stream") if isinstance(p.get("stream"), dict) else {}
        return {**(stream or {}), **derive_fields(str(p.get("raw", "")))}

    async def poll(self):
        c = self.cfg.config
        base = c["url"].rstrip("/")
        key_fallback = c.get("key", "unknown")
        match_re = re.compile(c["match"], re.I) if c.get("match") else None
        drop_re = re.compile(c["drop"], re.I) if c.get("drop") else None

        cursor = self.store.get_cursor(self.cfg.name)
        now_ns = int(now_utc().timestamp() * 1_000_000_000)
        start_ns = int(cursor) + 1 if cursor else now_ns - INITIAL_LOOKBACK_NS

        try:
            async with httpx.AsyncClient(timeout=15, **_client_kwargs(c)) as cx:
                r = await cx.get(f"{base}/loki/api/v1/query_range", params={
                    "query": c["query"], "start": str(start_ns), "end": str(now_ns),
                    "direction": "forward", "limit": MAX_PER_POLL,
                })
                streams = r.json().get("data", {}).get("result", [])
        except Exception:
            return []   # an unreachable Loki is a quiet poll, like every other poll connector

        # Merge all streams' entries into one time-ordered pass: the cursor is global for the
        # source, so it must only ever advance over lines actually consumed.
        entries = []
        for s in streams:
            stream_labels = s.get("stream") or {}
            for ts, line in s.get("values") or []:
                entries.append((int(ts), str(line), stream_labels))
        entries.sort(key=lambda e: e[0])

        out = []
        max_ts = None
        for ts_ns, line, stream_labels in entries:
            if ts_ns < start_ns:
                continue
            max_ts = ts_ns
            msg = line.strip()
            if match_re and not match_re.search(msg):
                continue
            if drop_re and drop_re.search(msg):
                continue
            event_time = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
            ctx = {**stream_labels, **derive_fields(msg)}
            labels, key = self.keyed(ctx, fallback=key_fallback)
            out.append(Envelope(
                source=self.cfg.name, source_type=self.cfg.type, key_value=key,
                event_type="log", text=msg[:300], event_time=event_time,
                payload={"raw": line, "stream": stream_labels}, labels=labels,
            ))
            if len(out) >= MAX_PER_POLL:
                break   # leave the rest; cursor is at this entry
        if max_ts is not None:
            self.store.set_cursor(self.cfg.name, str(max_ts))
        return out
