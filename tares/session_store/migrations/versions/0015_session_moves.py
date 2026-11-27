"""The ledger of the one-time move of recent sessions into the session store (P-TR-295, TR-1609).

`session_moves` has one row per claude_code session that DuckDB holds lines for: `state` is
`pending`, `verified` or `failed`, `target` is `table` or `archive`, `lines_events` the lines DuckDB
holds, `lines_after` the rows the move wrote, `started_at` and `verified_at` when it ran, `error` why
the last try failed, `attempts` how many tries failed (a session is `failed` for good when its counts
do not match, or after the cap of tries). `tares/session_move.py` fills it and moves the sessions in it.

Additive only: one new table and its index. A table that is already there (a second run against a
database that stopped half way) is taken over as it is. The columns are written out here, not
imported from tables.py: a revision is a record of what ran.

Downgrade: drops the table and the plan's mark (so an upgrade plans again) and nothing else. The
lines the move already wrote stay in `session_lines`.

Revision ID: 0015_session_moves
Revises: 0014_core_people
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_session_moves"
down_revision = "0014_core_people"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_moves" not in have:
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


def downgrade() -> None:
    op.drop_table("session_moves")
    if "session_ingest_marks" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.execute("DELETE FROM session_ingest_marks WHERE name = 'session_move_planned'")
