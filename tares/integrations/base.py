"""The base every integration extends. A slot interface (tickets.py, review.py, ...) adds the
methods of its slot; an integration subclasses its slot's interface, never this class alone."""
from __future__ import annotations

from .types import Field


class Integration:
    """`slot` is one of the seven slots, `name` the integration's key inside it, `title` the
    words a person sees. `ee` is True for an integration that needs a licence.
    `settings_schema` lists the fields its settings take."""
    slot: str = ""
    name: str = ""
    title: str = ""
    ee: bool = False
    settings_schema: list[Field] = []

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        """Test the integration with `settings` without changing anything.

        Returns (ok, message): the message says what worked, or the symptom and its cause when
        it did not. Never raises for a bad setting or an unreachable service; it returns
        (False, message)."""
        raise NotImplementedError
