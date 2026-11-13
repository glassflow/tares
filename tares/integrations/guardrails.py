"""The guardrails slot: rules checked before a build, a merge or a release."""
from __future__ import annotations

from .base import Integration
from .types import Decision


class Guardrail(Integration):
    """A guardrail integration. When it runs as a guardrail pair, `self.settings` holds that pair's
    integration settings (secrets included) before `check` is called; the slot's own guardrail
    reads its settings itself and leaves it empty."""
    slot = "guardrails"
    settings: dict = {}

    async def check(self, stage: str, payload: dict) -> Decision:
        """Check `payload` at `stage` (`pre_build`, `pre_merge` or `pre_release`). Returns a
        Decision: `allow` False blocks with a `reason`, and `warn_only` shows the reason
        without blocking. Raises when the guardrail cannot be reached; the caller decides
        whether that blocks."""
        raise NotImplementedError
