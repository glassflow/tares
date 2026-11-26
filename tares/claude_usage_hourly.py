"""Kept hourly totals of the usage rows (P-TR-296, TR-1632).

`station_rollup`'s spend and `usage_by_project` used to sum every `session_usage` row on each page
request: 219,361 rows in the factory seed, about 400 ms on SQLite. They read `session_usage_hourly`
now, one row per session, hour and project, about 7,000 rows for the same data.

How it stays right:
- A bucket is rebuilt from the raw rows of its (session, hour), never changed by a delta. The
  reply upsert keeps the largest count and fills a missing project, so a delta would have to know
  the old row; a rebuild is right for any change of the row. `record_reply_usage` rebuilds in the
  transaction that writes the reply, and so do the other writers (`_usage_forget_project`, the
  start-up fill's inserts, deletes and pricing).
- The rows that exist before this change are filled in chunks of sessions, in session order, by a
  background step (`fill`). Its cursor is one row in `session_rollup_state`, so a stopped fill
  resumes. A write for a session after the cursor is left to the fill; up to it, rebuilt. "Up to"
  is the database's order of the ids (the one `_next_sessions` walks), so `_covers` compares in SQL:
  Python's order differs under a Postgres collation such as en_US.UTF-8 (TR-1661).
- The readers use the buckets only once the fill has checked them against the raw rows and set
  `ready_at`; until then they scan, as before. The mark is never unset.
- On Postgres, `pg_advisory_xact_lock` only (transaction level, so PgBouncer's transaction mode
  is fine). SQLite's writers already take turns (`BEGIN IMMEDIATE`), so both locks do nothing there.

Lock order, for every transaction that writes the usage rows or their buckets (TR-1661):
  1. The fill's gate (`hold`). Every chunk of the fill takes it, and every writer takes it while the
     fill is not done, before it reads anything. So a writer and a chunk never overlap: the chunk
     sees every row committed before it, and the writer sees the cursor the chunk left. Without it a
     new session's first reply committed inside an open chunk was missed by both.
  2. The session locks (`lock`), all of them in one call, in ascending order, before the first row
     is written. A session maps to one of `STRIPES` locks, so a transaction holds at most that many
     however many sessions it touches (Postgres's lock table is small and shared).
  3. Row locks, from the writes themselves.
  Nothing asks for a lock of an earlier step once it holds one of a later step, so no two of these
  transactions can wait for each other. A writer that skips the gate (the fill is done) takes only
  steps 2 and 3.
"""
from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select, text, update

from .session_store.tables import session_rollup_state, session_usage, session_usage_hourly

NAME = "usage_hourly"              # the fill's row in session_rollup_state
CHUNK = 50                         # sessions rebuilt in one transaction; writers wait for it
HIGH = "\U0010ffff"                # the cursor once no session is left: every write rebuilds (told
                                   # by equality; a collation may not sort it after every id)
GATE = "usage_hourly_fill"         # the advisory lock of the fill's chunks and the writers meanwhile
STRIPES = 256                      # session locks there are, so the most one transaction holds
NO_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)    # the hour of a reply with no time (counted in
                                                       # all-time totals only, as the row scan does)
HOUR = timedelta(hours=1)


def hour_of(t: datetime | None) -> datetime:
    if t is None:
        return NO_TIME
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def first_whole_hour(since: datetime) -> datetime:
    """The first hour boundary at or after `since`: the buckets from here on lie wholly in the window."""
    h = hour_of(since)
    return h if h == since.astimezone(timezone.utc) else h + HOUR


# ── state ────────────────────────────────────────────────────────────────────────────────────
def ready(store) -> bool:
    """True once the buckets were checked against the raw rows. One primary-key lookup; only a True
    answer is kept on the store (the mark is never unset)."""
    if getattr(store, "_usage_hourly_ready", False):
        return True
    s = session_rollup_state
    with store.sessions_db.read() as conn:
        at = conn.execute(select(s.c.ready_at).where(s.c.name == NAME)).scalar_one_or_none()
    if at is None:
        return False
    store._usage_hourly_ready = True
    return True


def _covers(store, conn, session: str) -> bool:
    """Whether a write for `session` must rebuild its bucket: the fill is done, or has passed it.
    The caller holds the gate (`hold`), so the cursor stays where it is until the write commits."""
    if getattr(store, "_usage_hourly_ready", False):
        return True
    s = session_rollup_state
    # `cursor >= session` runs in the database, so it is the order `_next_sessions` walks (TR-1661)
    row = conn.execute(select(s.c.cursor, s.c.ready_at, s.c.cursor >= session)
                       .where(s.c.name == NAME)).first()
    if row is None:
        return False
    if row[1] is not None:
        store._usage_hourly_ready = True
        return True
    return row[0] is not None and (row[0] == HIGH or bool(row[2]))


