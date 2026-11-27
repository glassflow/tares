"""The hourly archive moves the lines of sessions idle for a week out of `session_lines` (TR-1506).

A session whose last line is over `ARCHIVE_AFTER_DAYS` days old, whose `lines_complete` is true and
that still has rows in `session_lines`, is written as a gzip JSON-lines part to the archive (a folder
or an S3 bucket), read back and checked, and then its rows are deleted and a row in
`session_archive_parts` says where they went, in one transaction. A failure leaves the rows. A late
line makes a later part. `lines_complete` counts the rows plus the archived lines. S3 is tested
against botocore's Stubber: no bucket, endpoint or key is used.

Plain:                python tests/test_session_archive.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_archive.py
"""
import asyncio
import contextlib
import gzip
import hashlib
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

from tares import metrics, session_archive, session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_archive import ArchiveError, FileBackend, S3Backend, backend_for  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import (metadata, session_archive_parts,  # noqa: E402
                                        session_lines, sessions)
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 1, 8, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=30)               # a month on: every session of the test is over a week old
HEAD = "0023_brain_tables"
SRC = "claude_code"
_N = [0]
os.environ.pop("TARES_ARCHIVE_URL", None)


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, etype="assistant", payload=None, sec=None, days=0):
    _N[0] += 1
    sec = _N[0] if sec is None else sec
    p = {"uuid": "u-" + uuid.uuid4().hex[:10], "n": _N[0]} if payload is None else payload
    return Envelope(source=SRC, source_type="agent_session", key_value=session, event_type=etype,
                    text="t", event_time=T0 + timedelta(days=days, seconds=sec), payload=p,
                    labels={"tares_person": "ann"})


def open_store():
    return Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))


def ready(s, *names, n=3, days=0):
    """Sessions `names` with `n` lines each, reconciled so lines_complete is true."""
    s.append([line(name, "assistant", days=days) for name in names for _ in range(n)])
    session_ingest.reconcile_sessions(s)


def rows_of(s, session):
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(select(
            session_lines.c.seq, session_lines.c.line_key, session_lines.c.at, session_lines.c.type,
            session_lines.c.labels, session_lines.c.render, session_lines.c.payload).where(
            session_lines.c.session == session).order_by(session_lines.c.seq)).all()]


def parts_of(s, session):
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(select(
            session_archive_parts.c.part, session_archive_parts.c.first_seq,
            session_archive_parts.c.last_seq, session_archive_parts.c.lines, session_archive_parts.c.bytes,
            session_archive_parts.c.md5, session_archive_parts.c.key).where(
            session_archive_parts.c.session == session).order_by(session_archive_parts.c.part)).all()]


def flags(s, session):
    with s.sessions_db.begin() as c:
        r = c.execute(select(sessions.c.line_count, sessions.c.lines_complete).where(
            sessions.c.session == session)).first()
    return None if r is None else (r[0], bool(r[1]))


def duck_count(s, session):
    with s._lock:
        return s.con.execute("SELECT count(*) FROM events WHERE key_value = ?", [session]).fetchone()[0]


def unzip(data):
    return [json.loads(x) for x in gzip.decompress(data).decode("ascii").splitlines()]


def counter(name):
    return getattr(metrics, name)._value.get() if metrics.AVAILABLE else 0


