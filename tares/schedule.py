"""Schedule triggers (TR-320): a trigger that fires on a clock instead of a condition.

A trigger whose condition carries `every` fires once per interval for all its sources, whether or
not anything matched: a quiet window is information too. The agent it wakes is handed a summary
of the window, not raw lines, because a busy stream is far too big to hand over and the render is
capped per source anyway: per `summary_by` label, the counts against the window before (the same
table as the `stats` tool), the totals, and a few recent lines.

The clock is one task in the daemon. It checks every CLOCK_SECONDS; the last tick is the
trigger's `set_fired` row, so a restart neither double-fires nor skips more than one tick.
"""
from __future__ import annotations

import asyncio
import math
import os
from datetime import timedelta

from .config import FINDINGS_SOURCE, parse_duration, trigger_entity_label
from .envelope import now_utc
from .stats import stats_table
from .reads import _render

CLOCK_SECONDS = float(os.getenv("TARES_SCHEDULE_CLOCK_SECONDS", "15"))
RECENT_LINES_PER_SOURCE = 3


def is_scheduled(trig) -> bool:
    return bool(getattr(trig.condition, "every", None))


def fire_key(trig) -> str:
    """One firing per tick for the whole trigger: the trigger names the tick."""
    return trig.name


def _aware(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summary(store, catalog, trig) -> str:
    every = trig.condition.every
    window = f"{int(every)}s" if every % 60 else f"{int(every // 60)}m"
    entity = trigger_entity_label(trig, catalog.sources) or "key_value"
    labels = trig.condition.summary_by or [entity]
    opts = getattr(trig.condition, "summary", None) or {}
    out = [f"=== scheduled look at {trig.name} ({', '.join(trig.sources)}) · every {window} · "
           f"this window against the one before ===",
           f"the entities here are values of `{entity}`; other labels describe them", ""]
    if opts:
        return _rich(store, trig, entity, labels, window, every, opts, out)
    for label in labels:
        out.append(stats_table(store, trig.sources, label, window, filters=trig.filters,
                               scope=trig.name))
        out.append("")
    since = now_utc() - timedelta(seconds=every)
    rows = store.read_window(trig.sources, None, since, cap=RECENT_LINES_PER_SOURCE,
                             filters=trig.filters)
    lines, _ = _render(rows)
    out.append("most recent lines:")
    out += lines or ["(no events in this window)"]
    return "\n".join(out)


# ── the richer summary (TR-400) ──────────────────────────────────────────────
# Generic: counts, history and samples over whatever labels the trigger names, nothing that
# knows what a status code or a service is. Every part is bounded, and the whole is cut at
# RICH_MAX_CHARS, so a busy stream costs the same as a quiet one.
RICH_TOP = 15              # rows per label table
RICH_TOP_BY_ENTITY = 10    # rows per label-by-entity table
RICH_TOP_NUMBERS = 10
RICH_MOVERS = 3            # values that get sample lines
RICH_MOVER_Z = 2.0         # with a baseline, how far above usual a value is to count as moved
RICH_FINDINGS = 8
RICH_MAX_CHARS = 16000


def _counts(store, trig, group, every: float) -> tuple[dict, dict]:
    """({group: count} this window, {group: count} the window before)."""
    now = now_utc()
    start = now - timedelta(seconds=every)
    cur = store.aggregate(trig.sources, None, "count", start, filters=trig.filters,
                          group_by=group)
    prev = store.aggregate(trig.sources, None, "count", start - timedelta(seconds=every),
                           filters=trig.filters, group_by=group, until=start)
    return cur, prev


def _history(store, trig, group, every: float, n: int) -> dict:
    """{group: (usual, spread)} over the n windows before this one."""
    if not n:
        return {}
    start = now_utc() - timedelta(seconds=every * (n + 1))
    hist = store.bucket_counts(trig.sources, group, start, every, n, filters=trig.filters)
    out = {}
    for g, counts in hist.items():
        mean = sum(counts) / n
        out[g] = (mean, math.sqrt(sum((c - mean) ** 2 for c in counts) / n))
    return out


def _change(c: int, p: int) -> str:
    if p == 0:
        return "new" if c else "none"
    if c == 0:
        return "gone"
    return f"{c - p:+d} (x{c / p:.1f})"


def _z(now: int, usual: float, spread: float) -> float:
    # a flat history has no spread; counts vary about sqrt(n) on their own, so that is the floor
    return (now - usual) / max(spread, math.sqrt(usual), 1.0)


def _rows(cur: dict, prev: dict, hist: dict, n: int, floor: int) -> tuple[list, int]:
    """Rows sorted by how unusual they are (z with history, else the absolute change), and how
    many were left out under the floor."""
    rows, small = [], 0
    for g in set(cur) | set(prev) | set(hist):
        c, p = int(cur.get(g, 0)), int(prev.get(g, 0))
        usual, spread = hist.get(g, (0.0, 0.0)) if n else (None, None)
        if floor and max(c, p, usual or 0) < floor:
            small += 1
            continue
        if not c and not p:
            continue
        z = _z(c, usual, spread) if n else None
        rows.append({"g": g, "now": c, "before": p, "usual": usual, "spread": spread, "z": z,
                     "change": _change(c, p)})
    rows.sort(key=lambda r: (-(abs(r["z"]) if r["z"] is not None else abs(r["now"] - r["before"])),
                             str(r["g"])))
    return rows, small


def _cols(r: dict, n: int) -> str:
    base = f"{r['now']} | {r['before']}"
    if n:
        base += f" | {r['usual']:.0f} ± {r['spread']:.0f} | {r['z']:+.1f}"
    return f"{base} | {r['change']}"


def _rich(store, trig, entity: str, labels: list, window: str, every: float, opts: dict,
          out: list) -> str:
    n = int(opts.get("baseline") or 0)
    floor = int(opts.get("min_count") or 0)
    head = "now | before | usual ± spread | z | change" if n else "now | before | change"
    if n:
        out.append(f"usual ± spread: the average of the {n} windows before this one and how much "
                   f"it varies; z: how far now is from usual, in those units")
    if floor:
        out.append(f"values under {floor} events in this window, the one before"
                   f"{' and usually' if n else ''} are left out")
    if n or floor:
        out.append("")
    movers = []           # (score, where, title) for sample lines
    listed = None         # the entities the entity table shows; numbers keep to these
    entity_values = []    # entities worth an earlier finding
    for label in labels:
        cur, prev = _counts(store, trig, label, every)
        rows, small = _rows(cur, prev, _history(store, trig, label, every, n), n, floor)
        out.append(f"{label} this window ({window}) against the one before")
        out.append(f"{label} | {head}")
        out += [f"{r['g']} | {_cols(r, n)}" for r in rows[:RICH_TOP]]
        if len(rows) > RICH_TOP:
            out.append(f"... {len(rows) - RICH_TOP} more values")
        if small:
            out.append(f"... {small} small values left out")
        tc, tp = sum(int(x) for x in cur.values()), sum(int(x) for x in prev.values())
        out.append(f"total | {tc} | {tp} | {_change(tc, tp)}")
        if not rows and not small:
            out.append(f"no events with a {label!r} label in either window")
        out.append("")
        for r in rows[:RICH_MOVERS]:
            if _moved(r, n):
                movers.append((_score(r), {label: r["g"]}, f"{label}={r['g']}"))
        if label == entity:
            entity_values += [r["g"] for r in rows[:RICH_FINDINGS] if r["now"]]
            listed = {r["g"] for r in rows[:RICH_TOP]}
    if opts.get("by_entity"):
        for label in labels:
            if label == entity:
                continue
            group = [label, entity]
            cur, prev = _counts(store, trig, group, every)
            rows, small = _rows(cur, prev, _history(store, trig, group, every, n), n, floor)
            if not rows:
                continue
            out.append(f"{label} by {entity}: which {entity} each value comes from")
            out.append(f"{label} | {entity} | {head}")
            out += [f"{r['g'][0]} | {r['g'][1]} | {_cols(r, n)}" for r in rows[:RICH_TOP_BY_ENTITY]]
            if len(rows) > RICH_TOP_BY_ENTITY:
                out.append(f"... {len(rows) - RICH_TOP_BY_ENTITY} more pairs")
            out.append("")
            for r in rows[:RICH_MOVERS]:
                if _moved(r, n):
                    movers.append((_score(r) + 0.5, {label: r["g"][0], entity: r["g"][1]},
                                   f"{label}={r['g'][0]} on {entity}={r['g'][1]}"))
                    entity_values.append(r["g"][1])
    for field in opts.get("numbers") or []:
        out += _numbers(store, trig, field, entity, every, listed)
    if opts.get("findings"):
        out += _earlier_findings(store, trig, entity, list(dict.fromkeys(entity_values)),
                                 opts["findings"])
    k = int(opts.get("examples") or 0)
    since = now_utc() - timedelta(seconds=every)
    if k and movers:
        out.append(f"sample lines for what moved most (up to {k} each):")
        seen, shown, n_shown = set(), set(), 0
        for _score_, where, title in sorted(movers, key=lambda m: -m[0]):
            key = tuple(sorted((a, str(b)) for a, b in where.items()))
            if key in seen:
                continue
            seen.add(key)
            rows = store.read_window(trig.sources, None, since, cap=k * 4, filters=trig.filters,
                                     where={a: str(b) for a, b in where.items()})
            lines, _ = _render(sorted(rows, key=lambda r: r[0], reverse=True))
            # one of each line, and none a mover above already showed
            fresh = []
            for x in lines:
                bare = _strip_age(x)
                if bare not in shown:
                    shown.add(bare)
                    fresh.append(x)
                if len(fresh) >= k:
                    break
            if not fresh:
                continue
            out.append(f"{title}:")
            out += [f"  {x}" for x in fresh]
            n_shown += 1
            if n_shown >= RICH_MOVERS:
                break
    else:
        rows = store.read_window(trig.sources, None, since, cap=RECENT_LINES_PER_SOURCE,
                                 filters=trig.filters)
        lines, _ = _render(rows)
        out.append("most recent lines:")
        out += lines or ["(no events in this window)"]
    text = "\n".join(out)
    if len(text) > RICH_MAX_CHARS:
        text = text[:RICH_MAX_CHARS] + f"\n... summary cut at {RICH_MAX_CHARS} characters"
    return text


def _strip_age(line: str) -> str:
    """A rendered line without its [T-…s] age, so the same line at two moments reads once."""
    return line.split("] ", 1)[1] if line.startswith("[T-") and "] " in line else line


def _moved(r: dict, n: int) -> bool:
    """Worth sample lines: up, and with a baseline at least 2 spreads above usual."""
    if not r["now"] or r["now"] <= r["before"] and not n:
        return False
    return r["z"] >= RICH_MOVER_Z if n else True


def _score(r: dict) -> float:
    return abs(r["z"]) if r["z"] is not None else float(abs(r["now"] - r["before"]))


def _numbers(store, trig, field: str, entity: str, every: float, keep: set | None) -> list:
    """avg and max of a numeric field per entity, now against before; only the entities the
    entity table shows (`keep`), and only those that carry the field."""
    now = now_utc()
    start = now - timedelta(seconds=every)
    before = start - timedelta(seconds=every)
    got = {}
    for agg in ("avg", "max"):
        got[agg] = (store.aggregate(trig.sources, field, agg, start, filters=trig.filters,
                                    group_by=entity),
                    store.aggregate(trig.sources, field, agg, before, filters=trig.filters,
                                    group_by=entity, until=start))
    groups = {g for g in set(got["avg"][0]) | set(got["avg"][1])
              if (keep is None or g in keep)
              and any(got[a][w].get(g) for a in ("avg", "max") for w in (0, 1))}
    if not groups:
        return [f"{field}: no numeric values in either window", ""]

    def num(v):
        return "-" if v is None else f"{v:.4g}"
    rows = []
    for g in groups:
        a_now, a_before = got["avg"][0].get(g), got["avg"][1].get(g)
        moved = abs((a_now or 0) - (a_before or 0)) / max(abs(a_before or 0), 1e-9)
        rows.append((moved, g, a_now, a_before, got["max"][0].get(g), got["max"][1].get(g)))
    rows.sort(key=lambda r: (-r[0], str(r[1])))
    out = [f"{field} per {entity}", f"{entity} | avg now | avg before | max now | max before"]
    out += [f"{g} | {num(an)} | {num(ab)} | {num(mn)} | {num(mb)}"
            for _m, g, an, ab, mn, mb in rows[:RICH_TOP_NUMBERS]]
    if len(rows) > RICH_TOP_NUMBERS:
        out.append(f"... {len(rows) - RICH_TOP_NUMBERS} more")
    return out + [""]


def _earlier_findings(store, trig, entity: str, values: list, lookback: str) -> list:
    """The newest finding on each entity in the window, so a known cause reads as known."""
    if not values:
        return []
    since = now_utc() - timedelta(seconds=parse_duration(lookback))
    found = store.recent_findings(FINDINGS_SOURCE, [str(v) for v in values[:RICH_FINDINGS * 2]],
                                  since, project=getattr(trig, "project", None))
    if not found:
        return [f"earlier findings on these {entity} values (last {lookback}): none", ""]
    out = [f"earlier findings on these {entity} values (last {lookback}):"]
    now = now_utc()
    for v, p in list(found.items())[:RICH_FINDINGS]:
        ago = int(max((now - _aware(p["at"])).total_seconds(), 0) // 60)
        what = p.get("headline") or " ".join(str(p.get("finding") or "").split())[:200]
        verdict = f" {p['verdict']}" if p.get("verdict") else ""
        out.append(f"- {v}:{verdict} by {p.get('agent') or 'an agent'}, {ago}m ago: {what}")
    return out + [""]


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
        if not is_scheduled(trig) or not trig.sources:
            continue
        if not due(store, trig, now):
            continue
        # recorded before the render and delivery, with no await in between, as condition
        # triggers do: two overlapping ticks cannot both fire
        store.set_fired(trig.name, fire_key(trig), now or now_utc())
        try:
            payload = summary(store, catalog, trig)
        except Exception as e:   # a broken summary must not stop the clock; say so in the payload
            payload = f"(the summary for {trig.name} failed: {type(e).__name__}: {e})"
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
