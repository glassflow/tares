"""What each Claude Code session's lines add up to, kept in the `sessions` table (P-TR-295, TR-1471).

A page that lists sessions used to count over the events table, and that table grows with every
line. Here each line is counted once, as it is stored, into one row per session. The readers
`Store.session_activity`, `Store.project_sessions`, `FactoryStore.station_sessions` and
`FactoryStore.coordination_calls` read the row (TR-1499) once the table has caught up (see
`caught_up`), and scan events until then (TR-1520), so a cell just upgraded shows right numbers
at once. Two writers keep the row right:

- `record`, called by `Store.append` after a batch of claude_code lines is committed to DuckDB. It
  adds the batch to each session's row in one transaction (`ON CONFLICT ... DO UPDATE`), so
  concurrent batches lose nothing.
- `reconcile_sessions`, run in a background thread at start. It reads the absolute counts from
  DuckDB for the sessions with lines newer than the last pass, and keeps the larger of what is
  stored and what DuckDB says. Lines written by an image that did not call `record`, or a batch
  whose `record` failed, are counted this way. Both writers only ever undercount, so the larger
  value is the right one, and a second pass changes nothing. When a pass has finished all its
  chunks it sets the `sessions_caught_up` row in `session_ingest_marks`; a pass that raised does
  not, so the readers stay on the scan and the next start tries again.

The readers know the table has caught up from one row in `session_ingest_marks`, `CAUGHT_UP`. It
is set by the first complete `reconcile_sessions`, or at once by `Store.__init__` on a cell whose
events hold no claude_code line (nothing to catch up on). It is never unset.

DuckDB is never changed: `Store.append` keeps writing every line there.

The same two writers keep the facts the factory reads (TR-1477): the crew's protocol messages in
`session_messages` and the shell commands sessions ran, with what they printed, in
`session_commands`. `message_facts`, `command_facts` and `result_facts` are the extraction rules, one
tool_use or tool_result line at a time; `record` writes what they find in a transaction after the
counts' (so a line the facts cannot take never loses a batch's counts), and `fill_facts` runs them over the events a first start has not seen (the watermark
`session_facts`). A row is made by its tool_use id, so a resumed session that copies old lines, a
batch written twice or a fill that runs twice adds nothing. A result sets the fields of the row of
the call it answers and makes none: a result that comes before its call is written by the next fill.

Two things to know when reading the table:

- A row holds the `source` of the session's first line, and the readers ask for the one source
  `cc_source` names, as the events scan did. A session whose lines come from two sources is
  counted under the first one that wrote it.
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

from sqlalchemy import bindparam, func, select

from . import metrics
from .envelope import now_utc
from .factory import is_coordination
from .session_store.tables import (session_commands, session_ingest_marks, session_messages,
                                   sessions)

MARK = "sessions_reconcile"          # the fill's row in session_ingest_marks
CAUGHT_UP = "sessions_caught_up"     # its row when a pass has finished (TR-1520): readers use the table
MARGIN = timedelta(minutes=10)       # a pass starts this long before the last one's newest line
CHUNK = 200                          # sessions read from DuckDB under one hold of the lock
FACTS_MARK = "session_facts"         # the facts fill's row in session_ingest_marks (TR-1477)
OUTPUT_TAIL = 20000                  # characters of a command's output kept: a test run's summary is at the end
MESSAGE_TEXT = 4000                  # characters of a protocol message kept

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


# ── the facts the factory reads (TR-1477) ────────────────────────────────────────────────────
def _blocks(payload, requests: bool = True) -> list:
    """The content blocks of a line's payload (a JSON object, or its text), [] when there are none.
    `requests` False leaves out Claude Code's copy of a request (an older plugin recorded those as
    `api-request...` lines): its calls are the same ones the transcript lines hold."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    if not requests and str(payload.get("type") or "").startswith("api-request"):
        return []
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    content = msg.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(v, limit: int | None = None, tail: bool = False) -> str:
    """A string for a text column: without NUL bytes (Postgres refuses them in text), cut to `limit`
    characters from the start, or from the end when `tail`."""
    s = str(v or "").replace("\x00", "")
    if limit is None:
        return s
    return s[-limit:] if tail else s[:limit]


def _fact_id(block: dict, session: str, at: datetime, n: int) -> str:
    """A call's id: its tool_use id, or, for a block without one, a stand-in from the line."""
    return str(block["id"]) if block.get("id") else f"{session}:{at.isoformat()}:{n}"


def message_facts(event_type: str, text: str | None, payload, session: str, at: datetime,
                  source: str) -> list[dict]:
    """The protocol messages of one tool_use line: its SendMessage calls whose text starts
    `[TF:<TYPE>]` (the crew's protocol, TR-429), as rows of `session_messages`."""
    if event_type != "tool_use" or not text or "SendMessage" not in text:
        return []
    out = []
    for n, b in enumerate(_blocks(payload, requests=False)):
        if not (b.get("type") == "tool_use"
                and str(b.get("name") or "").split("__")[-1] == "SendMessage"):
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        body = _text(inp.get("message") or inp.get("content"))
        m = re.match(r"\s*\[TF:([A-Z_]+)\]", body)
        if not m:
            continue
        out.append({"tool_use_id": _fact_id(b, session, at, n), "source": source,
                    "session": session, "at": at, "to_station": _text(inp.get("to")),
                    "type": m.group(1), "text": body[:MESSAGE_TEXT]})
    return out


