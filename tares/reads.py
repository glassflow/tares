"""Reads: sources (and filters) + an entity + a window -> the rendered, time-ordered timeline.

This is the read path the agent sees: the payload a trigger hands the agents it wakes, and the
raw label-native `read`. The rendered format matches the cookbook dummy exactly, so the agent path
is byte-identical; only the backing (DuckDB scan vs in-process pull) differs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import Catalog, parse_duration
from .envelope import now_utc


def parse_window(window: str) -> timedelta:
    return timedelta(seconds=parse_duration(window))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _labels(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw:
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _render(rows, include_payload: bool = False) -> tuple[list, list]:
    """From read_window rows → (payload lines, structured rows). The labels the connector
    extracted (endpoint, status, …) are appended to each line and returned structured, so a read is
    self-describing: the dimensions you filtered/sliced by are visible on every row, for the human
    timeline and the agent payload alike. When `include_payload` is set, each row is a 5-tuple whose
    trailing element is the raw stored record; it's parsed and attached as `raw` so agents can read
    the full lossless event, not just the summary `text`."""
    now = now_utc()
    lines, structured = [], []
    for row in rows:
        event_time, source, text, labels = row[0], row[1], row[2], row[3]
        ago = int(max((now - _aware(event_time)).total_seconds(), 0))
        lbls = _labels(labels)
        suffix = ("  ·  " + "  ".join(f"{k}={v}" for k, v in lbls.items())) if lbls else ""
        sub = (text or "").splitlines() or [""]
        for i, line in enumerate(sub):
            lines.append(f"[T-{ago}s] [{source}] {line}" + (suffix if i == 0 else ""))
        entry = {"offset": f"T-{ago}s", "source": source, "text": (text or ""), "labels": lbls}
        if include_payload:
            raw = row[4]
            try:
                entry["raw"] = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            except (ValueError, TypeError):
                entry["raw"] = raw   # stored non-JSON payload — pass through as-is
        structured.append(entry)
    return lines, structured


def _wrap(scope: str, selector: str, window: str, lines: list, empty: bool) -> str:
    out = [f"=== {scope} · {selector} · window={window} · ONE Tares read ===", "", *lines]
    if empty:
        out.append("(no events for this selector in the window)")
    return "\n".join(out)


def _selector(key, where) -> str:
    if where:
        return ", ".join(f"{k}={v}" for k, v in where.items())
    return f"key={key}" if key is not None else "all"


def resolve_sources_full(store, sources: list, scope: str, key=None, window: str = "15m",
                         where: dict | None = None, filters: list | None = None,
                         include_payload: bool = False) -> tuple[str, int, list]:
    """(rendered payload, row count, structured rows) for an entity across `sources`, narrowed by
    `filters`. `scope` heads the payload (a trigger's name). The entity is selected by `key`
    and/or `where`; `include_payload` adds the raw lossless record as `raw` on each row."""
    since = now_utc() - parse_window(window)
    rows = store.read_window(list(sources), key, since, filters=filters, where=where,
                             include_payload=include_payload) if sources else []
    lines, structured = _render(rows, include_payload)
    return _wrap(scope, _selector(key, where), window, lines, not rows), len(rows), structured


def resolve_trigger(store, trig, key=None, window: str = "15m",
                    where: dict | None = None) -> str:
    """The payload a trigger hands the agents it wakes: its sources, through its filters."""
    return resolve_sources_full(store, trig.sources, trig.name, key=key, window=window,
                                where=where, filters=trig.filters)[0]


def resolve_read(store, catalog: Catalog, where: dict, window: str = "15m",
                 include_payload: bool = False,
                 sources: list | None = None) -> tuple[str, int, list, list]:
    """Raw label-native read. `where` is a {label: value} conjunction (strict AND). `sources`
    narrows which sources are read (None = all of them). Reading every source is self-pruning: a
    source that doesn't stamp one of the selector's labels yields NULL for it and drops out, so
    the result is exactly the strict-AND match. Returns (rendered payload, row count,
    contributing sources, structured rows). `include_payload` adds the raw lossless record as
    `raw` on each structured row."""
    since = now_utc() - parse_window(window)
    names = sorted(catalog.sources) if sources is None else sorted(set(sources))
    rows = store.read_window(names, None, since, filters=None, where=where,
                             include_payload=include_payload) if names else []
    lines, structured = _render(rows, include_payload)
    payload = _wrap("read", _selector(None, where), window, lines, not rows)
    contributing = sorted({r[1] for r in rows})
    return payload, len(rows), contributing, structured
