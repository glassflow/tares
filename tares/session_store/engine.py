"""The session store's engine: SQLite beside the DuckDB file, or the cell's state Postgres.

Locally (no TARES_STATE_DSN) the store is `tares.sqlite` in the data directory, in WAL mode (it was
`sessions.sqlite` before TR-1480, and a start renames the old file, with its -wal and -shm files). Each
write transaction starts with BEGIN IMMEDIATE, so two writers queue on the busy timeout instead of
one failing with SQLITE_BUSY halfway through a read-then-write step. This is the pysqlite recipe in
the SQLAlchemy docs ("Serializable isolation / Savepoints / Transactional DDL"): pysqlite's own
transaction handling is turned off and the begin event emits the BEGIN.

With a state DSN the store is that Postgres, through psycopg 3. Cells reach Postgres through
PgBouncer in transaction mode, so nothing here relies on a session: no SET, no prepared statements
(`prepare_threshold=None`), no session advisory locks. The DSN is kept as it is except for its
scheme, so a query string such as `?options=-csearch_path%3Dx` reaches the driver.
"""
from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

MEMORY = ":memory:"      # the data_dir that means an in-memory SQLite (a Store on ":memory:")
SQLITE_FILE = "tares.sqlite"
LEGACY_SQLITE_FILE = "sessions.sqlite"       # the name until TR-1480


def adopt_legacy_file(data_dir: str) -> None:
    """Rename `sessions.sqlite` to `tares.sqlite` when only the old name exists, with its -wal and
    -shm files. The side files go first and the main file last: a start that stops half way leaves
    `sessions.sqlite` in place, so the next start renames again and finds the side files already
    under their new names (the other order would leave a `tares.sqlite` without its WAL, whose
    committed writes would be lost)."""
    old = os.path.join(data_dir, LEGACY_SQLITE_FILE)
    new = os.path.join(data_dir, SQLITE_FILE)
    if not os.path.exists(old) or os.path.exists(new):
        return
    for suffix in ("-wal", "-shm"):
        if os.path.exists(old + suffix):
            os.replace(old + suffix, new + suffix)
    os.replace(old, new)


def pg_url(dsn: str) -> str:
    """The DSN with SQLAlchemy's psycopg 3 scheme. Only the scheme changes: the rest, query
    string included, stays as it was."""
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return "postgresql+psycopg://" + dsn[len(scheme):]
    return dsn


def make_engine(data_dir: str, state_dsn: str | None = None) -> Engine:
    if state_dsn:
        pool = dict(pool_size=2, max_overflow=2, pool_pre_ping=True)
        if "://" not in state_dsn:
            # a libpq keyword DSN ("host=h dbname=d"), which PgState takes too: not a URL, so the
            # driver connects with the string itself
            import psycopg
            return create_engine(
                "postgresql+psycopg://",
                creator=lambda: psycopg.connect(state_dsn, prepare_threshold=None), **pool)
        return create_engine(
            make_url(pg_url(state_dsn)), connect_args={"prepare_threshold": None}, **pool)
    if data_dir == MEMORY:
        engine = create_engine("sqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
    else:
        adopt_legacy_file(data_dir)
        engine = create_engine("sqlite:///" + os.path.join(data_dir, SQLITE_FILE),
                               connect_args={"check_same_thread": False})
    _sqlite_events(engine)
    return engine


def _sqlite_events(engine: Engine) -> None:
    # Every transaction starts with BEGIN IMMEDIATE, so a writer waits for the one before it. A
    # read (`SessionStore.read()`) starts with a deferred BEGIN instead: in WAL mode it sees the
    # last committed state, never waits for a writer and holds none up.
    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):
        # pysqlite's own BEGIN handling is off: SQLAlchemy's begin event below emits it
        dbapi_connection.isolation_level = None
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN" if conn.get_execution_options().get("deferred") else "BEGIN IMMEDIATE")
