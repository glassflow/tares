"""`takeover` as revisions 0013, 0016 and 0017 call it: a frozen copy of session_store/takeover.py.

`takeover(conn, table)` makes the table when it is missing. For a table that exists, a column the
definition has and the database lacks is added, a column the database has and the definition lacks
is an error naming it, column types are not compared, and the definition's indexes are created when
missing. Rows are not touched.

Do not change what this does (see __init__.py).
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
