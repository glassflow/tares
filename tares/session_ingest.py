"""What each Claude Code session's lines add up to, kept in the `sessions` table (P-TR-295, TR-1471).

A page that lists sessions used to count over the events table (`Store.session_activity`,
`Store.coordination_calls`), and that table grows with every line. Here each line is counted once,
as it is stored, into one row per session. Two writers keep the row right:

- `record`, called by `Store.append` after a batch of claude_code lines is committed to DuckDB. It
  adds the batch to each session's row in one transaction (`ON CONFLICT ... DO UPDATE`), so
  concurrent batches lose nothing.
- `reconcile_sessions`, run in a background thread at start. It reads the absolute counts from
  DuckDB for the sessions with lines newer than the last pass, and keeps the larger of what is
  stored and what DuckDB says. Lines written by an image that did not call `record`, or a batch
  whose `record` failed, are counted this way. Both writers only ever undercount, so the larger
  value is the right one, and a second pass changes nothing.

DuckDB is never changed: `Store.append` keeps writing every line there.

Two things to know when reading the table against the readers it replaces:

- It counts the lines of every claude_code source, while `Store.session_activity` and
  `Store.coordination_calls` read the one source `cc_source` names. A cell with a second claude_code
  source sees its sessions here too.
- The reconcile's mark is the newest `ingest_time` it read, and a line's `ingest_time` is set when
  its envelope is built, not when it is committed. The 10 minute margin before the mark covers a batch
  that commits late.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import metrics
from .envelope import now_utc
from .factory import is_coordination
from .session_store.tables import session_ingest_marks, sessions

MARK = "sessions_reconcile"          # the fill's row in session_ingest_marks
MARGIN = timedelta(minutes=10)       # a pass starts this long before the last one's newest line
CHUNK = 200                          # sessions read from DuckDB under one hold of the lock

# the rendered text names each tool as "→ name(": a cheap filter before the payload is parsed
# (the same one `Store.coordination_calls` puts in its SQL)
_COORD_HINTS = ("→ mcp__", "→ SendMessage(", "→ ListAgents(", "→ Skill(", "→ Cron")
# the same test in Python, spelled as LIKE reads it (`_` is any one character), so a line passes
# here exactly when it passes the SQL filter
_COORD_TEXT = re.compile("|".join(re.escape(h).replace("_", ".") for h in _COORD_HINTS))


def coordination_in(text: str | None, payload) -> int:
    """How many tool calls of one tool_use line only coordinate the crew (factory.is_coordination).
    The rule `Store.coordination_calls` applies: the line's text names a coordination tool, then
    each tool_use block of its payload counts when its tool does."""
    if not text or not _COORD_TEXT.search(text):
        return 0
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            return 0
    if not isinstance(payload, dict):
        return 0
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    return sum(1 for b in msg.get("content") or []
               if isinstance(b, dict) and b.get("type") == "tool_use"
               and is_coordination(b.get("name")))


def claude_code_sources(store) -> set[str]:
    """The names of the cell's claude_code sources (`claude_code` too, which `cc_source` falls back
    to when the catalog has none)."""
    with store._sread:
        rows = store.sc.execute(
            "SELECT name FROM catalog_sources WHERE connector = 'claude_code'").fetchall()
    return {r[0] for r in rows} | {"claude_code"}


def claude_code_lines(store, envelopes) -> list:
    """The envelopes of `envelopes` that are lines of a claude_code source, with a session."""
    cands = [e for e in envelopes if e.key_value]
    if not cands:
        return []
    names = {e.source for e in cands}
    if names == {"claude_code"}:
        return cands          # the default name: no catalog lookup for the common case
    ours = claude_code_sources(store)
    return [e for e in cands if e.source in ours]


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def _fold(envelopes) -> dict:
    """{session: [source, first line time, last line time, lines, tool calls, coordination calls]}"""
    out: dict = {}
    for e in envelopes:
        t = _utc(e.event_time)
        r = out.get(e.key_value)
        if r is None:
            r = out[e.key_value] = [e.source, t, t, 0, 0, 0]
        r[1], r[2] = min(r[1], t), max(r[2], t)
        r[3] += 1
        if e.event_type == "tool_use":
            r[4] += 1
            r[5] += coordination_in(e.text, e.payload)
    return out


def _add(store, folded: dict) -> None:
    """Add each session's counts to its row, in one transaction. Rows are written in session order,
    so two writers on Postgres meet their rows in the same order and cannot deadlock."""
    db = store.sessions_db
    now = now_utc()
    stmt = db.upsert(sessions)
    stmt = stmt.on_conflict_do_update(
        index_elements=["session"],
        set_={"line_count": sessions.c.line_count + stmt.excluded.line_count,
              "tool_calls": sessions.c.tool_calls + stmt.excluded.tool_calls,
              "coordination_calls": (sessions.c.coordination_calls
                                     + stmt.excluded.coordination_calls),
              "started_at": db.least(sessions.c.started_at, stmt.excluded.started_at),
              "last_line_at": db.greatest(sessions.c.last_line_at, stmt.excluded.last_line_at),
              "updated_at": stmt.excluded.updated_at})
    rows = [{"session": sid, "source": r[0], "started_at": r[1], "last_line_at": r[2],
             "line_count": r[3], "tool_calls": r[4], "coordination_calls": r[5], "updated_at": now}
            for sid, r in sorted(folded.items())]
    with db.begin() as conn:
        conn.execute(stmt, rows)


def record(store, envelopes) -> None:
    """Count a batch of claude_code lines (already committed to DuckDB) into `sessions`. A failure
    logs one line and counts in tares_session_ingest_failures_total; it never reaches the caller,
    because the lines are stored and the next start's reconcile counts them."""
    if not envelopes:
        return
    t0 = time.perf_counter()
    ok = True
    try:
        _add(store, _fold(envelopes))
    except Exception as e:
        ok = False
        print(f"taresd: counting {len(envelopes)} claude_code line(s) into sessions failed "
              f"({type(e).__name__}: {e}); caught up at the next start", flush=True)
    metrics.session_ingest(time.perf_counter() - t0, ok)