# ── locks (the order is in the module's docstring) ───────────────────────────────────────────
def _advisory(db, conn, key: str) -> None:
    if db.dialect == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _gate(db, conn) -> None:
    _advisory(db, conn, GATE)


def hold(store, conn) -> None:
    """The first thing a writer of `session_usage` does in its transaction: while the fill is not
    done, take its gate, so no chunk runs until the transaction ends."""
    if not getattr(store, "_usage_hourly_ready", False):
        _gate(store.sessions_db, conn)


def stripe(session: str) -> int:
    """The lock a session maps to: the same in every process (Python's `hash` is not)."""
    return zlib.crc32(session.encode("utf-8")) % STRIPES


def lock(db, conn, sessions) -> None:
    """On Postgres, one transaction at a time per session while its bucket is rebuilt. Call it once
    per transaction with every session: the locks are taken in one ascending pass."""
    if db.dialect != "postgresql":
        return
    for n in sorted({stripe(s) for s in sessions}):
        _advisory(db, conn, f"usage_hourly:{n}")


def lock_covered(store, conn, sessions) -> list[str]:
    """Of the `sessions` this transaction is about to change, those whose buckets it must rebuild,
    locked. After `hold` and before the first row is written."""
    covered = sorted(s for s in set(sessions) if _covers(store, conn, s))
    lock(store.sessions_db, conn, covered)
    return covered


# ── rebuilding buckets ───────────────────────────────────────────────────────────────────────
def _buckets(rows) -> list[dict]:
    out: dict[tuple, dict] = {}
    for session, at, project, i, o, cw, cr, cost in rows:
        key = (session, hour_of(at), project or "")
        b = out.get(key)
        if b is None:
            b = out[key] = {"session": key[0], "hour": key[1], "project": key[2], "replies": 0,
                            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
                            "cache_read_tokens": 0, "cost_usd": None, "unpriced": 0}
        b["replies"] += 1
        b["input_tokens"] += i or 0
        b["output_tokens"] += o or 0
        b["cache_write_tokens"] += cw or 0
        b["cache_read_tokens"] += cr or 0
        if cost is None:
            b["unpriced"] += 1
        else:
            b["cost_usd"] = (b["cost_usd"] or 0.0) + cost
    return list(out.values())


def rebuild(db, conn, sessions, hour: datetime | None = None) -> None:
    """Rebuild the buckets of `sessions` (one hour of them when `hour` is given) from the raw rows,
    inside the caller's transaction, which holds their locks (`lock`)."""
    sessions = sorted(set(sessions))
    if not sessions:
        return
    u, h = session_usage, session_usage_hourly
    q = select(u.c.session, u.c.replied_at, u.c.project, u.c.input_tokens, u.c.output_tokens,
               u.c.cache_write_tokens, u.c.cache_read_tokens, u.c.cost_usd)
    d = delete(h)
    for i in range(0, len(sessions), CHUNK):
        part = sessions[i:i + CHUNK]
        qq, dd = q.where(u.c.session.in_(part)), d.where(h.c.session.in_(part))
        if hour is not None and hour != NO_TIME:
            qq = qq.where(u.c.replied_at >= hour, u.c.replied_at < hour + HOUR)
            dd = dd.where(h.c.hour == hour)
        elif hour == NO_TIME:
            qq, dd = qq.where(u.c.replied_at.is_(None)), dd.where(h.c.hour == NO_TIME)
        conn.execute(dd)
        rows = _buckets(conn.execute(qq).all())
        for j in range(0, len(rows), 1000):
            conn.execute(insert(h), rows[j:j + 1000])


def after_write(store, conn, covered) -> None:
    """The usage rows changed in this transaction in ways that move more than one hour (a project
    forgotten, a start-up fill): rebuild the buckets of `covered`, from `lock_covered`."""
    rebuild(store.sessions_db, conn, covered)


def after_reply(store, conn, session: str, message: str) -> None:
    """A reply row of a session `lock_covered` returned was written: rebuild its (session, hour)
    bucket."""
    u = session_usage
    row = conn.execute(select(u.c.replied_at).where(
        u.c.session == session, u.c.message == message)).first()
    if row is not None:
        rebuild(store.sessions_db, conn, [session], hour=hour_of(row[0]))


