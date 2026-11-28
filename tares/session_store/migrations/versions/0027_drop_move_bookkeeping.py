"""The bookkeeping of the moves into the session store goes (Tares 2.0, TR-1815).

Every line and every table is written to the session store alone now, so three tables that kept
track of copying from the old event store and catching up with it are read by nothing:

- `session_store_copies`: one row per table copied in at a start (0002);
- `session_ingest_marks`: how far each fill and catch-up had read, and which had finished (0003);
- `session_moves`: one row per session of the one-time move of older lines (0015).

They are dropped when they are there (an index goes with its table). The columns are written out
here, not imported from tables.py: a revision is a record of what ran.

Downgrade: makes the three tables again, empty.

Revision ID: 0027_drop_move_bookkeeping
Revises: 0026_drop_catalog_sources
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_drop_move_bookkeeping"
down_revision = "0026_drop_catalog_sources"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    for name in ("session_moves", "session_ingest_marks", "session_store_copies"):
        if name in have:
            op.drop_table(name)


def downgrade() -> None:
    op.create_table(
        "session_store_copies",
        sa.Column("table_name", sa.Text, primary_key=True),
        sa.Column("rows", sa.Integer),
        sa.Column("copied_at", _TS))
    op.create_table(
        "session_ingest_marks",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("at", _TS))
    op.create_table(
        "session_moves",
        sa.Column("session", sa.Text, primary_key=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("target", sa.Text),
        sa.Column("lines_events", sa.Integer),
        sa.Column("lines_after", sa.Integer),
        sa.Column("started_at", _TS),
        sa.Column("verified_at", _TS),
        sa.Column("error", sa.Text),
        sa.Column("attempts", sa.Integer))
    op.create_index("session_moves_state", "session_moves", ["state"])
