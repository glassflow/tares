"""The one-time move of older lines out of DuckDB (P-TR-295, TR-1609 and TR-1611).

In milestone 1 a line went to `session_lines` only from the upgrade on, while DuckDB got every line,
so DuckDB holds every line written before RC 1. A session read from the table must hold all of its
lines, so on the first start of RC 1 a background job rebuilds every `claude_code` session. One whose
last line is inside the archive window (`ARCHIVE_AFTER_DAYS`) goes into `session_lines`; an older one
goes straight to archive parts, without passing through the table, because the hourly job would
archive it at once. Nothing is ever deleted from DuckDB.

The ledger is `session_moves`, one row per session with its state: `pending`, `verified` or
`failed`.

- `plan` adds a `pending` row for each session in DuckDB's events, once (the mark
  `session_move_planned` in `session_ingest_marks`). A second call, or a second start, adds nothing.
- `move_recent` takes pending sessions whose last line is inside the window, oldest first, and moves
  each in one session-store transaction: every DuckDB line (read before the transaction opens, in
  chunks ordered by `event_time` then `rowid`, with the DuckDB lock let go between chunks), then
  every line the table or the archive holds that DuckDB does not (written on RC 1 with the gate
  shut by `HOLD_LINES_BACK`, or late). A line is matched by `(line_key, at)` as a multiset, because
  `line_key` repeats. The
  session's rows in `session_lines` and its parts in `session_archive_parts` are replaced by the
  full list, numbered from 1; the archive files of the replaced parts stay where they are, unused.
  Those extra lines come after all the DuckDB lines in the numbering, not merged by time.
  The count of rows written is checked against the lines read. A session that checks out is
  `verified` and `sessions.lines_complete` is set; one that does not is `failed` with the counts in
  `error`, and its transaction is rolled back, so its rows are as they were. Any other error (the
  database blipped, a part did not read) also rolls the transaction back, but leaves the session
  `pending` with the error and an `attempts` count, to be tried again after `RETRY_AFTER`; after
  `MAX_ATTEMPTS` tries it is `failed`.
- `move_old` takes pending sessions whose last line is outside the window, builds the same full
  list, and writes it as archive parts of at most `PART_LINES` lines, `seq` from 1. Then one
  transaction replaces the session's rows in `session_archive_parts`, deletes its rows in
  `session_lines`, sets the same columns of `sessions`, and marks it `verified` (target `archive`)
  when the parts hold as many lines as the list. A cell with no archive (Postgres without
  `TARES_ARCHIVE_URL`) skips these sessions, says so once, and their lines stay in DuckDB.
- `run` is the daemon's background thread: it waits for the sessions table to catch up, plans, and
  moves batches of 20 sessions with a pause between them, the recent ones first. It resumes from
  the ledger after a restart, and stops between sessions when `stop` is set.

An older session's parts are written with no lock held, so a line may arrive, or the hourly archive
job may write a part, before the replacing transaction. That transaction reads the session again
(its row, parts, table rows and DuckDB count); if any of it differs, nothing is replaced and the
session stays `pending` for another go after `RETRY_AFTER`, with no attempt counted (`move_recent`
takes it when the new line made it recent).
The parts are named by their content (see `session_archive`), so writing them never changes a file
a committed row points to. They are numbered after the session's last part, so an hourly pass that
read the session before the move committed runs into the move's part number and rolls back.

The move is `complete` when the plan ran and no session is `pending`. From then on, in the factory
edition, the passes of `session_ingest` read no claude_code line from DuckDB (`events_done`, TR-1613).

A recent session is moved under `Store._sess_lock`, as the reconcile and `Store.append` take it, so no
line is counted into it meanwhile. On Postgres the transaction also takes
`pg_advisory_xact_lock` on the session, which lets go with the transaction (PgBouncer in transaction
mode allows nothing longer). On SQLite the transaction is the write lock: other writers wait on its
busy timeout while one session is rebuilt. The replacing transaction of an older session runs under
`Store._sess_lock` too; on Postgres it also tries the hourly job's lock
(`tares_session_archive:`), leaving the session `pending` when the job holds it, and locks the
session's row, where another process's ingest waits. Ingest of this process waits on
`Store._sess_lock`, so on Postgres neither transaction waits longer than `LOCK_WAIT` for a row or
advisory lock another holds: it rolls back, and the session is tried again after `RETRY_AFTER`,
with no attempt counted. On SQLite the wait for the write lock is the store's busy timeout.

Nothing reads DuckDB inside either transaction. A store call holds the state lock, which on SQLite is
DuckDB's lock, while it writes the session store, so a read in there would wait on the call and the
call on the transaction, until one of them gave up with "database is locked". The lines of a recent
session read ahead wait in a temporary file beside the database, so a long one is not held in memory.
"""
from __future__ import annotations

