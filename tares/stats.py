"""Counts per label value over a set of sources, the last window against the one before (TR-319).

One function, two callers: the `stats` tool a Tares agent calls, and the summary a schedule
trigger hands the agent it wakes (TR-320), so both read the same table. `stats_rows` is the same
count as data, for a caller that needs the values themselves (the decision watcher's entity
options, TR-382).
"""
from __future__ import annotations

from datetime import timedelta

from .config import parse_duration
from .envelope import now_utc

STATS_MAX_TOP = 50


def _change(c: int, p: int) -> str:
    if p == 0:
        return "new" if c else "none"
    if c == 0:
        return "gone"
    return f"{c - p:+d} (x{c / p:.1f})"


def stats_rows(store, sources: list, by: str, window: str, where: dict | None = None,
               filters: list | None = None, project_rows: dict | None = None) -> dict:
    """Counts per value of `by`, now against the window before, as data: `rows` (each
    {value, now, before, change}, largest absolute change first) and the two totals."""
    span = parse_duration(window)
    if span <= 0:
        raise ValueError(f"bad window {window!r}")
    now = now_utc()
    start = now - timedelta(seconds=span)
    before_start = start - timedelta(seconds=span)
    sources = list(sources)
    if sources:
        cur = store.aggregate(sources, None, "count", start, filters=filters,
                              where=where, group_by=by, scope=project_rows)
        prev = store.aggregate(sources, None, "count", before_start, filters=filters,
                               where=where, group_by=by, until=start, scope=project_rows)
    else:
        cur, prev = {}, {}
    rows = []
    for value in set(cur) | set(prev):
        c, p = int(cur.get(value, 0)), int(prev.get(value, 0))
        rows.append({"value": str(value), "now": c, "before": p, "change": _change(c, p)})
    rows.sort(key=lambda r: (-abs(r["now"] - r["before"]), r["value"]))
    tc, tp = sum(int(x) for x in cur.values()), sum(int(x) for x in prev.values())
    return {"rows": rows, "total_now": tc, "total_before": tp}


def stats_table(store, sources: list, by: str, window: str, where: dict | None = None,
                top=20, filters: list | None = None, scope: str = "",
                project_rows: dict | None = None) -> str:
    """Counts per value of `by` over `sources` (narrowed by `filters`), now against the window
    before, as a few lines whatever the volume (TR-319). Largest absolute change first; a value
    only in one window reads as new or gone. `scope` names what was counted in the header (a
    trigger, a project); the sources are named when it is empty. `project_rows` narrows the shared
    findings and memory sources to one project (store._scope_sql)."""
    top = max(1, min(int(top), STATS_MAX_TOP))
    counted = stats_rows(store, sources, by, window, where=where, filters=filters,
                         project_rows=project_rows)
    rows = counted["rows"]
    sel = f" where {', '.join(f'{k}={v}' for k, v in where.items())}" if where else ""
    what = scope or ",".join(sources) or "(no sources)"
    lines = [f"stats: {what} by={by}{sel} window={window} "
             f"(now: last {window}, before: the {window} before that)",
             f"{by} | now | before | change"]
    lines += [f"{r['value']} | {r['now']} | {r['before']} | {r['change']}" for r in rows[:top]]
    if len(rows) > top:
        lines.append(f"... {len(rows) - top} more values")
    tc, tp = counted["total_now"], counted["total_before"]
    lines.append(f"total | {tc} | {tp} | {_change(tc, tp)}")
    if not rows:
        lines.append(f"no events with a {by!r} label in either window")
    return "\n".join(lines)
