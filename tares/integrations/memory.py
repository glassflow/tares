"""The memory slot: what the crew remembers between sessions."""
from __future__ import annotations

from .base import Integration


class MemoryStore(Integration):
    slot = "memory"

    async def add(self, scope: str, line: str) -> None:
        """Remember `line` under `scope` (a project or the whole cell). Returns nothing.
        Raises when the store cannot be reached."""
        raise NotImplementedError

    async def recall(self, scope: str, question: str, limit: int = 10) -> list[str]:
        """Up to `limit` remembered lines under `scope` that answer `question`, best first.
        Returns an empty list when nothing matches. Raises when the store cannot be reached."""
        raise NotImplementedError