import json
import os
import pickle
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import delete, func, insert, or_, select, text, update

from . import edition, metrics, session_archive, session_ingest
from .envelope import Envelope, now_utc
from .session_archive import ARCHIVE_AFTER_DAYS
from .session_store.tables import (session_archive_parts, session_ingest_marks, session_lines,
                                   session_moves, sessions)

# Set False only to park the move in a fix release: no plan, no move, and the sessions already
# verified stay as they are.
MOVE_ENABLED = True

PLANNED = "session_move_planned"     # the plan's row in session_ingest_marks
BATCH = 20                           # sessions taken at a time
CHUNK = 5000                         # DuckDB lines read under one hold of the lock
PAUSE = 0.5                          # seconds between batches
OLD_PAUSE = 1.0                      # seconds between batches of older sessions
PART_LINES = 5000                    # lines in one archive part of an older session, at most
WAIT = 2.0                           # seconds between looks at whether the sessions table caught up
MAX_ATTEMPTS = 5                     # tries of one session before it is `failed` for good
RETRY_AFTER = 30.0                   # seconds before a session that failed, or was left, is tried again
LOCK_WAIT = 0.5                      # seconds a transaction under `_sess_lock` waits for a lock
PENDING, VERIFIED, FAILED = "pending", "verified", "failed"
TARGET = "table"                     # where a recent session went
ARCHIVE = "archive"                  # where an older one went


class Mismatch(Exception):
    """The rows a move wrote are not the lines it read; the session is left as it was."""


class Changed(Exception):
    """DuckDB's lines of the session changed while they were read; the move is tried again."""


def _bound_lock_waits(conn) -> None:
    """On Postgres, no row or advisory lock is waited for longer than `LOCK_WAIT` in this
    transaction. `SET LOCAL` ends with it, as PgBouncer in transaction mode needs."""
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_WAIT * 1000)}"))


def _lock_timed_out(e: Exception) -> bool:
    """Postgres gave up a lock wait at `lock_timeout` (SQLSTATE 55P03)."""
    return getattr(getattr(e, "orig", None), "sqlstate", None) == "55P03"


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


# ── the plan ─────────────────────────────────────────────────────────────────────────────────
def planned(store) -> bool:
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == PLANNED)).first() is not None


def complete(store) -> bool:
    """True once the plan ran and no session is `pending`. A `failed` session does not hold it back:
    it was not born in the table, so a read of it still falls back to DuckDB (`session_read`). Only
    a True answer is kept on the store: the plan adds its rows once and a `verified` or `failed` row
    does not go back to `pending`, so losing the answer changes nothing."""
    if getattr(store, "_move_complete", False):
        return True
    with store.sessions_db.read() as conn:
        # the mark first: the plan writes it after its rows, so rows read next include them all
        if conn.execute(select(session_ingest_marks.c.at).where(
                session_ingest_marks.c.name == PLANNED)).first() is None:
            return False
        if conn.execute(select(session_moves.c.session).where(
                session_moves.c.state == PENDING).limit(1)).first() is not None:
            return False
    store._move_complete = True
    return True


def events_done(store) -> bool:
    """True in the factory edition once the move is complete: the start-up and ten-minute passes
    then read no claude_code line from DuckDB's events (TR-1613). The platform editions keep reading
    them, whatever the ledger says."""
    return edition.edition(store)[0] == "factory" and complete(store)


def plan(store) -> int:
    """Add a `pending` row for each claude_code session in DuckDB's events, with its line count,
    once. Returns how many rows it added (0 on a later call). A row that exists is left as it is,
    so a plan that stopped half way is finished by the next one."""
    if planned(store):
        return 0
    sources = sorted(session_ingest.claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    with store._lock:
        found = store.con.execute(
            f"SELECT key_value, count(*) FROM events WHERE source IN ({marks}) "
            "AND key_value IS NOT NULL AND key_value <> '' GROUP BY key_value ORDER BY key_value",
            sources).fetchall()
    db = store.sessions_db
    before = sum(counts(store).values())
    for i in range(0, len(found), 500):
        rows = [{"session": sid, "state": PENDING, "target": TARGET, "lines_events": int(n)}
                for sid, n in found[i:i + 500]]
        with db.begin() as conn:
            conn.execute(db.upsert(session_moves).on_conflict_do_nothing(
                index_elements=["session"]), rows)
    stmt = db.upsert(session_ingest_marks).values(name=PLANNED, at=now_utc())
    with db.begin() as conn:
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["name"]))
    refresh_gauges(store)
    return sum(counts(store).values()) - before


