"""The session store's engine: SQLite beside the DuckDB file, or the cell's state Postgres.

Locally (no TARES_STATE_DSN) the store is `sessions.sqlite` in the data directory, in WAL mode. Each
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
SQLITE_FILE = "sessions.sqlite"


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
        engine = create_engine("sqlite:///" + os.path.join(data_dir, SQLITE_FILE),
                               connect_args={"check_same_thread": False})
    _sqlite_events(engine)
    return engine


def _sqlite_events(engine: Engine) -> None:
    # Every transaction starts with BEGIN IMMEDIATE, reads included, so a read waits behind a
    # writer. Fine while the store holds little; a read path that must not queue (DEFERRED) is for
    # the ticket that moves the busy reads here.
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
        conn.exec_driver_sql("BEGIN IMMEDIATE")
