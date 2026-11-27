"""Where a session's lines were born (P-TR-295, TR-1606).

`sessions.lines_origin` says how `lines_complete` is judged. Null (every row that exists now) means
the session was first counted from the events: it is complete when the table and the archive hold as
many lines as DuckDB does. `'table'` means the session was born in `session_lines`: it is complete
when the table and the archive together hold `sessions.line_count` lines, whatever DuckDB holds.

Additive only: one nullable column, no default, no backfill. A column that is already there (a second
run against a database that stopped half way) is taken over as it is. Existing rows keep their
`lines_complete` value.

Downgrade: drops the column and nothing else.

Revision ID: 0013_session_origin
Revises: 0012_core_settings
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_session_origin"
down_revision = "0012_core_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("sessions")}
    if "lines_origin" not in cols:
        op.add_column("sessions", sa.Column("lines_origin", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("lines_origin")