@contextlib.contextmanager
def printed():
    """What the code prints, as a list of lines."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


class Flaky(session_archive.Backend):
    """A backend over a folder that can be told to fail in each way a store can."""

    def __init__(self, root):
        self.inner = FileBackend(root)
        self.mode = None
        self.on_put = None

    def put(self, key, data):
        if self.on_put:
            self.on_put()
        if self.mode == "put":
            raise OSError("bucket unreachable")
        if self.mode == "putetag":               # the put answers another etag than the data's MD5
            self.inner.put(key, data)
            return "0" * 32
        if self.mode == "short":                 # stores half the file but says all went well
            self.inner.put(key, data[:len(data) // 2])
            return hashlib.md5(data).hexdigest()
        return self.inner.put(key, data)

    def get(self, key):
        return self.inner.get(key)

    def head(self, key):
        if self.mode == "head":
            raise OSError("head refused")
        if self.mode == "etag":                  # a store that answers another etag (KMS, multipart)
            return self.inner.head(key)[0], "0" * 32
        return self.inner.head(key)


def on_pg(s):
    """Whether this Store keeps its session state in Postgres (CI sets TARES_TEST_PG for every suite)."""
    return s.sessions_db.dialect == "postgresql"


def revision():
    print("== revision 0007_session_archive ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "sa_" + uuid.uuid4().hex[:12]
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
        ck("the table exists", "session_archive_parts" in insp.get_table_names())
        ck("its columns", sorted(c["name"] for c in insp.get_columns("session_archive_parts")) == sorted(
            ["session", "part", "first_seq", "last_seq", "lines", "bytes", "md5", "key", "written_at"]))
        ck("its key is (session, part)", insp.get_pk_constraint("session_archive_parts")[
            "constrained_columns"] == ["session", "part"])
        ck("the store is at the head", version(sdb) == [HEAD], version(sdb))
        with sdb.begin() as c:
            c.execute(text("INSERT INTO sessions (session, lines_complete) VALUES ('x', true), "
                           "('y', true)"))
            c.execute(text("INSERT INTO session_archive_parts (session, part) VALUES ('x', 1)"))
        step_down(sdb, "0006_session_lines")
        names = inspect(sdb.engine).get_table_names()
        with sdb.begin() as c:
            fx = c.execute(text("SELECT lines_complete FROM sessions WHERE session = 'x'")).scalar()
            fy = c.execute(text("SELECT lines_complete FROM sessions WHERE session = 'y'")).scalar()
        ck("a downgrade removes the table and keeps the sessions and the line table",
           "session_archive_parts" not in names and "sessions" in names and "session_lines" in names, names)
        ck("and a session that had a part is no longer trusted as whole", not fx and fy, (fx, fy))
        sdb.upgrade()
        ck("an upgrade after it builds the table again", "session_archive_parts" in inspect(
            sdb.engine).get_table_names() and version(sdb) == [HEAD])
        sdb.upgrade()
        ck("an upgrade at the head again changes nothing", version(sdb) == [HEAD])
        # the stamped case: the table was made by hand while the version is behind
        step_down(sdb, "0006_session_lines")
        with sdb.begin() as c:
            metadata.tables["session_archive_parts"].create(c)
            c.execute(text("INSERT INTO session_archive_parts (session, part, key) VALUES ('kept', 1, 'k')"))
        sdb.upgrade()
        with sdb.begin() as c:
            kept = c.execute(text("SELECT key FROM session_archive_parts WHERE session = 'kept'")).scalar()
        ck("an upgrade over a table that already exists stamps the revision and keeps the rows",
           version(sdb) == [HEAD] and kept == "k", (version(sdb), kept))
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


def file_archive():
    print("== a week-old session is archived to a folder ==")
    s = open_store()
    root = tempfile.mkdtemp()
    b = FileBackend(root)
    ready(s, "old", "fresh", n=3)
    s.append([line("fresh", "assistant", days=26, sec=5)])          # last line 4 days before NOW
    session_ingest.reconcile_sessions(s)
    before = rows_of(s, "old")
    ck("seeded: three rows, complete", len(before) == 3 and flags(s, "old") == (3, True), flags(s, "old"))
    ck("the session is due, the one with a line 4 days ago is not", session_archive.due(s.sessions_db, NOW)
       == ["old"], session_archive.due(s.sessions_db, NOW))
    with s.sessions_db.begin() as c:
        last = c.execute(select(sessions.c.last_line_at).where(sessions.c.session == "old")).scalar()
    ck("just under 7 days since its last line is not due, just over is",
       session_archive.due(s.sessions_db, last + timedelta(days=7) - timedelta(seconds=1)) == []
       and session_archive.due(s.sessions_db, last + timedelta(days=7, seconds=1)) == ["old"])
    duck = duck_count(s, "old")
    full = rows_of_full(s, "old")
    sessions_before, bytes_before = counter("ARCHIVE_SESSIONS"), counter("ARCHIVE_BYTES")
    moved = session_archive.archive_pass(s, b, NOW)
    ck("one session moved", moved == 1, moved)
    ck("its rows are gone from session_lines", rows_of(s, "old") == [], rows_of(s, "old"))
    ck("the other session keeps its rows", len(rows_of(s, "fresh")) == 4)
    parts = parts_of(s, "old")
    ck("one part row says what it holds", len(parts) == 1 and parts[0][:4] == (1, 1, 3, 3)
       and parts[0][6] == "old/1.jsonl.gz", parts)
    data = b.get(parts[0][6])
    ck("the file is the size and MD5 the row says", len(data) == parts[0][4]
       and hashlib.md5(data).hexdigest() == parts[0][5], (len(data), parts[0]))
    objs = unzip(data)
    ck("the file has one JSON object per row, in seq order, with every column",
       [o["seq"] for o in objs] == [1, 2, 3] and all(sorted(o) == sorted(
           ["session", "seq", "line_key", "at", "type", "labels", "render", "payload"]) for o in objs), objs)
    ck("and the values are the rows' own", all(
        o["session"] == "old" and o["seq"] == r[0] and o["line_key"] == r[1] and o["type"] == r[3]
        and o["labels"] == r[4] and o["render"] == r[5] and o["payload"] == r[6]
        and datetime.fromisoformat(o["at"]) == r[2] for o, r in zip(objs, before)), (objs, before))
    ck("DuckDB is untouched", duck_count(s, "old") == duck == 3)
    ck("line_count is untouched and the session is still complete", flags(s, "old") == (3, True),
       flags(s, "old"))
    session_ingest.reconcile_sessions(s)
    ck("a reconcile counts rows plus archived lines: still complete", flags(s, "old") == (3, True),
       flags(s, "old"))
    ck("a second pass finds nothing", session_archive.archive_pass(s, b, NOW) == 0
       and len(parts_of(s, "old")) == 1)
    real_time = time.time
    time.time = lambda: real_time() + 4000.0
    try:
        later = session_archive.encode(full)
    finally:
        time.time = real_time
    ck("the same rows make the same bytes, whatever the time", later == data and session_archive.encode(
        full) == data)
    if metrics.AVAILABLE:
        ck("the session and byte counters moved", counter("ARCHIVE_SESSIONS") == sessions_before + 1
           and counter("ARCHIVE_BYTES") == bytes_before + len(data), (
               counter("ARCHIVE_SESSIONS"), counter("ARCHIVE_BYTES")))
        out = metrics.render() if hasattr(metrics, "render") else b""
        out = out if isinstance(out, str) else (out[0] if isinstance(out, tuple) else out).decode()
        ck("and /metrics names all three", all(n in out for n in (
            "tares_archive_sessions_total", "tares_archive_bytes_total", "tares_archive_failures_total")))
    s.close()

    print("== a session that is not complete is not archived ==")
    s = open_store()
    real = session_ingest.record
    session_ingest.record = lambda *a, **k: None
    try:
        s.append([line("gap", "user"), line("gap", "assistant")])
    finally:
        session_ingest.record = real
    s.append([line("gap", "assistant")])
    session_ingest.reconcile_sessions(s)
    ck("a session with lines only DuckDB holds is false and has one row", flags(s, "gap") == (3, False)
       and len(rows_of(s, "gap")) == 1, (flags(s, "gap"), rows_of(s, "gap")))
    ck("so it is not due, and a pass leaves it", session_archive.due(s.sessions_db, NOW) == []
       and session_archive.archive_pass(s, FileBackend(tempfile.mkdtemp()), NOW) == 0
       and len(rows_of(s, "gap")) == 1)
    s.close()

    print("== at most `limit` sessions a pass, the oldest first ==")
    s = open_store()
    b = FileBackend(tempfile.mkdtemp())
    s.append([line("s3", days=3), line("s1", days=1), line("s2", days=2)])
    session_ingest.reconcile_sessions(s)
    ck("the oldest first", session_archive.due(s.sessions_db, NOW) == ["s1", "s2", "s3"])
    ck("a pass of two moves two", session_archive.archive_pass(s, b, NOW, limit=2) == 2
       and rows_of(s, "s1") == [] and rows_of(s, "s2") == [] and len(rows_of(s, "s3")) == 1)
    ck("the next moves the last", session_archive.archive_pass(s, b, NOW, limit=2) == 1
       and rows_of(s, "s3") == [])
    s.close()


def late_lines():
    print("== a late line makes a later part ==")
    s = open_store()
    b = FileBackend(tempfile.mkdtemp())
    ready(s, "late", n=2)
    session_archive.archive_pass(s, b, NOW)
    s.append([line("late", "user", {"uuid": "after"}, days=1)])
    ck("the new line takes the next number: seq never repeats", [r[0] for r in rows_of(s, "late")] == [3]
       and flags(s, "late")[0] == 3, (rows_of(s, "late"), flags(s, "late")))
    session_ingest.reconcile_sessions(s)
    ck("the reconcile counts the part and the new row: complete", flags(s, "late") == (3, True),
       flags(s, "late"))
    ck("the session is due again", session_archive.due(s.sessions_db, NOW) == ["late"])
    ck("a pass writes part 2", session_archive.archive_pass(s, b, NOW) == 1, parts_of(s, "late"))
    parts = parts_of(s, "late")
    ck("part 2 holds seq 3 only, in its own file", [p[:4] for p in parts] == [(1, 1, 2, 2), (2, 3, 3, 1)]
       and parts[1][6] == "late/2.jsonl.gz" and unzip(b.get(parts[1][6]))[0]["line_key"] == "after", parts)
    ck("part 1 is as it was", len(unzip(b.get(parts[0][6]))) == 2)
    session_ingest.reconcile_sessions(s)
    ck("complete across two parts", flags(s, "late") == (3, True) and rows_of(s, "late") == [])
    s.close()

    print("== a line that arrives while the part is being written is kept ==")
    s = open_store()
    b = Flaky(tempfile.mkdtemp())
    ready(s, "busy", n=2)
    t0 = time.time()
    done = []

    def during():
        # the upload is under way: a line arrives for the same session, and must not wait for it
        t = threading.Thread(target=lambda: (s.append([line("busy", "user", {"uuid": "mid"}, days=2)]),
                                             done.append(time.time() - t0)))
        t.start()
        t.join(8)
    b.on_put = during
    session_archive.archive_pass(s, b, NOW)
    ck("the late line was stored while the upload ran", len(done) == 1, done)
    ck("the part holds seq 1 and 2 only; the new row stays in session_lines",
       [p[:4] for p in parts_of(s, "busy")] == [(1, 1, 2, 2)]
       and [(r[0], r[1]) for r in rows_of(s, "busy")] == [(3, "mid")], (parts_of(s, "busy"), rows_of(s, "busy")))
    s.close()


def failures():
    print("== a failure leaves the rows ==")
    s = open_store()
    b = Flaky(tempfile.mkdtemp())
    ready(s, "bad", n=2)
    before = rows_of(s, "bad")
    fails0 = counter("ARCHIVE_FAILURES")
    for mode, why in (("put", "a put that fails"), ("head", "a head that fails"),
                      ("short", "a file stored short"), ("etag", "a head etag that is not the MD5"),
                      ("putetag", "a put etag that is not the MD5")):
        b.mode = mode
        with printed() as out:
            moved = session_archive.archive_pass(s, b, NOW)
        ck(f"{why}: nothing moved, rows and no part row stay, one log line",
           moved == 0 and rows_of(s, "bad") == before and parts_of(s, "bad") == []
           and len([x for x in out.getvalue().splitlines() if "bad" in x]) == 1, (
               moved, rows_of(s, "bad"), parts_of(s, "bad"), out.getvalue()))
    if metrics.AVAILABLE:
        ck("each failure is counted", counter("ARCHIVE_FAILURES") == fails0 + 5,
           counter("ARCHIVE_FAILURES") - fails0)
    ck("the session is still complete and still due", flags(s, "bad") == (2, True)
       and session_archive.due(s.sessions_db, NOW) == ["bad"])
    b.mode = None
    ck("the next pass, with the store back, moves it", session_archive.archive_pass(s, b, NOW) == 1
       and rows_of(s, "bad") == [] and len(parts_of(s, "bad")) == 1)
    ck("and the file is the whole one, not the short one written before", len(unzip(b.get(
        "bad/1.jsonl.gz"))) == 2)
    s.close()

    print("== one failing session does not stop the others ==")
    s = open_store()
    class Picky(Flaky):
        def put(self, key, data):
            if key.startswith("b/"):
                raise OSError("refused")
            return super().put(key, data)
    b = Picky(tempfile.mkdtemp())
    s.append([line("a", days=1), line("b", days=2), line("c", days=3)])
    session_ingest.reconcile_sessions(s)
    with printed():
        moved = session_archive.archive_pass(s, b, NOW)
    ck("a and c moved, b stayed", moved == 2 and rows_of(s, "a") == [] and rows_of(s, "c") == []
       and len(rows_of(s, "b")) == 1 and parts_of(s, "b") == [])
    s.close()

    print("== a delete that does not match the rows rolls back the part row ==")
    s = open_store()
    b = FileBackend(tempfile.mkdtemp())
    ready(s, "mis", n=2)
    real = session_archive.delete

    session_archive.delete = lambda *a, **k: real(session_lines).where(session_lines.c.seq < 0)
    try:
        with printed():
            moved = session_archive.archive_pass(s, b, NOW)
    finally:
        session_archive.delete = real
    ck("the part row is not kept when the rows were not all deleted", moved == 0
       and parts_of(s, "mis") == [] and len(rows_of(s, "mis")) == 2, (moved, parts_of(s, "mis")))
    s.close()

    print("== a session purged while its part is written leaves no part row ==")
    s = open_store()
    b = Flaky(tempfile.mkdtemp())
    ready(s, "gone", n=2)

    def purge_now():
        with s.sessions_db.begin() as c:
            c.execute(session_lines.delete().where(session_lines.c.session == "gone"))
            c.execute(sessions.delete().where(sessions.c.session == "gone"))
    b.on_put = purge_now
    fails0 = counter("ARCHIVE_FAILURES")
    with printed() as out:
        moved = session_archive.archive_pass(s, b, NOW)
    ck("nothing recorded, and it is no failure: no log line, no count", moved == 0
       and parts_of(s, "gone") == [] and out.getvalue() == "" and counter("ARCHIVE_FAILURES") == fails0,
       (moved, parts_of(s, "gone"), out.getvalue()))
    s.close()

    print("== purging a source takes its parts too ==")
    s = open_store()
    b = FileBackend(tempfile.mkdtemp())
    s.upsert_catalog_source("claude_code", "claude_code", "claude_code", "", {})
    ready(s, "p", n=2)
    session_archive.archive_pass(s, b, NOW)
    ck("a part exists", len(parts_of(s, "p")) == 1)
    s.purge_events(SRC)
    ck("the purge deleted the part rows with the session", parts_of(s, "p") == [] and flags(s, "p") is None,
       (parts_of(s, "p"), flags(s, "p")))
    s.close()


def backends():
    print("== FileBackend ==")
    parent = tempfile.mkdtemp()
    root = os.path.join(parent, "archive")
    b = FileBackend(root)
    etag = b.put("a/b/1.jsonl.gz", b"hello")
    ck("put returns the MD5 and head the size and MD5", etag == hashlib.md5(b"hello").hexdigest()
       and b.head("a/b/1.jsonl.gz") == (5, etag) and b.get("a/b/1.jsonl.gz") == b"hello")
    ck("no temporary file is left behind", [f for f in os.listdir(os.path.join(root, "a", "b"))] == [
        "1.jsonl.gz"], os.listdir(os.path.join(root, "a", "b")))
    b.put("a/b/1.jsonl.gz", b"world")
    ck("a second put replaces it whole", b.get("a/b/1.jsonl.gz") == b"world")
    try:
        b.put("../escape", b"x")
        escaped = True
    except ArchiveError:
        escaped = False
    ck("a key that leaves the folder is refused", not escaped and not os.path.exists(
        os.path.join(parent, "escape")))
    ck("a session id cannot make such a key", all(
        session_archive.part_key(x, 1).split("/")[0] not in ("..", ".", "")
        and session_archive.part_key(x, 1).count("/") == 1
        for x in ("../../etc", "..", ".", "a/b", "", "x\\y", "é ✓", "a\x00b")))
    ck("the key is <session>/<part>.jsonl.gz", session_archive.part_key("abc-123_x", 4) == "abc-123_x/4.jsonl.gz")
    ck("two different ids make two different folders", len({session_archive.part_key(x, 1) for x in (
        "a/b", "a%2Fb", "a_b", "A/B", ".a", "%2Ea")}) == 6)

    print("== settings ==")
    d = tempfile.mkdtemp()
    ck("a file:// URL names the folder", isinstance(backend_for("file:///tmp/x%20y", d, False), FileBackend)
       and backend_for("file:///tmp/x%20y", d, False).root == "/tmp/x y")
    ck("no URL on SQLite defaults to <data dir>/archive", backend_for("", d, False).root == os.path.join(
        os.path.abspath(d), "archive"))
    ck("no URL on Postgres: nothing", backend_for(None, d, True) is None and backend_for("  ", d, True) is None)
    ck("a URL on Postgres is used", isinstance(backend_for("file:///tmp/pg", d, True), FileBackend))
    for bad in ("ftp://x/y", "s3:///nobucket", "file://", "/plain/path"):
        try:
            backend_for(bad, d, False)
            raised = False
        except ArchiveError:
            raised = True
        ck(f"{bad!r} is refused", raised)
    s3 = backend_for("s3://my-bucket/some/prefix/", d, True, env={})
    ck("an s3:// URL gives the bucket and the prefix", isinstance(s3, S3Backend) and s3.bucket == "my-bucket"
       and s3.prefix == "some/prefix")
    s3 = backend_for("s3://b", d, True, env={
        "TARES_ARCHIVE_ENDPOINT": "http://127.0.0.1:9", "TARES_ARCHIVE_REGION": "eu-test-1",
        "TARES_ARCHIVE_ACCESS_KEY_ID": "test-id", "TARES_ARCHIVE_SECRET_ACCESS_KEY": "test-secret"})
    ck("the endpoint and region come from the environment, no call is made", s3.client.meta.endpoint_url
       == "http://127.0.0.1:9" and s3.client.meta.region_name == "eu-test-1", (
           s3.client.meta.endpoint_url, s3.client.meta.region_name))


def s3_stubbed():
    print("== S3 against botocore's Stubber ==")
    import boto3
    from botocore.stub import ANY, Stubber
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test-id",
                          aws_secret_access_key="test-secret")

    class Body:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

    s = open_store()
    ready(s, "cloud", n=2)
    data = session_archive.encode(rows_of_full(s, "cloud"))
    md5 = hashlib.md5(data).hexdigest()
    b = S3Backend("bkt", "pre/fix/", client=client)
    with Stubber(client) as st:
        st.add_response("put_object", {"ETag": f'"{md5}"'}, {
            "Bucket": "bkt", "Key": "pre/fix/cloud/1.jsonl.gz", "Body": data})
        st.add_response("head_object", {"ContentLength": len(data), "ETag": f'"{md5}"'}, {
            "Bucket": "bkt", "Key": "pre/fix/cloud/1.jsonl.gz"})
        moved = session_archive.archive_pass(s, b, NOW)
        st.assert_no_pending_responses()
    ck("a put and a head under the prefix, and the rows are moved", moved == 1 and rows_of(s, "cloud") == []
       and parts_of(s, "cloud")[0][6] == "cloud/1.jsonl.gz" and parts_of(s, "cloud")[0][5] == md5)

    s2 = open_store()
    ready(s2, "kms", n=2)
    data2 = session_archive.encode(rows_of_full(s2, "kms"))
    with Stubber(client) as st:
        st.add_response("put_object", {"ETag": '"abc-2"'}, {
            "Bucket": "bkt", "Key": "pre/fix/kms/1.jsonl.gz", "Body": data2})
        st.add_response("head_object", {"ContentLength": len(data2), "ETag": '"abc-2"'}, {
            "Bucket": "bkt", "Key": "pre/fix/kms/1.jsonl.gz"})
        with printed() as out:
            moved = session_archive.archive_pass(s2, b, NOW)
    ck("an ETag that is not the MD5 (multipart or KMS) is a failure: the rows stay", moved == 0
       and len(rows_of(s2, "kms")) == 2 and parts_of(s2, "kms") == [] and "did not check out" in out.getvalue(),
       out.getvalue())

    s3 = open_store()
    ready(s3, "err", n=2)
    with Stubber(client) as st:
        st.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)
        with printed() as out:
            moved = session_archive.archive_pass(s3, b, NOW)
    ck("an S3 error on put leaves the rows", moved == 0 and len(rows_of(s3, "err")) == 2
       and parts_of(s3, "err") == [] and "AccessDenied" in out.getvalue(), out.getvalue())

    s4 = open_store()
    ready(s4, "short", n=2)
    data4 = session_archive.encode(rows_of_full(s4, "short"))
    m4 = hashlib.md5(data4).hexdigest()
    with Stubber(client) as st:
        st.add_response("put_object", {"ETag": f'"{m4}"'}, {
            "Bucket": "bkt", "Key": "pre/fix/short/1.jsonl.gz", "Body": ANY})
        st.add_response("head_object", {"ContentLength": len(data4) - 1, "ETag": f'"{m4}"'}, {
            "Bucket": "bkt", "Key": "pre/fix/short/1.jsonl.gz"})
        with printed():
            moved = session_archive.archive_pass(s4, b, NOW)
    ck("a stored size that is not the size sent leaves the rows", moved == 0 and len(rows_of(
        s4, "short")) == 2 and parts_of(s4, "short") == [])

    with Stubber(client) as st:
        st.add_response("get_object", {"Body": Body(b"bytes")}, {"Bucket": "bkt", "Key": "pre/fix/k"})
        ck("get reads the object under the prefix", b.get("k") == b"bytes")
    nopre = S3Backend("bkt", "", client=client)
    with Stubber(client) as st:
        st.add_response("head_object", {"ContentLength": 3, "ETag": '"x"'}, {"Bucket": "bkt", "Key": "k"})
        ck("no prefix: the key is the bare key", nopre.head("k") == (3, "x"))
    for x in (s, s2, s3, s4):
        x.close()


def rows_of_full(s, session):
    with s.sessions_db.begin() as c:
        return c.execute(select(session_lines.c.session, session_lines.c.seq, session_lines.c.line_key,
                                session_lines.c.at, session_lines.c.type, session_lines.c.labels,
                                session_lines.c.render, session_lines.c.payload).where(
            session_lines.c.session == session).order_by(session_lines.c.seq)).all()


def defaults_and_loop():
    print("== where the archive goes when no URL is set ==")
    s = open_store()
    ready(s, "dflt", n=2)
    if on_pg(s):
        ck("on Postgres with no URL the job does nothing", session_archive.archive_pass(s, None, NOW) == 0
           and len(rows_of(s, "dflt")) == 2 and not os.path.exists(os.path.join(
               os.path.dirname(s.path), "archive")))
    else:
        ck("on SQLite the folder is <data dir>/archive", session_archive.archive_pass(s, None, NOW) == 1
           and os.path.exists(os.path.join(os.path.dirname(s.path), "archive", "dflt", "1.jsonl.gz")))
    s.close()

    print("== a store with no file has no default folder ==")
    cwd = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        m = Store(":memory:")
        ready(m, "mem", n=2)
        moved = session_archive.archive_pass(m, None, NOW)
        ck("a :memory: store archives nothing and writes no ./archive", moved == 0 and len(rows_of(
            m, "mem")) == 2 and not os.path.exists(os.path.join(cwd, "archive")), (moved, os.listdir(cwd)))
        m.close()
    finally:
        os.chdir(old_cwd)

    print("== TARES_ARCHIVE_URL is read at each pass ==")
    s = open_store()
    ready(s, "env", n=2)
    folder = tempfile.mkdtemp()
    os.environ["TARES_ARCHIVE_URL"] = "file://" + folder
    try:
        ck("a file:// URL in the environment is where the part goes", session_archive.archive_pass(
            s, None, NOW) == 1 and os.path.exists(os.path.join(folder, "env", "1.jsonl.gz")))
        os.environ["TARES_ARCHIVE_URL"] = "gopher://x"
        s.append([line("env2", days=1)])
        session_ingest.reconcile_sessions(s)
        fails0 = counter("ARCHIVE_FAILURES")
        with printed() as out:
            moved = session_archive.archive_pass(s, None, NOW)
        ck("an unusable URL is counted as a failure", counter("ARCHIVE_FAILURES") == fails0 + 1)
        ck("an unusable URL moves nothing and says so once", moved == 0 and len(rows_of(s, "env2")) == 1
           and len([x for x in out.getvalue().splitlines() if "archive off" in x]) == 1, out.getvalue())
    finally:
        os.environ.pop("TARES_ARCHIVE_URL", None)
    s.close()

    print("== the loop waits `first` once, then `every` ==")

    async def ticks():
        calls = []
        real = session_archive.archive_pass
        session_archive.archive_pass = lambda *a, **k: calls.append(time.time()) or 0
        try:
            stop = asyncio.Event()
            task = asyncio.create_task(session_archive.run(None, stop, every=0.05, first=0.6))
            await asyncio.sleep(1.1)
            stop.set()
            await asyncio.wait_for(task, 5)
        finally:
            session_archive.archive_pass = real
        return calls
    calls = asyncio.run(ticks())
    ck("the first pass waits `first`, the next ones `every`", len(calls) >= 4 and calls[1] - calls[0] < 0.4,
       calls)

    print("== the loop ==")
    s = open_store()
    ready(s, "loop", n=2)
    folder = tempfile.mkdtemp()
    os.environ["TARES_ARCHIVE_URL"] = "file://" + folder
    real_now = session_archive.now_utc
    session_archive.now_utc = lambda: NOW
    try:
        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(session_archive.run(s, stop, every=0.05, first=0.05))
            for _ in range(100):
                if not rows_of(s, "loop"):
                    break
                await asyncio.sleep(0.05)
            moved = rows_of(s, "loop") == []
            s.append([line("loop2", days=1)])
            session_ingest.reconcile_sessions(s)
            for _ in range(100):
                if not rows_of(s, "loop2"):
                    break
                await asyncio.sleep(0.05)
            again = rows_of(s, "loop2") == []
            stop.set()
            await asyncio.wait_for(task, 5)
            return moved, again
        moved, again = asyncio.run(go())
        ck("the loop runs a pass, and runs again", moved and again, (moved, again))
    finally:
        session_archive.now_utc = real_now
        os.environ.pop("TARES_ARCHIVE_URL", None)
    s.close()


def pg_lock():
    s = open_store()
    if not on_pg(s):
        s.close()
        return
    print("== on Postgres a session held by another transaction is skipped ==")
    b = FileBackend(tempfile.mkdtemp())
    ready(s, "held", "free", n=2)
    with s.sessions_db.begin() as other:
        got = other.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('tares_session_archive:' || "
                                 "'held'))")).scalar()
        ck("the test holds the lock", got is True)
        moved = session_archive.archive_pass(s, b, NOW)
    ck("the held session is skipped, the free one moved", moved == 1 and len(rows_of(s, "held")) == 2
       and rows_of(s, "free") == [] and parts_of(s, "held") == [])
    ck("released, it is archived at the next pass", session_archive.archive_pass(s, b, NOW) == 1
       and rows_of(s, "held") == [])
    s.close()


if __name__ == "__main__":
    revision()
    file_archive()
    late_lines()
    failures()
    backends()
    s3_stubbed()
    defaults_and_loop()
    pg_lock()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED:\n  " + "\n  ".join(FAILS))
        sys.exit(1)
    print("\nall ok")