def command_facts(event_type: str, text: str | None, payload, session: str, at: datetime,
                  source: str) -> list[dict]:
    """The shell commands of one tool_use line: its Bash calls, as rows of `session_commands`
    (without a result yet)."""
    if event_type != "tool_use" or not text or "Bash(" not in text:
        return []
    out = []
    for n, b in enumerate(_blocks(payload)):
        if b.get("type") == "tool_use" and b.get("name") == "Bash":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            out.append({"tool_use_id": _fact_id(b, session, at, n), "source": source,
                        "session": session, "at": at, "command": _text(inp.get("command"))})
    return out


def result_facts(event_type: str, payload, at: datetime) -> list[dict]:
    """The results of one tool_result line, by the tool_use id they answer: the tail of what was
    printed, and 1 when the tool failed (0 when it did not)."""
    if event_type != "tool_result":
        return []
    out = []
    for b in _blocks(payload):
        if b.get("type") != "tool_result" or not b.get("tool_use_id"):
            continue
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
        out.append({"tool_use_id": str(b["tool_use_id"]), "exit_code": 1 if b.get("is_error") else 0,
                    "output_tail": _text(c, OUTPUT_TAIL, tail=True), "result_at": at})
    return out


def facts_of(lines) -> dict:
    """What `lines` (rows of (source, session, time, event_type, text, payload)) hold:
    {"messages": [...], "commands": [...], "results": [...]}, each deduplicated by tool_use id
    (the first message and command of an id wins, the last result) and in id order, so two writers
    meet their rows in the same order and cannot deadlock."""
    messages: dict = {}
    commands: dict = {}
    results: dict = {}
    for source, session, at, event_type, text, payload in lines:
        at = _utc(at)
        for r in message_facts(event_type, text, payload, session, at, source):
            messages.setdefault(r["tool_use_id"], r)
        for r in command_facts(event_type, text, payload, session, at, source):
            commands.setdefault(r["tool_use_id"], r)
        for r in result_facts(event_type, payload, at):
            results[r["tool_use_id"]] = r
    return {"messages": [messages[k] for k in sorted(messages)],
            "commands": [commands[k] for k in sorted(commands)],
            "results": [results[k] for k in sorted(results)]}


def _write_facts(conn, db, facts: dict) -> None:
    """Write `facts` (from `facts_of`) in the open transaction. A message is kept once. A command
    row takes its call's fields only where it has none, so a repeat changes nothing, and a result
    sets the result fields of the call's row (commands are written first, so a call and its result
    in one batch pair)."""
    if facts["messages"]:
        conn.execute(db.upsert(session_messages).on_conflict_do_nothing(
            index_elements=["tool_use_id"]), facts["messages"])
    if facts["commands"]:
        stmt = db.upsert(session_commands)
        ex = stmt.excluded
        t = session_commands.c
        conn.execute(stmt.on_conflict_do_update(
            index_elements=["tool_use_id"],
            set_={"source": func.coalesce(t.source, ex.source),
                  "session": func.coalesce(t.session, ex.session),
                  "at": func.coalesce(t.at, ex.at),
                  "command": func.coalesce(t.command, ex.command)}), facts["commands"])
    if facts["results"]:
        # a result sets the fields of the call it answers, and makes no row of its own: it does not
        # say which tool it answers, and a row for every tool's result would hold text no reader
        # asks for. A result that comes before its call (a later batch) is written by the next fill.
        t = session_commands.c
        conn.execute(session_commands.update().where(t.tool_use_id == bindparam("r_id")).values(
            exit_code=bindparam("r_exit"), output_tail=bindparam("r_out"),
            result_at=bindparam("r_at")),
            [{"r_id": r["tool_use_id"], "r_exit": r["exit_code"], "r_out": r["output_tail"],
              "r_at": r["result_at"]} for r in facts["results"]])


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


def _add_facts(store, facts: dict) -> None:
    """Write a batch's facts in a transaction of their own: a line the facts cannot take must not
    lose the batch's session counts."""
    if not any(facts.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_facts(conn, db, facts)


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
    try:
        _add_facts(store, facts_of(
            (e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload)
            for e in envelopes))
    except Exception as e:
        ok = False
        print(f"taresd: writing the facts of {len(envelopes)} claude_code line(s) failed "
              f"({type(e).__name__}: {e}); written at the next start", flush=True)
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


def caught_up(store) -> bool:
    """True once the `sessions` table holds every session's counts: the first complete reconcile
    ran, or the cell had no claude_code line to count. One primary-key lookup. Only a True answer
    is kept on the store (the row is never unset, and losing the answer changes nothing)."""
    if getattr(store, "_sessions_caught_up", False):
        return True
    with store.sessions_db.begin() as conn:
        at = conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == CAUGHT_UP)).first()
    if at is None:
        return False
    store._sessions_caught_up = True
    return True