# ── one session ──────────────────────────────────────────────────────────────────────────────
def _json(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v) if v else {}
        except ValueError:
            return {}
    return {} if v is None else v


def _duck_count(store, sources: list, sid: str) -> int:
    marks = ", ".join("?" for _ in sources)
    with store._lock:
        return store.con.execute(
            f"SELECT count(*) FROM events WHERE source IN ({marks}) AND key_value = ?",
            [*sources, sid]).fetchone()[0]


def _duck_chunks(store, sources: list, sid: str):
    """The session's DuckDB lines as envelopes, in chunks of CHUNK, ordered by `event_time` then
    `rowid`. The store's lock is held for one chunk's read only."""
    marks = ", ".join("?" for _ in sources)
    after = None
    while True:
        args = [*sources, sid]
        cond = ""
        if after is not None:
            cond = "AND (event_time, rowid) > (?, ?) "
            args += after
        with store._lock:
            rows = store.con.execute(
                "SELECT source, source_type, key_value, event_type, text, payload, labels, "
                f"event_time, ingest_time, rowid FROM events WHERE source IN ({marks}) "
                f"AND key_value = ? {cond}ORDER BY event_time, rowid LIMIT {CHUNK}", args).fetchall()
        if not rows:
            return
        yield [Envelope(source=r[0], source_type=r[1], key_value=r[2], event_type=r[3],
                        text=r[4] or "", event_time=r[7], payload=_json(r[5]), labels=_json(r[6]),
                        ingest_time=r[8]) for r in rows]
        if len(rows) < CHUNK:
            return
        after = [rows[-1][7], rows[-1][9]]


def _table_rows(conn, sid: str) -> list[dict]:
    """The rows `session_lines` holds for the session, in `seq` order."""
    t = session_lines.c
    return [{"seq": r.seq, "line_key": r.line_key, "at": r.at, "type": r.type, "labels": r.labels,
             "render": r.render, "payload": r.payload}
            for r in conn.execute(select(t.seq, t.line_key, t.at, t.type, t.labels, t.render,
                                         t.payload).where(t.session == sid).order_by(t.seq)).all()]


def _parts(conn, sid: str) -> list:
    return conn.execute(select(session_archive_parts).where(
        session_archive_parts.c.session == sid).order_by(session_archive_parts.c.part)).all()


def _from_parts(backend, parts: list) -> list[dict]:
    """The lines of the archived parts, as `_table_rows` gives them. Raises when one cannot be
    read."""
    out = []
    for p in parts:
        for r in session_archive.read_part(backend, p):
            at = r.get("at")
            out.append({"seq": r["seq"], "line_key": r.get("line_key"),
                        "at": _utc(datetime.fromisoformat(at)) if at else None,
                        "type": r.get("type"), "labels": r.get("labels"),
                        "render": r.get("render"), "payload": r.get("payload")})
    return out


def _existing(store, conn, sid: str) -> list[dict]:
    """The lines the table and the archived parts hold for the session, in `seq` order, each as the
    columns of `session_lines` without `session` and `seq`. Raises when a part cannot be read."""
    out = _table_rows(conn, sid)
    parts = _parts(conn, sid)
    if parts:
        backend = session_archive.backend_of(store)
        if backend is None:
            raise session_archive.ArchiveError("parts exist but no archive is configured")
        out += _from_parts(backend, parts)
        out.sort(key=lambda r: r["seq"])
    return out


def _extras(have: list, seen: Counter) -> list:
    """The lines of `have` (table and archive) that DuckDB lacks: each (line_key, at) is matched with
    one DuckDB line of the same key (`seen`) until DuckDB's are used up, and the rest are extra."""
    used: Counter = Counter()
    extra = []
    for r in have:
        k = (r["line_key"], _utc(r["at"]))
        if used[k] < seen[k]:
            used[k] += 1
        else:
            extra.append(r)
    return extra


