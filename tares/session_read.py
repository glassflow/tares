"""Read a session's lines from `session_lines` and the archive instead of DuckDB (P-TR-295, TR-1507).

`Store.key_events`, `key_payload_rows` and `key_tool_result` (a session read top to bottom, the
conversation, one tool call's output) ask here first. A session whose `sessions.lines_complete` is
true, whose source is the one asked for and whose rows add up (`session_lines` plus the archived
parts' lines equal `line_count`) is read from its table rows and, when the hourly archive moved some
away, from its parts, merged by `(at, seq)`. The answer is the one DuckDB gives; `lines_from` says
where it came from: `table`, `archive` (every line is in parts), `both`, or `events` (DuckDB, the
fallback). Anything else reads DuckDB: a session the table does not hold in full, a part that
cannot be fetched or does not check out (size, MD5, lines), a row that does not parse. A fallback
logs one line, at most once a minute per session. Nothing is removed from DuckDB in M1. In the
factory edition with an archive the lines of new sessions are not written there at all (TR-1647), so
for them the table and the parts are the only copy.

In the factory edition a session born in the table (`lines_origin = 'table'`, which a verified move
sets too) is never read from DuckDB (TR-1613). It is answered with what the table and the readable
parts hold: a row or a part that cannot be read is left out, and one line says so. A source other
than the session's own answers with no lines. The platform editions fall back as above.

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

from sqlalchemy import exists, func, select

from . import edition, session_archive
from .session_ingest import TABLE
from .session_store.tables import session_archive_parts, session_lines, sessions

TOOL_RESULT_ROWS = 5          # DuckDB's `key_tool_result` looks at this many newest candidates
SESSION_PAGE = 20             # sessions `newest_payloads` takes at a time
_MIN = datetime.min.replace(tzinfo=timezone.utc)
_WARNED: dict[str, float] = {}


class Unreadable(Exception):
    """The table and archive cannot answer for this session in full."""


def _warn(session: str, why: str, fallback: bool = True) -> None:
    now = time.monotonic()
    if now - _WARNED.get(session, -1e9) < 60:
        return
    if len(_WARNED) > 1000:
        _WARNED.clear()
    _WARNED[session] = now
    if fallback:
        print(f"taresd: session {session} read from DuckDB, its lines in the table and archive "
              f"cannot be used ({why})", flush=True)
    else:
        print(f"taresd: session {session} is shown with the lines the table and archive can give, "
              f"which may not be all of them ({why})", flush=True)


def _factory(store) -> bool:
    try:
        return edition.edition(store)[0] == "factory"
    except Exception:
        return False


def _answer(store, session: str, read):
    """`read(None, seen)` answers only when the table and archive hold the session in full. When
    they do not, the caller reads DuckDB (None), except in the factory edition for a session born
    in the table: `read(left_out, {})` then answers with what can be read and notes what it left
    out, and an error of the session store goes to the caller. The edition is looked at only in
    that case, so a session read in full costs no statement more."""
    seen: dict = {}
    why = None
    try:
        got = read(None, seen)
        if got is not None:
            return got
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
    if seen.get("born") and _factory(store):
        left_out: list = []
        got = read(left_out, {})
        if left_out:
            _warn(session, "; ".join(left_out), fallback=False)
        return got
    if why:
        _warn(session, why)
    return None


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
    had. With it: what can be had, with what is missing noted in the list. `seen["born"]` is set
    once the session's row says whether it was born in the table. `tail` is for a page that is the
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
        seen["born"] = head.lines_origin == TABLE
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
    from .connectors.claude_code import ClaudeCodeConnector
    flags = json.loads(r["render"] or "{}")
    o = json.loads(r["payload"])
    if not isinstance(o, dict):
        raise ValueError("payload is not an object")
    return ClaudeCodeConnector.line_text(o, r["type"], bool(flags.get("redact", True)),
                                         bool(flags.get("include_thinking", False)))


def _line(r: dict) -> dict:
    return {"type": r["type"], "at": r["at"], "labels": json.loads(r["labels"]) if r["labels"] else {},
            "text": _text(r), "payload": r["payload"]}


def lines(store, source: str, session: str, limit: int, offset: int, tail: dict | None = None):
    """`([{type, at, labels (parsed), text, payload (the stored JSON text)}, ...], lines_from)`
    for a page of the session, oldest first; None when the caller must read DuckDB. `tail`: see
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
    return _answer(store, session, read)


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
    """`(the tool_result block of one call or None, lines_from)`; None when the caller must read
    DuckDB. The same newest-five search DuckDB makes."""
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
    return _answer(store, session, read)


def newest_payloads(store, source: str, limit: int, hidden_sessions: set, hidden_line) -> list[str]:
    """The payload texts of the `limit` newest lines `session_lines` holds for the sessions of
    `source`, newest first, leaving out the sessions of `hidden_sessions` and the lines
    `hidden_line(labels, payload)` names. Archived lines are not read. `session_lines` has no source
    and is indexed by `(session, at)`, so the sessions are taken newest first by their last line and
    reading stops when no session left can hold a newer line than the ones in hand."""
    s, t = sessions.c, session_lines.c
    q = (select(s.session, s.last_line_at)
         .where(s.source == source, exists(select(t.seq).where(t.session == s.session)))
         .order_by(s.last_line_at.desc().nulls_last(), s.session))
    kept: list = []                                  # (at, seq, payload), newest first
    offset = 0
    with store.sessions_db.read() as c:
        while limit > 0:
            page = c.execute(q.limit(SESSION_PAGE).offset(offset)).all()
            offset += len(page)
            for sid, last in page:
                if len(kept) >= limit and (last or _MIN) <= kept[-1][0]:
                    return [r[2] for r in kept]
                if sid in hidden_sessions:
                    continue
                mine: list = []
                start = 0
                while len(mine) < limit:
                    rows = c.execute(select(t.at, t.seq, t.labels, t.payload).where(t.session == sid)
                                     .order_by(t.at.desc(), t.seq.desc())
                                     .limit(limit).offset(start)).all()
                    mine += [(r.at or _MIN, r.seq, r.payload) for r in rows
                             if not hidden_line(r.labels, r.payload)]
                    if len(rows) < limit:
                        break
                    start += limit
                kept = sorted(kept + mine[:limit], key=lambda r: r[:2], reverse=True)[:limit]
            if len(page) < SESSION_PAGE:
                break
    return [r[2] for r in kept]
