"""GitHub Issues connector (TR-569): keeps the tickets of every project that uses GitHub Issues
in sync, and emits one event per change (added, renamed, moved to another status, reordered,
left the project).

Provisioned by Tares when a project is linked to a repo and issue label; there is one per cell.
It polls with the cell's stored GitHub credentials, never one of its own, and only reads. The
sync itself lives in tares/github_tickets.py so linking a project can run it at once.
"""
from __future__ import annotations

from .. import github_tickets
from ..envelope import Envelope
from .base import Connector


class GitHubIssuesConnector(Connector):
    CONFIG_SCHEMA: dict = {}

    async def poll(self) -> list[Envelope]:
        out: list[Envelope] = []
        errors = []
        for uid, _link in self.store.github_projects():
            try:
                out.extend(await github_tickets.sync_project(self.store, uid))
            except github_tickets.GitHubTicketsError as e:
                errors.append(str(e))     # recorded on the project; the others still sync
        if errors and not out:
            # the source's health shows it when nothing synced at all
            raise github_tickets.GitHubTicketsError(errors[0])
        return out

    def label_context(self, payload: dict | None) -> dict:
        return payload or {}
