"""The turns, last words and touched files the factory reads (P-TR-295, TR-1505).

`session_turns` holds what a person typed (a user line that is not a tool result, a subagent's,
a harness message or a compaction summary), `session_said` the last three things each session's
assistant said, and `session_files` the files each session edited or wrote with when it last did.
They are written as each transcript line is stored. The factory's readers used to scan the events
table for them. All three tables are new: the old image never wrote them, so a table that is
already there (a second run against a database that stopped half way, or one made by the old DDL)
is taken over as it is.

`session_turns` is keyed by (session, line_key), not by line_key alone: a resumed session copies
its old lines under a new session id, and the events table keeps both copies.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops the three tables and their indexes. Nothing else reads them, and the events stay
in DuckDB, so the next upgrade fills them again.

Revision ID: 0005_session_turns
Revises: 0004_session_facts
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_session_turns"
down_revision = "0004_session_facts"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _index(table: str, name: str, columns: list) -> None:
    if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_turns" not in have:
        op.create_table(
            "session_turns",
            sa.Column("session", sa.Text),
            sa.Column("line_key", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("at", _TS),
            sa.Column("person", sa.Text),
            sa.Column("text", sa.Text),
            sa.PrimaryKeyConstraint("session", "line_key", name="session_turns_pkey"))
    _index("session_turns", "session_turns_session_at", ["session", "at"])
    _index("session_turns", "session_turns_at", ["at"])
    if "session_said" not in have:
        op.create_table(
            "session_said",
            sa.Column("session", sa.Text),
            sa.Column("line_key", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("at", _TS),
            sa.Column("text", sa.Text),
            sa.PrimaryKeyConstraint("session", "line_key", name="session_said_pkey"))
    _index("session_said", "session_said_session_at", ["session", "at"])
    if "session_files" not in have:
        op.create_table(
            "session_files",
            sa.Column("session", sa.Text),
            sa.Column("path", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("last_at", _TS),
            sa.PrimaryKeyConstraint("session", "path", name="session_files_pkey"))
    _index("session_files", "session_files_session_last_at", ["session", "last_at"])


def downgrade() -> None:
    op.drop_index("session_files_session_last_at", table_name="session_files")
    op.drop_table("session_files")
    op.drop_index("session_said_session_at", table_name="session_said")
    op.drop_table("session_said")
    op.drop_index("session_turns_at", table_name="session_turns")
    op.drop_index("session_turns_session_at", table_name="session_turns")
    op.drop_table("session_turns")
