"""The Claude Code connector of the source catalog: a thin wrapper over tares.session_map, kept until the
source catalog goes (Tares 2.0, TR-1811). The mapping itself lives in session_map."""
from __future__ import annotations

from ..envelope import Envelope
from ..session_map import (ClaudeCodeLines, _holds_nul, _redact, _redact_obj, _short,  # noqa: F401
                           _without_nul)
from .base import Connector


class ClaudeCodeConnector(ClaudeCodeLines, Connector):
    def __init__(self, cfg, store):
        Connector.__init__(self, cfg, store)
        ClaudeCodeLines.__init__(self, store, source=cfg.name, source_type=cfg.type)
        self.config = cfg.config if cfg.config is not None else {}   # the source's own, read live

    async def poll(self) -> list[Envelope]:
        return []   # push-only: events arrive via map_payload() from the plugin
