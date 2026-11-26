"""Tickets, their history and their numbers move out of DuckDB (P-TR-295, TR-1612).

`tickets`, `ticket_history` and `ticket_numbers` are a project's list of work. On SQLite they are
new here: the tables are made and copy.py fills them from DuckDB. On Postgres they are already the
cell's live tables, made by the old DDL with rows in them, so each is taken over as it is
(`takeover`): no row is touched, a column the definition has and the table lacks is added, and a
column the table has and the definition lacks stops the migration with its name.

The indexes `tickets_project` and `ticket_history_ticket` were made on Postgres only (DuckDB's
indexes have broken after upgrades). `takeover` makes each one when the table lacks it, so both
engines have them and a Postgres database that already has them is left as it is.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.
`depends_on` and `pr` are TEXT on both engines, as the old DDL made them on Postgres.

Downgrade: nothing is dropped. The tables stay on both engines: on Postgres they are the live
tables the code before this revision reads, and on SQLite they hold what was written since the
copy. The indexes stay too: on Postgres the code before this revision makes them itself.

Revision ID: 0014_ticket_tables
Revises: 0013_project_tables
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.takeover import takeover

revision = "0014_ticket_tables"
down_revision = "0013_project_tables"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _tickets() -> sa.Table:
    t = sa.Table(
        "tickets", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("owner", sa.Text),
        sa.Column("external_id", sa.Text),
        sa.Column("identifier", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("position", sa.Double),
        sa.Column("working_doc", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("milestone", sa.Text),
        sa.Column("depends_on", sa.Text),
        sa.Column("number", sa.Integer),
        sa.Column("holder", sa.Text),
        sa.Column("stage", sa.Text),
        sa.Column("stage_reason", sa.Text),
        sa.Column("pr", sa.Text),
        sa.Column("repo", sa.Text),
        sa.Column("assignee_person_id", sa.Text),
        sa.Column("assignee_raw", sa.Text))
    sa.Index("tickets_project", t.c.project)
    return t


def _ticket_history() -> sa.Table:
    t = sa.Table(
        "ticket_history", sa.MetaData(),
        sa.Column("ticket", sa.Text),
        sa.Column("project", sa.Text),
        sa.Column("changed_at", _TS),
        sa.Column("field", sa.Text),
        sa.Column("value", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("by_station", sa.Text),
        sa.Column("by_role", sa.Text),
        sa.Column("person_id", sa.Text))
    sa.Index("ticket_history_ticket", t.c.ticket)
    return t


def _ticket_numbers() -> sa.Table:
    return sa.Table(
        "ticket_numbers", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("last", sa.Integer))


def upgrade() -> None:
    conn = op.get_bind()
    for table in (_tickets(), _ticket_history(), _ticket_numbers()):
        takeover(conn, table)


def downgrade() -> None:
    pass
