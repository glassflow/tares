"""Linear connector (TR-408): keeps the tickets of every project that uses Linear in sync, and
emits one event per change (added, renamed, moved to another status, reordered, left the project).

Provisioned by Tares when a project is linked to a Linear project; there is one per cell. It polls
with the cell's Linear connection (Settings), never with a credential of its own, and only reads.
The sync itself lives in tares/linear.py so linking a project can run it at once.
"""
from __future__ import annotations

from .. import linear
from ..envelope import Envelope
from .base import Connector


class LinearConnector(Connector):
    CONFIG_SCHEMA: dict = {}

    async def poll(self) -> list[Envelope]:
        out: list[Envelope] = []
        errors = []
        for uid, _link in self.store.linear_projects():
            try:
                out.extend(await linear.sync_project(self.store, uid))
            except linear.LinearError as e:
                errors.append(str(e))     # recorded on the project; the others still sync
        if errors and not out:
            # the source's health shows it when nothing synced at all
            raise linear.LinearError(errors[0])
        return out

    def label_context(self, payload: dict | None) -> dict:
        return payload or {}
