"""The merge ledger: what each merge through the queue went through (TR-1672).

`merge_records` has one row per merge queue row (`queue_row`, the primary key): the project, the
ticket and the pull request, the project's autonomy level at the merge (`level`), the head the merge
gate passed (`reviewed_sha`) and the commit the merge made (`merged_sha`), the review rounds counted
by then (`rounds`), the CI state of the reviewed head (`ci_state`), who approved that head and when
(`approver_id`, `approved_at`), when it merged (`merged_at`) and whether somebody else than the queue
merged it (`outside`). A row is written when the queue takes a pull request for merging and completed
when the merge is seen, so a row with no `merged_at` is a merge that has not happened (yet).

The values are what was true at the merge: a later change of a project's level changes no row. A
value nobody knew stays null.

The table is new: a table that is already there (a second run against a database that stopped half
way) is taken over as it is. The columns are written out here, not imported from tables.py: a
revision is a record of what ran.

Downgrade: drops the table, and the records with it. They are not worked out again.

Revision ID: 0012_merge_ledger
Revises: 0011_session_station_runtime
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_merge_ledger"
down_revision = "0011_session_station_runtime"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "merge_records" not in have:
        op.create_table(
            "merge_records",
            sa.Column("queue_row", sa.Text),
            sa.Column("project", sa.Text),
            sa.Column("ticket", sa.Text),
            sa.Column("repo", sa.Text),
            sa.Column("pr", sa.Integer),
            sa.Column("level", sa.Text),
            sa.Column("reviewed_sha", sa.Text),
            sa.Column("merged_sha", sa.Text),
            sa.Column("rounds", sa.Integer),
            sa.Column("ci_state", sa.Text),
            sa.Column("approver_id", sa.Text),
            sa.Column("approved_at", _TS),
            sa.Column("merged_at", _TS),
            sa.Column("outside", sa.Boolean),
            sa.PrimaryKeyConstraint("queue_row", name="merge_records_pkey"))
        op.create_index("merge_records_project_merged_at", "merge_records",
                        ["project", "merged_at"])


def downgrade() -> None:
    op.drop_table("merge_records")
