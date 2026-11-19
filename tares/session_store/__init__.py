"""The session store: a second store, beside DuckDB, for session data (P-TR-295).

SQLAlchemy Core (no ORM) and Alembic. SQLite (`sessions.sqlite` next to the DuckDB file) locally,
the cell's state Postgres when TARES_STATE_DSN is set. Later tickets add the tables, one Alembic
revision each; nothing lives here yet beyond the revision table.
"""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text

from .engine import make_engine

log = logging.getLogger("tares.session_store")

VERSION_TABLE = "session_store_version"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('tares_session_store_migrate'))"


class SessionStore:
    def __init__(self, data_dir: str, state_dsn: str | None = None):
        self.engine = make_engine(data_dir, state_dsn)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name        # "sqlite" or "postgresql"

    def begin(self) -> contextlib.AbstractContextManager[Connection]:
        """One transaction: commits when the block ends, rolls back when it raises."""
        return self.engine.begin()

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
            if current is not None and current not in known:
                log.warning("session store is at revision %s, which this version does not know; "
                            "leaving it as it is", current)
                return
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
