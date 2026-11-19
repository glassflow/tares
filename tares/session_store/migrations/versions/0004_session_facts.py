"""The session facts the factory reads (P-TR-295, TR-1477).

`session_messages` holds the crew's protocol messages (a SendMessage call whose text starts
`[TF:<TYPE>]`) and `session_commands` the shell commands sessions ran with what they printed, one
row each by tool_use id, written as each transcript line is stored. The factory's readers used to
parse the whole events table for them. Both tables are new: the old image never wrote them, so a
table that is already there (a second run against a database that stopped half way, or one made by
the old DDL) is taken over as it is.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops both tables and their indexes. Nothing else reads them, and the events stay in
DuckDB, so the next upgrade fills them again.

Revision ID: 0004_session_facts
Revises: 0003_sessions
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_session_facts"
down_revision = "0003_sessions"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _index(table: str, name: str) -> None:
    if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, ["session", "at"])


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_messages" not in have:
        op.create_table(
            "session_messages",
            sa.Column("tool_use_id", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("session", sa.Text),
            sa.Column("at", _TS),
            sa.Column("to_station", sa.Text),
            sa.Column("type", sa.Text),
            sa.Column("text", sa.Text),
            sa.PrimaryKeyConstraint("tool_use_id", name="session_messages_pkey"))
    _index("session_messages", "session_messages_session_at")
    if "session_commands" not in have:
        op.create_table(
            "session_commands",
            sa.Column("tool_use_id", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("session", sa.Text),
            sa.Column("at", _TS),
            sa.Column("command", sa.Text),
            sa.Column("exit_code", sa.Integer),
            sa.Column("output_tail", sa.Text),
            sa.Column("result_at", _TS),
            sa.PrimaryKeyConstraint("tool_use_id", name="session_commands_pkey"))
    _index("session_commands", "session_commands_session_at")


def downgrade() -> None:
    op.drop_index("session_commands_session_at", table_name="session_commands")
    op.drop_table("session_commands")
    op.drop_index("session_messages_session_at", table_name="session_messages")
    op.drop_table("session_messages")
