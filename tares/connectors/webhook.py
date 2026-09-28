"""Webhook connector — push ingestion. The producer POSTs JSON to /ingest/{source}; nothing to
poll. This is the generic inbound path for GitHub/Vercel/custom webhooks: map fields out of the
payload into the Envelope, keep the original lossless.

A delivery sent with `X-Tares-Bypass-Cooldown: true` wakes this source's triggers even for a key
inside its cooldown, for an on-demand run. It re-arms the cooldown on firing, and works for any
push source, not only this connector; the route's API description has the contract.

config:
  key: api-server              # fixed key, or
  key_field: service           # top-level payload field to read the key from (wins over `key`)
  event_type: webhook_event    # fixed event_type, or
  event_type_field: action     # payload field to read it from (wins over `event_type`)
  text_template: "{action} by {sender}"   # str.format over top-level payload fields;
                                          # omitted/failed -> compact JSON of the payload
  event_time_field: timestamp  # optional ISO-8601 field for event_time (else ingest time)
"""
from __future__ import annotations

import json
from datetime import datetime

from ..envelope import Envelope, now_utc
from .base import Connector


class WebhookConnector(Connector):
    # Authoritative config schema (source of truth; SPECS fields generated from it).
    CONFIG_SCHEMA = {
        # key/key_field are the legacy way to set the entity key; the key is now just the label
        # marked primary. Kept (advanced) so existing configs round-trip and still work.
        "key": {"type": "string", "advanced": True,
                "help": "legacy fixed entity key; prefer a primary label"},
        "key_field": {"type": "string", "advanced": True,
                      "help": "legacy: payload field for the key; prefer a primary field-label"},
        "event_type": {"type": "string", "default": "webhook_event",
                       "help": "fixed event type"},
        "event_type_field": {"type": "string",
                             "help": "payload field to read the event type from, e.g. action"},
        "text_template": {"type": "string",
                          "help": "render template over payload fields, e.g. '{action} by {sender}'"},
        "event_time_field": {"type": "string",
                             "help": "payload field holding an ISO-8601 event timestamp"},
    }

    async def poll(self) -> list[Envelope]:
        return []  # push-only: envelopes arrive via map_payload from POST /ingest/{source}

    def map_payload(self, payload) -> list[Envelope]:
        items = payload if isinstance(payload, list) else [payload]
        return [self._map_one(item) for item in items]

    def _map_one(self, item) -> Envelope:
        if not isinstance(item, dict):
            item = {"value": item}
        c = self.cfg.config

        # key = the primary label's value; falls back to the legacy key/key_field config
        fallback = (str(item.get(c["key_field"], "")) if c.get("key_field") else "") \
            or str(c.get("key", "unknown"))
        labels, key = self.keyed(item, fallback=fallback)

        event_type = (str(item.get(c["event_type_field"], "")) if c.get("event_type_field") else "")
        event_type = event_type or str(c.get("event_type", "webhook_event"))

        text = ""
        if c.get("text_template"):
            try:
                text = str(c["text_template"]).format(**item)
            except (KeyError, IndexError):
                pass
        if not text:
            text = json.dumps(item, default=str)[:500]

        event_time = now_utc()
        if c.get("event_time_field") and item.get(c["event_time_field"]):
            try:
                event_time = datetime.fromisoformat(str(item[c["event_time_field"]]))
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=now_utc().tzinfo)
            except ValueError:
                event_time = now_utc()

        return Envelope(
            source=self.cfg.name, source_type=self.cfg.type, key_value=key,
            event_type=event_type, text=text, event_time=event_time,
            payload=item if isinstance(item, dict) else {"value": item},
            labels=labels,
        )
