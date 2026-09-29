"""Findings connector — the built-in source a Tares agent writes its conclusion into.

A finding is an ordinary event, which is the whole point (docs/design/navflow-agents.md): correlation puts
it on the entity's timeline automatically, `read`/`query` pick it up with no special case, and the
next dispatch to another agent carries prior findings as evidence. A Tares agent's conclusions do
NOT go to `agent_memory` — that source is where agents keep free-form notes via remember(); a
finding is the tagged output of a triggered run, distinct provenance.

Internal: the daemon provisions this source on the first finding; it is not offered in "Add source".

Payloads arrive from the agent runner (never from a user) shaped:
  { key: "api-server", finding: "...", agent: "error-agent", trigger: "incident",
    dispatch_id: "…", prompt_hash: "…", labels: {service: "api-server"} }
"""
from __future__ import annotations

from ..envelope import Envelope, now_utc
from .base import Connector


class FindingConnector(Connector):
    CONFIG_SCHEMA: dict = {}

    async def poll(self) -> list[Envelope]:
        return []  # push-only: findings arrive via map_payload

    def map_payload(self, payload) -> list[Envelope]:
        items = payload if isinstance(payload, list) else [payload]
        return [self._map_one(item) for item in items]

    def _map_one(self, item) -> Envelope:
        if not isinstance(item, dict):
            raise ValueError("a finding payload must be an object")
        finding = str(item.get("finding", "")).strip()
        if not finding:
            raise ValueError("finding payload needs a non-empty 'finding'")

        # Labels come from the payload, not from source config: the runner knows which label the
        # firing entity was identified by (the trigger's key_field) and stamps it, so the finding
        # carries the SAME axis as the evidence it was drawn from. Without that, a label-native
        # `read` for the entity would not return the finding written about it.
        key = str(item.get("key") or "agent")
        labels = {k: str(v) for k, v in (item.get("labels") or {}).items() if v not in (None, "")}
        labels["agent"] = str(item.get("agent") or "")

        return Envelope(
            source=self.cfg.name, source_type=self.cfg.type, key_value=key,
            event_type="finding", text=finding, event_time=now_utc(),
            payload=item, labels=labels,
        )
