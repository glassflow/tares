"""The crew's standup report, and its summary written by the cell's model (which the morning
status uses too)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial

from fastapi import APIRouter, Body

from .. import (
    factory_api,
)
from .. import providers as providers_mod
from .. import standup as standup_mod
from ..deps import Deps
from ..envelope import now_utc
from ..models import add_usage, empty_usage
from ..providers import (
    resolve_provider,
)


async def summarise(deps: Deps, report: dict, timeout: int | None = None) -> tuple[dict | None, str | None]:
    """A line or two per project written by the cell's model, as (summary, why not). Never
    raises: the standup stands on its facts alone."""
    wait = timeout or 90
    summary, why = None, None
    store = deps.store
    provider, origin = resolve_provider(store)
    if provider is None:
        why = "no model provider is set up"
    elif report["projects"]:
        pid = providers_mod.default_id(store)
        model = providers_mod.default_model_for(store, pid)
        try:
            reply = await asyncio.wait_for(provider.complete(
                model=model, system=standup_mod.SUMMARY_SYSTEM, tools=[],
                messages=[{"role": "user", "content": standup_mod.summary_prompt(report)}],
                max_tokens=standup_mod.SUMMARY_MAX_TOKENS, tools_allowed=False), timeout=wait)
            # one reply's usage has no call count; the ledger row wants the loops' total
            usage = empty_usage()
            add_usage(usage, reply.usage)
            deps.record_model_usage(model, usage, key_source=origin, kind=provider.kind,
                              provider_id=pid)
            summary = standup_mod.parse_summary(reply.text,
                                                {p["id"] for p in report["projects"]})
            if not summary["overall"] and not summary["projects"]:
                summary, why = None, "the model's answer could not be read"
        except asyncio.TimeoutError:
            why = f"the model took longer than {wait}s"
        except Exception as e:  # noqa: BLE001 — the report stands without the summary
            msg = str(e)
            why = ("the model provider refused its key; check it under Settings"
                   if any(c in msg for c in (" 401", " 403", "invalid x-api-key"))
                   else f"the model call failed ({type(e).__name__})")
            print(f"standup: summary failed: {type(e).__name__}: {msg[:300]}", flush=True)
    return summary, why


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _hidden_ids = deps.hidden_ids
    _summarise_standup = partial(summarise, deps)

    # ── the crew's standup report: before factory_api, whose /api/crew/{name} would take it ──
    def _standup_range(since: str | None, until: str | None) -> tuple[datetime, datetime]:
        def parse(v: str, what: str) -> datetime:
            try:
                d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            except ValueError:
                _err(ValueError(f"{what} is not a date and time: {v!r}"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        end = parse(until, "until") if until else now_utc()
        start = parse(since, "since") if since else end - timedelta(days=1)
        if start >= end:
            _err(ValueError("since must be before until"))
        if end - start > timedelta(days=92):
            _err(ValueError("a standup covers at most 92 days"))
        return start, end

    def _standup(since: str | None, until: str | None) -> dict:
        start, end = _standup_range(since, until)
        return standup_mod.facts(
            store, start, end,
            lambda uid, tickets, ms: factory_api.project_progress(store, uid, tickets, ms),
            hide=_hidden_ids())

    @r.get("/api/crew/standup")
    def crew_standup(since: str | None = None, until: str | None = None):
        """What moved in each project handed to the crew between `since` and `until` (ISO; the
        last day by default), read from the ledger: done, started, in review, blocked, and the
        questions waiting on the person. `markdown` is the same report ready to copy."""
        report = _standup(since, until)
        return {**report, "markdown": standup_mod.markdown(report)}

    @r.post("/api/crew/standup/summary")
    async def crew_standup_summary(body: dict = Body(default={})):
        """{since?, until?}: the same report with a line or two per project written by the cell's
        model from those facts. Without a model provider, or when it fails, the report comes back
        without the summary and `summary_error` says why."""
        report = _standup(body.get("since"), body.get("until"))
        summary, why = await _summarise_standup(report)
        return {**report, "summary": summary, "summary_error": why,
                "markdown": standup_mod.markdown(report, summary)}

    return r
