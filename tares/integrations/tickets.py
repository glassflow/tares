"""The tickets slot: where a project's tickets live."""
from __future__ import annotations

import re

from .base import Integration
from .types import Status, TicketRef


class LinkConflict(ValueError):
    """The project cannot be linked as asked: it is already linked somewhere else, or it already
    holds tickets of another home. The daemon answers 409."""


# a link field whose name says it carries a secret: credentials come from the cell, never from
# the caller (TR-849)
_SECRET_PARTS = ("key", "token", "secret", "password", "passwd", "credential", "authorization",
                 "bearer")


def check_link_fields(link_fields, allowed: tuple[str, ...]) -> dict:
    """`link_fields` as a dict holding only the non-secret fields `allowed` names. Raises
    ValueError for a body that is not a dict, that carries a key or token (credentials come from
    the cell), or that names a field the integration does not take."""
    if link_fields is None:
        return {}
    if not isinstance(link_fields, dict):
        raise ValueError("link_fields is a dict of what to link to")
    for k in link_fields:
        plain = re.sub(r"[^a-z0-9]", "", str(k).lower())
        if any(p in plain for p in _SECRET_PARTS):
            raise ValueError(f"link_fields takes no secrets ({k!r}): the cell's own credentials "
                             "are used")
    unknown = sorted(str(k) for k in set(link_fields) - set(allowed))
    if unknown:
        raise ValueError("this link takes " + ", ".join(allowed) + ", not: " + ", ".join(unknown))
    return dict(link_fields)


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
        found; a ticket's ref carries its identifier (ENG-12, GH-7), a milestone's does not. Raises when
        the system refuses the item or cannot be reached."""
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
