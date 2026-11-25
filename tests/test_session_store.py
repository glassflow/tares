"""The session store (TR-1493, TR-1497): SQLite beside the DuckDB file, or the state Postgres, with Alembic.

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
from alembic import command  # noqa: E402
from sqlalchemy import Column, Integer, MetaData, Table, inspect, text  # noqa: E402

import tares.store as store_mod  # noqa: E402
from tares import state_db  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store import tables as declared_tables  # noqa: E402
from tares.session_store.copy import CopyMismatch, copy_from_duckdb  # noqa: E402
from tares.session_store.engine import make_engine  # noqa: E402
from tares.session_store.types import UTCDateTime  # noqa: E402
from old_ddl import OLD_DDL  # noqa: E402
from tares.store import Store, StoreUnavailable  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
HEAD = "0012_session_usage_hourly"              # the newest revision: tests that count revisions move with it
REVISIONS = 12
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
    ck("a first start runs every revision", len(upgrade_lines(recs)) == REVISIONS, [r.getMessage() for r in recs])
    ck(f"the revision table holds {HEAD}", versions(s.sessions_db) == [HEAD], versions(s.sessions_db))
    if s.state_dsn:
        ck("with a state DSN the store is Postgres", s.sessions_db.dialect == "postgresql")
        ck("and no tares.sqlite is made", not os.path.exists(os.path.join(d, "tares.sqlite")))
    else:
        ck("tares.sqlite is made next to the DuckDB file", os.path.exists(os.path.join(d, "tares.sqlite")))
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
    ck("the revision table still holds the one row", versions(s.sessions_db) == [HEAD])
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
    e = make_engine("/unused", "host=h port=5432 dbname=db user=u")
    ck("a libpq keyword DSN gets a psycopg engine", e.url.drivername == "postgresql+psycopg"
       and e.pool.size() == 2 and e.pool._pre_ping, str(e.url))
    e.dispose()
    e = make_engine(":memory:", None)
    ck("a Store on :memory: gets an in-memory SQLite", str(e.url) == "sqlite://", str(e.url))
    e.dispose()
    sdb = SessionStore(":memory:")
    sdb.upgrade()
    ck("and it migrates", versions(sdb) == [HEAD])
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


SESSION_TABLES = ["project_sessions", "session_meters", "session_states", "session_stations", "session_usage"]
ACCOUNT_TABLES = ["people", "api_keys", "project_members"]      # TR-1608, with settings: taken over
ALL_TABLES = SESSION_TABLES + ["settings"] + ACCOUNT_TABLES + ["session_store_copies", "session_ingest_marks", "sessions",
                               "session_messages", "session_commands", "session_turns", "session_said",
                               "session_files", "session_lines", "session_archive_parts", "session_moves",
                               "session_usage_hourly", "session_rollup_state"]    # TR-1632


def shape(engine, names=ALL_TABLES):
    """{table: (columns as (name, type, nullable), primary key)} as the database reports it."""
    insp = inspect(engine)
    out = {}
    for n in names:
        cols = sorted((c["name"], c["type"].compile(dialect=engine.dialect), bool(c["nullable"]))
                      for c in insp.get_columns(n))
        out[n] = (cols, sorted(insp.get_pk_constraint(n)["constrained_columns"]))
    return out


def declared(engine):
    """The same shape, from the declarations in tables.py."""
    out = {}
    for t in declared_tables.metadata.sorted_tables:
        cols = sorted((c.name, c.type.compile(dialect=engine.dialect), bool(c.nullable)) for c in t.columns)
        out[t.name] = (cols, sorted(c.name for c in t.primary_key.columns))
    return out


def index_columns(engine, table, name):
    for i in inspect(engine).get_indexes(table):
        if i["name"] == name:
            return i["column_names"]
    return None


def step_down(sessions_db, revision):
    with sessions_db.begin() as conn:
        cfg = sessions_db._config()
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def session_tables():
    print("== the session tables on SQLite ==")
    d = tempfile.mkdtemp()
    sdb = SessionStore(d)
    sdb.upgrade()
    ck("the five tables and the copy ledger exist", sorted(inspect(sdb.engine).get_table_names()
       ) == sorted(ALL_TABLES + ["session_store_version"]), inspect(sdb.engine).get_table_names())
    ck("each table has the columns, types and primary key tables.py declares",
       shape(sdb.engine) == declared(sdb.engine), (shape(sdb.engine), declared(sdb.engine)))
    ck("project_sessions has an index on session",
       index_columns(sdb.engine, "project_sessions", "project_sessions_session") == ["session"])
    ck("the copy ledger is empty", _count(sdb, "session_store_copies") == 0)
    step_down(sdb, "0001_base")
    ck("a downgrade puts the version back to the base", versions(sdb) == ["0001_base"], versions(sdb))
    ck("and removes the tables the store made, but not settings and the accounts (a downgrade never "
       "drops a taken-over table)",
       sorted(inspect(sdb.engine).get_table_names())
       == sorted(["session_store_version", "settings"] + ACCOUNT_TABLES),
       inspect(sdb.engine).get_table_names())
    sdb.upgrade()
    ck("an upgrade after a downgrade builds them again", shape(sdb.engine) == declared(sdb.engine)
       and versions(sdb) == [HEAD])
    sdb.close()


def _count(sdb, table):
    with sdb.begin() as c:
        return c.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def _duck_rows(store, table):
    cols = [c.name for c in declared_tables.metadata.tables[table].columns]
    with store._lock:
        return cols, store.con.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1, 2").fetchall()


def _norm(v):
    return v.astimezone(timezone.utc) if isinstance(v, datetime) and v.tzinfo else v


def _seed_duckdb(s, full=True):
    """Rows in DuckDB's five session tables, as a version before the session store left them.
    Written to DuckDB directly: nothing else writes there now. `full` False leaves session_meters
    empty."""
    t1 = datetime(2026, 11, 19, 9, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    t2 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)
    rows = {
        "session_stations": [("s1", "crew-builder", "builder", "crew-orch", "p1", t1, "crew1"),
                             ("s2", "crew-reviewer", "", "", None, t2, None)],     # empty and null values
        "session_states": [("s1", "working", "building", t1), ("s2", "idle", None, t2)],
        "project_sessions": [("p1", "s1", "glassflow/tares", t1), ("p1", "s2", None, t2)],
        "session_usage": [("s1", "m1", t1, "claude-sonnet-5-5", "p1", None, 120, 40, 30, 900, 0.000805, 0),
                          ("s1", "m2", t2, None, None, "sub1", 0, 0, 0, 0, None, 0)],   # no model, zero tokens
        "session_meters": ([("s1", '{"plan": "max", "n": 3}', t1, '{"used": 12}', t2, None, "person1"),
                            ("s2", None, None, None, None, t2, None)] if full else []),  # a row of mostly nulls
    }
    with s._lock:
        for stmt in _old_ddl(SESSION_TABLES):      # a Store no longer makes them in DuckDB
            s.con.execute(stmt)
        for table, data in rows.items():
            for r in data:
                s.con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(r))})", list(r))


def _forget_copy(s):
    """Empty the session store and its ledger: the state of a cell that has not copied yet."""
    with s.sessions_db.begin() as c:
        for t in SESSION_TABLES + ["session_store_copies"]:
            c.execute(text(f"DELETE FROM {t}"))


def _seeded_store(full=True):
    """A DuckDB-only Store (its session store is SQLite) whose DuckDB holds rows in the session
    tables and whose session store holds none, so the copy has something to do."""
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if PG else None)
    _seed_duckdb(s, full)
    _forget_copy(s)
    return s


def copy_at_start():
    print("== the copy at start, on SQLite ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    s = Store(path, state_dsn="" if PG else None)
    ck("a new cell copies nothing, and the ledger names every moved table",
       _count(s.sessions_db, "session_store_copies") == len(declared_tables.MOVED_TABLES))
    _seed_duckdb(s)
    before = {t: _duck_rows(s, t)[1] for t in SESSION_TABLES}
    s.con.execute("CHECKPOINT")
    s.con.close()
    s.close()
    for f in os.listdir(d):                  # a cell from before the session store: no tares.sqlite
        if f.startswith("tares.sqlite"):
            os.remove(os.path.join(d, f))
    seen = {}
    orig = Store._upgrade

    def spy(self):
        seen.update({t: _count(self.sessions_db, t) for t in SESSION_TABLES})
        return orig(self)
    Store._upgrade = spy
    try:
        s = Store(path, state_dsn="" if PG else None)
    finally:
        Store._upgrade = orig
    ck("the data upgrades start after the copy: every table already holds its rows",
       seen == {t: len(before[t]) for t in SESSION_TABLES}, seen)
    for t in SESSION_TABLES:
        got = [tuple(_norm(v) for v in r) for r in _session_rows(s, t)]
        want = [tuple(_norm(v) for v in r) for r in before[t]]
        if t == "session_stations":
            # the data upgrade gives a session with no crew the cell's crew, in the copied table
            ck("the upgrade's fill landed in the copy: a station with no crew has one, DuckDB's row does not",
               got[1][-1] and want[1][-1] is None, (got, want))
            got, want = [r[:-1] for r in got], [r[:-1] for r in want]
        ck(f"{t} arrived, row for row", got == want, (got, want))
    ck("DuckDB's copies are untouched", {t: _duck_rows(s, t)[1] for t in SESSION_TABLES} == before)
    s.close()
    s2 = Store(path, state_dsn="" if PG else None)
    ck("a second start copies nothing more", {t: _count(s2.sessions_db, t) for t in SESSION_TABLES}
       == {t: len(before[t]) for t in SESSION_TABLES})
    s2.set_session_state("s1", "ended", None, datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc))
    ck("a write after the copy stays in the session store only",
       _duck_rows(s2, "session_states")[1] == before["session_states"]
       and s2.session_state("s1")["state"] == "ended")
    s2.close()


def _session_rows(s, t):
    tbl = declared_tables.metadata.tables[t]
    with s.sessions_db.begin() as c:
        return c.execute(tbl.select().order_by(*list(tbl.columns)[:2])).all()


def copy_sqlite():
    print("== copy_from_duckdb on SQLite ==")
    s = _seeded_store()
    before = {t: _duck_rows(s, t)[1] for t in SESSION_TABLES}
    ck("every session table has rows to copy", all(before[t] for t in SESSION_TABLES),
       {t: len(r) for t, r in before.items()})
    ck("nothing is in the session store yet", all(_count(s.sessions_db, t) == 0 for t in SESSION_TABLES))
    got = copy_from_duckdb(s, s.sessions_db)
    ck("the first call reports the rows it copied (settings and the accounts are not in DuckDB here: zero)",
       got == {**{t: len(before[t]) for t in SESSION_TABLES}, "settings": 0,
               **{t: 0 for t in ACCOUNT_TABLES}}, got)
    for t in SESSION_TABLES:
        tbl = declared_tables.metadata.tables[t]
        with s.sessions_db.begin() as c:       # through the declared table, so times come back as UTC
            rows = c.execute(tbl.select().order_by(*list(tbl.columns)[:2])).all()
        ck(f"{t}: every row arrived, values and times the same",
           [tuple(_norm(v) for v in r) for r in rows] == [tuple(_norm(v) for v in r) for r in before[t]],
           (rows, before[t]))
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger holds each table and its count", ledger == {**{t: len(before[t]) for t in SESSION_TABLES},
                                                              "settings": 0, **{t: 0 for t in ACCOUNT_TABLES}},
       ledger)
    ck("DuckDB is unchanged", {t: _duck_rows(s, t)[1] for t in SESSION_TABLES} == before)
    again = copy_from_duckdb(s, s.sessions_db)
    ck("a second call copies nothing", again == {}, again)
    ck("and adds no row", {t: _count(s.sessions_db, t) for t in SESSION_TABLES}
       == {t: len(before[t]) for t in SESSION_TABLES})
    with s._lock:
        s.con.execute("INSERT INTO session_usage (session, message, replied_at, model) VALUES "
                      "('s1', 'm3', now(), 'x')")
    ck("a row written to DuckDB after the copy stays out", copy_from_duckdb(s, s.sessions_db) == {}
       and _count(s.sessions_db, "session_usage") == len(before["session_usage"]))
    s.close()

    # an empty table is copied too: zero rows, and the ledger says so
    e = _seeded_store(full=False)
    got = copy_from_duckdb(e, e.sessions_db)
    with e.sessions_db.begin() as c:
        zero = c.execute(text("SELECT rows FROM session_store_copies WHERE table_name = 'session_meters'")).all()
    ck("an empty table copies zero rows and is recorded", got.get("session_meters") == 0 and zero == [(0,)],
       (got, zero))
    e.close()

    # a mismatch raises with the table and both numbers, and leaves that table out of the ledger
    m = _seeded_store()
    with m.sessions_db.begin() as c:
        c.execute(text("CREATE TRIGGER drop_states BEFORE INSERT ON session_states "
                       "BEGIN SELECT RAISE(IGNORE); END"))
    try:
        copy_from_duckdb(m, m.sessions_db)
        ck("a table that loses rows raises", False, "no error")
    except CopyMismatch as err:
        msg = str(err)
        ck("a table that loses rows raises with its name and both counts",
           "session_states" in msg and "read 2" in msg and "inserted 0" in msg, msg)
    with m.sessions_db.begin() as c:
        ledger = [r[0] for r in c.execute(text("SELECT table_name FROM session_store_copies")).all()]
    ck("the table before it is in the ledger and the failing one is not",
       ledger == ["session_stations"] and _count(m.sessions_db, "session_states") == 0, ledger)
    ck("the tables after it were not touched", _count(m.sessions_db, "session_usage") == 0)
    with m.sessions_db.begin() as c:
        c.execute(text("DROP TRIGGER drop_states"))
    got = copy_from_duckdb(m, m.sessions_db)
    ck("after the cause is gone, the next call copies the rest and not the first",
       sorted(got) == sorted(["project_sessions", "session_meters", "session_states", "session_usage", "settings"]
                             + ACCOUNT_TABLES), got)
    m.close()


def copy_on_postgres():
    print("== copy_from_duckdb with the state in Postgres ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    s = Store(path, state_dsn="")                          # an older cell: its state, and these tables, in DuckDB
    _seed_duckdb(s)
    before = {t: _duck_rows(s, t)[1] for t in SESSION_TABLES}
    s.con.execute("CHECKPOINT")
    s.con.close()
    s.close()
    _drop_local_file(d)        # an older cell has no tares.sqlite: its rows are only in DuckDB
    sch = "ss_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {sch}")
    try:
        s = Store(path, state_dsn=f"{PG}?options=-csearch_path%3D{sch}")   # its first start with a state database
        if s.sessions_db.dialect != "postgresql":
            print("  skip  the Store did not take the state database")
            s.close()
            return
        ck("the session store is Postgres", True)
        ck("the ledger names the five tables, with their row counts",
           sorted(_ledger5(s)) == sorted((t, len(before[t])) for t in SESSION_TABLES), _ledger(s))
        for t in SESSION_TABLES:
            got = [tuple(_norm(v) for v in r) for r in _session_rows(s, t)]
            want = [tuple(_norm(v) for v in r) for r in before[t]]
            if t == "session_stations":                    # the data upgrade fills the crew after the copy
                got, want = [r[:-1] for r in got], [r[:-1] for r in want]
            ck(f"{t} arrived in Postgres, row for row", got == want, (got, want))
        ck("DuckDB's copies are untouched", {t: _duck_rows(s, t)[1] for t in SESSION_TABLES} == before)
        s.link_session("p1", "s3")
        ck("nothing is copied afterwards: the rows are already there", copy_from_duckdb(s, s.sessions_db) == {})
        ck("and the ledger is as it was",
           sorted(_ledger5(s)) == sorted((t, len(before[t])) for t in SESSION_TABLES))
        s.close()
        s = Store(path, state_dsn=f"{PG}?options=-csearch_path%3D{sch}")
        ck("a second start copies nothing more", _count(s.sessions_db, "project_sessions") == 3)
        s.close()
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")


def _drop_local_file(d):
    for f in os.listdir(d):
        if f.startswith("tares.sqlite"):
            os.remove(os.path.join(d, f))


def local_then_postgres():
    print("== a cell that ran on SQLite moves to Postgres (the rows since the copy come along) ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    s = Store(path, state_dsn="")
    _seed_duckdb(s)                                        # what a version before the session store left in DuckDB
    _forget_copy(s)
    s.close()
    s = Store(path, state_dsn="")                          # the local start: DuckDB's rows are copied to tares.sqlite
    ck("the local start copied DuckDB's rows", _count(s.sessions_db, "project_sessions") == 2)
    s.link_session("p1", "s-local")                        # written after the copy, to tares.sqlite only
    s.set_session_state("s-local", "working", "after the copy", datetime(2026, 11, 20, tzinfo=timezone.utc))
    with s.sessions_db.begin() as c:                       # and a row DuckDB still has that tares.sqlite lost
        c.execute(text("DELETE FROM project_sessions WHERE session = 's2'"))
    local = {t: [tuple(_norm(v) for v in r) for r in _session_rows(s, t)] for t in SESSION_TABLES}
    duck = {t: _duck_rows(s, t)[1] for t in SESSION_TABLES}
    s.con.execute("CHECKPOINT")
    s.con.close()
    s.close()
    sch = "ss_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {sch}")
    try:
        s = Store(path, state_dsn=f"{PG}?options=-csearch_path%3D{sch}")   # the first start with a state database
        if s.sessions_db.dialect != "postgresql":
            print("  skip  the Store did not take the state database")
            s.close()
            return
        ck("a link written after the local copy is in Postgres",
           sorted(s.sessions_of_projects(["p1"])) == ["s-local", "s1"], s.sessions_of_projects(["p1"]))
        ck("so is the state written after it", (s.session_state("s-local") or {}).get("state") == "working")
        ck("a row only DuckDB still holds is not brought back (the move leaves the five tables alone)",
           _count(s.sessions_db, "project_sessions") == 2)
        for t in SESSION_TABLES:
            got = sorted((tuple(_norm(v) for v in r) for r in _session_rows(s, t)), key=repr)
            want = sorted(local[t], key=repr)             # Postgres and SQLite order '-' differently
            if t == "session_stations":                    # the data upgrade fills the crew after the copy
                got, want = [r[:-1] for r in got], [r[:-1] for r in want]
            ck(f"{t} is what tares.sqlite held", got == want, (got, want))
        ck("the ledger names the five tables, with the local counts",
           sorted(_ledger5(s)) == sorted((t, len(local[t])) for t in SESSION_TABLES), _ledger(s))
        ck("DuckDB's copies are untouched", {t: _duck_rows(s, t)[1] for t in SESSION_TABLES} == duck)
        s.close()
        ck("tares.sqlite was left as it was", os.path.exists(os.path.join(d, "tares.sqlite")))
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")


def greatest_null():
    print("== SessionStore.greatest ignores a NULL ==")
    from sqlalchemy import literal, select
    sdb = SessionStore(":memory:")
    cases = [((3, 5), 5), ((5, 3), 5), ((None, 5), 5), ((5, None), 5), ((None, None), None)]
    for (a, b), want in cases:
        with sdb.begin() as c:
            got = c.execute(select(sdb.greatest(literal(a, Integer), literal(b, Integer)))).scalar()
        ck(f"greatest({a}, {b}) is {want} on SQLite", got == want, got)
    sdb.close()
    if PG:
        pg = SessionStore(":memory:", PG)
        for (a, b), want in cases:
            with pg.begin() as c:
                got = c.execute(select(pg.greatest(literal(a, Integer), literal(b, Integer)))).scalar()
            ck(f"greatest({a}, {b}) is {want} on Postgres", got == want, got)
        pg.close()


def _ledger(s):
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()]


def _ledger5(s):
    """The ledger's rows for the five session tables (settings has its own tests)."""
    return [r for r in _ledger(s) if r[0] in SESSION_TABLES]