# ── the fill ─────────────────────────────────────────────────────────────────────────────────
def _cursor(conn) -> str | None:
    s = session_rollup_state
    return conn.execute(select(s.c.cursor).where(s.c.name == NAME)).scalar_one_or_none()


def _set_cursor(db, conn, cursor: str | None) -> None:
    stmt = db.upsert(session_rollup_state).values(name=NAME, cursor=cursor)
    conn.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"cursor": stmt.excluded.cursor}))


def _next_sessions(conn, after: str | None, chunk: int) -> list[str]:
    u = session_usage
    q = select(u.c.session).group_by(u.c.session).order_by(u.c.session).limit(chunk)
    if after is not None:
        q = q.where(u.c.session > after)
    return [r[0] for r in conn.execute(q).all()]


def check(store) -> tuple[bool, str]:
    """The buckets against the raw rows, in one snapshot: reply count, the token sums, the cost sum."""
    u, h = session_usage, session_usage_hourly
    with store.sessions_db.consistent_read() as conn:
        raw = conn.execute(select(func.count(), func.coalesce(func.sum(u.c.input_tokens), 0),
                                  func.coalesce(func.sum(u.c.output_tokens), 0),
                                  func.coalesce(func.sum(u.c.cache_write_tokens), 0),
                                  func.coalesce(func.sum(u.c.cache_read_tokens), 0),
                                  func.coalesce(func.sum(u.c.cost_usd), 0.0))).one()
        kept = conn.execute(select(func.coalesce(func.sum(h.c.replies), 0),
                                   func.coalesce(func.sum(h.c.input_tokens), 0),
                                   func.coalesce(func.sum(h.c.output_tokens), 0),
                                   func.coalesce(func.sum(h.c.cache_write_tokens), 0),
                                   func.coalesce(func.sum(h.c.cache_read_tokens), 0),
                                   func.coalesce(func.sum(h.c.cost_usd), 0.0))).one()
    exact = [int(a) == int(b) for a, b in zip(raw[:5], kept[:5])]
    cost_ok = abs(float(raw[5]) - float(kept[5])) <= 1e-6 * max(1.0, abs(float(raw[5])))
    if all(exact) and cost_ok:
        return True, ""
    return False, f"rows {tuple(raw)} against buckets {tuple(kept)}"


def fill(store, chunk: int = CHUNK, stop_after: int | None = None) -> int:
    """Build `session_usage_hourly` from the raw rows, a chunk of sessions per transaction, from
    where the cursor stands. Each transaction takes the gate, so no write is under way while it
    picks its sessions. The one that finds none left moves the cursor to HIGH (from then on every
    write rebuilds) in that same transaction, so a session written after the last chunk is either
    picked up as another chunk or rebuilt by its writer. The buckets are then checked against the
    raw rows, and only then is `ready_at` set. A failed check starts again from the first session at
    the next pass. `stop_after` ends the pass after that many chunks (tests).
    Returns how many sessions it rebuilt."""
    if ready(store):
        return 0
    db, s = store.sessions_db, session_rollup_state
    with db.begin() as conn:
        db_stmt = db.upsert(s).values(name=NAME, cursor=None)
        conn.execute(db_stmt.on_conflict_do_nothing(index_elements=["name"]))
    n = chunks = 0
    while True:
        with db.begin() as conn:
            _gate(db, conn)
            cursor = _cursor(conn)               # read under the gate: another pass may have moved it
            if cursor == HIGH:
                break
            sessions = _next_sessions(conn, cursor, chunk)
            if sessions:
                lock(db, conn, sessions)
                rebuild(db, conn, sessions)
            _set_cursor(db, conn, sessions[-1] if sessions else HIGH)
        if not sessions:
            break
        n += len(sessions)
        chunks += 1
        if stop_after is not None and chunks >= stop_after:
            return n
    ok, why = check(store)
    if not ok:
        with db.begin() as conn:
            _gate(db, conn)
            _set_cursor(db, conn, None)
        raise RuntimeError(f"the hourly usage totals do not match the usage rows ({why}); "
                           "they are built again at the next pass")
    with db.begin() as conn:
        conn.execute(update(s).where(s.c.name == NAME, s.c.ready_at.is_(None)).values(
            ready_at=datetime.now(timezone.utc)))
    store._usage_hourly_ready = True
    return n
