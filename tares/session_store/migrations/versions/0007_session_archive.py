"""The parts of a session's lines that the archive holds (P-TR-295, TR-1506).

`session_archive_parts` has one row per file the hourly archive wrote for a session: `part` is its
number (1, 2, ...), `first_seq` and `last_seq` the numbers of the `session_lines` rows it holds,
`lines` how many, `bytes` the size of the file, `md5` its MD5 and `key` where the archive keeps it.
A part is written, checked against the archive, and only then are its rows deleted from
`session_lines`, in the same transaction that inserts this row. `sessions.lines_complete` counts the
rows of `session_lines` plus the `lines` of the parts, so an archived session stays complete.

The table is new: a table that is already there (a second run against a database that stopped half
way) is taken over as it is. The columns are written out here, not imported from tables.py: a
revision is a record of what ran.

Downgrade: drops the table. The archive files stay where they are, and the lines they hold are no
longer in `session_lines`; they are still in DuckDB, and `lines_complete` is set false for every
session that had a part, so nothing reads the table as whole.

Revision ID: 0007_session_archive
Revises: 0006_session_lines
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_session_archive"
down_revision = "0006_session_lines"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_archive_parts" not in have:
        op.create_table(
            "session_archive_parts",
            sa.Column("session", sa.Text),
            sa.Column("part", sa.Integer),
            sa.Column("first_seq", sa.Integer),
            sa.Column("last_seq", sa.Integer),
            sa.Column("lines", sa.Integer),
            sa.Column("bytes", sa.BigInteger),
            sa.Column("md5", sa.Text),
            sa.Column("key", sa.Text),
            sa.Column("written_at", _TS),
            sa.PrimaryKeyConstraint("session", "part", name="session_archive_parts_pkey"))


def downgrade() -> None:
    op.execute("UPDATE sessions SET lines_complete = false WHERE session IN "
               "(SELECT session FROM session_archive_parts)")
    op.drop_table("session_archive_parts")
