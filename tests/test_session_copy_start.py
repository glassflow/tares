"""The first start with a state database and the copy of the five session tables (P-TR-295, TR-1517).

Needs Postgres: set TARES_TEST_PG, e.g.
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres python tests/test_session_copy_start.py
Without it the cases are skipped (the copy's Postgres path has nothing to run on). They are skipped
too under tests/pg_harness, which moves every Store to a schema of its own: each case here starts its
Stores by hand, with a DSN it names.

  failed_first_start   a first start that fails after the copy committed (the data upgrade or the
                       move raises), the cell then runs on SQLite and writes rows, and the next
                       Postgres start has them all
  local_file_guards    the three guards before the copy reads `sessions.sqlite`: the file's
                       existence, its upgrade, and its ledger
"""
import contextlib
import gc
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import test_session_store as ts  # noqa: E402  (its helpers; its checks share its FAILS list)
from sqlalchemy import text  # noqa: E402

from tares import state_db  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import COPIED  # noqa: E402
from tares.store import Store, StoreUnavailable  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
# the harness (CI's postgres suite) swaps the DSN a Store gets for a schema of its own, so a case that
# names its own DSN cannot run under it; the duckdb suite has TARES_TEST_PG and no harness
if Store.__init__.__module__ != "tares.store":
    PG = None
SESSION_TABLES = [t.name for t in COPIED]
ck = ts.ck


@contextlib.contextmanager
def pg_schema():
    """A schema of its own, and the state DSN that points a Store at it."""
    import psycopg
    sch = "ss_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {sch}")
    try:
        yield f"{PG}?options=-csearch_path%3D{sch}"
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")


def older_cell():
    """A cell from before the session store: rows in DuckDB's five tables, no sessions.sqlite."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    s = Store(path, state_dsn="")
    ts._seed_duckdb(s)
    s.con.execute("CHECKPOINT")
    s.con.close()
    s.close()
    ts._drop_local_file(d)
    return d, path


def close(s):
    s.con.execute("CHECKPOINT")
    s.con.close()
    s.close()


def _pg_query(dsn, sql):
    """Read the state database a DSN names (its search_path schema), without starting a Store."""
    import psycopg
    sch = dsn.split("csearch_path%3D")[1]
    with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
        return c.execute(sql).fetchall()


def pg_count(dsn, table):
    return _pg_query(dsn, f"SELECT count(*) FROM {table}")[0][0]


def pg_ledger(dsn):
    return sorted(tuple(r) for r in _pg_query(dsn, "SELECT table_name, rows FROM session_store_copies"))


def local_rows(s):
    return {t: [tuple(ts._norm(v) for v in r) for r in ts._session_rows(s, t)] for t in SESSION_TABLES}


def same_rows(s, want, label):
    """Each table of the Store's session store holds `want`'s rows (the crew a data upgrade fills
    into a station is left out, and Postgres and SQLite order '-' differently)."""
    for t in SESSION_TABLES:
        got = sorted((tuple(ts._norm(v) for v in r) for r in ts._session_rows(s, t)), key=repr)
        exp = sorted(want[t], key=repr)
        if t == "session_stations":
            got, exp = [r[:-1] for r in got], [r[:-1] for r in exp]
        ck(f"{label}: {t} holds the same rows", got == exp, (got, exp))


def _fail_upgrade(self):
    raise RuntimeError("the data upgrade stopped")


def _fail_move(*a, **k):
    raise RuntimeError("the move stopped")


def failed_first_start():
    print("== a first Postgres start that fails after the copy, then the cell runs on SQLite ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    for what, target, name, fake in [("the data upgrade", Store, "_upgrade", _fail_upgrade),
                                     ("the move", state_db, "move", _fail_move)]:
        d, path = older_cell()
        duck = None
        with pg_schema() as dsn:
            orig = getattr(target, name)
            setattr(target, name, fake)
            try:
                try:
                    Store(path, state_dsn=dsn)
                    ck(f"{what} raising stops the start", False, "it started")
                except StoreUnavailable:
                    ck(f"{what} raising stops the start", True)
            finally:
                setattr(target, name, orig)
            gc.collect()
            ck(f"{what} raised after the copy committed: the five tables are in Postgres",
               [n for n, _ in pg_ledger(dsn)] == sorted(SESSION_TABLES)
               and pg_count(dsn, "project_sessions") == 2, pg_ledger(dsn))
            # the cell is brought back up without the state database, and works
            s = Store(path, state_dsn="")
            ck("the local start copied DuckDB's rows to sessions.sqlite",
               os.path.exists(os.path.join(d, "sessions.sqlite"))
               and ts._count(s.sessions_db, "project_sessions") == 2)
            s.link_session("p1", "s-local")
            s.set_session_state("s-local", "working", "after the failed start",
                                datetime(2026, 11, 20, tzinfo=timezone.utc))
            want = local_rows(s)
            duck = {t: ts._duck_rows(s, t)[1] for t in SESSION_TABLES}
            close(s)
            # the next Postgres start
            s = Store(path, state_dsn=dsn)
            ck(f"{what}: the next start has the state database",
               s.sessions_db.dialect == "postgresql" and state_db.moved(s.sc))
            ck(f"{what}: the row written on SQLite is in Postgres",
               sorted(s.sessions_of_projects(["p1"])) == ["s-local", "s1", "s2"], s.sessions_of_projects(["p1"]))
            ck(f"{what}: so is the state written there",
               (s.session_state("s-local") or {}).get("state") == "working")
            ck(f"{what}: and no row is there twice", ts._count(s.sessions_db, "project_sessions") == 3)
            same_rows(s, want, what)
            ck(f"{what}: the ledger holds the local counts",
               sorted(ts._ledger(s)) == sorted((t, len(want[t])) for t in SESSION_TABLES), ts._ledger(s))
            ck(f"{what}: DuckDB's copies are untouched",
               {t: ts._duck_rows(s, t)[1] for t in SESSION_TABLES} == duck)
            close(s)
            ck(f"{what}: sessions.sqlite is left as it was", os.path.exists(os.path.join(d, "sessions.sqlite")))
            # a later start is not a first start: nothing is copied again, and what Postgres has stays
            s = Store(path, state_dsn=dsn)
            s.link_session("p1", "s-after")
            close(s)
            s = Store(path, state_dsn=dsn)
            ck(f"{what}: a later start copies nothing and keeps the rows written in Postgres",
               ts._count(s.sessions_db, "project_sessions") == 4)
            close(s)


def _local_file_cell():
    """A cell that ran on SQLite first: sessions.sqlite holds a copy of DuckDB's rows."""
    d, path = older_cell()
    s = Store(path, state_dsn="")
    s.link_session("p1", "s-local")                    # a row only the local file has
    close(s)
    return d, path


