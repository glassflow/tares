"""The guardrail hooks (TR-855, TR-861, TR-863).

A hook point is a place where Tares asks the project's guardrails before it acts. Three exist:

  pre_build    the hand-over of a project to the crew, before anything is recorded
  pre_merge    merge_ready, before a pull request may merge
  pre_release  release_ready, before a commit is shipped

`HOOK_POINTS` maps each name to the function that shapes its payload. A new hook point is one more
entry there: `run`, `gate` and every caller work for it with no other change.

`run(stage, project, payload)` asks the guardrail integration the project's `guardrails` slot
resolves to and returns its Decisions. A blocking Decision (`allow` False, no `warn_only`) stops
the action; a `warn_only` one is only shown. A guardrail that allows with nothing to say adds
nothing to the list. `run` raises when that guardrail cannot be reached; `gate` turns that into a
block, because an unreachable guardrail must not let an action through.

Guardrail pairs (the `guardrail_pairs` table) add to that. A pair is a hook point and a guardrail
integration with its own integration settings, and `on_fail` says what a "no" means: `block` stops the
action, `warn` only shows it. After the slot's guardrail, `run` runs the factory's pairs for the hook,
then the project's own, each in the order they were added; a project pair that is the same as a
factory pair (same hook, integration and settings) runs once. Each pair gets `PAIR_SECONDS` to
answer. A guardrail that denies keeps its `on_fail`; one that raises, times out, is no longer
installed or needs a licence the cell lacks did not answer, and that follows `on_fail` as well
("guardrail did not answer: <integration>"; the cause and the settings are never put in the reason).
One pair failing never stops the next, and `run` raises only for the slot's guardrail.

Given a `ticket`, a warning with a reason is also written to that ticket's history as
`guardrail_warn`, once: the same warning is not written again while it is the latest one.

The store is bound once by the daemon (`bind`); with none bound there is no guardrail to ask.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from typing import Callable

from .. import licence
from ..envelope import now_utc
from . import registry, resolve
from .types import Decision

log = logging.getLogger("tares.hooks")

_store = None

# how long one guardrail pair has to answer before it counts as not answering
PAIR_SECONDS = 10


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


def _did_not_answer(pair: dict) -> Decision:
    name = pair["integration"]
    return Decision(allow=False, reason=f"guardrail did not answer: {name}", integration=name,
                    warn_only=pair["on_fail"] == "warn")


async def _run_pair(pair: dict, stage: str, shaped: dict) -> Decision | None:
    """One pair's Decision, or None when it has nothing to say. Never raises."""
    name = pair["integration"]
    try:
        cls = registry.get("guardrails", name)
        if cls is None or (cls.ee and not licence.ee_allowed()):
            return _did_not_answer(pair)
        inst = cls(_store)
        inst.settings = dict(pair["settings"])
        d = await asyncio.wait_for(inst.check(stage, copy.deepcopy(shaped)), PAIR_SECONDS)
        if d is None or (d.allow and not d.reason):
            return None
        if not d.integration:
            d = replace(d, integration=name)
        if not d.allow and not d.warn_only and pair["on_fail"] == "warn":
            d = replace(d, warn_only=True)
        return d
    except Exception as e:  # noqa: BLE001 - a pair that fails follows its on_fail
        log.warning("guardrail pair %s (%s) did not answer: %s", pair.get("id"), name, type(e).__name__)
        return _did_not_answer(pair)


async def _run_pairs(stage: str, project: str, shaped: dict) -> list[Decision]:
    """The decisions of the pairs for `stage`: the factory's, then the project's own."""
    out: list[Decision] = []
    seen: set[tuple] = set()
    for pair in _store.guardrail_pairs_for_run(stage, project or None):
        key = (pair["hook"], pair["integration"], json.dumps(pair["settings"], sort_keys=True))
        if pair["scope"] == "project" and key in seen:
            continue                     # the factory already runs exactly this
        seen.add(key)
        d = await _run_pair(pair, stage, shaped)
        if d is not None:
            out.append(d)
    return out


def _record_warnings(stage: str, project: str, ticket: str, decisions: list[Decision]) -> None:
    """Write each warning with a reason to the ticket's history as `guardrail_warn`, unless the
    newest one from the same guardrail at the same hook point already says the same. Never raises."""
    if not ticket or _store is None:
        return
    try:
        warnings = [d for d in decisions if d.warn_only and d.reason]
        if not warnings:
            return
        rows = [r for r in _store.ticket_history(ticket) if r["field"] == "guardrail_warn"]
        latest: dict[str, str] = {}          # source ("stage: integration") -> its newest warning
        for r in rows:
            latest[r["reason"]] = r["value"]
        at = now_utc()
        for d in warnings:
            source = f"{stage}: {d.integration}"
            if latest.get(source) == d.reason:
                continue
            _store.add_ticket_history(ticket, project, "guardrail_warn", d.reason,
                                      source, None, "guardrail", at)
    except Exception:  # noqa: BLE001 - recording must not break the answer
        log.exception("could not record a guardrail warning for ticket %s", ticket)


async def run(stage: str, project: str, payload: dict, ticket: str = "") -> list[Decision]:
    """The guardrails' decisions for `stage`: the slot's guardrail first, then the guardrail pairs
    (see the module docstring). None while they have nothing to say (or no store is bound).
    `project` is the project's id; `ticket` is the ticket's id, when the action is about one, so a
    warning can be recorded on it."""
    shaped = payload_for(stage, payload)
    if _store is None:
        return []
    decisions: list[Decision] = []
    eff = resolve.effective("guardrails", project, store=_store)
    cls = registry.get("guardrails", eff.integration)
    if cls is not None:
        decision = await cls(_store).check(stage, shaped)
        if decision is not None and not (decision.allow and not decision.reason):
            decisions.append(decision)
    decisions.extend(await _run_pairs(stage, project, shaped))
    _record_warnings(stage, project, ticket, decisions)
    return decisions


async def gate(stage: str, project: str, payload: dict, ticket: str = "") -> dict:
    """`run`, folded for a caller that only needs the outcome: {ok, reasons, warnings, decisions}.
    `ok` is False when a guardrail blocks or cannot be reached; `reasons` are the sentences for
    what blocks, `warnings` the `warn_only` ones, `decisions` each Decision as a dict. `ticket` is passed
    to `run`."""
    reasons: list[str] = []
    warnings: list[str] = []
    rails: list[dict] = []
    try:
        decisions = await run(stage, project, payload, ticket=ticket)
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