def _set_caught_up(store) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=CAUGHT_UP, at=now_utc())
    stmt = stmt.on_conflict_do_nothing(index_elements=["name"])
    with db.begin() as conn:
        conn.execute(stmt)
    store._sessions_caught_up = True


def mark_caught_up_if_empty(store) -> bool:
    """The one-time shortcut at `Store.__init__`: a cell with no caught-up row whose DuckDB events
    hold no claude_code line has nothing to catch up on, so it counts as caught up at once. A cell
    that has such lines stays on the scan until its reconcile finishes. Returns the state."""
    if caught_up(store):
        return True
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    with store._lock:
        has = store.con.execute(
            f"SELECT 1 FROM events WHERE source IN ({marks}) LIMIT 1", sources).fetchone()
    if has is not None:
        return False
    _set_caught_up(store)
    return True


def reconcile_sessions(store, chunk: int = CHUNK) -> int:
    """Bring `sessions` up to what DuckDB holds, for the sessions with lines newer than the last
    pass (every session on the first pass). Each counter becomes the larger of the stored value and
    the one DuckDB computes; the mark then moves to the newest `ingest_time` read. Reads DuckDB in
    chunks of `chunk` sessions, taking and letting go of the store's lock for each, and writes
    under the same lock that `Store.append` holds across its commit and `record`, so a line is
    never counted by both. Sets the caught-up row when every chunk has been written (even when it
    found no session). Returns how many sessions it looked at."""
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
    _set_caught_up(store)          # only here: a pass that raised above leaves the readers on the scan
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


def _facts_mark(store):
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == FACTS_MARK)).scalar_one_or_none()


def _set_facts_mark(store, at: datetime) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=FACTS_MARK, at=at)
    stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"at": stmt.excluded.at})
    with db.begin() as conn:
        conn.execute(stmt)


def fill_facts(store, chunk: int = CHUNK) -> int:
    """Write the facts of the lines DuckDB holds that this table has not seen: every tool_use and
    tool_result line of a claude_code source newer than the `session_facts` mark minus the margin
    (all of them on the first pass). The same functions `record` runs, written as an upsert, so a
    line `record` already wrote, or a pass that runs twice, changes nothing. Reads DuckDB for
    `chunk` sessions at a time, taking and letting go of the store's lock for each; the mark moves
    to the newest `ingest_time` read once every chunk is written, so a pass that raised is read
    again from the same place. Returns how many sessions it looked at."""
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    mark = _facts_mark(store)
    since = mark - MARGIN if mark is not None else None
    with store._lock:
        found = store.con.execute(
            f"SELECT key_value, max(ingest_time) FROM events WHERE source IN ({marks}) "
            "AND event_type IN ('tool_use', 'tool_result') "
            "AND key_value IS NOT NULL AND key_value <> '' "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) GROUP BY key_value",
            [*sources, since, since]).fetchall()
    newest = max((r[1] for r in found), default=None)
    sids = sorted(r[0] for r in found)
    for i in range(0, len(sids), chunk):
        _fill_chunk(store, sources, sids[i:i + chunk], since)
    if newest is not None:
        _set_facts_mark(store, _utc(newest))
    return len(sids)


def _fill_chunk(store, sources: list, sids: list, since) -> None:
    marks = ", ".join("?" for _ in sources)
    keys = ", ".join("?" for _ in sids)
    with store._lock:
        # the tool_use lines that name a tool the facts come from: a cheap filter before the parse
        lines = store.con.execute(
            "SELECT source, key_value, event_time, event_type, text, payload FROM events "
            f"WHERE source IN ({marks}) AND key_value IN ({keys}) "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) "
            "AND (event_type = 'tool_result' OR (event_type = 'tool_use' "
            "AND (text LIKE '%SendMessage%' OR text LIKE '%Bash(%'))) ORDER BY event_time",
            [*sources, *sids, since, since]).fetchall()
    facts = facts_of(lines)
    if not any(facts.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_facts(conn, db, facts)


def start_reconcile(store) -> threading.Thread:
    """Run `reconcile_sessions` and then `fill_facts` in a background thread, once. A failure
    prints one line: the next start tries again, and the tables only undercount until then."""
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
        t1 = time.perf_counter()
        try:
            n = fill_facts(store)
            if n:
                print(f"taresd: session facts filled for {n} session(s) in "
                      f"{time.perf_counter() - t1:.1f}s", flush=True)
        except Exception as e:
            print(f"taresd: session facts fill failed ({type(e).__name__}: {e}); "
                  "tried again at the next start", flush=True)
    th = threading.Thread(target=run, name="sessions-reconcile", daemon=True)
    th.start()
    return th