def _count_rows(conn, sid: str) -> int:
    """The rows `session_lines` holds for the session (a seam for the test of a failed check)."""
    return conn.execute(select(func.count()).select_from(session_lines).where(
        session_lines.c.session == sid)).scalar_one()


@contextmanager
def _duck_lines(store, sources: list, sid: str):
    """The session's DuckDB lines as rows of `session_lines` (without `session` and `seq`), all read
    before the rebuild's write opens: yields (their count in DuckDB, the rows a chunk at a time).
    The rows wait in an unnamed file that is gone when the block ends. The caller holds
    `Store._sess_lock`, so no line is ingested meanwhile; the lines are counted again after the
    read, and `Changed` is raised if anything else added or removed one."""
    wanted = _duck_count(store, sources, sid)
    where = None if str(store.path) == ":memory:" else os.path.dirname(os.path.abspath(store.path))
    with tempfile.TemporaryFile(dir=where) as spool:
        chunks = 0
        for envs in _duck_chunks(store, sources, sid):
            pickle.dump(session_ingest._line_rows(store, envs).get(sid, []), spool)
            chunks += 1
        after = _duck_count(store, sources, sid)
        if after != wanted:
            raise Changed(f"DuckDB held {wanted} lines before the read and {after} after")
        spool.seek(0)
        yield wanted, (pickle.load(spool) for _ in range(chunks))


def _rebuild(store, conn, read: tuple, sid: str) -> tuple[int, int]:
    """Replace the session's rows and parts by every DuckDB line (`read`, from `_duck_lines`)
    followed by the lines only the table or the archive holds, in the open transaction. Returns
    (DuckDB lines, extra lines). Raises `Mismatch` when the rows written are not those lines."""
    _bound_lock_waits(conn)
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('tares_session_move:' "
                          "|| CAST(:s AS text)))"), {"s": sid})
    wanted, chunks = read
    have = _existing(store, conn, sid)
    conn.execute(delete(session_lines).where(session_lines.c.session == sid))
    conn.execute(delete(session_archive_parts).where(session_archive_parts.c.session == sid))
    seq = 0
    seen: Counter = Counter()
    last = None
    for rows in chunks:
        for r in rows:
            seen[(r["line_key"], _utc(r["at"]))] += 1
            if last is None or _utc(r["at"]) > last:
                last = _utc(r["at"])
        conn.execute(insert(session_lines), [{**r, "session": sid, "seq": seq + i + 1}
                                             for i, r in enumerate(rows)])
        seq += len(rows)
    duck = seq
    extra = _extras(have, seen)
    for i in range(0, len(extra), CHUNK):
        part = extra[i:i + CHUNK]
        conn.execute(insert(session_lines), [
            {"session": sid, "seq": seq + j + 1, "line_key": r["line_key"], "at": r["at"],
             "type": r["type"], "labels": r["labels"], "render": r["render"],
             "payload": r["payload"]} for j, r in enumerate(part)])
        seq += len(part)
    for r in extra:
        if r["at"] is not None and (last is None or _utc(r["at"]) > last):
            last = _utc(r["at"])
    written = _count_rows(conn, sid)
    if duck != wanted or written != duck + len(extra):
        raise Mismatch(f"{duck} of {wanted} DuckDB lines read, {len(extra)} extra, "
                       f"{written} rows written")
    conn.execute(update(sessions).where(sessions.c.session == sid).values(
        line_count=written, last_line_at=last, lines_origin=session_ingest.TABLE,
        lines_complete=True, updated_at=now_utc()))
    return duck, len(extra)


def move_session(store, sid: str, sources: list | None = None) -> tuple[bool, str | None]:
    """Move one session. Returns (verified, error); (False, None) means another held a lock it
    needs, and it stays `pending` with no attempt counted. The ledger row is set `verified` in the
    rebuild's own transaction. A rebuild that fails is rolled back, and then:
    - `Mismatch` (the rows are not the lines): the row is `failed` with the counts, for good;
    - any other error (a database that blipped, a part that did not read): the row stays `pending`
      with the error and one more attempt, and is tried again after `RETRY_AFTER`; at `MAX_ATTEMPTS`
      it is `failed`.
    A failure of the write that records this is printed and leaves the row as it was."""
    sources = sources or sorted(session_ingest.claude_code_sources(store))
    db = store.sessions_db
    started = now_utc()
    try:
        with store._sess_lock, _duck_lines(store, sources, sid) as read:
            with db.begin() as conn:
                duck, extra = _rebuild(store, conn, read, sid)
                conn.execute(update(session_moves).where(session_moves.c.session == sid).values(
                    state=VERIFIED, target=TARGET, lines_events=duck, lines_after=duck + extra,
                    started_at=started, verified_at=now_utc(), error=None))
    except Exception as e:
        if _lock_timed_out(e):
            _back_off(db, sid)
            return False, None
        err = f"{type(e).__name__}: {e}"[:1000]
        try:
            _record_failure(db, sid, started, err, final=isinstance(e, Mismatch))
        except Exception as e2:
            print(f"taresd: session move of {sid}: the failure was not recorded "
                  f"({type(e2).__name__}: {e2}); the row stays as it was", flush=True)
        return False, err
    metrics.session_move_lines(duck + extra)
    return True, None


