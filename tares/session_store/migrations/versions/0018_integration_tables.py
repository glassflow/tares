"""Merge queue, integration, secret and guardrail tables move out of DuckDB (P-TR-295, TR-1617).

`guardrail_rules`, `guardrail_pairs`, `integration_choices`, `integration_connections`,
`project_integrations`, `pending_writeback`, `project_secrets`, `project_required_checks`,
`merge_queue` and `project_merge_method`. On SQLite they are new here: the tables are made and
copy.py fills them from DuckDB. On Postgres they are already the cell's live tables, made by the old
DDL with rows in them, so each is taken over as it is: no row is touched, a column the definition
has and the table lacks is added, and a column the table has and the definition lacks stops the
migration with its name.

`idx_merge_queue_repo` is the one index the old DDL made on these tables. `guardrail_pairs.on_fail`
keeps the default the old DDL gave it. The JSON columns are TEXT on both engines, as the old DDL
made them on Postgres. A secret's `value` is the sealed text and moves as it is.

This revision is frozen: it imports only the frozen take-over, and the columns are written out here.

Downgrade: nothing is dropped. The tables stay on both engines: on Postgres they are the live
tables the code before this revision reads, and on SQLite they hold what was written since the
copy. The index stays too: on Postgres the code before this revision makes it itself.

Revision ID: 0018_integration_tables
Revises: 0017_crew_tables
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.takeover import takeover

revision = "0018_integration_tables"
down_revision = "0017_crew_tables"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _guardrail_rules() -> sa.Table:
    return sa.Table(
        "guardrail_rules", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("scope", sa.Text),
        sa.Column("project", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("text", sa.Text),
        sa.Column("created_by", sa.Text),
        sa.Column("created_at", _TS))


def _guardrail_pairs() -> sa.Table:
    return sa.Table(
        "guardrail_pairs", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("scope", sa.Text),
        sa.Column("project_uid", sa.Text),
        sa.Column("hook", sa.Text),
        sa.Column("integration", sa.Text),
        sa.Column("settings", sa.Text),
        sa.Column("on_fail", sa.Text, server_default="block"),
        sa.Column("created_at", _TS),
        sa.Column("created_by", sa.Text))


def _integration_choices() -> sa.Table:
    return sa.Table(
        "integration_choices", sa.MetaData(),
        sa.Column("slot", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("settings", sa.Text),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def _integration_connections() -> sa.Table:
    return sa.Table(
        "integration_connections", sa.MetaData(),
        sa.Column("slot", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("settings", sa.Text),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def _project_integrations() -> sa.Table:
    return sa.Table(
        "project_integrations", sa.MetaData(),
        sa.Column("project_uid", sa.Text, primary_key=True),
        sa.Column("slot", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("settings", sa.Text),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def _pending_writeback() -> sa.Table:
    return sa.Table(
        "pending_writeback", sa.MetaData(),
        sa.Column("ticket", sa.Text, primary_key=True),
        sa.Column("integration", sa.Text, primary_key=True),
        sa.Column("status", sa.Text),
        sa.Column("attempts", sa.Integer),
        sa.Column("last_error", sa.Text),
        sa.Column("updated_at", _TS))


def _project_secrets() -> sa.Table:
    return sa.Table(
        "project_secrets", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("value", sa.Text),
        sa.Column("set_by", sa.Text),
        sa.Column("set_at", _TS),
        sa.Column("used_by", sa.Text),
        sa.Column("used_at", _TS))


def _project_required_checks() -> sa.Table:
    return sa.Table(
        "project_required_checks", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("commands", sa.Text),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def _merge_queue() -> sa.Table:
    table = sa.Table(
        "merge_queue", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("repo", sa.Text),
        sa.Column("base", sa.Text),
        sa.Column("pr", sa.Integer),
        sa.Column("head", sa.Text),
        sa.Column("ticket", sa.Text),
        sa.Column("project_uid", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("desk_item", sa.Text),
        sa.Column("approved_head", sa.Text),
        sa.Column("entered_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("before_files", sa.Text),
        sa.Column("unknown_ticks", sa.Integer))
    sa.Index("idx_merge_queue_repo", table.c.repo, table.c.base)
    return table


def _project_merge_method() -> sa.Table:
    return sa.Table(
        "project_merge_method", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("method", sa.Text),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def upgrade() -> None:
    conn = op.get_bind()
    for table in (_guardrail_rules(), _guardrail_pairs(), _integration_choices(),
                  _integration_connections(), _project_integrations(), _pending_writeback(),
                  _project_secrets(), _project_required_checks(), _merge_queue(),
                  _project_merge_method()):
        takeover(conn, table)


def downgrade() -> None:
    pass
