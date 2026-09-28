"""Counts per label value over a view, the last window against the one before (TR-319).

One function, two callers: the `stats` tool a Tares agent calls, and the summary a schedule
trigger hands the agent it wakes (TR-320), so both read the same table.
"""
from __future__ import annotations

from datetime import timedelta

from .config import parse_duration
from .envelope import now_utc

STATS_MAX_TOP = 50


def stats_table(store, view, by: str, window: str, where: dict | None = None,
                top=20) -> str:
    """Counts per value of `by` over the view, now against the window before, as a few lines
    whatever the volume (TR-319). Largest absolute change first; a value only in one window
    reads as new or gone."""
    span = parse_duration(window)
    if span <= 0:
        raise ValueError(f"bad window {window!r}")
    top = max(1, min(int(top), STATS_MAX_TOP))
    now = now_utc()
    start = now - timedelta(seconds=span)
    before_start = start - timedelta(seconds=span)
    cur = store.aggregate(view.sources, None, "count", start, filters=view.filters,
                          where=where, group_by=by)
    prev = store.aggregate(view.sources, None, "count", before_start, filters=view.filters,
                           where=where, group_by=by, until=start)

    def change(c: int, p: int) -> str:
        if p == 0:
            return "new" if c else "none"
        if c == 0:
            return "gone"
        return f"{c - p:+d} (x{c / p:.1f})"

    rows = []
    for value in set(cur) | set(prev):
        c, p = int(cur.get(value, 0)), int(prev.get(value, 0))
        rows.append((abs(c - p), str(value), c, p, change(c, p)))
    rows.sort(key=lambda r: (-r[0], r[1]))
    sel = f" where {', '.join(f'{k}={v}' for k, v in where.items())}" if where else ""
    lines = [f"stats: view={view.name} by={by}{sel} window={window} "
             f"(now: last {window}, before: the {window} before that)",
             f"{by} | now | before | change"]
    lines += [f"{v} | {c} | {p} | {ch}" for _d, v, c, p, ch in rows[:top]]
    if len(rows) > top:
        lines.append(f"... {len(rows) - top} more values")
    tc, tp = sum(int(x) for x in cur.values()), sum(int(x) for x in prev.values())
    lines.append(f"total | {tc} | {tp} | {change(tc, tp)}")
    if not rows:
        lines.append(f"no events with a {by!r} label in either window")
    return "\n".join(lines)
