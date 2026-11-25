"""The factory's weekly figures per project (TR-1666).

`report_weeks` has one row per project and ISO week: `week` is the week's Monday (UTC), `project`
"" holds what belongs to no project, the Text columns are JSON and every duration is in hours. The
rows are worked out from the factory's records and `session_usage` (report.py), so nothing is lost
with them: a rebuild writes them again.

The table is new: a table that is already there (a second run against a database that stopped half
way) is taken over as it is. The columns are written out here, not imported from tables.py: a
revision is a record of what ran.

Downgrade: drops the table.

Revision ID: 0008_report_weeks
Revises: 0007_session_archive
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_report_weeks"
down_revision = "0007_session_archive"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "report_weeks" not in have:
        op.create_table(
            "report_weeks",
            sa.Column("project", sa.Text),
            sa.Column("week", sa.Date),
            sa.Column("merged", sa.Integer),
            sa.Column("start_to_merge", sa.Text),
            sa.Column("stage_hours", sa.Text),
            sa.Column("waited_hours", sa.Double),
            sa.Column("milestones_accepted", sa.Integer),
            sa.Column("reviewed", sa.Integer),
            sa.Column("passed_first", sa.Integer),
            sa.Column("rounds", sa.Text),
            sa.Column("findings", sa.Text),
            sa.Column("questions", sa.Text),
            sa.Column("cost_usd", sa.Double),
            sa.Column("unpriced", sa.Integer),
            sa.Column("cost_by_role", sa.Text),
            sa.Column("updated_at", _TS),
            sa.PrimaryKeyConstraint("project", "week", name="report_weeks_pkey"))


def downgrade() -> None:
    op.drop_table("report_weeks")
