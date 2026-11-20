"""Read a session's lines from `session_lines` and the archive instead of DuckDB (P-TR-295, TR-1507).

`Store.key_events`, `key_payload_rows` and `key_tool_result` (a session read top to bottom, the
conversation, one tool call's output) ask here first. A session whose `sessions.lines_complete` is
true, whose source is the one asked for and whose rows add up (`session_lines` plus the archived
parts' lines equal `line_count`) is read from its table rows and, when the hourly archive moved some
away, from its parts, merged by `(at, seq)`. The answer is the one DuckDB gives; `lines_from` says
where it came from: `table`, `archive` (every line is in parts), `both`, or `events` (DuckDB, the
fallback). Anything else reads DuckDB as before: a session the table does not hold in full, a part
that cannot be fetched or does not check out (size, MD5, lines), a row that does not parse. A
fallback logs one line, at most once a minute per session. Nothing is removed in M1, so DuckDB still
holds every line.

The text of a line is not stored: it is rebuilt from the payload with the `render` flags the line
was ingested with (`ClaudeCodeConnector.line_text`, the same function ingest uses). One difference
from DuckDB is possible, only where a secret straddles the 500-character cut: DuckDB cut the text
before redacting it, and the payload this rebuilds from was redacted first, so the table text hides
the secret DuckDB's text could show in part.

Ordering is `(at, seq)`. DuckDB orders by `(event_time, ingest_time)`; two lines at the same
instant come out in the order they were stored in both.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import session_archive
from .session_store.tables import session_archive_parts, session_lines, sessions

TOOL_RESULT_ROWS = 5          # DuckDB's `key_tool_result` looks at this many newest candidates
_MIN = datetime.min.replace(tzinfo=timezone.utc)
_WARNED: dict[str, float] = {}


class Unreadable(Exception):
    """The table and archive cannot answer for this session; the caller reads DuckDB."""


def _warn(session: str, why: str) -> None:
    now = time.monotonic()
    if now - _WARNED.get(session, -1e9) < 60:
        return
    if len(_WARNED) > 1000:
        _WARNED.clear()
    _WARNED[session] = now
    print(f"taresd: session {session} read from DuckDB, its lines in the table and archive "
          f"cannot be used ({why})", flush=True)


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


def _fetch(store, source: str, session: str, tool_needle: str | None, limit: int, offset: int):
    """The rows the caller wants as `session_lines` rows (dicts with `seq`, `at`, `type`, `labels`,
    `render`, `payload`), and where they came from. Raises `Unreadable` when they cannot be had."""
    db = store.sessions_db
    t = session_lines.c
    cols = [t.seq, t.at, t.type, t.labels, t.render, t.payload]
    with db.snapshot() as c:
        head = c.execute(select(sessions.c.lines_complete, sessions.c.line_count, sessions.c.source)
                         .where(sessions.c.session == session)).first()
        if head is None or not head.lines_complete:
            return None
        if head.source != source:
            return None
        parts = c.execute(select(session_archive_parts).where(
            session_archive_parts.c.session == session).order_by(
            session_archive_parts.c.part)).all()
        kept = c.execute(select(func.count()).where(t.session == session)).scalar_one()
        if kept + sum(p.lines or 0 for p in parts) != head.line_count:
            raise Unreadable(f"{kept} rows and {sum(p.lines or 0 for p in parts)} archived lines "
                             f"for {head.line_count} counted")
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
    try:
        backend = session_archive.backend_of(store)
        if backend is None:
            raise session_archive.ArchiveError("no archive is configured for this cell")
        old = []
        for p in parts:
            old += [_archived(r) for r in session_archive.read_part(backend, p)]
    except Exception as e:
        raise Unreadable(f"{type(e).__name__}: {e}") from e
    if tool_needle is not None:
        old = [r for r in old if r["type"] == "tool_result" and tool_needle in (r["payload"] or "")]
        merged = sorted(mine + old, key=_key, reverse=True)[:TOOL_RESULT_ROWS]
    else:
        merged = sorted(old + mine, key=_key)[offset:offset + limit]
    return merged, where


def _text(r: dict) -> str:
    from .connectors.claude_code import ClaudeCodeConnector
    flags = json.loads(r["render"] or "{}")
    o = json.loads(r["payload"])
    if not isinstance(o, dict):
        raise ValueError("payload is not an object")
    return ClaudeCodeConnector.line_text(o, r["type"], bool(flags.get("redact", True)),
                                         bool(flags.get("include_thinking", False)))


def lines(store, source: str, session: str, limit: int, offset: int):
    """`([{type, at, labels (parsed), text, payload (the stored JSON text)}, ...], lines_from)`
    for a page of the session, oldest first; None when the caller must read DuckDB."""
    try:
        got = _fetch(store, source, session, None, int(limit), int(offset))
        if got is None:
            return None
        rows, where = got
        return [{"type": r["type"], "at": r["at"], "labels": json.loads(r["labels"]) if r["labels"] else {},
                 "text": _text(r), "payload": r["payload"]} for r in rows], where
    except Exception as e:
        _warn(session, f"{type(e).__name__}: {e}")
        return None


def tool_result(store, source: str, session: str, tool_use_id: str):
    """`(the tool_result block of one call or None, lines_from)`; None when the caller must read
    DuckDB. The same newest-five search DuckDB makes."""
    try:
        got = _fetch(store, source, session, tool_use_id, 0, 0)
        if got is None:
            return None
        rows, where = got
        for r in rows:
            try:
                o = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
            except ValueError:
                continue
            msg = o.get("message") if isinstance(o, dict) and isinstance(o.get("message"), dict) else {}
            for b in msg.get("content") or []:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and str(b.get("tool_use_id")) == tool_use_id):
                    return b, where
        return None, where
    except Exception as e:
        _warn(session, f"{type(e).__name__}: {e}")
        return None
