"""The recent lines of every session are kept in a table, `session_lines`, beside DuckDB (TR-1478).

`session_ingest.record` writes each claude_code line it counts into `session_lines`, numbered within
its session by `seq` (from the `line_count` the `sessions` upsert returns, in the same transaction),
and `sessions.lines_complete` says whether the table holds every line DuckDB holds for the session.
The reconcile sets that flag, at start and every ten minutes. Nothing reads the table yet.

Plain:                python tests/test_session_lines.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_lines.py
"""
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

from tares import metrics, session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import metadata, session_lines, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
HEAD = "0006_session_lines"
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
    with s._swrite:
        s.sc.execute("INSERT INTO catalog_sources (name, connector, config) VALUES "
                     "('plain', 'claude_code', '{}')")
        s.sc.execute("INSERT INTO catalog_sources (name, connector, config) VALUES "
                     "('open', 'claude_code', ?)", [json.dumps({"redact": False, "include_thinking": True})])
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
    ck("a new row starts false", row(s, "n") == (2, False), row(s, "n"))
    ck("one reconcile sets it, the table holds every line", (session_ingest.reconcile_sessions(s),
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
    with s._swrite:
        s.sc.execute("INSERT INTO catalog_sources (name, connector) VALUES ('laptop', 'claude_code')")
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


def main():
    for fn in (revision, lines_in_both, complete_flag, concurrent_appends, purge_and_gauges, cycle):
        fn()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
