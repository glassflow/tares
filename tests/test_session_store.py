"""The session store (TR-1493): SQLite beside the DuckDB file, or the state Postgres, with Alembic.

Plain:                python tests/test_session_store.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness \\
        python tests/test_session_store.py
Without TARES_TEST_PG the Postgres cases are skipped; with it, they run on a schema of their own.
"""
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import Column, Integer, MetaData, Table, text  # noqa: E402

import tares.store as store_mod  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.engine import make_engine  # noqa: E402
from tares.session_store.types import UTCDateTime  # noqa: E402
from tares.store import Store, StoreUnavailable  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class capture_logs:
    """The records the named loggers emit inside the block."""
    def __init__(self, *names):
        self.names, self.h = names, Capture()

    def __enter__(self):
        self.saved = []
        for n in self.names:
            lg = logging.getLogger(n)
            self.saved.append((lg, lg.level))
            lg.setLevel(logging.INFO)
            lg.addHandler(self.h)
        return self.h.records

    def __exit__(self, *a):
        for lg, level in self.saved:
            lg.removeHandler(self.h)
            lg.setLevel(level)


def versions(sessions_db):
    with sessions_db.begin() as c:
        return [r[0] for r in c.execute(text("SELECT version_num FROM session_store_version")).all()]


def upgrade_lines(records):
    return [r for r in records if r.name == "alembic.runtime.migration" and "Running upgrade" in r.getMessage()]


def fresh_start():
    print("== a fresh start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    with capture_logs("alembic.runtime.migration") as recs:
        s = Store(path)
    ck("a first start runs the base revision", len(upgrade_lines(recs)) == 1, [r.getMessage() for r in recs])
    ck("the revision table holds 0001_base", versions(s.sessions_db) == ["0001_base"], versions(s.sessions_db))
    if s.state_dsn:
        ck("with a state DSN the store is Postgres", s.sessions_db.dialect == "postgresql")
        ck("and no sessions.sqlite is made", not os.path.exists(os.path.join(d, "sessions.sqlite")))
    else:
        ck("sessions.sqlite is made next to the DuckDB file", os.path.exists(os.path.join(d, "sessions.sqlite")))
        ck("the dialect is sqlite", s.sessions_db.dialect == "sqlite")
        with s.sessions_db.begin() as c:
            mode = c.execute(text("PRAGMA journal_mode")).scalar()
            busy = c.execute(text("PRAGMA busy_timeout")).scalar()
        ck("the journal mode is wal", mode == "wal", mode)
        ck("the busy timeout is 5s", busy == 5000, busy)
    s.close()
    return d, path


def second_start(path):
    print("== a second start ==")
    with capture_logs("alembic.runtime.migration", "tares.session_store") as recs:
        s = Store(path)
    ck("no revision runs again", upgrade_lines(recs) == [], [r.getMessage() for r in recs])
    ck("the revision table still holds the one row", versions(s.sessions_db) == ["0001_base"])
    s.close()


def unknown_revision(path):
    print("== a revision this code does not know ==")
    s = Store(path)
    with s.sessions_db.begin() as c:
        c.execute(text("UPDATE session_store_version SET version_num = '9999_future'"))
    s.close()
    with capture_logs("alembic.runtime.migration", "tares.session_store") as recs:
        s = Store(path)          # raises StoreUnavailable if the guard is gone
    mine = [r for r in recs if r.name == "tares.session_store"]
    ck("the start logs one line about it", len(mine) == 1, [r.getMessage() for r in mine])
    ck("the line names the revision", bool(mine) and "9999_future" in mine[0].getMessage())
    ck("the store started", s.sessions_db is not None)
    ck("nothing ran", upgrade_lines(recs) == [])
    ck("the row is left as it was", versions(s.sessions_db) == ["9999_future"], versions(s.sessions_db))
    s.close()


def utc_datetime():
    print("== UTCDateTime ==")
    d = tempfile.mkdtemp()
    engine = make_engine(d, None)
    md = MetaData()
    t = Table("tmp_times", md, Column("id", Integer, primary_key=True), Column("at", UTCDateTime))
    md.create_all(engine)
    ist = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2026, 11, 19, 12, 30, 15, 123456, tzinfo=ist)
    naive = datetime(2026, 11, 19, 7, 0, 15)
    with engine.begin() as c:
        c.execute(t.insert(), [{"id": 1, "at": aware}, {"id": 2, "at": naive}, {"id": 3, "at": None}])
    with engine.begin() as c:
        rows = {r[0]: r[1] for r in c.execute(t.select()).all()}
        raw = c.exec_driver_sql("SELECT at FROM tmp_times WHERE id = 1").scalar()
    got = rows[1]
    ck("an aware +05:30 value comes back as the same instant", got == aware, got)
    ck("it comes back aware, in UTC", got.tzinfo is timezone.utc, repr(got.tzinfo))
    ck("SQLite holds the UTC wall time", str(raw).startswith("2026-11-19 07:00:15.123456"), raw)
    ck("a naive value is taken as UTC", rows[2] == naive.replace(tzinfo=timezone.utc)
       and rows[2].tzinfo is timezone.utc, rows[2])
    ck("None stays None", rows[3] is None)
    try:
        with engine.begin() as c:
            c.execute(t.insert(), {"id": 4, "at": "2026-11-19"})
        ck("a string is rejected", False, "stored")
    except Exception as e:
        ck("a string is rejected", "UTCDateTime takes a datetime" in str(e), str(e))
    engine.dispose()


