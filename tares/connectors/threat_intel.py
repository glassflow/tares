"""Threat-intel feed connector. Polls a JSON indicator feed (the normalized shape a MISP, OTX, or
AbuseIPDB export flattens to: indicator, indicator_type, threat_type, confidence, source,
first_seen) and emits one Envelope per indicator, keyed by the indicator value itself (an IP,
domain, or hash).

Point this at the same feed a SOC already consumes and it lands on the SAME timeline as everything
else keyed by that indicator, a webhook source logging auth attempts by IP, a Postgres table of
account activity by user, whatever else is already flowing in. An agent reading `read(ip)` then
sees "5 failed logins in the last minute" and "this IP is a known credential-stuffing proxy,
confidence 92" as one correlated read, instead of separately calling an auth-log tool and a
threat-intel lookup tool.

Normalization happens once, in `label_context`, and both `poll` and any relabel/backfill path
build labels and the entity key from its output, so a live poll and a retroactive relabel agree on
what a given stored payload means (the base class asks for this; see its docstring).

Cursor is a fixed-size bookmark, not a set that grows with the feed: the newest `first_seen` value
observed plus the indicator values tied at that exact timestamp, so a poll only has to ask "is this
item newer than the bookmark, or new at the bookmark's own timestamp." Real feeds carry tens of
thousands of entries and turn over daily; storing every indicator ever seen would mean megabytes
rewritten on every poll. If a feed carries no `first_seen` at all, cursor falls back to a hash of
the fetched list: unchanged hash skips the poll, a changed hash re-emits the full feed, since
there's no timestamp to determine what's actually new.

config:
  feed_url: https://example.com/iocs.json   # a JSON array of indicator objects, or
  feed_path: /etc/tares/iocs.json            # a local file (self-hosted feeds, no network dependency)
  token: <bearer token>                      # optional, sent as `Authorization: Bearer <token>`
  field_map:                                 # optional, only if the feed's field names differ from
    indicator: ioc                            # the standard shape (indicator/indicator_type/
    indicator_type: ioc_type                  # threat_type/confidence/source/first_seen)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from ..envelope import Envelope, now_utc
from .base import Connector

# standard field name -> the feed's own field name it maps from by default; field_map overrides
DEFAULT_SOURCE_FIELD = {
    "indicator": "indicator",
    "indicator_type": "type",
    "threat_type": "threat_type",
    "confidence": "confidence",
    "source": "source",
    "first_seen": "first_seen",
}

EVENT_TYPES = {"ip", "domain", "hash", "url"}


class ThreatIntelConnector(Connector):
    CONFIG_SCHEMA = {
        "feed_url": {"type": "string", "discover_input": True,
                     "help": "URL returning a JSON array of indicator objects. Set this or "
                             "feed_path, not both"},
        "feed_path": {"type": "string", "discover_input": True,
                      "help": "path to a JSON array of indicator objects, read from the machine "
                              "the daemon runs on (not your local machine, if the daemon runs "
                              "elsewhere, e.g. in the cloud)"},
        "token": {"type": "string", "secret": True,
                  "help": "bearer token for feed_url, if the feed requires auth"},
        "field_map": {"type": "json", "advanced": True,
                      "help": "rename source fields to the standard shape, e.g. "
                              '{"indicator": "ioc", "indicator_type": "ioc_type"}, only needed '
                              "when the feed doesn't already use indicator/indicator_type/"
                              "threat_type/confidence/source/first_seen"},
    }

    PROVIDES = [
        {"name": "indicator", "primary": True, "help": "the IOC value itself: an IP, domain, or hash"},
        {"name": "indicator_type", "help": "ip | domain | hash | url"},
        {"name": "threat_type", "help": "e.g. credential_stuffing_proxy, botnet_c2, known_scanner"},
    ]

    async def _fetch(self) -> list[dict]:
        c = self.cfg.config
        if c.get("feed_path"):
            return json.loads(Path(c["feed_path"]).read_text())
        if not c.get("feed_url"):
            raise ValueError("set feed_url or feed_path")
        headers = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
        async with httpx.AsyncClient(timeout=15) as cx:
            try:
                r = await cx.get(c["feed_url"], headers=headers)
            except Exception as e:
                raise ValueError(f"could not reach feed_url: {e}")
            if r.status_code != 200:
                raise ValueError(f"feed returned {r.status_code}")
            return r.json()

    def label_context(self, payload: dict | None) -> dict:
        """The one place a raw feed entry becomes the standard shape. Used by `poll` for live
        ingest and available to the base class's relabel/backfill path for stored payloads, so
        both agree on what a given entry means."""
        payload = payload or {}
        field_map = self.cfg.config.get("field_map") or {}
        out = {}
        for field, default_source in DEFAULT_SOURCE_FIELD.items():
            source_field = field_map.get(field, default_source)
            out[field] = payload.get(source_field)
        return out

    async def poll(self) -> list[Envelope]:
        raw = await self._fetch()
        if not isinstance(raw, list):
            raise ValueError("feed must be a JSON array of indicator objects")
        items = [it for it in raw if isinstance(it, dict)]

        cursor_raw = self.store.get_cursor(self.cfg.name)
        cursor = json.loads(cursor_raw) if cursor_raw else None

        new_items = self._select_new(items, cursor)
        new_cursor = self._build_cursor(items)
        if new_cursor is not None:
            self.store.set_cursor(self.cfg.name, json.dumps(new_cursor))

        return [self._envelope(item) for item in new_items]

    def _select_new(self, items: list[dict], cursor: dict | None) -> list[dict]:
        if cursor is None:
            return items  # first poll: everything is new
        if cursor.get("mode") == "hash":
            return [] if cursor.get("hash") == self._feed_hash(items) else items
        # timestamp-bookmark mode
        max_seen = cursor.get("max_first_seen")
        at_max = set(cursor.get("at_max") or [])
        out = []
        for item in items:
            ctx = self.label_context(item)
            fs, indicator = ctx.get("first_seen"), ctx.get("indicator")
            if fs is None or indicator is None:
                continue
            if fs > max_seen or (fs == max_seen and str(indicator) not in at_max):
                out.append(item)
        return out

    def _build_cursor(self, items: list[dict]) -> dict | None:
        contexts = [self.label_context(it) for it in items]
        if items and all(c.get("first_seen") for c in contexts):
            max_seen = max(c["first_seen"] for c in contexts)
            at_max = sorted({str(c["indicator"]) for c in contexts if c["first_seen"] == max_seen})
            return {"mode": "timestamp", "max_first_seen": max_seen, "at_max": at_max}
        if items:
            return {"mode": "hash", "hash": self._feed_hash(items)}
        return None  # empty feed: leave any existing cursor as-is

    @staticmethod
    def _feed_hash(items: list[dict]) -> str:
        blob = json.dumps(items, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    #changed from norm getting it directly to fetching it label_context everytime for proper filed value especially for type
    def _envelope(self, raw_item: dict) -> Envelope:
        ctx = self.label_context(raw_item)
        labels, key = self.keyed(ctx, fallback=str(ctx.get("indicator")))
        confidence = ctx.get("confidence")
        threat_type = ctx.get("threat_type") or "unknown"
        indicator_type = ctx.get("indicator_type")
        event_type = indicator_type if indicator_type in EVENT_TYPES else "indicator"
        text = (
            f"{ctx.get('indicator')} flagged as {threat_type}"
            + (f" (confidence {confidence})" if confidence is not None else "")
            + f", source: {ctx.get('source') or 'feed'}"
            + (f", first seen {ctx['first_seen']}" if ctx.get("first_seen") else "")
        )
        return Envelope(
            source=self.cfg.name, source_type=self.cfg.type, key_value=key,
            event_type=event_type, text=text[:300], event_time=now_utc(),
            payload=raw_item, labels=labels,
        )

    @classmethod
    async def discover(cls, config: dict) -> dict | None:
        source = config.get("feed_url") or config.get("feed_path")
        if not source:
            raise ValueError("enter feed_url or feed_path first, then Discover")
        if config.get("feed_path"):
            try:
                raw = json.loads(Path(config["feed_path"]).read_text())
            except Exception as e:
                raise ValueError(f"could not read feed_path: {e}")
        else:
            headers = {"Authorization": f"Bearer {config['token']}"} if config.get("token") else {}
            async with httpx.AsyncClient(timeout=15) as cx:
                try:
                    r = await cx.get(config["feed_url"], headers=headers)
                except Exception as e:
                    raise ValueError(f"could not reach feed_url: {e}")
                if r.status_code != 200:
                    raise ValueError(f"feed returned {r.status_code}")
                raw = r.json()
        if not isinstance(raw, list) or not raw:
            raise ValueError("feed must be a non-empty JSON array of indicator objects")
        sample = raw[0] if isinstance(raw[0], dict) else {}
        threat_types = sorted({str(i.get("threat_type")) for i in raw
                               if isinstance(i, dict) and i.get("threat_type")})
        return {
            "connector": "threat_intel",
            "summary": f"{len(raw)} indicator(s)"
                       + (f", threat types: {', '.join(threat_types[:6])}" if threat_types else ""),
            "sample_fields": sorted(sample.keys()),
            "proposed_config": {
                **({"feed_url": config["feed_url"]} if config.get("feed_url") else {}),
                **({"feed_path": config["feed_path"]} if config.get("feed_path") else {}),
                "labels": [
                    {"name": "indicator", "field": "indicator", "primary": True},
                    {"name": "indicator_type", "field": "indicator_type"},
                    {"name": "threat_type", "field": "threat_type"},
                ],
            },
        }
