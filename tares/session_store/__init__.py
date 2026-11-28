"""The session store: the cell's one database (P-TR-295, Tares 2.0).

SQLAlchemy Core (no ORM) and Alembic. SQLite (`tares.sqlite` in the data directory) locally, the
cell's Postgres when TARES_STATE_DSN is set. Each table is made by an Alembic revision.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Iterator

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, event, func, text
from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

from .. import request_memo
from .engine import MEMORY, make_engine

log = logging.getLogger("tares.session_store")

VERSION_TABLE = "session_store_version"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('tares_session_store_migrate'))"


def _seen(conn, cursor, statement, parameters, context, executemany) -> None:
    request_memo.statement(statement)      # a write ends what a page read has kept (TR-1631)


# The revisions of the Tares 1.x session store, squashed into 0001_baseline for 2.0 (TR-1816). A
# database at one of them holds the 1.x tables and is converted by the 2.0 migration script, never
# upgraded here; at "0001_base" it is a 1.x one too (2.0's first revision is "0001_baseline").
OLD_REVISIONS = frozenset((
    "0001_base", "0002_session_tables", "0003_sessions", "0004_session_facts", "0005_session_turns",
    "0006_session_lines", "0007_session_archive", "0008_report_weeks", "0009_report_texts",
    "0010_session_command_outcome", "0011_session_station_runtime", "0012_core_settings",
    "0013_session_origin", "0014_core_people", "0015_session_moves", "0016_session_usage_hourly",
    "0017_project_tables", "0018_ticket_tables", "0019_ticket_pr_columns", "0020_review_tables",
    "0021_crew_tables", "0022_integration_tables", "0023_brain_tables", "0024_merge_ledger",
    "0025_integration_keys_pr_events", "0026_drop_catalog_sources", "0027_drop_move_bookkeeping"))


class OldStore(RuntimeError):
    """The database is a Tares 1.x one: the store refuses to start on it (the daemon serves its
    degraded page with this reason) instead of running against tables it does not have."""


class SessionStore:
    def __init__(self, data_dir: str, state_dsn: str | None = None):
        self.engine = make_engine(data_dir, state_dsn)
        event.listen(self.engine, "before_cursor_execute", _seen)
        # An in-memory SQLite is one shared connection (StaticPool): two threads in a transaction
        # at once would interleave on it, so they take turns. A file or Postgres needs no lock.
        self._turns = threading.RLock() if data_dir == MEMORY and not state_dsn else None

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name        # "sqlite" or "postgresql"

    @contextlib.contextmanager
    def begin(self) -> Iterator[Connection]:
        """One transaction: commits when the block ends, rolls back when it raises."""
        with self._turns or contextlib.nullcontext():
            with self.engine.begin() as conn:
                yield conn

    @contextlib.contextmanager
    def read(self) -> Iterator[Connection]:
        """One read-only transaction; never write in it. On SQLite it does not take the write lock,
        so it does not queue behind a writer and no writer queues behind it (WAL gives it the last
        committed state, the same one from its first query to its end). On Postgres it is a plain
        transaction like `begin()`, each statement seeing what had committed when it ran; a read
        whose queries must agree uses `consistent_read()`."""
        with self._turns or contextlib.nullcontext():
            with self.engine.connect() as conn:
                conn = conn.execution_options(deferred=True)
                with conn.begin():
                    yield conn

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[Connection]:
        """One transaction whose statements all see the same data. On SQLite it takes the write
        lock, like `begin()`, so a read-only caller uses `consistent_read()` instead. Postgres
        would let a row committed between two statements show in the second only, so the
        transaction runs at REPEATABLE READ: set for this transaction only, which is what
        PgBouncer in transaction mode allows."""
        with self._turns or contextlib.nullcontext():
            with self.engine.connect() as conn:
                if self.dialect == "postgresql":
                    conn = conn.execution_options(isolation_level="REPEATABLE READ")
                with conn.begin():
                    yield conn

    def consistent_read(self):
        """`snapshot()` without SQLite's write lock, for a read that joins several queries and
        writes nothing: SQLite's deferred transaction already sees one state from its first read to
        its end."""
        return self.snapshot() if self.dialect == "postgresql" else self.read()

    def upsert(self, table):
        """An INSERT for `table` that takes `.on_conflict_do_update(...)` and
        `.on_conflict_do_nothing()`, in the dialect the store runs on."""
        return (_pg_insert if self.dialect == "postgresql" else _sqlite_insert)(table)

    def greatest(self, a, b):
        """The larger of two values, a NULL one ignored (Postgres `greatest`; SQLite's scalar
        `max` gives NULL for a NULL argument, so the other value is the fallback)."""
        if self.dialect == "postgresql":
            return func.greatest(a, b)
        return func.coalesce(func.max(a, b), a, b)

    def least(self, a, b):
        """The smaller of two values, a NULL one ignored (Postgres `least`; SQLite's scalar `min`
        gives NULL for a NULL argument, so the other value is the fallback)."""
        if self.dialect == "postgresql":
            return func.least(a, b)
        return func.coalesce(func.min(a, b), a, b)

    def close(self) -> None:
        self.engine.dispose()

    def _config(self) -> Config:
        cfg = Config()          # no alembic.ini: everything is set here
        here = os.path.dirname(os.path.abspath(__file__))
        cfg.set_main_option("script_location", os.path.join(here, "migrations").replace("%", "%%"))
        return cfg

    def upgrade(self) -> None:
        """Bring the store to the newest revision, in one transaction. On Postgres a transaction
        level advisory lock first, so cells starting together migrate one at a time (and it lets
        go with the transaction, which PgBouncer's transaction mode needs). A database written by
        a newer image (a revision this code does not know, after a rollback) is left as it is."""
        cfg = self._config()
        with self.engine.begin() as conn:
            if self.dialect == "postgresql":
                conn.execute(text(_LOCK_SQL))
            current = MigrationContext.configure(
                conn, opts={"version_table": VERSION_TABLE, "version_table_schema": None}
            ).get_current_revision()
            known = {r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()}
            if current in OLD_REVISIONS:
                raise OldStore(
                    f"this database was written by Tares 1.x (revision {current}); Tares 2.0 starts "
                    "only on a converted one. Run the 2.0 migration script on it first (TR-1821), or "
                    "keep running Tares 1.61")
            if current is not None and current not in known:
                log.warning("session store is at revision %s, which this version does not know; "
                            "leaving it as it is", current)
                return
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
