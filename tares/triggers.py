"""Trigger evaluation — runs in taresd after each ingest tick.

Collapsed form of the design doc's Trigger Engine: instead of a stream consumer with windowed
state in JetStream KV, we evaluate each condition as a SQL aggregate over the DuckDB window. On a
match (not in cooldown) we render the view and dispatch it. Expand path: move the window in-memory
and evaluate on the in-flight batch to decouple latency from the poll interval.
"""
from __future__ import annotations

import asyncio
import operator
import os
import time
from datetime import timezone

from .config import Catalog
from .envelope import now_utc
from .views import parse_window, resolve_query

_OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, ">": operator.gt, "<": operator.lt}

# Debounce: a trigger is re-evaluated at most once per this many seconds (capped to its own window,
# so a short-window trigger still evaluates often enough to catch a crossing). Without it, a
# push-heavy source re-runs the same windowed aggregate on every push — poll=5s, window=60s means the
# same window is re-scanned ~12x, and cooldown already prevents double-fires. Trades up to this many
# seconds of detection latency for dropping the redundant scans off the ingest hot path.
_DEBOUNCE_SECONDS = float(os.getenv("TARES_TRIGGER_DEBOUNCE_SECONDS", "10"))


def _predicate(value: float, pred: str) -> bool:
    pred = pred.strip()
    for sym in (">=", "<=", "==", ">", "<"):
        if pred.startswith(sym):
            return _OPS[sym](value, float(pred[len(sym):].strip()))
    raise ValueError(f"unparseable predicate: {pred!r}")


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_catchups: dict = {}   # trigger name -> pending asyncio task for a debounced re-evaluation


def cancel_catchups() -> None:
    """Drop pending catch-up evaluations (daemon shutdown)."""
    for t in list(_catchups.values()):
        t.cancel()
    _catchups.clear()


def _schedule_catchup(name: str, delay: float, store, catalog, dispatcher, eval_state) -> None:
    """Re-evaluate `name` once the debounce interval has passed, unless a catch-up is already
    pending. Evaluates without an affected-source filter so every key of the view is considered.
    The catalog is re-read at fire time through `dispatcher.runtime` when available, so a trigger
    edited or a project created inside the interval is evaluated as it is then, not as it was."""
    if name in _catchups and not _catchups[name].done():
        return

    async def _later():
        try:
            await asyncio.sleep(max(delay, 0.05))
            live = getattr(getattr(dispatcher, "runtime", None), "catalog", None) or catalog
            await eval_triggers(store, live, dispatcher, affected_sources=None,
                                eval_state=eval_state, only=name)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            _catchups.pop(name, None)

    try:
        _catchups[name] = asyncio.get_running_loop().create_task(_later())
    except RuntimeError:   # no running loop (tests calling synchronously): evaluate next time
        pass


async def eval_triggers(store, catalog: Catalog, dispatcher, affected_sources=None,
                        eval_state: dict | None = None, only: str | None = None) -> list:
    """Timed and counted (tares_trigger_evaluation_seconds): the pass runs on the event loop, so
    its duration is the daemon's stall (TR-308). The work is in _eval_triggers."""
    from . import metrics
    t0 = time.monotonic()
    fired = await _eval_triggers(store, catalog, dispatcher, affected_sources, eval_state, only)
    metrics.trigger_evaluation(time.monotonic() - t0, fired)
    return fired


async def _eval_triggers(store, catalog: Catalog, dispatcher, affected_sources=None,
                         eval_state: dict | None = None, only: str | None = None) -> list:
    """Evaluate every trigger whose view touches an affected source. Returns [(trigger, key)] fired.
    `only` restricts the pass to one trigger (the debounce catch-up).

    `eval_state` is a caller-owned {trigger_name: last_eval_datetime} map for debouncing across ticks;
    pass None (tests) to evaluate on every call."""
    fired = []
    now = now_utc()
    for trig in catalog.triggers:
        if only is not None and trig.name != only:
            continue
        # A paused trigger is inert: not evaluated, never fires, until resumed.
        if getattr(trig, "paused", False):
            continue
        view = catalog.views[trig.view]
        if affected_sources and not (set(view.sources) & set(affected_sources)):
            continue

        c = trig.condition
        # Debounce: skip if this trigger was evaluated within min(window, _DEBOUNCE_SECONDS) ago.
        # The first evaluation of a trigger always runs (no prior state). A skipped evaluation is
        # not dropped: it is re-run once the interval is over (see _schedule_catchup), otherwise
        # two sources of the same view that ingest within one interval leave the second one
        # unevaluated until the next ingest, by which time a short window has slid past its events.
        if eval_state is not None:
            interval = min(parse_window(c.window).total_seconds(), _DEBOUNCE_SECONDS)
            last_eval = eval_state.get(trig.name)
            if last_eval is not None and (now - last_eval).total_seconds() < interval:
                _schedule_catchup(trig.name, interval - (now - last_eval).total_seconds(),
                                  store, catalog, dispatcher, eval_state)
                continue
            eval_state[trig.name] = now

        group_by = c.group_by or ["key_value"]
        legacy = group_by == ["key_value"]
        since = now_utc() - parse_window(c.window)
        per_group = store.aggregate(view.sources, c.field, c.aggregate, since,
                                    filters=view.filters, group_by=group_by)

        for grp, value in per_group.items():
            try:
                hit = _predicate(value, c.predicate)
            except ValueError:
                hit = False
            if not hit:
                continue

            # `grp` is a tuple (one element per group_by label). The group identifies the entity
            # that fired: legacy key_value grouping selects context by key; label grouping selects
            # by a {label: value} `where`. The cooldown / dispatch key is a stable string.
            if legacy:
                fire_key, where = grp[0], None
            else:
                where = dict(zip(group_by, grp))
                fire_key = ", ".join(f"{k}={v}" for k, v in where.items())

            last = store.last_fired(trig.name, fire_key)
            if last and (now_utc() - _aware(last)).total_seconds() < trig.cooldown_seconds:
                continue
            # Record the firing decision BEFORE rendering/delivering. Both store calls above and
            # this one are synchronous — no await between check and set — so on the single event
            # loop the cooldown check is an atomic critical section. Recording after delivery
            # left the whole delivery await as a window where a concurrent ingest re-evaluated
            # and double-fired. Cooldown rate-limits decisions; delivery outcomes are the
            # dispatcher's ledger (dispatch_deliveries).
            store.set_fired(trig.name, fire_key, now_utc())

            # Detection uses the (narrow) condition window; the attached context is wider so the
            # woken agent gets the correlating deploy/config, not just the spike that tripped it.
            ctx_window = trig.emit.get("context_window", "15m")
            payload = resolve_query(store, catalog, trig.view,
                                    key=(fire_key if legacy else None),
                                    window=ctx_window, where=where)
            await dispatcher.fire(trig, fire_key, payload)
            fired.append((trig.name, fire_key))

    return fired
