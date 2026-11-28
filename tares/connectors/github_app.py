"""The GitHub App connector of the source catalog: a thin wrapper over tares.github_webhook, kept until
the source catalog goes (Tares 2.0, TR-1811). The delivery logic lives in github_webhook."""
from __future__ import annotations

from ..envelope import Envelope
from ..github_webhook import (DEDUPE_MAX, DEDUPE_SECONDS, STATS, GithubAppDeliveries,  # noqa: F401
                              _seen, _stats)
from . import github_events as gh
from .base import Connector


class GithubAppConnector(GithubAppDeliveries, Connector):
    CONFIG_SCHEMA = {
        "credential": {"type": "string", "required": True,
                       "help": "the GitHub App credential (Settings > GitHub) whose webhook feeds "
                               "this source; its webhook secret checks every delivery"},
    }
    PROVIDES = gh.CONTRACT_LABELS

    def __init__(self, cfg, store):
        Connector.__init__(self, cfg, store)
        GithubAppDeliveries.__init__(self, store, cfg.name, (cfg.config or {}).get("credential") or "")
        self.source_type = cfg.type

    async def poll(self) -> list[Envelope]:
        return []   # push-only: deliveries arrive via map_payload from POST /ingest/{key}

    def labels_for(self, context: dict | None = None) -> dict:
        # the contract's labels always, whatever the source declares; declared labels win
        return {**(context or {}), **Connector.labels_for(self, context)}

    def _primary_label(self):
        return Connector._primary_label(self)
