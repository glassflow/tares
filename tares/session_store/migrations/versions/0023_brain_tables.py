"""Repo map and brain tables move out of DuckDB (P-TR-295, TR-1618).

`repo_maps`, `repo_map_history`, `brain_repos`, `brain_repo_folders`, `brain_repo_updates` and
`brain_calls`. On SQLite they are new here: the tables are made and copy.py fills them from DuckDB.
On Postgres they are already the cell's live tables, made by the old DDL with rows in them, so each
is taken over as it is: no row is touched, a column the definition has and the table lacks is
added, and a column the table has and the definition lacks stops the migration with its name.

`idx_brain_repo_updates_repo` is the one index the old DDL made on these tables. The counters of
`brain_repos` and `brain_calls.deep` keep the default 0 the old DDL gave them. `repo_map_history`
has no primary key, as before.

This revision is frozen: it imports only the frozen take-over, and the columns are written out here.

Downgrade: nothing is dropped. The tables stay on both engines: on Postgres they are the live
tables the code before this revision reads, and on SQLite they hold what was written since the
copy. The index stays too: on Postgres the code before this revision makes it itself.

Revision ID: 0023_brain_tables
Revises: 0022_integration_tables
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.takeover import takeover

revision = "0023_brain_tables"
down_revision = "0022_integration_tables"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _repo_maps() -> sa.Table:
    return sa.Table(
        "repo_maps", sa.MetaData(),
        sa.Column("repo", sa.Text, primary_key=True),
        sa.Column("body", sa.Text),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def _repo_map_history() -> sa.Table:
    return sa.Table(
        "repo_map_history", sa.MetaData(),
        sa.Column("repo", sa.Text),
        sa.Column("changed_at", _TS),
        sa.Column("by_station", sa.Text),
        sa.Column("project", sa.Text),
        sa.Column("note", sa.Text))


def _brain_repos() -> sa.Table:
    return sa.Table(
        "brain_repos", sa.MetaData(),
        sa.Column("repo", sa.Text, primary_key=True),
        sa.Column("repo_source_id", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("files_done", sa.Integer, server_default="0"),
        sa.Column("files_total", sa.Integer, server_default="0"),
        sa.Column("indexed_commit", sa.Text),
        sa.Column("last_synced", _TS),
        sa.Column("error", sa.Text),
        sa.Column("added_by", sa.Text),
        sa.Column("added_at", _TS),
        sa.Column("place", sa.Integer),
        sa.Column("job_started_at", _TS),
        sa.Column("sync_failures", sa.Integer, server_default="0"),
        sa.Column("sync_error", sa.Text))


def _brain_repo_folders() -> sa.Table:
    return sa.Table(
        "brain_repo_folders", sa.MetaData(),
        sa.Column("repo", sa.Text, primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("chosen_by", sa.Text),
        sa.Column("chosen_at", _TS))


def _brain_repo_updates() -> sa.Table:
    table = sa.Table(
        "brain_repo_updates", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("repo", sa.Text),
        sa.Column("at", _TS),
        sa.Column("kind", sa.Text),
        sa.Column("ok", sa.Integer),
        sa.Column("indexed_commit", sa.Text),
        sa.Column("files_changed", sa.Integer),
        sa.Column("files_deleted", sa.Integer),
        sa.Column("files_total", sa.Integer),
        sa.Column("error", sa.Text))
    sa.Index("idx_brain_repo_updates_repo", table.c.repo, table.c["at"])
    return table


def _brain_calls() -> sa.Table:
    return sa.Table(
        "brain_calls", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("at", _TS),
        sa.Column("tool", sa.Text),
        sa.Column("deep", sa.Integer, server_default="0"),
        sa.Column("question", sa.Text),
        sa.Column("repos", sa.Text),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("ok", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("station", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("via", sa.Text))


def upgrade() -> None:
    for table in (_repo_maps(), _repo_map_history(), _brain_repos(), _brain_repo_folders(),
                  _brain_repo_updates(), _brain_calls()):
        takeover(op.get_bind(), table)


def downgrade() -> None:
    pass
