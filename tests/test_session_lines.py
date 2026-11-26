"""The recent lines of every session are kept in a table, `session_lines`, beside DuckDB (TR-1478).

`session_ingest.record` writes each claude_code line it counts into `session_lines`, numbered within
its session by `seq` (from the `line_count` the `sessions` upsert returns, in the same transaction),
and `sessions.lines_complete` says whether the table holds every line DuckDB holds for the session.
The reconcile sets that flag, at start and every ten minutes. A session whose lines are complete is read from
the table and the archive (TR-1507): `Store.key_events`, `key_payload_rows` and `key_tool_result` answer what
DuckDB answers, and `*_from` also say where the lines came from (`table`, `archive`, `both`, `events`).

Plain:                python tests/test_session_lines.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_lines.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alembic import command  # noqa: E402
from sqlalchemy import func, inspect, select, text  # noqa: E402

from tares import metrics, session_archive, session_ingest, session_read  # noqa: E402
from tares.connectors.claude_code import ClaudeCodeConnector  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_archive import FileBackend  # noqa: E402
from tares.session_store.tables import metadata, session_archive_parts, session_lines, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
HEAD = "0017_crew_tables"
SRC = "claude_code"
_N = [0]


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, etype="assistant", payload=None, labels=None, source=SRC, sec=None):
    _N[0] += 1
    sec = _N[0] if sec is None else sec
    p = {"uuid": "u-" + uuid.uuid4().hex[:10]} if payload is None else payload
    return Envelope(source=source, source_type="agent_session", key_value=session, event_type=etype,
                    text="t", event_time=T0 + timedelta(seconds=sec), payload=p, labels=labels or {})


def open_store():
    return Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))


def stored(s, session):
    """The rows of session_lines for a session, in seq order."""
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(select(
            session_lines.c.seq, session_lines.c.line_key, session_lines.c.at, session_lines.c.type,
            session_lines.c.labels, session_lines.c.render, session_lines.c.payload).where(
            session_lines.c.session == session).order_by(session_lines.c.seq)).all()]


def duck(s, session):
    """The same lines as DuckDB holds them, in the order they were stored."""
    with s._lock:
        return s.con.execute(
            "SELECT event_type, labels::VARCHAR, payload::VARCHAR FROM events WHERE key_value = ? "
            "ORDER BY ingest_time, event_time", [session]).fetchall()


def row(s, session):
    with s.sessions_db.begin() as c:
        r = c.execute(select(sessions.c.line_count, sessions.c.lines_complete).where(
            sessions.c.session == session)).first()
    return None if r is None else (r[0], bool(r[1]))


def total(s):
    with s.sessions_db.begin() as c:
        return c.execute(select(func.count()).select_from(session_lines)).scalar()


def revision():
    print("== revision 0006_session_lines ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "sl_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")

    def step_down(sdb, to):
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, to)

    def version(sdb):
        with sdb.begin() as c:
            return [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]

    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        insp = inspect(sdb.engine)
        ck("the table exists", "session_lines" in insp.get_table_names())
        ck("its columns", sorted(c["name"] for c in insp.get_columns("session_lines")) == sorted(
            ["session", "seq", "line_key", "at", "type", "labels", "render", "payload"]))
        ck("no text column: the text is rebuilt from the payload",
           "text" not in [c["name"] for c in insp.get_columns("session_lines")])
        ck("its key is (session, seq)", insp.get_pk_constraint("session_lines")[
            "constrained_columns"] == ["session", "seq"])
        ck("an index on (session, at)", [tuple(i["column_names"]) for i in insp.get_indexes(
            "session_lines")] == [("session", "at")])
        ck("the store is at the head", version(sdb) == [HEAD], version(sdb))
        with sdb.begin() as c:
            c.execute(text("INSERT INTO sessions (session, lines_complete) VALUES ('x', true)"))
        step_down(sdb, "0005_session_turns")
        names = inspect(sdb.engine).get_table_names()
        ck("a downgrade removes the table and keeps the sessions", "session_lines" not in names
           and "sessions" in names and "session_turns" in names, names)
        sdb.upgrade()
        with sdb.begin() as c:
            flag = c.execute(text("SELECT lines_complete FROM sessions WHERE session = 'x'")).scalar()
        ck("an upgrade after it builds the table again and clears lines_complete, so a row that "
           "was true before the table was lost is not trusted", "session_lines" in inspect(
               sdb.engine).get_table_names() and not flag, flag)
        sdb.upgrade()
        ck("an upgrade at the head again changes nothing", version(sdb) == [HEAD])
        # the stamped case: the table was made by hand while the version is behind
        step_down(sdb, "0005_session_turns")
        with sdb.begin() as c:
            metadata.tables["session_lines"].create(c)
            c.execute(text("INSERT INTO session_lines (session, seq, payload) VALUES ('kept', 1, 'p')"))
        sdb.upgrade()
        with sdb.begin() as c:
            kept = c.execute(text("SELECT payload FROM session_lines WHERE session = 'kept'")).scalar()
        ck("an upgrade over a table that already exists stamps the revision and keeps the rows",
           version(sdb) == [HEAD] and kept == "p", (version(sdb), kept))
        ck("and the index is there", [tuple(i["column_names"]) for i in inspect(
            sdb.engine).get_indexes("session_lines")] == [("session", "at")])
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


def lines_in_both():
    print("== a line is in both stores after Store.append ==")
    s = open_store()
    s.upsert_catalog_source("plain", "claude_code", "claude_code", "", {})
    s.upsert_catalog_source("open", "claude_code", "claude_code", "", {"redact": False, "include_thinking": True})
    batch = [line("a", "user", {"uuid": "k1", "message": {"content": "hi"}}, {"tares_person": "ann"}),
             line("a", "assistant", {"uuid": "k2"}), line("a", "tool_use", {"x": 1}),
             line("b", "user", {"uuid": "kb", "text": "é ✓ \u0000 nul"}, source="open"),
             line("c", "assistant", {}, source="plain")]
    s.append(batch)
    a, b, c = stored(s, "a"), stored(s, "b"), stored(s, "c")
    ck("every line is in session_lines, numbered from 1 in order", [r[0] for r in a] == [1, 2, 3]
       and [r[0] for r in b] == [1] and [r[0] for r in c] == [1], (a, b, c))
    ck("the key is the uuid, else the hash of the payload", a[0][1] == "k1" and a[1][1] == "k2"
       and len(a[2][1]) == 64 and len(c[0][1]) == 64, (a[0][1], a[2][1], c[0][1]))
    ck("the type and the time are the line's", [r[3] for r in a] == ["user", "assistant", "tool_use"]
       and a[0][2] == batch[0].event_time, (a[0][2], batch[0].event_time))
    d = duck(s, "a")
    ck("payload and labels are the text DuckDB stores, character for character", len(a) == len(d) == 3
       and all(r[6] == dd[2] and r[4] == dd[1] for r, dd in zip(a, d)), (a, d))
    ck("a NUL byte and non-ASCII text keep their meaning", json.loads(b[0][6])["text"] == "é ✓ \u0000 nul")
    ck("the non-ASCII payload is the same text DuckDB stores, character for character",
       b[0][6] == duck(s, "b")[0][2] and b[0][6].isascii(), (b[0][6], duck(s, "b")))
    ck("the render flags are the connector's defaults for a source with no setting",
       json.loads(a[0][5]) == {"include_thinking": False, "redact": True}
       and json.loads(c[0][5]) == {"include_thinking": False, "redact": True}, (a[0][5], c[0][5]))
    ck("and the source's own setting when it has one", json.loads(b[0][5]) == {
        "include_thinking": True, "redact": False}, b[0][5])
    ck("the empty payload is stored as {}", c[0][6] == "{}", c[0][6])
    ck("sessions.line_count agrees with the rows", row(s, "a")[0] == 3 and row(s, "b")[0] == 1)
    s.append([line("a", "assistant", {"uuid": "k9"}), line("a", "user", {"uuid": "k10"})])
    ck("a second batch continues the numbering", [r[0] for r in stored(s, "a")] == [1, 2, 3, 4, 5]
       and stored(s, "a")[3][1] == "k9", stored(s, "a"))
    s.append([line("a", "assistant", {"uuid": "late"}, sec=-100)])
    ck("a line with an older time still takes the next number", stored(s, "a")[-1][0] == 6
       and stored(s, "a")[-1][1] == "late")
    s.close()


def complete_flag():
    print("== lines_complete ==")
    s = open_store()
    s.append([line("n", "user", {"uuid": "1"}), line("n", "assistant", {"uuid": "2"})])
    ck("a new row is born in the table: true at once", row(s, "n") == (2, True), row(s, "n"))
    ck("one reconcile leaves it true, the table holds every line", (session_ingest.reconcile_sessions(s),
                                                                     row(s, "n"))[1] == (2, True), row(s, "n"))
    s.append([line("n", "assistant", {"uuid": "3"})])
    ck("a later batch leaves it as it was (the next reconcile reads it again)", row(s, "n") == (3, True),
       row(s, "n"))
    # earlier lines only DuckDB holds: record did not run for them
    real = session_ingest.record
    session_ingest.record = lambda *a, **k: None
    try:
        s.append([line("old", "user", {"uuid": "o1"}), line("old", "assistant", {"uuid": "o2"})])
    finally:
        session_ingest.record = real
    s.append([line("old", "assistant", {"uuid": "o3"})])
    session_ingest.reconcile_sessions(s)
    ck("a session with earlier DuckDB-only lines is false", stored(s, "old") != [] and
       row(s, "old") is not None and row(s, "old")[1] is False and len(stored(s, "old")) == 1, (
           row(s, "old"), stored(s, "old")))
    ck("and it stays false on every later reconcile", (session_ingest.reconcile_sessions(s),
                                                        row(s, "old")[1])[1] is False)
    ck("another session is not touched by it", row(s, "n")[1] is True)
    s.close()

    print("== a line sent twice is stored twice, and the session stays true ==")
    s = open_store()
    twice = line("t", "assistant", {"uuid": "same"})
    s.append([twice])
    s.append([twice, line("t", "user", {"uuid": "other"})])
    ck("DuckDB keeps both copies", len(duck(s, "t")) == 3, len(duck(s, "t")))
    ck("so does session_lines, with two numbers", [(r[0], r[1]) for r in stored(s, "t")] == [
        (1, "same"), (2, "same"), (3, "other")], stored(s, "t"))
    session_ingest.reconcile_sessions(s)
    ck("and the session is true", row(s, "t") == (3, True), row(s, "t"))
    s.close()

    print("== a record that fails ==")
    s = open_store()
    s.append([line("f", "user", {"uuid": "f1"})])
    session_ingest.reconcile_sessions(s)
    ck("it was true", row(s, "f") == (1, True))
    real = session_ingest._line_rows

    def boom(*a, **k):
        raise RuntimeError("lines refused")
    session_ingest._line_rows = boom
    try:
        s.append([line("f", "assistant", {"uuid": "f2"}), line("g", "user", {"uuid": "g1"})])
    finally:
        session_ingest._line_rows = real
    ck("the counts are still added", row(s, "f")[0] == 2 and row(s, "g")[0] == 1, (row(s, "f"), row(s, "g")))
    ck("and the sessions are false at once, without waiting for a reconcile",
       row(s, "f")[1] is False and row(s, "g")[1] is False, (row(s, "f"), row(s, "g")))
    ck("no line of the failed batch is in session_lines", len(stored(s, "f")) == 1 and stored(s, "g") == [])
    session_ingest.reconcile_sessions(s)
    ck("the reconcile keeps them false: the table is short", row(s, "f")[1] is False
       and row(s, "g")[1] is False)
    s.close()


def concurrent_appends():
    print("== four threads add lines at once ==")
    s = open_store()
    per, threads = 25, 4
    errors = []

    def work(i):
        try:
            for j in range(4):
                session_ingest.record(s, [line("hot", "assistant", {"uuid": f"t{i}-{j}-{k}"})
                                          for k in range(per)])
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    rows = stored(s, "hot")
    seqs = [r[0] for r in rows]
    n = row(s, "hot")[0]
    ck("no thread failed", not errors, errors[:1])
    ck("every line is stored", len(rows) == n and n == threads * 4 * per, (len(rows), n))
    ck("seq is dense: 1 .. line_count", seqs == list(range(1, n + 1)), seqs[:10])
    ck("and unique", len(set(seqs)) == len(seqs))
    ck("no thread's lines were dropped", len({r[1] for r in rows}) == n)
    s.close()

    print("== four threads append through the Store ==")
    s = open_store()
    errors = []

    def work2(i):
        try:
            for j in range(5):
                s.append([line("hot2", "assistant", {"uuid": f"s{i}-{j}-{k}"}) for k in range(5)])
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=work2, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    rows = stored(s, "hot2")
    ck("no thread failed", not errors, errors[:1])
    ck("100 lines in both stores", len(rows) == 100 and len(duck(s, "hot2")) == 100, (len(rows),))
    ck("seq is dense and unique", [r[0] for r in rows] == list(range(1, 101)))
    session_ingest.reconcile_sessions(s)
    ck("and the reconcile says true", row(s, "hot2") == (100, True), row(s, "hot2"))
    s.close()


def purge_and_gauges():
    print("== purging a source ==")
    s = open_store()
    s.upsert_catalog_source("laptop", "claude_code", "claude_code", "", {})
    s.append([line("p", "user", {"uuid": "p1"}, source="laptop"), line("keep", "user", {"uuid": "k1"})])
    s.purge_events("laptop")
    ck("the purged source's lines are gone from session_lines", stored(s, "p") == [])
    ck("another source's lines stay", len(stored(s, "keep")) == 1)
    s.append([line("p", "user", {"uuid": "p1"}, source="laptop")])
    ck("a re-ship starts at 1 again, without a clash", [r[0] for r in stored(s, "p")] == [1], stored(s, "p"))
    s.close()

    print("== the size gauges ==")
    s = open_store()
    s.append([line("z", "user", {"uuid": f"z{i}"}) for i in range(7)])
    rows, size = session_ingest.lines_size(s)
    ck("rows counts the table", rows == 7, rows)
    ck("bytes is a positive size", isinstance(size, int) and size > 0, size)
    session_ingest.refresh_lines_gauges(s)
    if metrics.AVAILABLE:
        ck("tares_session_lines_rows and _bytes are set", metrics.SESSION_LINES_ROWS._value.get() == 7
           and metrics.SESSION_LINES_BYTES._value.get() == size, (
               metrics.SESSION_LINES_ROWS._value.get(), metrics.SESSION_LINES_BYTES._value.get()))
        out = metrics.render() if hasattr(metrics, "render") else b""
        out = out if isinstance(out, str) else (out[0] if isinstance(out, tuple) else out).decode()
        ck("and /metrics names both", "tares_session_lines_rows" in out and "tares_session_lines_bytes" in out)
    s.close()


def cycle():
    print("== the reconcile repeats ==")
    old = session_ingest.CYCLE
    session_ingest.CYCLE = 0.2
    s = open_store()
    try:
        s.append([line("c", "user", {"uuid": "c1"})])
        passes = []
        real = session_ingest._reconcile_pass
        session_ingest._reconcile_pass = lambda st, t0: (passes.append(1), real(st, t0))[1]
        th = s.start_session_reconcile()
        th.join(30)
        ck("the first pass ends (the thread returned does not run forever)", not th.is_alive())
        names = [t.name for t in threading.enumerate()]
        ck("one repeating thread runs", names.count("sessions-reconcile-cycle") == 1, names)
        s.start_session_reconcile().join(30)
        ck("a second start adds none", [t.name for t in threading.enumerate()].count(
            "sessions-reconcile-cycle") == 1)
        s.append([line("c", "assistant", {"uuid": "c2"})])       # counted and stored after the first pass
        end = time.time() + 15
        while time.time() < end and not row(s, "c")[1]:
            time.sleep(0.1)
        ck("a session that was false becomes true on a later pass, without a restart", row(s, "c") == (2, True),
           row(s, "c"))
        end = time.time() + 15
        while time.time() < end and len(passes) < 3:
            time.sleep(0.1)
        ck("the cycle keeps repeating: the start-up pass and at least two more", len(passes) >= 3, len(passes))
    finally:
        session_ingest._reconcile_pass = real
        session_ingest.CYCLE = old
        s.close()
    end = time.time() + 10
    while time.time() < end and "sessions-reconcile-cycle" in [t.name for t in threading.enumerate()]:
        time.sleep(0.1)
    ck("Store.close ends it", "sessions-reconcile-cycle" not in [t.name for t in threading.enumerate()])

    print("== a store that is dropped without close is freed ==")
    import gc
    import weakref
    session_ingest.CYCLE = 600.0
    s2 = open_store()
    s2.start_session_reconcile().join(30)
    ref = weakref.ref(s2)
    del s2
    gc.collect()
    ck("the repeating thread does not keep the store alive (the daemon exits without close)",
       ref() is None, ref())
    session_ingest.CYCLE = old


SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
NOW = T0 + timedelta(days=30)


def raw(session, kind, seq, **kw):
    """One transcript line as the plugin ships it (sessionId, uuid, a timestamp `seq` seconds in)."""
    ts = (T0 + timedelta(seconds=seq)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    base = {"sessionId": session, "uuid": f"{session}-{seq}", "timestamp": ts}
    if kind == "user":
        return {**base, "type": "user", "message": {"role": "user", "content": kw.get("text", f"question {seq}")}}
    if kind == "assistant":
        return {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": kw.get("text", f"answer {seq}")}]}}
    if kind == "thinking":
        return {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": kw.get("text", "private reasoning")}]}}
    if kind == "tool_use":
        return {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": kw["id"], "name": "Bash", "input": {"command": "ls"}}]}}
    if kind == "tool_result":
        return {**base, "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": kw["id"], "content": kw.get("text", "out " + kw["id"])}]}}
    raise ValueError(kind)


def envelopes(source, lines, config=None):
    """What the claude_code connector makes of raw lines, the way ingest does (text, labels, payload)."""
    c = ClaudeCodeConnector(type("Cfg", (), {"name": source, "type": "claude_code", "config": config or {}})(),
                            store=None)
    out = []
    for r in lines:
        out += c.map_payload(r)
    return out


def duck_read(s, source, session, limit=1000, offset=0):
    """What DuckDB answers for the three readers: the path before TR-1507."""
    with s._lock:
        ev = s.con.execute(
            "SELECT event_type, text, event_time, labels FROM events WHERE source = ? AND key_value = ? "
            "ORDER BY event_time, ingest_time LIMIT ? OFFSET ?", [source, session, limit, offset]).fetchall()
        pay = s.con.execute(
            "SELECT event_type, text, event_time, payload FROM events WHERE source = ? AND key_value = ? "
            "ORDER BY event_time, ingest_time LIMIT ? OFFSET ?", [source, session, limit, offset]).fetchall()
    return ([{"event_type": r[0], "text": r[1], "event_time": r[2], "labels": json.loads(r[3]) if r[3] else {}}
             for r in ev],
            [{"event_type": r[0], "text": r[1], "event_time": r[2], "payload": r[3]} for r in pay])


def same(a, b):
    """Equal as lists of dicts; a time is the same instant (DuckDB answers in the session's zone)."""
    return a == b


def parts_of(s, session):
    with s.sessions_db.begin() as c:
        return c.execute(select(session_archive_parts).where(session_archive_parts.c.session == session)
                         .order_by(session_archive_parts.c.part)).all()


@contextlib.contextmanager
def archive_at(folder):
    """TARES_ARCHIVE_URL points at a folder, the way a cell is set up to archive."""
    old = os.environ.get("TARES_ARCHIVE_URL")
    os.environ["TARES_ARCHIVE_URL"] = "file://" + folder
    try:
        yield FileBackend(folder)
    finally:
        if old is None:
            os.environ.pop("TARES_ARCHIVE_URL", None)
        else:
            os.environ["TARES_ARCHIVE_URL"] = old


@contextlib.contextmanager
def printed():
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def make_session(s, session, n=6, config=None, source=SRC, extra=()):
    """A session of n lines (user, assistant, a tool call and its result, a secret, thinking...)."""
    kinds = [("user", {"text": "hello " + SECRET}), ("assistant", {}), ("tool_use", {"id": "toolu_" + session}),
             ("tool_result", {"id": "toolu_" + session}), ("thinking", {}), ("user", {"text": "x" * 6000})]
    lines = []
    for i in range(n):
        k, kw = kinds[i % len(kinds)]
        lines.append(raw(session, k, i + 1, **kw))
    lines += list(extra)
    s.append(envelopes(source, lines, config))
    session_ingest.reconcile_sessions(s)


def reads_equal(s, session, source=SRC, label=""):
    """The three readers answer what DuckDB answers, for the whole session and for pages of it."""
    ok = True
    for limit, offset in ((1000, 0), (2, 0), (2, 2), (3, 4), (10, 50)):
        want_ev, want_pay = duck_read(s, source, session, limit, offset)
        got_ev, w1 = s.key_events_from(source, session, limit, offset)
        got_pay, w2 = s.key_payload_rows_from(source, session, limit, offset)
        if not (same(got_ev, want_ev) and same(got_pay, want_pay)):
            print("   ", label, limit, offset, "\n    want", want_ev, "\n    got ", got_ev)
            ok = False
        if not (s.key_events(source, session, limit, offset) == got_ev
                and s.key_payload_rows(source, session, limit, offset) == got_pay):
            ok = False
    return ok


def tool_equal(s, session, tool_id, source=SRC):
    with s._lock:
        rows = s.con.execute(
            "SELECT payload FROM events WHERE source = ? AND key_value = ? AND event_type = 'tool_result' "
            "AND payload LIKE ? ORDER BY event_time DESC LIMIT 5", [source, session, f"%{tool_id}%"]).fetchall()
    want = None
    for (pj,) in rows:
        for b in (json.loads(pj).get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and str(b.get("tool_use_id")) == tool_id:
                want = want or b
    return s.key_tool_result(source, session, tool_id) == want, want


def reads():
    print("== a session whose lines are complete is read from the table, with DuckDB's answer ==")
    s = open_store()
    s.upsert_catalog_source("open", "claude_code", "claude_code", "", {"redact": False, "include_thinking": True})
    make_session(s, "t1")
    make_session(s, "t2", n=3)
    make_session(s, "o1", config={"redact": False, "include_thinking": True}, source="open")
    ck("the lines are complete", row(s, "t1") == (6, True) and row(s, "o1") == (6, True), (row(s, "t1"), row(s, "o1")))
    ck("the three readers answer what DuckDB answers, whole and by page", reads_equal(s, "t1", label="t1"))
    ck("a source with redact off and thinking on is read the same way", reads_equal(s, "o1", "open", "o1"))
    ev, where = s.key_events_from(SRC, "t1")
    ck("lines_from says table", where == "table" and s.key_payload_rows_from(SRC, "t1")[1] == "table", where)
    ck("the secret is hidden in the text, and the thinking is left out",
       SECRET not in json.dumps(ev, default=str) and not any("private reasoning" in e["text"] for e in ev), ev[:1])
    ov, _ = s.key_events_from("open", "o1")
    ck("a source with redact off shows the secret in the text, and thinking", any(SECRET in e["text"] for e in ov)
       and any("private reasoning" in e["text"] for e in ov), [e["text"][:40] for e in ov])
    ck("the long turn is cut at the same length DuckDB cut it", any(len(e["text"]) > 500 for e in ev)
       and reads_equal(s, "t1"), [len(e["text"]) for e in ev])
    ok, want = tool_equal(s, "t1", "toolu_t1")
    ck("key_tool_result answers the block DuckDB answers", ok and want and want["tool_use_id"] == "toolu_t1", want)
    ck("and says table", s.key_tool_result_from(SRC, "t1", "toolu_t1")[1] == "table")
    ck("a tool id the session does not hold is None, from the table", s.key_tool_result_from(
        SRC, "t1", "toolu_nope") == (None, "table"))
    ck("a tool id of another session is not found in this one", s.key_tool_result_from(
        SRC, "t2", "toolu_t1") == (None, "table"))
    s.append(envelopes(SRC, [raw("sub", "tool_use", 1, id="toolu_sub10"), raw("sub", "tool_result", 2, id="toolu_sub10")]))
    session_ingest.reconcile_sessions(s)
    ck("an id that only appears inside a longer id is not that call: None, as DuckDB",
       tool_equal(s, "sub", "toolu_sub1")[0] and s.key_tool_result_from(SRC, "sub", "toolu_sub1") == (None, "table")
       and s.key_tool_result_from(SRC, "sub", "toolu_sub10")[0] is not None)
    ck("paging past the end is an empty list, like DuckDB", s.key_events_from(SRC, "t1", 10, 50) == ([], "table"))

    print("== lines at the same instant, lines out of order, and a limit ==")
    t = [raw("tie", "user", 5), raw("tie", "assistant", 5), raw("tie", "assistant", 5, text="third"),
         raw("tie", "user", 1), raw("tie", "assistant", 9)]
    s.append(envelopes(SRC, t))
    session_ingest.reconcile_sessions(s)
    ck("lines with one timestamp come in the order they were stored, older first", reads_equal(s, "tie", label="tie"))
    ck("by time, not by arrival", [e["event_time"] for e in s.key_events(SRC, "tie")] == sorted(
        e["event_time"] for e in s.key_events(SRC, "tie")))

    print("== a session the table does not hold in full reads DuckDB ==")
    s.append(envelopes(SRC, [raw("gap", "user", 1), raw("gap", "assistant", 2)]))
    ev, where = s.key_events_from(SRC, "gap")
    ck("a session born in the table reads the table at once, with no reconcile",
       row(s, "gap") == (2, True) and where == "table" and len(ev) == 2, (row(s, "gap"), where))
    with s.sessions_db.begin() as c:
        c.execute(sessions.update().where(sessions.c.session == "gap").values(lines_complete=False))
    ev, where = s.key_events_from(SRC, "gap")
    ck("with the flag false lines_from is events, the lines are all there",
       row(s, "gap") == (2, False) and where == "events" and len(ev) == 2 and same(ev, duck_read(s, SRC, "gap")[0]), row(s, "gap"))
    with s.sessions_db.begin() as c:
        c.execute(session_lines.delete().where(session_lines.c.session == "t2", session_lines.c.seq == 2))
    ck("a row the table lost reads DuckDB even though the flag says complete", row(s, "t2") == (3, True)
       and s.key_events_from(SRC, "t2")[1] == "events" and same(s.key_events(SRC, "t2"), duck_read(s, SRC, "t2")[0]))
    ck("so does the payload reader and the tool reader", s.key_payload_rows_from(SRC, "t2")[1] == "events"
       and s.key_tool_result_from(SRC, "t2", "toolu_t2")[1] == "events")
    ck("a session with no row in sessions reads DuckDB", s.key_events_from(SRC, "no-such")[1] == "events"
       and s.key_events_from(SRC, "no-such")[0] == [])
    ck("another source's session of that name reads DuckDB: the table does not know the source",
       s.key_events_from("other", "t1") == ([], "events"))
    ck("a missing session on every reader", s.key_payload_rows_from(SRC, "no-such") == ([], "events")
       and s.key_tool_result_from(SRC, "no-such", "x") == (None, "events"))
    s.close()


def archived():
    print("== an archived session is read from the archive, a mixed one from both ==")
    s = open_store()
    folder = tempfile.mkdtemp()
    with archive_at(folder) as b:
        make_session(s, "a1")
        make_session(s, "a2", n=4)
        before = (s.key_events(SRC, "a1"), s.key_payload_rows(SRC, "a1"))
        ck("table only first", s.key_events_from(SRC, "a1")[1] == "table")
        moved = session_archive.archive_pass(s, b, NOW)
        ck("both sessions moved to parts", moved == 2 and len(parts_of(s, "a1")) == 1
           and total_of(s, "a1") == 0, (moved, total_of(s, "a1")))
        ev, where = s.key_events_from(SRC, "a1")
        ck("lines_from says archive", where == "archive" and s.key_payload_rows_from(SRC, "a1")[1] == "archive"
           and s.key_tool_result_from(SRC, "a1", "toolu_a1")[1] == "archive", where)
        ck("the readers answer what DuckDB answers, whole and by page", reads_equal(s, "a1", label="a1"))
        ck("and what they answered before the archive moved the rows", (ev, s.key_payload_rows(SRC, "a1")) == before)
        ok, want = tool_equal(s, "a1", "toolu_a1")
        ck("the tool result comes from the part", ok and want is not None, want)
        ck("a tool id the part does not hold is None", s.key_tool_result_from(SRC, "a1", "toolu_nope") == (None, "archive"))

        print("== lines that arrive after the archive: both ==")
        s.append(envelopes(SRC, [raw("a1", "user", 100, text="later"), raw("a1", "assistant", 50, text="in between"),
                                 raw("a1", "tool_use", 101, id="toolu_new"), raw("a1", "tool_result", 102, id="toolu_new")]))
        session_ingest.reconcile_sessions(s)
        ck("a1 is complete across a part and the table", row(s, "a1") == (10, True) and total_of(s, "a1") == 4, row(s, "a1"))
        ev, where = s.key_events_from(SRC, "a1")
        ck("lines_from says both", where == "both" and s.key_payload_rows_from(SRC, "a1")[1] == "both", where)
        ck("merged by time: the line that arrives later but is older sits among the archived ones",
           reads_equal(s, "a1", label="mixed") and [e["text"] for e in ev if e["text"] == "in between"] != [],
           [e["text"][:12] for e in ev])
        s.append(envelopes(SRC, [raw("a1", "assistant", 3, text="same instant as an archived line")]))
        session_ingest.reconcile_sessions(s)
        ev, _ = s.key_events_from(SRC, "a1")
        same_at = [e["text"] for e in ev if e["event_time"] == ev[2]["event_time"]]
        ck("a late line at the instant of an archived one comes after it, as DuckDB has it",
           reads_equal(s, "a1", label="tie across part and table") and len(same_at) == 2
           and same_at[-1] == "same instant as an archived line", same_at)
        ck("a tool result in the table and one in the part are both found", tool_equal(s, "a1", "toolu_new")[0]
           and tool_equal(s, "a1", "toolu_a1")[0] and s.key_tool_result_from(SRC, "a1", "toolu_new")[1] == "both"
           and s.key_tool_result_from(SRC, "a1", "toolu_a1")[0] is not None)

        print("== a second part ==")
        session_archive.archive_pass(s, b, NOW + timedelta(days=1))
        session_ingest.reconcile_sessions(s)
        ck("two parts, nothing left in the table", len(parts_of(s, "a1")) == 2 and total_of(s, "a1") == 0)
        ck("read from the archive, same as DuckDB", s.key_events_from(SRC, "a1")[1] == "archive"
           and reads_equal(s, "a1", label="two parts"))
    s.close()


def total_of(s, session):
    with s.sessions_db.begin() as c:
        return c.execute(select(func.count()).select_from(session_lines).where(
            session_lines.c.session == session)).scalar()


def broken_archive():
    print("== a part that cannot be read falls back to DuckDB and logs one line ==")
    s = open_store()
    folder = tempfile.mkdtemp()
    with archive_at(folder) as b:
        for name in ("missing", "short", "flip", "rot", "count", "none", "sum"):
            make_session(s, name, n=4)
        session_archive.archive_pass(s, b, NOW, limit=10)
        want = {n: duck_read(s, SRC, n)[0] for n in ("missing", "short", "flip", "rot", "count", "none", "sum")}
        key = lambda n: parts_of(s, n)[0].key
        os.remove(os.path.join(folder, key("missing")))
        path = os.path.join(folder, key("short"))
        data = open(path, "rb").read()
        open(path, "wb").write(data[:len(data) // 2])
        path = os.path.join(folder, key("flip"))
        data = bytearray(open(path, "rb").read())
        data[len(data) // 2] ^= 0x01
        open(path, "wb").write(bytes(data))
        import gzip
        path = os.path.join(folder, key("rot"))              # same size and MD5 cannot hold: change both rows
        junk = gzip.compress(b"not json\n", mtime=0)
        open(path, "wb").write(junk)
        import hashlib
        with s.sessions_db.begin() as c:
            c.execute(session_archive_parts.update().where(session_archive_parts.c.session == "rot").values(
                bytes=len(junk), md5=hashlib.md5(junk).hexdigest()))
        with s.sessions_db.begin() as c:                     # the row says more lines than the file holds
            c.execute(session_archive_parts.update().where(session_archive_parts.c.session == "count").values(lines=9))
        with s.sessions_db.begin() as c:                     # a file that is whole but is not the one written
            c.execute(session_archive_parts.update().where(session_archive_parts.c.session == "sum").values(
                md5="0" * 32))
        for n in ("missing", "short", "flip", "rot", "sum"):
            session_read._WARNED.clear()
            with printed() as out:
                got, where = s.key_events_from(SRC, n)
            log = [x for x in out.getvalue().splitlines() if "read from DuckDB" in x]
            ck(f"{n}: DuckDB answers, and lines_from says events", where == "events" and got == want[n], where)
            ck(f"{n}: one line is logged naming the session", len(log) == 1 and n in log[0], log)
        with s.sessions_db.begin() as c:
            c.execute(sessions.update().where(sessions.c.session == "count").values(line_count=9))
        with printed():
            got, where = s.key_events_from(SRC, "count")
        ck("a count that does not add up reads DuckDB", where == "events" and got == want["count"], where)
        with printed() as out:
            for _ in range(3):
                s.key_events_from(SRC, "sum")      # the last one that logged above
        ck("the same session logs once a minute, not once a read: the first read above logged, these three do not",
           out.getvalue().count("read from DuckDB") == 0, out.getvalue())
        with printed():
            ok_tool = s.key_tool_result_from(SRC, "short", "toolu_short")
            ok_pay = s.key_payload_rows_from(SRC, "short")
        ck("the payload and tool readers fall back too", ok_tool[1] == "events" and ok_tool[0] is not None
           and ok_pay[1] == "events", (ok_tool[1], ok_pay[1]))
        with printed() as out:
            os.environ.pop("TARES_ARCHIVE_URL", None)         # no archive configured on this cell any more
            session_read._WARNED.clear()
            got, where = s.key_events_from(SRC, "none")
        if s.sessions_db.dialect == "postgresql":
            ck("no archive configured: DuckDB answers", where == "events" and got == want["none"], where)
        else:
            ck("no URL set: SQLite's own archive folder is read, which holds no such file, so DuckDB answers",
               where == "events" and got == want["none"], where)
    s.close()


def origin():
    print("== lines_origin: a session born in the table is complete without DuckDB (TR-1606) ==")

    def origin_of(s, session):
        with s.sessions_db.begin() as c:
            return c.execute(select(sessions.c.lines_origin).where(sessions.c.session == session)).scalar()

    def empty_duck(s):
        """What a cell after TR-1479 looks like: DuckDB holds none of the session's lines."""
        with s._lock:
            s.con.execute("DELETE FROM events")

    s = open_store()
    s.append([line("o", "user", {"uuid": "1"}), line("o", "assistant", {"uuid": "2"}),
              line("o", "assistant", {"uuid": "3"})])
    empty_duck(s)
    ck("a session recorded fresh is born in the table and complete", row(s, "o") == (3, True)
       and origin_of(s, "o") == "table", (row(s, "o"), origin_of(s, "o")))
    session_ingest.reconcile_sessions(s)
    session_ingest.repair_table_sessions(s)
    ck("it stays complete with no DuckDB rows, after a reconcile and a repair", row(s, "o") == (3, True), row(s, "o"))
    ck("and the table is what a reader gets", s.key_events_from(SRC, "o")[1] == "table"
       and len(s.key_events(SRC, "o")) == 3)
    s.append([line("o", "assistant", {"uuid": "4"})])
    ck("a later batch keeps it complete", row(s, "o") == (4, True) and origin_of(s, "o") == "table")
    session_ingest.reconcile_sessions(s)     # DuckDB now holds 1 of its 4 lines: a table-born session ignores that
    ck("a reconcile that sees 1 of 4 lines in DuckDB leaves it complete", row(s, "o") == (4, True), row(s, "o"))

    print("== a session whose earlier lines are only in DuckDB heals at the first reconcile ==")
    real = session_ingest.record
    session_ingest.record = lambda *a, **k: None
    try:
        s.append([line("h", "user", {"uuid": "h1"}), line("h", "assistant", {"uuid": "h2"}),
                  line("h", "assistant", {"uuid": "h3"})])
    finally:
        session_ingest.record = real
    s.append([line("h", "assistant", {"uuid": "h4"})])
    ck("it is born in the table and true with its one line before any reconcile",
       row(s, "h") == (1, True) and origin_of(s, "h") == "table", (row(s, "h"), origin_of(s, "h")))
    session_ingest.reconcile_sessions(s)
    ck("the first reconcile counts the 4 lines DuckDB holds and marks it incomplete, the table has 1",
       row(s, "h") == (4, False), row(s, "h"))
    s.close()

    print("== a failed write leaves the session incomplete ==")
    s = open_store()
    real = session_ingest._line_rows

    def boom(*a, **k):
        raise RuntimeError("lines refused")
    session_ingest._line_rows = boom
    real_incomplete = session_ingest._lines_incomplete
    session_ingest._lines_incomplete = lambda *a, **k: None     # the birth alone: nothing fixes the flag after
    try:
        with printed():
            s.append([line("bad", "user", {"uuid": "b1"})])
    finally:
        session_ingest._line_rows = real
        session_ingest._lines_incomplete = real_incomplete
    ck("a session whose first batch failed is born incomplete, with no help from _lines_incomplete",
       row(s, "bad") == (1, False), row(s, "bad"))
    ck("a session whose first batch failed has its counts and is incomplete", row(s, "bad") == (1, False)
       and origin_of(s, "bad") == "table", (row(s, "bad"), origin_of(s, "bad")))
    s.append([line("ok", "user", {"uuid": "k1"})])
    session_ingest._line_rows = boom
    try:
        with printed():
            s.append([line("ok", "assistant", {"uuid": "k2"})])
    finally:
        session_ingest._line_rows = real
    ck("a complete session whose later batch failed turns incomplete", row(s, "ok") == (2, False), row(s, "ok"))
    session_ingest.repair_table_sessions(s)
    session_ingest.reconcile_sessions(s)
    ck("a repair and a reconcile keep both incomplete: the table is short", row(s, "bad") == (1, False)
       and row(s, "ok") == (2, False), (row(s, "bad"), row(s, "ok")))
    s.close()

    print("== a session with no origin still compares with DuckDB ==")
    s = open_store()
    real = session_ingest.record
    session_ingest.record = lambda *a, **k: None
    try:
        s.append([line("legacy", "user", {"uuid": "l1"}), line("legacy", "assistant", {"uuid": "l2"})])
    finally:
        session_ingest.record = real
    session_ingest.reconcile_sessions(s)          # counts it from DuckDB: a row with a null origin
    ck("a session counted from the events has a null origin and no table rows", origin_of(s, "legacy") is None
       and row(s, "legacy") == (2, False) and stored(s, "legacy") == [], (row(s, "legacy"), origin_of(s, "legacy")))
    with s.sessions_db.begin() as c:
        for i in (1, 2):
            c.execute(session_lines.insert().values(session="legacy", seq=i, line_key=f"l{i}", type="user",
                                                    labels="{}", render="{}", payload="{}"))
    session_ingest.reconcile_sessions(s)
    ck("it is complete when the table holds as many lines as DuckDB", row(s, "legacy") == (2, True), row(s, "legacy"))
    session_ingest.repair_table_sessions(s)
    ck("the start-up repair does not look at it", row(s, "legacy") == (2, True))
    with s.sessions_db.begin() as c:     # line_count says 5, DuckDB and the table say 2: only DuckDB's count counts
        c.execute(sessions.update().where(sessions.c.session == "legacy").values(line_count=5, lines_complete=False))
    session_ingest.reconcile_sessions(s)
    ck("a null-origin session is judged by DuckDB's count, not by line_count", row(s, "legacy") == (5, True),
       row(s, "legacy"))
    with s._lock:
        s.con.execute("DELETE FROM events WHERE key_value = 'legacy'")
    session_ingest.reconcile_sessions(s)     # DuckDB no longer holds it: a null-origin row is not recomputed
    ck("and with no DuckDB rows it is not recomputed from line_count", row(s, "legacy")[1] is True, row(s, "legacy"))
    s.close()

    print("== a table session set false by hand is repaired on the next start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    s = Store(path)
    s.append([line("r", "user", {"uuid": "r1"}), line("r", "assistant", {"uuid": "r2"})])
    s.append([line("short", "user", {"uuid": "s1"}), line("short", "assistant", {"uuid": "s2"})])
    with s.sessions_db.begin() as c:
        c.execute(session_lines.delete().where(session_lines.c.session == "short", session_lines.c.seq == 2))
        c.execute(sessions.update().where(sessions.c.session.in_(["r", "short"])).values(lines_complete=False))
        c.execute(sessions.update().where(sessions.c.session == "short").values(lines_complete=True))
        c.execute(sessions.update().where(sessions.c.session == "r").values(lines_complete=False))
    ck("set by hand: r false though whole, short true though missing a line", row(s, "r") == (2, False)
       and row(s, "short") == (2, True))
    empty_duck(s)
    s.close()
    s = Store(path)
    s.start_session_reconcile().join()
    ck("the next start puts r right", row(s, "r") == (2, True), row(s, "r"))
    ck("and the one whose table is short false", row(s, "short") == (2, False), row(s, "short"))
    s.close()


def upgrade_keeps_flags():
    print("== existing rows keep their lines_complete through the upgrade (TR-1606) ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "so_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0007_session_archive")
        ck("a downgrade drops the column and only that", "lines_origin" not in [
            x["name"] for x in inspect(sdb.engine).get_columns("sessions")]
           and "session_archive_parts" in inspect(sdb.engine).get_table_names())
        with sdb.begin() as c:
            c.execute(text("INSERT INTO sessions (session, line_count, lines_complete) VALUES ('t', 4, true)"))
            c.execute(text("INSERT INTO sessions (session, line_count, lines_complete) VALUES ('f', 4, false)"))
        sdb.upgrade()
        with sdb.begin() as c:
            got = {r[0]: (bool(r[1]), r[2]) for r in c.execute(text(
                "SELECT session, lines_complete, lines_origin FROM sessions")).all()}
        ck("the old rows keep their flag and have a null origin", got == {"t": (True, None), "f": (False, None)}, got)
        col = [x for x in inspect(sdb.engine).get_columns("sessions") if x["name"] == "lines_origin"][0]
        ck("the column is nullable text with no default", col["nullable"] and col["default"] is None
           and "TEXT" in str(col["type"]).upper(), col)
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


def old_ddl_database():
    print("== Postgres: a database the old DDL made upgrades to the head, with the new column (TR-1606) ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import re
    import psycopg
    from tares import state_db
    sch = "od_" + uuid.uuid4().hex[:12]
    dsn = f"{PG}?options=-csearch_path%3D{sch}"
    names = ["session_stations", "session_states", "project_sessions", "session_usage", "session_meters"]
    pat = re.compile(r"\b(" + "|".join(names) + r")\b")
    try:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {sch}")
        pg = state_db.PgState(dsn)
        for stmt in [x for x in state_db.state_ddl(Store._ddl()) if pat.search(x)]:
            pg.execute(stmt)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        with sdb.begin() as c:
            v = [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]
            c.execute(text("INSERT INTO sessions (session, line_count) VALUES ('x', 1)"))
            got = c.execute(text("SELECT lines_complete, lines_origin FROM sessions")).all()
        ck("the old database is at the head and sessions has lines_origin", v == [HEAD] and "lines_origin" in [
            x["name"] for x in inspect(sdb.engine).get_columns("sessions")], v)
        ck("a row made with only its key has a null origin and is not complete", [tuple(r) for r in got] == [
            (False, None)], got)
        sdb.close()
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {sch} CASCADE")


def main():
    for fn in (revision, lines_in_both, complete_flag, origin, upgrade_keeps_flags, old_ddl_database, concurrent_appends, purge_and_gauges, cycle,
               reads, archived, broken_archive):
        fn()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
