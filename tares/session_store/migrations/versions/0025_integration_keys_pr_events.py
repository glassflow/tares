"""GitHub App webhook keys and pull request events (Tares 2.0, TR-1805, TR-1807).

`integration_keys`: one row per key a GitHub App's webhook is delivered on (/ingest/<key>): the
integration (`ci_github`), the App credential's name, the name its events go under (`source`) and when
it was made. The webhook secret stays on the credential. Before, the key lived in the source catalog.

`pr_events`: the GitHub `pull_request` events of the last 30 days, one row per delivery: the repo, the
pull request's number and head branch, the action and when. The factory links a ticket in the work to
its pull request by its `factory/<ref>` branch from here; before, it read the DuckDB events table.

Both tables are new: a table that is already there is taken over as it is. The columns are written out
here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops both tables.

Revision ID: 0025_integration_keys_pr_events
Revises: 0024_merge_ledger
"""
import sqlalchemy as sa
from alembic import op

revision = "0025_integration_keys_pr_events"
down_revision = "0024_merge_ledger"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "integration_keys" not in have:
        op.create_table(
            "integration_keys",
            sa.Column("key", sa.Text),
            sa.Column("integration", sa.Text, nullable=False),
            sa.Column("credential", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("created_at", _TS),
            sa.PrimaryKeyConstraint("key", name="integration_keys_pkey"))
        op.create_index("integration_keys_credential", "integration_keys", ["integration", "credential"])
    if "pr_events" not in have:
        op.create_table(
            "pr_events",
            sa.Column("delivery_id", sa.Text),
            sa.Column("repo", sa.Text, nullable=False),
            sa.Column("number", sa.Integer, nullable=False),
            sa.Column("branch", sa.Text, nullable=False),
            sa.Column("action", sa.Text),
            sa.Column("at", _TS, nullable=False),
            sa.PrimaryKeyConstraint("delivery_id", name="pr_events_pkey"))
        op.create_index("pr_events_repo_branch", "pr_events", ["repo", "branch"])
        op.create_index("pr_events_at", "pr_events", ["at"])


def downgrade() -> None:
    op.drop_table("pr_events")
    op.drop_table("integration_keys")
