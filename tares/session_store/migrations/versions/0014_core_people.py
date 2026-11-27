"""People, keys and members move out of DuckDB (P-TR-295, TR-1608).

`people`, `api_keys` and `project_members` are the cell's accounts. On SQLite they are new here: the
tables are made and copy.py fills them from DuckDB. On Postgres they are already the cell's live
tables, made by the old DDL with rows in them, so each is taken over as it is (`takeover`): no row
is touched, a column the definition has and the table lacks is added, and a column the table has and
the definition lacks stops the migration with its name.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.
`emails` and `scopes` are TEXT on both engines (JSON text, as the old DDL made them).

Downgrade: this revision added no column and no index, so there is nothing to drop. The tables stay
on both engines: on Postgres they are the live tables the code before this revision reads, and on
SQLite they hold the people and keys written since the copy.

Revision ID: 0014_core_people
Revises: 0013_session_origin
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.takeover import takeover

revision = "0014_core_people"
down_revision = "0013_session_origin"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _people() -> sa.Table:
    return sa.Table(
        "people", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text),
        sa.Column("emails", sa.Text),
        sa.Column("github", sa.Text),
        sa.Column("is_admin", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", _TS),
        sa.Column("created_via", sa.Text),
        sa.Column("picture", sa.Text),
        sa.Column("removed_at", _TS))


def _api_keys() -> sa.Table:
    return sa.Table(
        "api_keys", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("prefix", sa.Text),
        sa.Column("hash", sa.Text),
        sa.Column("scopes", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("last_used_at", _TS),
        sa.Column("revoked_at", _TS),
        sa.Column("project", sa.Text),
        sa.Column("person_id", sa.Text))


def _project_members() -> sa.Table:
    return sa.Table(
        "project_members", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("person_id", sa.Text, primary_key=True),
        sa.Column("added_by", sa.Text),
        sa.Column("added_at", _TS),
        sa.Column("reason", sa.Text))


def upgrade() -> None:
    conn = op.get_bind()
    for table in (_people(), _api_keys(), _project_members()):
        takeover(conn, table)


def downgrade() -> None:
    pass
