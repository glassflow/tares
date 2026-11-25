"""The one-time move of recent sessions' lines into the session store (P-TR-295, TR-1609).

In milestone 1 a line went to `session_lines` only from the upgrade on, while DuckDB got every line,
so DuckDB holds every line written before RC 1. A session read from the table must hold all of its
lines, so on the first start of RC 1 a background job rebuilds every `claude_code` session whose last
line is inside the archive window (`ARCHIVE_AFTER_DAYS`). Older sessions stay in DuckDB: they would
be archived straight away. Nothing is ever deleted from DuckDB.

The ledger is `session_moves`, one row per session with its state: `pending`, `verified` or
`failed`.

- `plan` adds a `pending` row for each session in DuckDB's events, once (the mark
  `session_move_planned` in `session_ingest_marks`). A second call, or a second start, adds nothing.
- `move_recent` takes pending sessions whose last line is inside the window, oldest first, and moves
  each in one session-store transaction: every DuckDB line (read in chunks, ordered by `event_time`
  then `rowid`, with the DuckDB lock let go between chunks), then every line the table or the
  archive holds that DuckDB does not (written on RC 1 with the gate shut by `HOLD_LINES_BACK`, or
  late). A line is matched by `(line_key, at)` as a multiset, because `line_key` repeats. The
  session's rows in `session_lines` and its parts in `session_archive_parts` are replaced by the
  full list, numbered from 1; the archive files of the replaced parts stay where they are, unused.
  The count of rows written is checked against the lines read. A session that checks out is
  `verified` and `sessions.lines_complete` is set; one that does not is `failed` with the counts in
  `error`, and its transaction is rolled back, so its rows are as they were.
- `run` is the daemon's background thread: it waits for the sessions table to catch up, plans, and
  moves batches of 20 sessions with a pause between them. It resumes from the ledger after a
  restart, and stops between sessions when `stop` is set.

A session is moved under `Store._sess_lock`, as the reconcile and `Store.append` take it, so no
line is counted into it meanwhile. On Postgres the transaction also takes
`pg_advisory_xact_lock` on the session, which lets go with the transaction (PgBouncer in transaction
mode allows nothing longer). On SQLite the transaction is the write lock: other writers wait on its
busy timeout while one session is rebuilt.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select, text, update

from . import metrics, session_archive, session_ingest
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
WAIT = 2.0                           # seconds between looks at whether the sessions table caught up
PENDING, VERIFIED, FAILED = "pending", "verified", "failed"
TARGET = "table"


class Mismatch(Exception):
    """The rows a move wrote are not the lines it read; the session is left as it was."""


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


# ── the plan ─────────────────────────────────────────────────────────────────────────────────
def planned(store) -> bool:
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == PLANNED)).first() is not None


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


def _existing(store, conn, sid: str) -> list[dict]:
    """The lines the table and the archived parts hold for the session, in `seq` order, each as the
    columns of `session_lines` without `session` and `seq`. Raises when a part cannot be read."""
    t = session_lines.c
    out = [{"seq": r.seq, "line_key": r.line_key, "at": r.at, "type": r.type, "labels": r.labels,
            "render": r.render, "payload": r.payload}
           for r in conn.execute(select(t.seq, t.line_key, t.at, t.type, t.labels, t.render,
                                        t.payload).where(t.session == sid).order_by(t.seq)).all()]
    parts = conn.execute(select(session_archive_parts).where(
        session_archive_parts.c.session == sid).order_by(session_archive_parts.c.part)).all()
    if parts:
        backend = session_archive.backend_of(store)
        if backend is None:
            raise session_archive.ArchiveError("parts exist but no archive is configured")
        for p in parts:
            for r in session_archive.read_part(backend, p):
                at = r.get("at")
                out.append({"seq": r["seq"], "line_key": r.get("line_key"),
                            "at": _utc(datetime.fromisoformat(at)) if at else None,
                            "type": r.get("type"), "labels": r.get("labels"),
                            "render": r.get("render"), "payload": r.get("payload")})
        out.sort(key=lambda r: r["seq"])
    return out


def _count_rows(conn, sid: str) -> int:
    """The rows `session_lines` holds for the session (a seam for the test of a failed check)."""
    return conn.execute(select(func.count()).select_from(session_lines).where(
        session_lines.c.session == sid)).scalar_one()


def _rebuild(store, conn, sources: list, sid: str) -> tuple[int, int]:
    """Replace the session's rows and parts by every DuckDB line followed by the lines only the
    table or the archive holds, in the open transaction. Returns (DuckDB lines, extra lines).
    Raises `Mismatch` when the rows written are not those lines."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('tares_session_move:' "
                          "|| CAST(:s AS text)))"), {"s": sid})
    wanted = _duck_count(store, sources, sid)
    have = _existing(store, conn, sid)
    conn.execute(delete(session_lines).where(session_lines.c.session == sid))
    conn.execute(delete(session_archive_parts).where(session_archive_parts.c.session == sid))
    seq = 0
    seen: Counter = Counter()
    last = None
    for envs in _duck_chunks(store, sources, sid):
        rows = session_ingest._line_rows(store, envs).get(sid, [])
        for r in rows:
            seen[(r["line_key"], _utc(r["at"]))] += 1
            if last is None or _utc(r["at"]) > last:
                last = _utc(r["at"])
        conn.execute(insert(session_lines), [{**r, "session": sid, "seq": seq + i + 1}
                                             for i, r in enumerate(rows)])
        seq += len(rows)
    duck = seq
    # the lines DuckDB lacks: each (line_key, at) of the table or archive is matched with one DuckDB
    # line of the same key until DuckDB's are used up, and the rest are extra
    used: Counter = Counter()
    extra = []
    for r in have:
        k = (r["line_key"], _utc(r["at"]))
        if used[k] < seen[k]:
            used[k] += 1
        else:
            extra.append(r)
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
    """Move one session. Returns (verified, error). The ledger row is set `verified`, or `failed`
    with the counts, in a transaction after the rebuild's; a failed rebuild is rolled back."""
    sources = sources or sorted(session_ingest.claude_code_sources(store))
    db = store.sessions_db
    started = now_utc()
    try:
        with store._sess_lock:
            with db.begin() as conn:
                duck, extra = _rebuild(store, conn, sources, sid)
                conn.execute(update(session_moves).where(session_moves.c.session == sid).values(
                    state=VERIFIED, target=TARGET, lines_events=duck, lines_after=duck + extra,
                    started_at=started, verified_at=now_utc(), error=None))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:1000]
        with db.begin() as conn:
            conn.execute(update(session_moves).where(session_moves.c.session == sid).values(
                state=FAILED, started_at=started, verified_at=None, error=err))
        return False, err
    metrics.session_move_lines(duck + extra)
    return True, None


