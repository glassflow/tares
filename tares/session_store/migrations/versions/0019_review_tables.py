"""Milestones, checks and reviews move out of DuckDB (P-TR-295, TR-1615).

`milestones`, `milestone_check_results`, `milestone_acceptances`, `ticket_checks`, `challenges`,
`reviews`, `review_findings` and `rule_proposals`. On SQLite they are new here: the tables are made
and copy.py fills them from DuckDB. On Postgres they are already the cell's live tables, made by
the old DDL with rows in them, so each is taken over as it is: no row is touched, a column the
definition has and the table lacks is added, and a column the table has and the definition lacks
stops the migration with its name.

`ix_challenges_branch` is the one index the old DDL made on these tables; it is made when the table
lacks it, so both engines have it and a Postgres database that already has it is left as it is.

This revision is frozen: it imports nothing from tares. The columns are written out here, and the
take-over is the copy of `session_store/takeover.py` below, as it was when this revision was
written. `checks`, `tickets` and `payload` are TEXT on both engines, as the old DDL made them on
Postgres.

Downgrade: nothing is dropped. The tables stay on both engines: on Postgres they are the live
tables the code before this revision reads, and on SQLite they hold what was written since the
copy. The index stays too: on Postgres the code before this revision makes it itself.

Revision ID: 0019_review_tables
Revises: 0018_ticket_pr_columns
"""
import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import CreateColumn

revision = "0019_review_tables"
down_revision = "0018_ticket_pr_columns"
branch_labels = None
depends_on = None

log = logging.getLogger("tares.session_store")

_TS = sa.DateTime(timezone=True)


def _milestones() -> sa.Table:
    return sa.Table(
        "milestones", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("owner", sa.Text),
        sa.Column("external_id", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("goal", sa.Text),
        sa.Column("checks", sa.Text),
        sa.Column("position", sa.Double),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS))


def _milestone_check_results() -> sa.Table:
    return sa.Table(
        "milestone_check_results", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("milestone", sa.Text),
        sa.Column("idx", sa.Integer),
        sa.Column("result", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("by_kind", sa.Text),
        sa.Column("by_name", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("checked_at", _TS))


def _milestone_acceptances() -> sa.Table:
    return sa.Table(
        "milestone_acceptances", sa.MetaData(),
        sa.Column("milestone", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("by_name", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("accepted_at", _TS))


def _ticket_checks() -> sa.Table:
    return sa.Table(
        "ticket_checks", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("ticket", sa.Text),
        sa.Column("command", sa.Text),
        sa.Column("result", sa.Text),
        sa.Column("commit_sha", sa.Text),
        sa.Column("broke_test", sa.Text),
        sa.Column("by_station", sa.Text),
        sa.Column("by_session", sa.Text),
        sa.Column("checked_at", _TS),
        sa.Column("person_id", sa.Text))


def _challenges() -> sa.Table:
    t = sa.Table(
        "challenges", sa.MetaData(),
        sa.Column("source", sa.Text, primary_key=True),
        sa.Column("key_value", sa.Text, primary_key=True),
        sa.Column("event_time", _TS, primary_key=True),
        sa.Column("event_type", sa.Text, primary_key=True),
        sa.Column("branch", sa.Text, primary_key=True),
        sa.Column("tares_project", sa.Text),
        sa.Column("payload", sa.Text))
    sa.Index("ix_challenges_branch", t.c.source, t.c.branch)
    return t


def _reviews() -> sa.Table:
    return sa.Table(
        "reviews", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("ticket", sa.Text),
        sa.Column("round", sa.Integer),
        sa.Column("pr", sa.Text),
        sa.Column("head", sa.Text),
        sa.Column("verdict", sa.Text),
        sa.Column("verified", sa.Text),
        sa.Column("not_verified", sa.Text),
        sa.Column("by_station", sa.Text),
        sa.Column("reviewed_at", _TS),
        sa.Column("person_id", sa.Text))


def _review_findings() -> sa.Table:
    return sa.Table(
        "review_findings", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("ticket", sa.Text),
        sa.Column("review", sa.Text),
        sa.Column("number", sa.Integer),
        sa.Column("severity", sa.Text),
        sa.Column("file", sa.Text),
        sa.Column("line", sa.Integer),
        sa.Column("blocking", sa.Boolean),
        sa.Column("text", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("fixed_in", sa.Text),
        sa.Column("found_at", _TS))


def _rule_proposals() -> sa.Table:
    return sa.Table(
        "rule_proposals", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("repo", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("project", sa.Text),
        sa.Column("scope", sa.Text),
        sa.Column("text", sa.Text),
        sa.Column("tickets", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("decided_at", _TS))


class TakeoverError(RuntimeError):
    """The table in the database does not fit its definition."""


def _takeover(conn: sa.Connection, table: sa.Table) -> None:
    """Make the table when it is missing; otherwise leave its rows and columns, add the columns
    and indexes it lacks, and refuse a table with a column the definition does not have."""
    insp = sa.inspect(conn)
    if table.name not in insp.get_table_names():
        table.create(conn)                      # the table and its indexes
        return
    have = {c["name"] for c in insp.get_columns(table.name)}
    extra = sorted(have - {c.name for c in table.columns})
    if extra:
        raise TakeoverError(
            f"table {table.name} has the column {', '.join(extra)}, which this version does not "
            f"define; it is left as it is")
    pg = conn.dialect.name == "postgresql"
    name = conn.dialect.identifier_preparer.format_table(table)
    added_columns, added_indexes = [], []
    for col in table.columns:
        if col.name not in have:
            ddl = str(CreateColumn(col).compile(dialect=conn.dialect))
            conn.execute(sa.text(
                f"ALTER TABLE {name} ADD COLUMN {'IF NOT EXISTS ' if pg else ''}{ddl}"))
            added_columns.append(col.name)
    have_ix = {i["name"] for i in insp.get_indexes(table.name)}
    for ix in sorted(table.indexes, key=lambda i: i.name or ""):
        if ix.name not in have_ix:
            ix.create(conn)
            added_indexes.append(ix.name)
    if added_columns or added_indexes:
        log.info("session store: took over %s, added columns %s, indexes %s", table.name,
                 added_columns, added_indexes)


def upgrade() -> None:
    conn = op.get_bind()
    for table in (_milestones(), _milestone_check_results(), _milestone_acceptances(),
                  _ticket_checks(), _challenges(), _reviews(), _review_findings(),
                  _rule_proposals()):
        _takeover(conn, table)


def downgrade() -> None:
    pass
