"""The settings table, the first table moved out of DuckDB (P-TR-295, TR-1480).

`settings` holds the instance's configuration (key, value, updated_at). On SQLite it is new here:
the table is made and copy.py fills it from DuckDB. On Postgres it is already the cell's live table,
made by the old DDL with rows in it, so it is taken over as it is (`takeover`): no row is touched,
a column the definition has and the table lacks is added, and a column the table has and the
definition lacks stops the migration with its name.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: this revision added no column and no index, so there is nothing to drop. The table stays
on both engines: on Postgres it is the live table the code before this revision reads, and on SQLite
it holds the settings written since the copy (a drop would lose them, and the copy ledger would
stop a second upgrade from filling the table again).

Revision ID: 0012_core_settings
Revises: 0011_session_station_runtime
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.takeover import takeover

revision = "0012_core_settings"
down_revision = "0011_session_station_runtime"
branch_labels = None
depends_on = None


def _settings() -> sa.Table:
    return sa.Table(
        "settings", sa.MetaData(),
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True)))


def upgrade() -> None:
    takeover(op.get_bind(), _settings())


def downgrade() -> None:
    pass
