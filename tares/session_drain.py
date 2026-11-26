"""The M1 drain (P-TR-295, TR-1665): in the factory edition, the ONE reader of claude_code lines in
DuckDB's `events` once the one-time move is complete (`session_move.events_done`). Milestone 2, which
drops those lines from DuckDB, deletes this file, `session_ingest._drain_pass`,
`session_ingest.mark_drain_todo` and the two mark names.

A factory page reads only the session store. The drain copies into it what a write missed, for the
sessions that say so and no other:

- a session born in the table with `lines_complete` false: the store refused some of its lines
  (`session_ingest.record_first`), or a write failed before the factory wrote the store first;
- a session with the todo mark: the move could not fill its facts and turns (`session_move._fill`).

With no such session it does not read DuckDB. It runs in the background job of `session_ingest`
(at start, then every `CYCLE`), never for a request, at most `BATCH` sessions a pass.

A session is rebuilt as the move rebuilds one, under `Store._sess_lock`: its DuckDB lines are read in
chunks first (the DuckDB lock let go between chunks), then one session-store transaction replaces its
rows and archive parts by every DuckDB line plus the lines only the store holds, recounts it from
them and writes their facts and turns. The rows are counted before the session is marked complete.

A line of DuckDB the store will not take is left out. So is a counted line neither store holds any
more. Such a session stays `lines_complete` false and gets the gave-up mark, said once: it is not
tried again until a later write of its lines is refused too. A try that fails as a whole (the
database is away, or a line only the session store holds cannot be written back) changes nothing,
goes to the back of the queue and is said once per process.

A rebuild that stored the lines but could not write their facts and turns keeps the todo mark, so
the next pass tries again, until both are in; it is said once per process (TR-1647).
"""
from __future__ import annotations

import threading
from collections import Counter

from sqlalchemy import and_, delete, exists, insert, literal, or_, select, text, update

from . import session_archive, session_ingest, session_move, session_read
from .envelope import now_utc
from .session_store.tables import session_archive_parts, session_ingest_marks, session_lines, sessions

BATCH = 20                           # sessions taken by one pass


def marked(store, limit: int = BATCH) -> list[str]:
    """The sessions the drain has to repair, the longest untouched first."""
    s, m = sessions.c, session_ingest_marks.c

    def mark(prefix):
        return exists(select(m.name).where(m.name == literal(prefix) + s.session))

    with store.sessions_db.read() as conn:
        return [r[0] for r in conn.execute(
            select(s.session).where(
                s.lines_origin == session_ingest.TABLE,
                or_(and_(s.lines_complete.is_(False), ~mark(session_ingest.DRAIN_GAVE_UP)),
                    mark(session_ingest.DRAIN_TODO)))
            .order_by(s.updated_at, s.session).limit(limit)).all()]


def drain(store, limit: int = BATCH, stop: threading.Event | None = None) -> int:
    """Repair up to `limit` marked sessions. Returns how many it took: 0 outside the factory
    edition, before the move is complete, and when none is marked."""
    if not session_move.events_done(store):
        return 0
    sids = marked(store, limit)
    if not sids:
        return 0
    sources = sorted(session_ingest.claude_code_sources(store))
    n = 0
    for sid in sids:
        if stop is not None and stop.is_set():
            break
        n += 1
        try:
            said = _drain_session(store, sources, sid)
        except Exception as e:
            _later(store, sid, e)
            continue
        if said:
            print(said, flush=True)
    return n


def _drain_session(store, sources: list, sid: str) -> str | None:
    """Rebuild one session from DuckDB. Returns the line to log, None when there is none to say:
    the hourly archive job holds the session (the next pass takes it), it is gone, or its facts and
    turns are still not written and that was said before."""
    db = store.sessions_db
    with store._sess_lock, session_move._duck_lines(store, sources, sid) as (wanted, chunks):
        with db.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('tares_session_move:' "
                                  "|| CAST(:s AS text)))"), {"s": sid})
                if not session_archive._lock(conn, sid):
                    return None
            return _rebuild(store, conn, sid, wanted, chunks)


