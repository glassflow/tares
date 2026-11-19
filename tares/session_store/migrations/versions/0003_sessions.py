"""The sessions table and the fill marks (P-TR-295, TR-1471).

`sessions` holds what each Claude Code session's lines add up to (first and last line time, lines,
tool calls, coordination calls), so a page that lists sessions reads one row each instead of
counting over the whole event history. `session_ingest_marks` holds how far a fill from DuckDB has
read, by name. Both are new: the old image never wrote them, so a table that is already there (a
second run against a database that stopped half way) is taken over as it is.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops both tables and the index. Nothing else reads them, and the events stay in DuckDB.

Revision ID: 0003_sessions
Revises: 0002_session_tables
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_sessions"
down_revision = "0002_session_tables"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "sessions" not in have:
        op.create_table(
            "sessions",
            sa.Column("session", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("started_at", _TS),
            sa.Column("last_line_at", _TS),
            sa.Column("line_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("tool_calls", sa.Integer, nullable=False, server_default="0"),
            sa.Column("coordination_calls", sa.Integer, nullable=False, server_default="0"),
            sa.Column("lines_complete", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("updated_at", _TS),
            sa.PrimaryKeyConstraint("session", name="sessions_pkey"))
    if "sessions_last_line_at" not in {i["name"] for i in sa.inspect(op.get_bind())
                                       .get_indexes("sessions")}:
        op.create_index("sessions_last_line_at", "sessions", ["last_line_at"])
    if "session_ingest_marks" not in have:
        op.create_table(
            "session_ingest_marks",
            sa.Column("name", sa.Text),
            sa.Column("at", _TS),
            sa.PrimaryKeyConstraint("name", name="session_ingest_marks_pkey"))


def downgrade() -> None:
    op.drop_table("session_ingest_marks")
    op.drop_index("sessions_last_line_at", table_name="sessions")
    op.drop_table("sessions")