def _record_failure(db, sid: str, started: datetime, err: str, final: bool) -> None:
    m = session_moves.c
    with db.begin() as conn:
        tries = (conn.execute(select(m.attempts).where(m.session == sid)).scalar() or 0) + 1
        if final or tries >= MAX_ATTEMPTS:
            state = FAILED
            if not final:
                err = f"gave up after {tries} tries: {err}"[:1000]
        else:
            state = PENDING
        conn.execute(update(session_moves).where(m.session == sid, m.state != VERIFIED).values(
            state=state, started_at=started, verified_at=None, error=err, attempts=tries))


def _back_off(db, sid: str) -> None:
    """Leave a `pending` session that was held or changed alone for `RETRY_AFTER`, without counting
    an attempt."""
    m = session_moves.c
    try:
        with db.begin() as conn:
            conn.execute(update(session_moves).where(m.session == sid, m.state == PENDING)
                         .values(started_at=now_utc()))
    except Exception as e:
        print(f"taresd: session move of {sid}: the wait was not recorded ({type(e).__name__}: {e})",
              flush=True)


# ── one older session ────────────────────────────────────────────────────────────────────────
class _Line(NamedTuple):
    """One line as `session_archive.write_part` takes it: the columns of `session_lines`."""
    session: str
    seq: int
    line_key: str | None
    at: datetime | None
    type: str | None
    labels: str | None
    render: str | None
    payload: str | None


def _state(conn, sid: str, lock: bool = False) -> tuple:
    """What the session store holds of the session: its row, its parts and its table rows. A move
    that finds this changed when it comes to replace them leaves them alone. `lock` takes the
    session's row for the transaction (Postgres), where an ingest of the session waits."""
    s = sessions.c
    q = select(s.source, s.line_count, s.last_line_at).where(s.session == sid)
    head = conn.execute(q.with_for_update() if lock else q).first()
    parts = [(p.part, p.key, p.md5) for p in _parts(conn, sid)]
    t = session_lines.c
    n, top = conn.execute(select(func.count(), func.max(t.seq)).where(t.session == sid)).one()
    return (None if head is None else tuple(head)), parts, int(n), top


def _write_list(store, backend, sources: list, sid: str, have: list, part_no: int) -> tuple:
    """Write the session's full list (every DuckDB line, then the lines of `have` DuckDB lacks) as
    parts of at most `PART_LINES` lines, `seq` from 1, parts from `part_no`. Returns (the parts' rows,
    DuckDB lines, all lines, the newest line time). Nothing is recorded in the store."""
    parts: list = []
    buf: list = []
    seq = 0
    last = None
    seen: Counter = Counter()

    def flush():
        parts.append(session_archive.write_part(backend, sid, buf, part_no + len(parts),
                                                by_content=True))
        buf.clear()

    def add(r):
        nonlocal seq, last
        seq += 1
        at = _utc(r["at"])
        if at is not None and (last is None or at > last):
            last = at
        buf.append(_Line(sid, seq, r["line_key"], at, r["type"], r["labels"], r["render"],
                         r["payload"]))
        if len(buf) >= PART_LINES:
            flush()

    for envs in _duck_chunks(store, sources, sid):
        for r in session_ingest._line_rows(store, envs).get(sid, []):
            seen[(r["line_key"], _utc(r["at"]))] += 1
            add(r)
    duck = seq
    for r in _extras(have, seen):
        add(r)
    if buf:
        flush()
    return parts, duck, seq, last


def _count_archived(conn, sid: str) -> int:
    """The lines the session's parts hold (a seam for the test of a failed check)."""
    return conn.execute(select(func.coalesce(func.sum(session_archive_parts.c.lines), 0)).where(
        session_archive_parts.c.session == sid)).scalar_one()


