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


def _group_by(condition) -> list:
    """The condition's grouping labels as a list. `["key_value"]` is the legacy single-key form."""
    g = condition.group_by or ["key_value"]
    return [g] if isinstance(g, str) else list(g)


def _fire_key(group_by: list, values: list) -> str | None:
    """The cooldown/dispatch key for one group: the key itself under legacy `key_value` grouping,
    else a stable `label=value, ...` rendering. `values` is one value per `group_by` name, in
    order. None when a component is missing — a row without every grouping label is not an entity,
    and store.aggregate drops it. Shared with the ingest bypass so the two derive the same key."""
    if any(v is None for v in values):
        return None
    if group_by == ["key_value"]:
        return str(values[0])
    return ", ".join(f"{k}={v}" for k, v in zip(group_by, values))


def _envelope_fire_key(group_by: list, env) -> str | None:
    """The key `env` would fire under. Reads the same two places store.aggregate groups by: the
    stamped `key_value` column, or a named label."""
    return _fire_key(group_by, [env.key_value if name == "key_value" else env.labels.get(name)
                                for name in group_by])


def clear_cooldowns(store, catalog: Catalog, source: str, envelopes: list) -> list:
    """Drop the cooldown state for the keys `envelopes` would fire under, on every trigger whose
    view reads `source`. Returns the [(trigger, key)] cleared.

    The ingest path calls this for a delivery marked X-Tares-Bypass-Cooldown, so the evaluation
    that follows it fires for a key that is still cooling down (Rius asking for one alert's
    analysis on demand). It does not switch the cooldown off: that firing writes its usual
    `set_fired`, so the next unmarked event for the key waits the full interval again.

    Two deliberate imprecisions, both erring towards clearing: a view `filters` clause that would
    exclude the event is not applied, and a label whose value is not a string is rendered by
    Python rather than by the store's JSON extraction."""
    cleared = []
    for trig in catalog.triggers:
        if getattr(trig, "paused", False):
            continue
        view = catalog.views.get(trig.view)
        if view is None or source not in view.sources:
            continue
        group_by = _group_by(trig.condition)
        for key in sorted({k for e in envelopes
                           if (k := _envelope_fire_key(group_by, e)) is not None}):
            store.clear_fired(trig.name, key)
            cleared.append((trig.name, key))
    return cleared


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

        group_by = _group_by(c)
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
            where = None if legacy else dict(zip(group_by, grp))
            fire_key = _fire_key(group_by, list(grp))
            if fire_key is None:
                continue

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
