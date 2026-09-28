"""Schedule triggers (TR-320): a trigger that fires on a clock instead of a condition.

A trigger whose condition carries `every` fires once per interval for the whole view, whether or
not anything matched: a quiet window is information too. The agent it wakes is handed a summary
of the window, not raw lines, because a busy view is far too big to hand over and the render is
capped per source anyway: per `summary_by` label, the counts against the window before (the same
table as the `stats` tool), the totals, and a few recent lines.

The clock is one task in the daemon. It checks every CLOCK_SECONDS; the last tick is the
trigger's `set_fired` row, so a restart neither double-fires nor skips more than one tick.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from .envelope import now_utc
from .stats import stats_table
from .views import _render

CLOCK_SECONDS = float(os.getenv("TARES_SCHEDULE_CLOCK_SECONDS", "15"))
RECENT_LINES_PER_SOURCE = 3


def is_scheduled(trig) -> bool:
    return bool(getattr(trig.condition, "every", None))


def fire_key(trig) -> str:
    """One firing per tick for the whole view: the view names the tick."""
    return trig.view


def _aware(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summary(store, catalog, trig) -> str:
    view = catalog.views[trig.view]
    every = trig.condition.every
    window = f"{int(every)}s" if every % 60 else f"{int(every // 60)}m"
    labels = trig.condition.summary_by or [view.key_field or "key_value"]
    out = [f"=== scheduled look at view {view.name} · every {window} · "
           f"this window against the one before ===", ""]
    for label in labels:
        out.append(stats_table(store, view, label, window))
        out.append("")
    since = now_utc() - timedelta(seconds=every)
    rows = store.read_view_window(view.sources, None, since, cap=RECENT_LINES_PER_SOURCE,
                                  filters=view.filters)
    lines, _ = _render(rows)
    out.append("most recent lines:")
    out += lines or ["(no events in this window)"]
    return "\n".join(out)


def due(store, trig, now=None) -> bool:
    if getattr(trig, "paused", False):
        return False
    last = store.last_fired(trig.name, fire_key(trig))
    now = now or now_utc()
    return last is None or (now - _aware(last)).total_seconds() >= trig.condition.every


async def tick(store, catalog, dispatcher, now=None) -> list:
    """Fire every schedule trigger that is due. Returns the names fired."""
    fired = []
    for trig in catalog.triggers:
        if not is_scheduled(trig) or trig.view not in catalog.views:
            continue
        if not due(store, trig, now):
            continue
        # recorded before the render and delivery, with no await in between, as condition
        # triggers do: two overlapping ticks cannot both fire
        store.set_fired(trig.name, fire_key(trig), now or now_utc())
        try:
            payload = summary(store, catalog, trig)
        except Exception as e:   # a broken summary must not stop the clock; say so in the payload
            payload = f"(the summary for view {trig.view} failed: {type(e).__name__}: {e})"
        await dispatcher.fire(trig, fire_key(trig), payload)
        fired.append(trig.name)
    return fired


async def run(runtime, stop: asyncio.Event) -> None:
    """The daemon's clock. Reads the live catalog each pass, so a trigger created, edited or
    paused takes effect on the next pass."""
    while not stop.is_set():
        try:
            await tick(runtime.store, runtime.catalog, runtime.dispatcher)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"taresd: schedule tick failed: {type(e).__name__}: {e}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=CLOCK_SECONDS)
        except asyncio.TimeoutError:
            pass
