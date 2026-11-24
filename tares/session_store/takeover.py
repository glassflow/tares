"""Take a table over in place (TR-1480): the one routine a table's revision calls.

A table that moves out of DuckDB already exists in the cell's state Postgres, made by the old
DDL, with rows in it. A revision of the session store must not rebuild it: `takeover(conn, table)`
makes the table when it is missing and otherwise leaves its rows and columns as they are.

For a table that exists it compares column names with the definition:
- a column the definition has and the database lacks is added, `ADD COLUMN IF NOT EXISTS` on
  Postgres (so two cells starting together, or a start that ran before, add nothing twice);
- a column the database has and the definition lacks is an error naming it: a newer image may own
  it (1.61.0-rc.1 must keep running on the same database), so nothing here guesses;
- column types are not compared.
The definition's indexes are created when missing.

Rule for a revision that uses this: its `downgrade()` drops only what the revision itself added
(columns, indexes) and never drops a table it took over, because the table is the cell's live one
and the code before the revision still reads it. `takeover` returns what it added so a revision
can log it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import Connection, Table
from sqlalchemy.schema import CreateColumn

log = logging.getLogger("tares.session_store")


class TakeoverError(RuntimeError):
    """The table in the database does not fit its definition."""


@dataclass
class Taken:
    table: str
    created: bool = False
    added_columns: list[str] = field(default_factory=list)
    added_indexes: list[str] = field(default_factory=list)


def takeover(conn: Connection, table: Table) -> Taken:
    taken = Taken(table.name)
    insp = sa.inspect(conn)
    if table.name not in insp.get_table_names():
        table.create(conn)                      # the table and its indexes
        taken.created = True
        return taken
    have = {c["name"] for c in insp.get_columns(table.name)}
    extra = sorted(have - {c.name for c in table.columns})
    if extra:
        raise TakeoverError(
            f"table {table.name} has the column {', '.join(extra)}, which this version does not "
            f"define; it is left as it is")
    pg = conn.dialect.name == "postgresql"
    name = conn.dialect.identifier_preparer.format_table(table)
    for col in table.columns:
        if col.name not in have:
            ddl = str(CreateColumn(col).compile(dialect=conn.dialect))
            conn.execute(sa.text(
                f"ALTER TABLE {name} ADD COLUMN {'IF NOT EXISTS ' if pg else ''}{ddl}"))
            taken.added_columns.append(col.name)
    have_ix = {i["name"] for i in insp.get_indexes(table.name)}
    for ix in sorted(table.indexes, key=lambda i: i.name or ""):
        if ix.name not in have_ix:
            ix.create(conn)
            taken.added_indexes.append(ix.name)
    if taken.added_columns or taken.added_indexes:
        log.info("session store: took over %s, added columns %s, indexes %s", table.name,
                 taken.added_columns, taken.added_indexes)
    return taken