def two_writers():
    print("== two writers on SQLite ==")
    d = tempfile.mkdtemp()
    sdb = SessionStore(d)
    sdb.upgrade()
    with sdb.begin() as c:
        c.execute(text("CREATE TABLE tmp_counter (v INTEGER PRIMARY KEY)"))
    errors, per = [], 15

    def writer():
        try:
            for _ in range(per):
                with sdb.begin() as c:
                    n = c.execute(text("SELECT COALESCE(MAX(v), 0) FROM tmp_counter")).scalar()
                    time.sleep(0.002)        # hold the transaction so the other writer arrives inside it
                    c.execute(text("INSERT INTO tmp_counter (v) VALUES (:v)"), {"v": n + 1})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with sdb.begin() as c:
        count = c.execute(text("SELECT COUNT(*) FROM tmp_counter")).scalar()
    ck("both writers finish with no error", not errors, errors[:1])
    ck("every read-then-write step was atomic", count == 2 * per, count)
    sdb.close()


def dsn_rewrite():
    print("== the DSN ==")
    q = "?options=-csearch_path%3Dx"
    for scheme in ("postgres", "postgresql"):
        e = make_engine("/unused", f"{scheme}://u:p@h:5432/db{q}")
        url = e.url
        ck(f"{scheme}:// becomes psycopg", url.drivername == "postgresql+psycopg", url.drivername)
        ck(f"{scheme}:// keeps host, port, database and password",
           (url.host, url.port, url.database, url.password) == ("h", 5432, "db", "p"), str(url))
        ck(f"{scheme}:// keeps the query string", url.query.get("options") == "-csearch_path=x", dict(url.query))
        ck(f"{scheme}:// pool is 2 plus 2 and pre-pinged",
           e.pool.size() == 2 and e.pool._max_overflow == 2 and e.pool._pre_ping, "")
        e.dispose()
    e = make_engine("/unused", "postgresql+psycopg://u:p@h/db")
    ck("a psycopg DSN is left alone", e.url.drivername == "postgresql+psycopg")
    e.dispose()
    e = make_engine(":memory:", None)
    ck("a Store on :memory: gets an in-memory SQLite", str(e.url) == "sqlite://", str(e.url))
    e.dispose()
    sdb = SessionStore(":memory:")
    sdb.upgrade()
    ck("and it migrates", versions(sdb) == ["0001_base"])
    sdb.close()


def upgrade_failure():
    print("== a failed upgrade stops the start ==")
    d = tempfile.mkdtemp()
    orig = store_mod.SessionStore.upgrade

    def boom(self):
        raise RuntimeError("session store down")

    store_mod.SessionStore.upgrade = boom
    try:
        Store(os.path.join(d, "t.duckdb"), state_dsn="" if PG else None)
        ck("Store raises", False, "no error")
    except StoreUnavailable as e:
        ck("Store raises StoreUnavailable", "session store down" in str(e), str(e))
    except Exception as e:
        ck("Store raises StoreUnavailable", False, f"{type(e).__name__}: {e}")
    finally:
        store_mod.SessionStore.upgrade = orig


def postgres():
    print("== Postgres, a schema of its own ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    schema = "ss_" + uuid.uuid4().hex[:12]
    dsn = f"{PG}?options=-csearch_path%3D{schema}"
    d = tempfile.mkdtemp()
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")

    def tables(sch):
        with psycopg.connect(PG, autocommit=True) as c:
            return [r[0] for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", [sch]).fetchall()]

    try:
        sdb = SessionStore(d, dsn)
        ck("the dialect is postgresql", sdb.dialect == "postgresql")
        with capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a first upgrade runs the base revision", len(upgrade_lines(recs)) == 1)
        ck("session_store_version is in the schema from the DSN", "session_store_version" in tables(schema),
           tables(schema))
        ck("and not in public", "session_store_version" not in tables("public"))
        ck("it holds 0001_base", versions(sdb) == ["0001_base"], versions(sdb))
        with capture_logs("alembic.runtime.migration", "tares.session_store") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", upgrade_lines(recs) == [])
        ck("no sessions.sqlite is made", not os.path.exists(os.path.join(d, "sessions.sqlite")),
           os.listdir(d))
        with sdb.begin() as c:
            c.execute(text("UPDATE session_store_version SET version_num = '9999_future'"))
        with capture_logs("tares.session_store") as recs:
            sdb.upgrade()
        ck("an unknown revision logs one line and goes on", len(recs) == 1 and versions(sdb) == ["9999_future"],
           [r.getMessage() for r in recs])
        with sdb.begin() as c:
            c.execute(text("UPDATE session_store_version SET version_num = '0001_base'"))
        md = MetaData()
        t = Table("tmp_times", md, Column("id", Integer, primary_key=True), Column("at", UTCDateTime))
        ist = timezone(timedelta(hours=5, minutes=30))
        aware = datetime(2026, 11, 19, 12, 30, 15, tzinfo=ist)
        with sdb.begin() as c:
            md.create_all(c)
            c.execute(t.insert(), {"id": 1, "at": aware})
        with sdb.begin() as c:
            got = c.execute(t.select()).one()[1]
        ck("an aware +05:30 value round-trips as the same instant in UTC",
           got == aware and got.tzinfo is timezone.utc, repr(got))
        errors = []

        def writer():
            try:
                for i in range(10):
                    with sdb.begin() as c:
                        c.execute(text("SELECT 1"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        ck("four threads on a pool of 2+2 finish", not errors, errors[:1])
        sdb.close()
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


if __name__ == "__main__":
    d, path = fresh_start()
    second_start(path)
    unknown_revision(path)
    utc_datetime()
    two_writers()
    dsn_rewrite()
    upgrade_failure()
    postgres()
    print("FAIL " + ", ".join(FAILS) if FAILS else "OK")
    sys.exit(1 if FAILS else 0)
