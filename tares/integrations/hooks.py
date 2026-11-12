"""The guardrail hooks (TR-855, TR-861).

A hook point is a place where Tares asks the project's guardrails before it acts. Three exist:

  pre_build    the hand-over of a project to the crew, before anything is recorded
  pre_merge    merge_ready, before a pull request may merge
  pre_release  release_ready, before a commit is shipped

`HOOK_POINTS` maps each name to the function that shapes its payload. A new hook point is one more
entry there: `run`, `gate` and every caller work for it with no other change.

`run(stage, project, payload)` asks the guardrail integration the project's `guardrails` slot
resolves to and returns its Decisions. A blocking Decision (`allow` False, no `warn_only`) stops
the action; a `warn_only` one is only shown. A guardrail that allows with nothing to say adds
nothing to the list. `run` raises when a guardrail cannot be reached; `gate` turns that into a
block, because an unreachable guardrail must not let an action through.

The store is bound once by the daemon (`bind`); with none bound there is no guardrail to ask.
"""
from __future__ import annotations

from typing import Callable

from . import registry, resolve
from .types import Decision

_store = None


def bind(store) -> None:
    """Give the hooks the store they resolve the project's guardrails from (the daemon, at start)."""
    global _store
    _store = store


def _fill(defaults: dict) -> Callable[[dict], dict]:
    """A payload shaper: the keys a hook point promises, each its empty value when the caller had
    none; anything else the caller passed is kept."""
    def shape(payload: dict) -> dict:
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in defaults.items()}
        out.update({k: v for k, v in (payload or {}).items() if v is not None})
        return out
    return shape


# stage -> payload shaper. Keys are the contract a guardrail can rely on at that stage.
HOOK_POINTS: dict[str, Callable[[dict], dict]] = {
    "pre_build": _fill({"project": "", "goal": "", "spec_text": "", "repos": [], "autonomy": ""}),
    "pre_merge": _fill({"repo": "", "pr": 0, "head": "", "base": "", "files": [], "ticket": "",
                        "review": {}, "ci": {}}),
    "pre_release": _fill({"repo": "", "commit": "", "milestone": "", "merged_prs": []}),
}


def payload_for(stage: str, payload: dict | None) -> dict:
    """The payload as `stage` promises it. Raises ValueError for a stage that is not a hook point,
    so a misspelt stage never reads as "nothing objected"."""
    shape = HOOK_POINTS.get(stage)
    if shape is None:
        raise ValueError(f"{stage!r} is not a hook point (one of: {', '.join(HOOK_POINTS)})")
    return shape(payload or {})


async def run(stage: str, project: str, payload: dict) -> list[Decision]:
    """The guardrails' decisions for `stage`: none while the project's guardrail has nothing to say
    (or no store is bound). `project` is the project's id."""
    shaped = payload_for(stage, payload)
    if _store is None:
        return []
    eff = resolve.effective("guardrails", project, store=_store)
    cls = registry.get("guardrails", eff.integration)
    if cls is None:
        return []
    decision = await cls(_store).check(stage, shaped)
    if decision is None or (decision.allow and not decision.reason):
        return []
    return [decision]


async def gate(stage: str, project: str, payload: dict) -> dict:
    """`run`, folded for a caller that only needs the outcome: {ok, reasons, warnings, decisions}.
    `ok` is False when a guardrail blocks or cannot be reached; `reasons` are the sentences for
    what blocks, `warnings` the `warn_only` ones, `decisions` each Decision as a dict."""
    reasons: list[str] = []
    warnings: list[str] = []
    rails: list[dict] = []
    try:
        decisions = await run(stage, project, payload)
    except Exception as e:  # noqa: BLE001 - a guardrail that cannot be reached blocks
        return {"ok": False, "reasons": [f"The guardrails could not be checked: {type(e).__name__}: {e}"],
                "warnings": [], "decisions": []}
    for d in decisions or []:
        rails.append({"allow": bool(d.allow), "reason": d.reason, "integration": d.integration,
                      "warn_only": bool(d.warn_only)})
        who = d.integration or "a guardrail"
        if not d.allow and not d.warn_only:
            reasons.append(f"Blocked by {who}: {d.reason or 'no reason given'}")
        elif d.warn_only and d.reason:
            warnings.append(f"{who}: {d.reason}")
    return {"ok": not reasons, "reasons": reasons, "warnings": warnings, "decisions": rails}
