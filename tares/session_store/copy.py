"""Copy the session tables from DuckDB into the session store (P-TR-295, TR-1497).

`copy_from_duckdb(store, sessions_db)` is the one-time move on a cell whose session store is SQLite.
The Store calls it at start, after the session store's upgrade and before the data upgrades, so
the fills they run write into a table that already holds its copy. It reads DuckDB and never
changes it.

Each of the five tables is copied once. A table with a row in `session_store_copies` is skipped, so
a second call copies nothing. A table's rows and its ledger row go in one transaction, so a failure
leaves neither: the next call tries the table again. The number of rows inserted is compared with
the number read, and a difference raises with the table's name and both numbers.

On Postgres the session store is the cell's state database itself: the tables there are the live
ones and the rows are already in place, so there is nothing to copy and the call returns at once.
The exception is the cell's first start with a state database (`state_move`): its state is still in
DuckDB, and these five tables go over here, before the data upgrades write to them. Each table is
copied afresh then: a start that failed after its copy (the data upgrade or the move raised) left
rows and ledger entries in Postgres, the cell may have run on SQLite since and written to
`sessions.sqlite`, and the next first start must bring those rows over. Nothing has been served
from Postgres yet, so replacing its copy loses nothing; the delete, the copy and the ledger row
share one transaction, so a failing copy leaves the earlier one as it was. A cell that ran on
SQLite first has its live rows in `sessions.sqlite`, so a table that file's ledger shows as copied
comes from the file, and DuckDB's older copy of it is not used.
"""
from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, select

from . import SessionStore
from .engine import SQLITE_FILE
from .tables import COPIED, session_store_copies

log = logging.getLogger("tares.session_store")

CHUNK = 5000


class CopyMismatch(RuntimeError):
    """A table's inserted row count differs from the count read from DuckDB."""


def _in_ledger(sessions_db, table) -> bool:
    with sessions_db.begin() as conn:
        return conn.execute(select(session_store_copies.c.table_name).where(
            session_store_copies.c.table_name == table.name)).first() is not None


def copy_from_duckdb(store, sessions_db, *, state_move: bool = False,
                     local_dir: str | None = None) -> dict[str, int]:
    """Copy each session table not yet in the ledger. Returns {table: rows copied} for the tables
    copied by this call (empty when there was nothing to do). `state_move` is the first start of a
    cell with a state database: its state is still in DuckDB and the tables in Postgres are new
    and empty, so the copy runs there too (the one-time move of the rest of the state follows and
    leaves these five tables alone).

    A cell that ran locally first holds its live session rows in `sessions.sqlite` in `local_dir`,
    and DuckDB's copies are only what they were at the first local start. On the first start with
    a state database a table the local file's ledger shows as copied comes from that file; any other
    table comes from DuckDB. The local file is read, never emptied, and stays as a fallback."""
    if sessions_db.dialect != "sqlite" and not state_move:
        return {}
    local = None
    if state_move and local_dir and os.path.exists(os.path.join(local_dir, SQLITE_FILE)):
        local = SessionStore(local_dir)
        local.upgrade()
    copied: dict[str, int] = {}
    try:
        for table in COPIED:
            if _in_ledger(sessions_db, table) and not state_move:
                continue
            cols = [c.name for c in table.columns]
            from_local = local is not None and _in_ledger(local, table)
            label = SQLITE_FILE if from_local else "DuckDB"
            # the source is read a chunk at a time and each chunk goes into the same transaction,
            # so the table is never held in memory whole
            with contextlib.ExitStack() as stack:
                if from_local:
                    src = stack.enter_context(local.begin())
                    result = src.execution_options(stream_results=True).execute(select(table))
                    chunks = (lambda: [dict(r._mapping) for r in result.fetchmany(CHUNK)])
                else:
                    stack.enter_context(store._lock)
                    cur = store.con.execute(f"SELECT {', '.join(cols)} FROM {table.name}")
                    chunks = (lambda: [dict(zip(cols, r)) for r in cur.fetchmany(CHUNK)])
                conn = stack.enter_context(sessions_db.begin())
                if state_move:
                    # The state has not moved, so nothing has served from these tables yet: a row
                    # or ledger entry here is from a first start that copied and then failed, and
                    # the cell may have written to the local file since. Copy the table afresh, in
                    # this transaction, so a failure leaves the earlier copy as it was.
                    conn.execute(delete(table))
                    conn.execute(delete(session_store_copies).where(
                        session_store_copies.c.table_name == table.name))
                before = conn.execute(select(func.count()).select_from(table)).scalar_one()
                read = 0
                while True:
                    chunk = chunks()
                    if not chunk:
                        break
                    read += len(chunk)
                    conn.execute(insert(table), chunk)
                inserted = conn.execute(
                    select(func.count()).select_from(table)).scalar_one() - before
                if inserted != read:
                    raise CopyMismatch(f"{table.name}: read {read} rows from {label}, "
                                       f"inserted {inserted}")
                conn.execute(insert(session_store_copies), {
                    "table_name": table.name, "rows": inserted,
                    "copied_at": datetime.now(timezone.utc)})
            copied[table.name] = inserted
            log.info("session store: copied %d rows of %s from %s", inserted, table.name, label)
    finally:
        if local is not None:
            local.close()
    return copied