def _old_ddl(names):
    """The statements the cell ran before the tables moved (tests/old_ddl.py), for the named
    tables only."""
    import re
    pat = re.compile(r"\b(" + "|".join(names) + r")\b")
    return [x for x in OLD_DDL if pat.search(x)]


def takeover():
    print("== Postgres: tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    made = []

    def schema_for(names):
        sch = "ss_" + uuid.uuid4().hex[:12]
        made.append(sch)
        dsn = f"{PG}?options=-csearch_path%3D{sch}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {sch}")
        pg = state_db.PgState(dsn)
        for stmt in _old_ddl(names):
            pg.execute(stmt)
        return sch, dsn

    def snapshot(sch):
        """Every row of the five tables, and every column's type, as Postgres holds them."""
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"SET search_path = {sch}")
            out = {}
            for t in SESSION_TABLES:
                if c.execute("SELECT to_regclass(%s)", [t]).fetchone()[0] is None:
                    continue
                cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                 "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
                                 [sch, t]).fetchall()
                rows = c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                out[t] = (cols, [tuple(map(str, r)) for r in rows])
            return out

    try:
        sch, dsn = schema_for(SESSION_TABLES)
        old = snapshot(sch)
        ck("the old DDL made all five tables", sorted(old) == SESSION_TABLES, sorted(old))
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"SET search_path = {sch}")
            c.execute("INSERT INTO session_stations (session, station, role, seen_at) VALUES "
                      "('s1', 'crew-b', 'builder', '2026-11-19 04:00:00+00'), ('s2', 'crew-r', NULL, NULL)")
            c.execute("INSERT INTO session_states (session, state, state_at) VALUES ('s1', 'idle', now())")
            c.execute("INSERT INTO project_sessions (project, session, linked_at) VALUES ('p1', 's1', now())")
            c.execute("INSERT INTO session_usage (session, message, replied_at, input_tokens, cost_usd) VALUES "
                      "('s1', 'm1', now(), 5, 0.25), ('s1', 'm2', NULL, NULL, NULL)")
            c.execute("INSERT INTO session_meters (session, status, status_at) VALUES "
                      "('s1', '{\"a\": 1}', now()), ('s2', NULL, NULL)")
        before = snapshot(sch)
        ck("seeded rows are there", all(before[t][1] for t in SESSION_TABLES), {t: len(v[1]) for t, v in before.items()})
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        ck("no revision table yet: the database is one the old code made", "session_store_version"
           not in inspect(sdb.engine).get_table_names())
        sdb.upgrade()
        ck(f"the upgrade stamps {HEAD}", versions(sdb) == [HEAD], versions(sdb))
        after = snapshot(sch)
        ck("no row and no column of the five tables changed", after == before,
           [t for t in SESSION_TABLES if after[t] != before[t]])
        ck("each table's columns, types and primary key are what tables.py declares",
           shape(sdb.engine) == declared(sdb.engine), (shape(sdb.engine), declared(sdb.engine)))
        ck("project_sessions has its index on session",
           index_columns(sdb.engine, "project_sessions", "project_sessions_session") == ["session"])
        ck("the copy ledger is made", _count(sdb, "session_store_copies") == 0)
        with capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", upgrade_lines(recs) == [])
        # the types are the ones a database the session store built itself has
        sch2 = "ss_" + uuid.uuid4().hex[:12]
        made.append(sch2)
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {sch2}")
        fresh = SessionStore(tempfile.mkdtemp(), f"{PG}?options=-csearch_path%3D{sch2}")
        fresh.upgrade()
        ck("a database the revision built has the same shape as the taken-over one",
           shape(fresh.engine) == shape(sdb.engine), (shape(fresh.engine), shape(sdb.engine)))
        ck("and the same index", index_columns(fresh.engine, "project_sessions", "project_sessions_session") == ["session"])
        # a downgrade leaves the live tables and their rows
        step_down(sdb, "0001_base")
        ck("a downgrade keeps the five tables and their rows", snapshot(sch) == before)
        names = inspect(sdb.engine).get_table_names()
        ck("and drops the ledger", "session_store_copies" not in names, names)
        ck("and the index", index_columns(sdb.engine, "project_sessions", "project_sessions_session") is None)
        sdb.upgrade()
        ck("an upgrade after it takes them over again", versions(sdb) == [HEAD] and snapshot(sch) == before)
        sdb.close()
        fresh.close()

        # only some of the tables exist: the others are made, the ones there are left alone
        sch3, dsn3 = schema_for(["session_stations", "session_usage"])
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"SET search_path = {sch3}")
            c.execute("INSERT INTO session_stations (session, station) VALUES ('s9', 'x')")
        part = SessionStore(tempfile.mkdtemp(), dsn3)
        part.upgrade()
        ck("with two of the five there, all five end up declared", shape(part.engine) == declared(part.engine))
        ck("and the row in the one that was there is kept", _count(part, "session_stations") == 1)
        part.close()
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            for sch in made:
                c.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")


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
        ck("a first upgrade runs every revision", len(upgrade_lines(recs)) == REVISIONS)
        ck("session_store_version is in the schema from the DSN", "session_store_version" in tables(schema),
           tables(schema))
        ck("and not in public", "session_store_version" not in tables("public"))
        ck(f"it holds {HEAD}", versions(sdb) == [HEAD], versions(sdb))
        with capture_logs("alembic.runtime.migration", "tares.session_store") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", upgrade_lines(recs) == [])
        ck("no tares.sqlite is made", not os.path.exists(os.path.join(d, "tares.sqlite")),
           os.listdir(d))
        with sdb.begin() as c:
            c.execute(text("UPDATE session_store_version SET version_num = '9999_future'"))
        with capture_logs("tares.session_store") as recs:
            sdb.upgrade()
        ck("an unknown revision logs one line and goes on", len(recs) == 1 and versions(sdb) == ["9999_future"],
           [r.getMessage() for r in recs])
        with sdb.begin() as c:
            c.execute(text("UPDATE session_store_version SET version_num = '" + HEAD + "'"))
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
        # the keyword form of the same DSN reaches the same schema
        parts = psycopg.conninfo.conninfo_to_dict(PG)
        kw = " ".join(f"{k}={v}" for k, v in parts.items()) + f" options=-csearch_path={schema}"
        kdb = SessionStore(d, kw)
        ck("a keyword DSN reads the same schema", versions(kdb) == [HEAD], "")
        ck("and its connections use no prepared statements",
           kdb.engine.raw_connection().driver_connection.prepare_threshold is None, "")
        kdb.close()
        # cells starting together: two upgrades on an empty schema, one runs the revision
        schema2 = "ss_" + uuid.uuid4().hex[:12]
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema2}")
        try:
            dsn2 = f"{PG}?options=-csearch_path%3D{schema2}"
            a, b = SessionStore(d, dsn2), SessionStore(d, dsn2)
            errs = []

            def up(s_):
                try:
                    s_.upgrade()
                except Exception as e:
                    errs.append(e)

            ths = [threading.Thread(target=up, args=(s_,)) for s_ in (a, b)]
            with capture_logs("alembic.runtime.migration") as recs:   # one capture: the logger is shared
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()
            ck("two upgrades at once raise nothing", not errs, errs[:1])
            ck("and each revision ran once", len(upgrade_lines(recs)) == REVISIONS, len(upgrade_lines(recs)))
            ck("one version row", versions(a) == [HEAD], versions(a))
            a.close()
            b.close()
        finally:
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA IF EXISTS {schema2} CASCADE")
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
    session_tables()
    copy_sqlite()
    copy_at_start()
    copy_on_postgres()
    local_then_postgres()
    greatest_null()
    takeover()
    postgres()
    print("FAIL " + ", ".join(FAILS) if FAILS else "OK")
    sys.exit(1 if FAILS else 0)
