"""Read a session's lines from `session_lines` and the archive (P-TR-295, TR-1507).

`Store.key_events`, `key_payload_rows` and `key_tool_result` (a session read top to bottom, the
conversation, one tool call's output) read here. A session is read from its table rows and, when the
hourly archive moved some away, from its parts, merged by `(at, seq)`; `lines_from` says where they
came from: `table`, `archive` (every line is in parts) or `both`. They are the only copy of the
lines. A session is answered with what the table and the readable parts hold: a row or a part that
cannot be read, or a session whose rows do not add up to its `line_count`, is answered with the rest,
and one line says so (at most once a minute per session). A source other than the session's own
answers with no lines.

The text of a line is not stored: it is rebuilt from the payload with the `render` flags the line
was ingested with (`ClaudeCodeConnector.line_text`, the same function ingest uses).

Ordering is `(at, seq)`: two lines at the same instant come out in the order they were stored.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import session_archive
from .session_store.tables import session_archive_parts, session_lines, sessions

TOOL_RESULT_ROWS = 5          # `tool_result` looks at this many newest candidates
_MIN = datetime.min.replace(tzinfo=timezone.utc)
_WARNED: dict[str, float] = {}


class Unreadable(Exception):
    """The table and archive cannot answer for this session in full."""


def _warn(session: str, why: str) -> None:
    now = time.monotonic()
    if now - _WARNED.get(session, -1e9) < 60:
        return
    if len(_WARNED) > 1000:
        _WARNED.clear()
    _WARNED[session] = now
    print(f"taresd: session {session} is shown with the lines the table and archive can give, "
          f"which may not be all of them ({why})", flush=True)


def _answer(store, session: str, read, empty):
    """`read(left_out, seen)` answers with what can be read and notes in `left_out` what it left
    out, which is said once; `empty` when the session has no row."""
    left_out: list = []
    got = read(left_out, {})
    if left_out:
        _warn(session, "; ".join(left_out))
    return empty if got is None else got


def _row(r) -> dict:
    return {"seq": r.seq, "at": r.at, "type": r.type, "labels": r.labels, "render": r.render,
            "payload": r.payload}


def _archived(r: dict) -> dict:
    at = r.get("at")
    return {"seq": r["seq"], "at": datetime.fromisoformat(at) if at else None,
            "type": r.get("type"), "labels": r.get("labels"), "render": r.get("render"),
            "payload": r.get("payload")}


def _key(r: dict):
    return (r["at"] or _MIN, r["seq"])


def _fetch(store, source: str, session: str, tool_needle: str | None, limit: int, offset: int,
           left_out: list | None, seen: dict, tail: dict | None = None):
    """The rows the caller wants as `session_lines` rows (dicts with `seq`, `at`, `type`, `labels`,
    `render`, `payload`), and where they came from. Without `left_out`: None for a session with no
    row, an incomplete one or another source, and `Unreadable` when the rows or a part cannot be
    had. With it: what can be had, with what is missing noted in the list. `tail` is for a page that is the
    session's end, its offset worked out from `line_count`: when fewer lines can be had, the page
    moves back to the newest of them, and `tail["start"]` says where it then starts."""
    db = store.sessions_db
    t = session_lines.c
    cols = [t.seq, t.at, t.type, t.labels, t.render, t.payload]
    with db.consistent_read() as c:
        head = c.execute(select(sessions.c.lines_complete, sessions.c.line_count, sessions.c.source,
                                sessions.c.lines_origin)
                         .where(sessions.c.session == session)).first()
        if head is None:
            return None
        if left_out is None:
            if not head.lines_complete or head.source != source:
                return None
        elif head.source != source:
            return [], "table"
        elif not head.lines_complete:
            left_out.append("not marked complete")
        parts = c.execute(select(session_archive_parts).where(
            session_archive_parts.c.session == session).order_by(
            session_archive_parts.c.part)).all()
        kept = c.execute(select(func.count()).where(t.session == session)).scalar_one()
        if kept + sum(p.lines or 0 for p in parts) != head.line_count:
            why = (f"{kept} rows and {sum(p.lines or 0 for p in parts)} archived lines "
                   f"for {head.line_count} counted")
            if left_out is None:
                raise Unreadable(why)
            left_out.append(why)
        if tail is not None and left_out is not None and not parts:
            offset = tail["start"] = min(offset, max(kept - limit, 0))
        q = select(*cols).where(t.session == session)
        if tool_needle is not None:
            find = func.strpos if db.dialect == "postgresql" else func.instr
            q = q.where(t.type == "tool_result", find(t.payload, tool_needle) > 0)
            q = q.order_by(t.at.desc(), t.seq.desc()).limit(TOOL_RESULT_ROWS)
        elif not parts:
            q = q.order_by(t.at, t.seq).limit(limit).offset(offset)
        else:
            q = q.order_by(t.at, t.seq)
        mine = [_row(r) for r in c.execute(q).all()]
    where = "archive" if parts and not kept else "both" if parts else "table"
    if not parts:
        return mine, where
    old: list = []
    backend = None
    for p in parts:
        try:
            if backend is None:
                backend = session_archive.backend_of(store)
            if backend is None:
                raise session_archive.ArchiveError("no archive is configured for this cell")
            old += [_archived(r) for r in session_archive.read_part(backend, p)]
        except Exception as e:
            if left_out is None:
                raise Unreadable(f"{type(e).__name__}: {e}") from e
            left_out.append(f"part {p.part}: {type(e).__name__}: {e}")
    if tool_needle is not None:
        old = [r for r in old if r["type"] == "tool_result" and tool_needle in (r["payload"] or "")]
        merged = sorted(mine + old, key=_key, reverse=True)[:TOOL_RESULT_ROWS]
    else:
        merged = sorted(old + mine, key=_key)
        if tail is not None and left_out is not None:
            offset = tail["start"] = min(offset, max(len(merged) - limit, 0))
        merged = merged[offset:offset + limit]
    return merged, where


def _text(r: dict) -> str:
    from .session_map import ClaudeCodeLines
    flags = json.loads(r["render"] or "{}")
    o = json.loads(r["payload"])
    if not isinstance(o, dict):
        raise ValueError("payload is not an object")
    return ClaudeCodeLines.line_text(o, r["type"], bool(flags.get("redact", True)),
                                         bool(flags.get("include_thinking", False)))


def _line(r: dict) -> dict:
    return {"type": r["type"], "at": r["at"], "labels": json.loads(r["labels"]) if r["labels"] else {},
            "text": _text(r), "payload": r["payload"]}


def lines(store, source: str, session: str, limit: int, offset: int, tail: dict | None = None):
    """`([{type, at, labels (parsed), text, payload (the stored JSON text)}, ...], lines_from)`
    for a page of the session, oldest first; no lines for a session with no row. `tail`: see
    `_fetch`."""
    def read(left_out, seen):
        got = _fetch(store, source, session, None, int(limit), int(offset), left_out, seen, tail)
        if got is None:
            return None
        rows, where = got
        out, bad = [], []
        for r in rows:
            try:
                out.append(_line(r))
            except Exception as e:
                if left_out is None:
                    raise
                bad.append(f"{type(e).__name__}: {e}")
        if bad:
            left_out.append(f"{len(bad)} row(s) that do not parse, the first: {bad[0]}")
        return out, where
    return _answer(store, session, read, ([], "table"))


def _result_block(r: dict, tool_use_id: str):
    try:
        o = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
    except ValueError:
        return None
    msg = o.get("message") if isinstance(o, dict) and isinstance(o.get("message"), dict) else {}
    for b in msg.get("content") or []:
        if (isinstance(b, dict) and b.get("type") == "tool_result"
                and str(b.get("tool_use_id")) == tool_use_id):
            return b
    return None


def tool_result(store, source: str, session: str, tool_use_id: str):
    """`(the tool_result block of one call or None, lines_from)`, from the newest five
    tool_result lines that name the call."""
    def read(left_out, seen):
        got = _fetch(store, source, session, tool_use_id, 0, 0, left_out, seen)
        if got is None:
            return None
        rows, where = got
        for r in rows:
            try:
                b = _result_block(r, tool_use_id)
            except Exception as e:
                if left_out is None:
                    raise
                left_out.append(f"a row that does not parse: {type(e).__name__}: {e}")
                continue
            if b is not None:
                return b, where
        return None, where
    return _answer(store, session, read, (None, "table"))
