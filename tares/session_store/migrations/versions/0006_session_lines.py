"""The lines of each session, stored as they arrive (P-TR-295, TR-1478).

`session_lines` holds one row per claude_code line, numbered within its session: `seq` is the
primary key with the session and comes from `sessions.line_count` in the transaction that adds the
line, so it is dense and unique per session. `line_key` is the line's uuid (else a hash of its
payload) and is not unique, because DuckDB keeps a line sent twice twice and this table mirrors it.
`render` holds the flags the line's text was built with, `payload` the JSON text as DuckDB keeps
it. `sessions.lines_complete` (already there) says whether the two stores hold the same lines.

The table is new: the old image never wrote it, so a table that is already there (a second run
against a database that stopped half way, or one made by the old DDL) is taken over as it is. The
columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops the table and its index. The lines stay in DuckDB. When the upgrade makes the
table it sets `lines_complete` to false for every session (the table is empty, so no session is
complete), and the reconcile sets it right again.

Revision ID: 0006_session_lines
Revises: 0005_session_turns
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_session_lines"
down_revision = "0005_session_turns"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _index(table: str, name: str, columns: list) -> None:
    if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_lines" not in have:
        op.create_table(
            "session_lines",
            sa.Column("session", sa.Text),
            sa.Column("seq", sa.Integer),
            sa.Column("line_key", sa.Text),
            sa.Column("at", _TS),
            sa.Column("type", sa.Text),
            sa.Column("labels", sa.Text),
            sa.Column("render", sa.Text),
            sa.Column("payload", sa.Text),
            sa.PrimaryKeyConstraint("session", "seq", name="session_lines_pkey"))
        op.execute("UPDATE sessions SET lines_complete = false")
    _index("session_lines", "session_lines_session_at", ["session", "at"])


def downgrade() -> None:
    op.drop_index("session_lines_session_at", table_name="session_lines")
    op.drop_table("session_lines")