def _replace(store, sid: str, sources: list, before: tuple, wanted: int, built: tuple,
             started: datetime) -> bool:
    """Point the session at the parts `_write_list` wrote, in one transaction, and mark it verified.
    False, with nothing replaced and the ledger row `pending`, when the session is not as it was
    when the list was read, or the hourly job holds it. Raises `Mismatch` when the lines do not add
    up."""
    parts, duck, total, last = built
    m = session_moves.c
    db = store.sessions_db
    with store._sess_lock:          # no line goes into DuckDB or is counted from here to the commit
        # counted before the transaction: on SQLite a store call can hold DuckDB's lock while it
        # waits to write the session store, so that lock is not taken inside this write
        duck_now = _duck_count(store, sources, sid)
        with db.begin() as conn:
            _bound_lock_waits(conn)
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('tares_session_move:' "
                                  "|| CAST(:s AS text)))"), {"s": sid})
                # not waited for: the hourly job holds its lock across an upload, and ingest waits
                # on `_sess_lock` meanwhile
                if not session_archive._lock(conn, sid):
                    return False
            if _state(conn, sid, lock=True) != before or duck_now != wanted:
                conn.execute(update(session_moves).where(m.session == sid, m.state != VERIFIED)
                             .values(state=PENDING, error=None))
                return False
            if duck != wanted:
                raise Mismatch(f"{duck} of {wanted} DuckDB lines read")
            now = now_utc()
            # no row in `sessions` means no line to replace; with no row to lock, nothing is deleted
            if before[0] is not None:
                conn.execute(delete(session_lines).where(session_lines.c.session == sid))
                conn.execute(delete(session_archive_parts).where(
                    session_archive_parts.c.session == sid))
                if parts:
                    conn.execute(insert(session_archive_parts),
                                 [{**p, "written_at": now} for p in parts])
                held = _count_archived(conn, sid)
                if held != total:
                    raise Mismatch(f"{duck} of {wanted} DuckDB lines read, {total - duck} extra, "
                                   f"{held} lines archived of {total}")
                values = dict(line_count=total, lines_origin=session_ingest.TABLE,
                              lines_complete=True, updated_at=now)
                if last is not None:
                    values["last_line_at"] = last
                conn.execute(update(sessions).where(sessions.c.session == sid).values(**values))
            conn.execute(update(session_moves).where(m.session == sid).values(
                state=VERIFIED, target=ARCHIVE, lines_events=duck, lines_after=total,
                started_at=started, verified_at=now, error=None))
    return True


def move_old_session(store, sid: str, backend, sources: list | None = None) -> tuple[bool, str | None]:
    """Move one older session to the archive. Returns (verified, error); (False, None) means it
    changed while it was moved, or another held it, and it stays `pending` until `RETRY_AFTER` with
    no attempt counted. A failure leaves its rows and parts as they were and is recorded as
    `move_session` records one."""
    sources = sources or sorted(session_ingest.claude_code_sources(store))
    db = store.sessions_db
    started = now_utc()
    try:
        # the sweep of unused files leaves this session's alone until the parts are committed
        with session_archive.writing(sid):
            wanted = _duck_count(store, sources, sid)
            # one snapshot of the row, the parts and the table rows. On SQLite without the write
            # lock: a deferred transaction in WAL mode sees one state from its first read to its end
            with (db.snapshot() if db.dialect == "postgresql" else db.read()) as conn:
                before = _state(conn, sid)
                have = _table_rows(conn, sid)
                old = _parts(conn, sid)
            have += _from_parts(backend, old)
            have.sort(key=lambda r: r["seq"])
            if before[0] is None and (wanted or have):
                raise RuntimeError("the session has lines but no row in sessions")
            first = max((p.part for p in old), default=0) + 1
            built = _write_list(store, backend, sources, sid, have, first)
            replaced = _replace(store, sid, sources, before, wanted, built, started)
        if not replaced:
            _back_off(db, sid)
            return False, None
    except Exception as e:
        if _lock_timed_out(e):
            _back_off(db, sid)
            return False, None
        err = f"{type(e).__name__}: {e}"[:1000]
        try:
            _record_failure(db, sid, started, err, final=isinstance(e, Mismatch))
        except Exception as e2:
            print(f"taresd: session move of {sid}: the failure was not recorded "
                  f"({type(e2).__name__}: {e2}); the row stays as it was", flush=True)
        return False, err
    metrics.session_move_lines(built[2])
    return True, None


