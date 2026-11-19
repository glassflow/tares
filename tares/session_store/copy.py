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
DuckDB, and these five tables go over here, before the data upgrades write to them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, insert, select

from .tables import COPIED, session_store_copies

log = logging.getLogger("tares.session_store")

CHUNK = 5000


class CopyMismatch(RuntimeError):
    """A table's inserted row count differs from the count read from DuckDB."""


def copy_from_duckdb(store, sessions_db, *, state_move: bool = False) -> dict[str, int]:
    """Copy each session table not yet in the ledger. Returns {table: rows copied} for the tables
    copied by this call (empty when there was nothing to do). `state_move` is the first start of a
    cell with a state database: its state is still in DuckDB and the tables in Postgres are new
    and empty, so the copy runs there too (the one-time move of the rest of the state follows and
    leaves these five tables alone)."""
    if sessions_db.dialect != "sqlite" and not state_move:
        return {}
    copied: dict[str, int] = {}
    for table in COPIED:
        with sessions_db.begin() as conn:
            done = conn.execute(select(session_store_copies.c.table_name).where(
                session_store_copies.c.table_name == table.name)).first()
        if done is not None:
            continue
        cols = [c.name for c in table.columns]
        # DuckDB is read a chunk at a time under its lock and each chunk goes into the same
        # transaction, so the table is never held in memory whole
        with store._lock, sessions_db.begin() as conn:
            before = conn.execute(select(func.count()).select_from(table)).scalar_one()
            cur = store.con.execute(f"SELECT {', '.join(cols)} FROM {table.name}")
            read = 0
            while True:
                chunk = cur.fetchmany(CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                conn.execute(insert(table), [dict(zip(cols, r)) for r in chunk])
            inserted = conn.execute(select(func.count()).select_from(table)).scalar_one() - before
            if inserted != read:
                raise CopyMismatch(f"{table.name}: read {read} rows from DuckDB, "
                                   f"inserted {inserted}")
            conn.execute(insert(session_store_copies), {
                "table_name": table.name, "rows": inserted, "copied_at": datetime.now(timezone.utc)})
        copied[table.name] = inserted
        log.info("session store: copied %d rows of %s from DuckDB", inserted, table.name)
    return copied
