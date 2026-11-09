"""The ci slot: whether the checks of a pull request passed."""
from __future__ import annotations

from .base import Integration
from .types import CIState, PRRef


class CISource(Integration):
    slot = "ci"

    async def state(self, pr: PRRef, required: list[str]) -> CIState:
        """The state of the `required` checks on `pr.head`; an empty list means every check.
        Returns a CIState: `success` when all required checks passed, `failure` with the
        failing names in `failing`, `pending` while any still runs, `none` when no check
        reported. Raises when the CI system cannot be reached."""
        raise NotImplementedError