# ── a pass ───────────────────────────────────────────────────────────────────────────────────
def _ready():
    """A pending session that may be tried now: never tried, or `RETRY_AFTER` since its last try."""
    m = session_moves.c
    return or_(m.started_at.is_(None),
               m.started_at <= now_utc() - timedelta(seconds=RETRY_AFTER))


def _older(cutoff: datetime):
    """Outside the window: a last line before `cutoff`, or no last line (or no row) at all."""
    return or_(sessions.c.last_line_at.is_(None), sessions.c.last_line_at < cutoff)


def due(store, limit: int = BATCH, now: datetime | None = None) -> list[str]:
    """The pending sessions whose last line is inside the window, the oldest first. One that was
    tried is left out until `RETRY_AFTER` has passed since that try."""
    cutoff = (now or now_utc()) - timedelta(days=ARCHIVE_AFTER_DAYS)
    m, s = session_moves.c, sessions.c
    with store.sessions_db.begin() as conn:
        return [r[0] for r in conn.execute(
            select(m.session).join(sessions, s.session == m.session)
            .where(m.state == PENDING, s.last_line_at >= cutoff, _ready())
            .order_by(s.last_line_at, m.session).limit(limit)).all()]


def due_old(store, limit: int = BATCH, now: datetime | None = None) -> list[str]:
    """The pending sessions outside the window, the oldest last line first; as `due` for one that
    was tried."""
    cutoff = (now or now_utc()) - timedelta(days=ARCHIVE_AFTER_DAYS)
    m, s = session_moves.c, sessions.c
    with store.sessions_db.begin() as conn:
        return [r[0] for r in conn.execute(
            select(m.session).outerjoin(sessions, s.session == m.session)
            .where(m.state == PENDING, _older(cutoff), _ready())
            .order_by(s.last_line_at, m.session).limit(limit)).all()]


def retrying(store, now: datetime | None = None, old: bool = False) -> int:
    """How many pending sessions inside the window (outside it with `old`) were tried and wait."""
    cutoff = (now or now_utc()) - timedelta(days=ARCHIVE_AFTER_DAYS)
    m, s = session_moves.c, sessions.c
    with store.sessions_db.begin() as conn:
        return int(conn.execute(
            select(func.count()).select_from(session_moves)
            .outerjoin(sessions, s.session == m.session)
            .where(m.state == PENDING, _older(cutoff) if old else s.last_line_at >= cutoff,
                   m.started_at.is_not(None))).scalar() or 0)


def counts(store) -> dict:
    with store.sessions_db.begin() as conn:
        return {r[0]: int(r[1]) for r in conn.execute(
            select(session_moves.c.state, func.count()).group_by(session_moves.c.state)).all()}


def refresh_gauges(store) -> None:
    """Set `tares_session_move_sessions`; a failure leaves it as it was (a gauge is never the news)."""
    try:
        metrics.session_move_sessions(counts(store))
    except Exception:
        pass


def _fill(store, sources: list, sids: list) -> None:
    """The movers pass one session: filling a batch of 20 held all their lines at once, which took
    the whole move on a large cell over its memory limit (TR-1664)."""
    try:
        # the facts and turns of these sessions, from all their lines (since=None reads every one)
        session_ingest._fill_chunk(store, sources, sids, None)
        session_ingest._fill_turns_chunk(store, sources, sids, None)
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
        # the start-up fill reads no line once the move is complete: the sessions are marked for
        # the M1 drain, which refills them then (`session_drain`, TR-1665)
        try:
            session_ingest.mark_drain_todo(store, sids)
            then = "marked, so the M1 drain fills them once the move is complete"
        except Exception as e2:
            then = f"and not marked ({type(e2).__name__}: {e2}), so they are not filled again"
        print(f"taresd: session move: facts and turns of {len(sids)} session(s) not filled ({why}); "
              f"{then}", flush=True)


def _old_backend(store):
    """The archive older sessions go to, or None when the cell has none: said once per process
    (the flag only holds back the repeat of the line)."""
    why = "this cell has no archive (TARES_ARCHIVE_URL is not set)"
    try:
        backend = session_archive.backend_of(store)
    except Exception as e:
        backend, why = None, f"the archive cannot be used ({type(e).__name__}: {e})"
    if backend is None and not getattr(store, "_move_old_skip_said", False):
        store._move_old_skip_said = True
        print(f"taresd: session move: older sessions stay in DuckDB, {why}", flush=True)
    return backend