# ── catching up from DuckDB ──────────────────────────────────────────────────────────────────
def _mark(store):
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == MARK)).scalar_one_or_none()


def _set_mark(store, at: datetime) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=MARK, at=at)
    stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"at": stmt.excluded.at})
    with db.begin() as conn:
        conn.execute(stmt)


def reconcile_sessions(store, chunk: int = CHUNK) -> int:
    """Bring `sessions` up to what DuckDB holds, for the sessions with lines newer than the last
    pass (every session on the first pass). Each counter becomes the larger of the stored value and
    the one DuckDB computes; the mark then moves to the newest `ingest_time` read. Reads DuckDB in
    chunks of `chunk` sessions, taking and letting go of the store's lock for each, and writes
    under the same lock that `Store.append` holds across its commit and `record`, so a line is
    never counted by both. Returns how many sessions it looked at."""
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    mark = _mark(store)
    since = mark - MARGIN if mark is not None else None
    with store._lock:
        found = store.con.execute(
            f"SELECT key_value, max(ingest_time) FROM events WHERE source IN ({marks}) "
            "AND key_value IS NOT NULL AND key_value <> '' "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) GROUP BY key_value",
            [*sources, since, since]).fetchall()
    newest = max((r[1] for r in found), default=None)
    sids = sorted(r[0] for r in found)
    for i in range(0, len(sids), chunk):
        _reconcile_chunk(store, sources, sids[i:i + chunk])
    if newest is not None:
        _set_mark(store, _utc(newest))
    return len(sids)


def _reconcile_chunk(store, sources: list, sids: list) -> None:
    marks = ", ".join("?" for _ in sources)
    keys = ", ".join("?" for _ in sids)
    args = [*sources, *sids]
    with store._sess_lock:
        with store._lock:
            counts = store.con.execute(
                "SELECT key_value, any_value(source), min(event_time), max(event_time), count(*), "
                "count(*) FILTER (WHERE event_type = 'tool_use') FROM events "
                f"WHERE source IN ({marks}) AND key_value IN ({keys}) GROUP BY key_value",
                args).fetchall()
            hints = " OR ".join("text LIKE ?" for _ in _COORD_HINTS)
            lines = store.con.execute(
                "SELECT key_value, text, payload FROM events "
                f"WHERE source IN ({marks}) AND key_value IN ({keys}) AND event_type = 'tool_use' "
                f"AND ({hints})", [*args, *[f"%{h}%" for h in _COORD_HINTS]]).fetchall()
        coord: dict = {}
        for sid, text, payload in lines:
            n = coordination_in(text, payload)
            if n:
                coord[sid] = coord.get(sid, 0) + n
        computed = {sid: (src, _utc(first), _utc(last), int(n), int(tools), coord.get(sid, 0))
                    for sid, src, first, last, n, tools in counts}
        if not computed:
            return
        db = store.sessions_db
        now = now_utc()
        stmt = db.upsert(sessions)
        ex = stmt.excluded
        # the larger of what is stored and what DuckDB says: either one can only undercount
        stmt = stmt.on_conflict_do_update(
            index_elements=["session"],
            set_={"line_count": db.greatest(sessions.c.line_count, ex.line_count),
                  "tool_calls": db.greatest(sessions.c.tool_calls, ex.tool_calls),
                  "coordination_calls": db.greatest(sessions.c.coordination_calls,
                                                    ex.coordination_calls),
                  "started_at": db.least(sessions.c.started_at, ex.started_at),
                  "last_line_at": db.greatest(sessions.c.last_line_at, ex.last_line_at),
                  "updated_at": ex.updated_at})
        rows = [{"session": sid, "source": c[0], "started_at": c[1], "last_line_at": c[2],
                 "line_count": c[3], "tool_calls": c[4], "coordination_calls": c[5],
                 "updated_at": now} for sid, c in sorted(computed.items())]
        with db.begin() as conn:
            conn.execute(stmt, rows)


def start_reconcile(store) -> threading.Thread:
    """Run `reconcile_sessions` in a background thread, once. A failure prints one line: the next
    start tries again, and the table only undercounts until then."""
    def run():
        t0 = time.perf_counter()
        try:
            n = reconcile_sessions(store)
            if n:          # a start with nothing to catch up on prints nothing
                print(f"taresd: sessions table caught up for {n} session(s) in "
                      f"{time.perf_counter() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"taresd: sessions table catch-up failed ({type(e).__name__}: {e}); "
                  "tried again at the next start", flush=True)
    th = threading.Thread(target=run, name="sessions-reconcile", daemon=True)
    th.start()
    return th
