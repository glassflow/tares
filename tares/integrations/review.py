"""The review slot: who says a pull request may merge."""
from __future__ import annotations

from .base import Integration
from .types import PRRef, Verdict


class ReviewGate(Integration):
    slot = "review"

    async def verdict(self, pr: PRRef) -> Verdict:
        """The gate's verdict for `pr.head`. Returns a Verdict; `state` is `none` when the gate
        has not looked at the PR and `stale` when it only looked at an older head. Raises when
        the gate cannot be reached."""
        raise NotImplementedError

    async def findings(self, pr: PRRef) -> list[str]:
        """The gate's open findings for `pr`, one plain sentence each. Returns an empty list
        when there are none. Raises when the gate cannot be reached."""
        raise NotImplementedError