def _rebuild(store, conn, sid: str, wanted: int, chunks) -> str | None:
    db = store.sessions_db
    s = sessions.c
    row = conn.execute(select(s.source, s.line_count, s.tool_calls, s.coordination_calls,
                              s.started_at, s.last_line_at).where(
        s.session == sid, s.lines_origin == session_ingest.TABLE)).first()
    if row is None:
        return None                      # purged since it was listed
    have = session_move._existing(store, conn, sid)
    conn.execute(delete(session_lines).where(session_lines.c.session == sid))
    conn.execute(delete(session_archive_parts).where(session_archive_parts.c.session == sid))

    seq = refused = tools = coord = 0
    times = [session_move._utc(t) for t in (row.started_at, row.last_line_at) if t is not None]
    seen: Counter = Counter()
    unread: list = []                     # why facts or turns were not written, if so

    def take(rows: list) -> None:
        nonlocal seq, refused, tools, coord
        if not rows:
            return
        refused += _insert(conn, [{**{k: r[k] for k in ("line_key", "at", "type", "labels",
                                                        "render", "payload")},
                                   "session": sid, "seq": seq + i + 1} for i, r in enumerate(rows)])
        seq += len(rows)
        lines = []
        for r in rows:
            try:
                words = session_read._text(r)
            except (ValueError, TypeError):      # a payload that is not an object says nothing
                words = ""
            if r["type"] == "tool_use":
                tools += 1
                coord += session_ingest.coordination_in(words, r["payload"])
            if r["at"] is not None:
                at = session_move._utc(r["at"])
                times[:] = [min([*times, at]), max([*times, at])]
            lines.append((row.source, sid, r["at"], r["type"], words, r["payload"], r["labels"]))
        try:
            with conn.begin_nested():
                session_ingest._write_facts(conn, db, session_ingest.facts_of(
                    line[:6] for line in lines))
                session_ingest._write_turns(conn, db, session_ingest.turns_of(lines))
        except Exception as e:
            unread.append(f"{type(e).__name__}: {e}")

    read = 0
    for rows in chunks:
        for r in rows:
            seen[(r["line_key"], session_move._utc(r["at"]))] += 1
        read += len(rows)
        take(rows)
    extra = session_move._extras(have, seen)
    left_out = refused
    for i in range(0, len(extra), session_move.CHUNK):
        take(extra[i:i + session_move.CHUNK])
    if refused > left_out:
        # only the session store holds these lines (they are not in DuckDB): leaving one out would
        # lose it, so nothing of this try is kept (TR-1647)
        raise session_move.Mismatch(f"{refused - left_out} line(s) only the session store holds "
                                    "could not be written back")

    written = session_move._count_rows(conn, sid)
    if read != wanted or written != seq - refused:
        raise session_move.Mismatch(f"{read} of {wanted} DuckDB lines read, {written} rows "
                                    f"written of {seq - refused}")
    # every line it counted, and none left out
    complete = not refused and written >= row.line_count
    values = dict(lines_complete=complete, updated_at=now_utc())
    if complete:
        values.update(line_count=written, tool_calls=tools, coordination_calls=coord)
    else:
        # never below the highest seq: the next line of the session takes line_count + 1
        values.update(line_count=max(row.line_count, seq), tool_calls=max(row.tool_calls or 0, tools),
                      coordination_calls=max(row.coordination_calls or 0, coord))
    if times:
        values.update(started_at=min(times), last_line_at=max(times))
    conn.execute(update(sessions).where(s.session == sid).values(**values))
    m = session_ingest_marks.c
    conn.execute(delete(session_ingest_marks).where(m.name.in_(
        [session_ingest.DRAIN_TODO + sid, session_ingest.DRAIN_GAVE_UP + sid])))
    if not complete:
        conn.execute(insert(session_ingest_marks).values(
            name=session_ingest.DRAIN_GAVE_UP + sid, at=now_utc()))
    if unread:      # facts and turns still to be written: marked until both land (TR-1647)
        conn.execute(insert(session_ingest_marks).values(
            name=session_ingest.DRAIN_TODO + sid, at=now_utc()))
    said = f"taresd: M1 drain: session {sid} rebuilt from DuckDB's lines, {written} line(s) stored"
    if not complete:
        said += (f", {max(row.line_count, seq) - written} cannot be stored or are gone; the session "
                 "stays marked incomplete" + ("" if unread else " and is not tried again"))
    if unread:
        said += (f"; some of its facts and turns were not written ({unread[0]}); tried again at a "
                 "later pass")
        waiting = store.__dict__.setdefault("_drain_unread", set())   # only holds back the repeat
        if sid in waiting:
            return None
        waiting.add(sid)
    else:
        store.__dict__.get("_drain_unread", set()).discard(sid)
    return said


def _insert(conn, rows: list) -> int:
    """Insert `rows` into `session_lines`, leaving out those the store will not take (found by
    halving, each try in a savepoint). Returns how many were left out. Raises when the store fails
    for a plain line too."""
    try:
        with conn.begin_nested():
            conn.execute(insert(session_lines), rows)
        return 0
    except Exception:
        if len(rows) == 1:
            probe = conn.begin_nested()
            try:
                conn.execute(insert(session_lines).values(
                    session=session_ingest.PROBE_SESSION, seq=1, payload="{}"))
            finally:
                probe.rollback()
            return 1
        mid = len(rows) // 2
        return _insert(conn, rows[:mid]) + _insert(conn, rows[mid:])


def _later(store, sid: str, e: Exception) -> None:
    """A try that failed as a whole: the marks stay, the session goes to the back of the queue, and
    it is said once per process."""
    try:
        with store.sessions_db.begin() as conn:
            conn.execute(update(sessions).where(sessions.c.session == sid)
                         .values(updated_at=now_utc()))
    except Exception:
        pass
    said = store.__dict__.setdefault("_drain_said", set())    # only holds back the repeat
    if sid not in said:
        said.add(sid)
        print(f"taresd: M1 drain of session {sid} failed ({type(e).__name__}: {e}); tried again "
              "at a later pass", flush=True)
