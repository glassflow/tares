"""The tickets slot: where a project's tickets live."""
from __future__ import annotations

from .base import Integration
from .types import Status, TicketRef


class TicketBackend(Integration):
    slot = "tickets"

    async def sync(self, project) -> int:
        """Read the project's tickets from the ticket system and upsert them into the Tares
        ticket table. Returns the number of tickets upserted. Raises when the ticket system
        cannot be reached; tickets already stored stay as they were."""
        raise NotImplementedError

    async def set_status(self, ticket, status: Status) -> None:
        """Set `ticket`'s status in the ticket system. Returns nothing. Raises when the system
        refuses the change or cannot be reached."""
        raise NotImplementedError

    async def create(self, project, milestones: list, tickets: list) -> list[TicketRef]:
        """Create milestones or tickets in the ticket system. Called with one item per call:
        either one milestone and no tickets, or one ticket and no milestones. A retry of the
        same item must not create a second copy. Returns one TicketRef per item created or
        found. Raises when the system refuses the item or cannot be reached."""
        raise NotImplementedError

    async def update(self, ticket, fields: dict) -> None:
        """Change `ticket` in the ticket system. `fields` may hold title, order, milestone and
        depends_on. Returns nothing. Raises when the system refuses the change or cannot be
        reached."""
        raise NotImplementedError

    async def link(self, project, link_fields: dict) -> dict:
        """Link `project` to a project, team or repo in the ticket system. Returns the link as
        a dict of non-secret fields only. Raises when the target is not found."""
        raise NotImplementedError
