"""The guardrail hooks (TR-855).

`run(stage, project, payload)` is the one place a stage asks the guardrails slot what to do.
Today the slot has no integration that decides anything, so it answers with no decisions. When a
guardrail integration is installed (a later milestone), this is where it is called, and every
caller already handles what comes back: a list of `Decision`s, a blocking one (`allow` False,
no `warn_only`) stops the action, a `warn_only` one is only shown.

`stage` is `pre_build`, `pre_merge` or `pre_release`; `project` is the project's id; `payload`
is a plain dict the stage describes (for `pre_merge`: the PR, its head and the ticket).
"""
from __future__ import annotations

from .types import Decision


async def run(stage: str, project: str, payload: dict) -> list[Decision]:
    """The guardrails' decisions for `stage`, none while no guardrail integration is installed."""
    return []