def move_old(store, limit: int = BATCH, stop: threading.Event | None = None,
             now: datetime | None = None) -> int:
    """Move up to `limit` pending sessions outside the window to the archive, oldest first. Returns
    how many it took: 0 when none is due, or when the cell has no archive. Stops between sessions
    when `stop` is set."""
    sids = due_old(store, limit, now)
    if not sids:
        return 0
    backend = _old_backend(store)
    if backend is None:
        return 0
    sources = sorted(session_ingest.claude_code_sources(store))
    n = 0
    for sid in sids:
        _fill(store, sources, [sid])         # one session's lines in memory at a time
        if stop is not None and stop.is_set():
            break
        ok, err = move_old_session(store, sid, backend, sources)
        n += 1
        if err:
            print(f"taresd: session move of {sid} to the archive failed ({err}); its lines stay as "
                  "they were", flush=True)
        elif not ok:
            print(f"taresd: session move of {sid}: it changed while it moved, or another writer "
                  "held it, so it is moved again later", flush=True)
    refresh_gauges(store)
    return n


def move_recent(store, limit: int = BATCH, stop: threading.Event | None = None,
                now: datetime | None = None) -> int:
    """Move up to `limit` pending sessions with a last line inside the window, oldest first.
    The facts and turns of each are filled first (both are idempotent). Returns how many sessions it
    took; a session that failed for good is not taken again, one that was tried waits for
    `RETRY_AFTER`. Stops between sessions when `stop` is set."""
    sids = due(store, limit, now)
    if not sids:
        return 0
    sources = sorted(session_ingest.claude_code_sources(store))
    n = 0
    for sid in sids:
        _fill(store, sources, [sid])         # one session's lines in memory at a time
        if stop is not None and stop.is_set():
            break
        ok, err = move_session(store, sid, sources)
        n += 1
        if err:
            print(f"taresd: session move of {sid} failed ({err}); its lines stay as they were",
                  flush=True)
        elif not ok:
            print(f"taresd: session move of {sid}: another writer held it, so it is moved again "
                  "later", flush=True)
    refresh_gauges(store)
    return n


# ── the daemon's thread ──────────────────────────────────────────────────────────────────────
def run(store, stop: threading.Event, pause: float = PAUSE, old_pause: float = OLD_PAUSE) -> None:
    """Wait for the sessions table to catch up with the events, plan, and move batches until none
    is left: recent sessions with `pause` seconds between batches, then older ones with
    `old_pause`. A failure of the job itself prints one line; the
    ledger holds the progress, so the next start carries on."""
    t0 = time.perf_counter()
    try:
        while not session_ingest.caught_up(store):
            if stop.wait(WAIT):
                return
        n = plan(store)
        if n:
            print(f"taresd: session move planned for {n} session(s)", flush=True)
        while not stop.is_set():
            if move_recent(store, stop=stop):
                if stop.wait(pause):
                    return
                continue
            if stop.is_set():
                break
            if move_old(store, stop=stop):
                if stop.wait(old_pause):
                    return
                continue
            waiting = retrying(store) or (retrying(store, old=True) and _old_backend(store))
            if waiting and not stop.wait(RETRY_AFTER):
                continue              # sessions that were tried wait to be tried again
            break
        done = counts(store)
        if done.get(VERIFIED) or done.get(FAILED):
            print(f"taresd: session move: {done.get(VERIFIED, 0)} verified, {done.get(FAILED, 0)} "
                  f"failed, {done.get(PENDING, 0)} pending in {time.perf_counter() - t0:.1f}s",
                  flush=True)
    except Exception as e:
        print(f"taresd: session move stopped ({type(e).__name__}: {e}); it carries on at the next "
              "start", flush=True)


def start(store) -> threading.Thread | None:
    """Run `run` in a background thread, on a factory cell only. `Store.close` stops it and waits.
    Returns the thread, or None when the move is parked (`MOVE_ENABLED`) or the cell is not a
    factory."""
    if not MOVE_ENABLED:
        return None
    try:
        if edition.edition(store)[0] != "factory":
            return None
    except Exception:
        return None
    stop = store._move_stop = threading.Event()
    th = threading.Thread(target=run, args=(store, stop), name="session-move", daemon=True)
    th.start()
    store._move_thread = th
    return th