# ── a pass ───────────────────────────────────────────────────────────────────────────────────
def due(store, limit: int = BATCH, now: datetime | None = None) -> list[str]:
    """The pending sessions whose last line is inside the window, the oldest first."""
    cutoff = (now or now_utc()) - timedelta(days=ARCHIVE_AFTER_DAYS)
    m, s = session_moves.c, sessions.c
    with store.sessions_db.begin() as conn:
        return [r[0] for r in conn.execute(
            select(m.session).join(sessions, s.session == m.session)
            .where(m.state == PENDING, s.last_line_at >= cutoff)
            .order_by(s.last_line_at, m.session).limit(limit)).all()]


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


def move_recent(store, limit: int = BATCH, stop: threading.Event | None = None,
                now: datetime | None = None) -> int:
    """Move up to `limit` pending sessions with a last line inside the window, oldest first.
    The facts and turns of each are filled first (both are idempotent). Returns how many sessions it
    took, verified or failed; a failed one is not taken again. Stops between sessions when `stop`
    is set."""
    sids = due(store, limit, now)
    if not sids:
        return 0
    sources = sorted(session_ingest.claude_code_sources(store))
    try:
        # the facts and turns of these sessions, from all their lines (since=None reads every one)
        session_ingest._fill_chunk(store, sources, sids, None)
        session_ingest._fill_turns_chunk(store, sources, sids, None)
    except Exception as e:
        print(f"taresd: session move: facts and turns not filled ({type(e).__name__}: {e}); "
              "the start-up fill covers them", flush=True)
    n = 0
    for sid in sids:
        if stop is not None and stop.is_set():
            break
        ok, err = move_session(store, sid, sources)
        n += 1
        if not ok:
            print(f"taresd: session move of {sid} failed ({err}); its lines stay as they were",
                  flush=True)
    refresh_gauges(store)
    return n


# ── the daemon's thread ──────────────────────────────────────────────────────────────────────
def run(store, stop: threading.Event, pause: float = PAUSE) -> None:
    """Wait for the sessions table to catch up with the events, plan, and move batches until none
    is left, with `pause` seconds between them. A failure of the job itself prints one line; the
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
            if move_recent(store, stop=stop) == 0:
                break
            if stop.wait(pause):
                return
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
    from . import edition
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
