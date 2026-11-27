"""A ticket's pull request as two indexed columns (P-TR-295, TR-1614).

`tickets.pr_repo` (lower case) and `tickets.pr_number` say what the `pr` JSON says, so the ticket
of a pull request is found through the index `tickets_pr` and not by searching the text of every
`pr`. The rows there are get their values here, by the fill in migrations/frozen/pr_columns.py (every
start also fills, with the live session_store/pr_columns.py: a release before this one writes `pr`
and not the columns).

Additive only: two nullable columns, no default, and one index. A column or index that is already
there (a second run against a database that stopped half way) is left as it is. `pr` is not
changed.

Downgrade: drops the index and the two columns and nothing else.

Revision ID: 0018_ticket_pr_columns
Revises: 0017_ticket_tables
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.pr_columns import fill_pr_columns

revision = "0018_ticket_pr_columns"
down_revision = "0017_ticket_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("tickets")}
    if "pr_repo" not in cols:
        op.add_column("tickets", sa.Column("pr_repo", sa.Text, nullable=True))
    if "pr_number" not in cols:
        op.add_column("tickets", sa.Column("pr_number", sa.BigInteger, nullable=True))
    if "tickets_pr" not in {i["name"] for i in insp.get_indexes("tickets")}:
        op.create_index("tickets_pr", "tickets", ["pr_repo", "pr_number"])
    fill_pr_columns(conn)


def downgrade() -> None:
    op.drop_index("tickets_pr", table_name="tickets")
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("pr_repo")
        batch.drop_column("pr_number")