def local_file_guards():
    print("== the guards before the copy reads sessions.sqlite ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    # 1. the file is read only when it exists: an older cell gets no empty sessions.sqlite
    d, path = older_cell()
    with pg_schema() as dsn:
        s = Store(path, state_dsn=dsn)
        ck("an older cell's first Postgres start copies DuckDB's rows",
           s.sessions_db.dialect == "postgresql" and ts._count(s.sessions_db, "project_sessions") == 2)
        close(s)
    ck("and leaves no sessions.sqlite behind", [f for f in os.listdir(d) if f.startswith("sessions.sqlite")] == [],
       os.listdir(d))

    # 2. the file is brought to the newest revision before it is read
    d, path = _local_file_cell()
    local = SessionStore(d)
    ts.step_down(local, "0001_base")                   # a file written by an older image: no tables
    local.close()
    with pg_schema() as dsn:
        s = Store(path, state_dsn=dsn)
        ck("a sessions.sqlite from an older revision is upgraded, then read: DuckDB's rows arrive",
           s.sessions_db.dialect == "postgresql" and ts._count(s.sessions_db, "project_sessions") == 2
           and ts._count(s.sessions_db, "session_usage") == 2)
        close(s)

    # 3. a table comes from the file only when the file's ledger says it copied it
    d, path = _local_file_cell()
    local = SessionStore(d)
    with local.begin() as c:                           # the file lost its copy of four tables
        for t in ["session_stations", "session_states", "session_usage", "session_meters"]:
            c.execute(text(f"DELETE FROM {t}"))
            c.execute(text("DELETE FROM session_store_copies WHERE table_name = :t"), {"t": t})
    local.close()
    with pg_schema() as dsn:
        s = Store(path, state_dsn=dsn)
        ck("a table the file's ledger does not name comes from DuckDB, not from the file's empty one",
           {t: ts._count(s.sessions_db, t) for t in SESSION_TABLES}
           == {"session_stations": 2, "session_states": 2, "session_usage": 2, "session_meters": 2,
               "project_sessions": 3}, {t: ts._count(s.sessions_db, t) for t in SESSION_TABLES})
        ck("and the table it does name comes from the file", "s-local" in s.sessions_of_projects(["p1"]))
        close(s)


if __name__ == "__main__":
    failed_first_start()
    local_file_guards()
    print("FAIL " + ", ".join(ts.FAILS) if ts.FAILS else "OK")
    sys.exit(1 if ts.FAILS else 0)
