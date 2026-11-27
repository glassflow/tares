"""Projects, their parts, docs, skills, sources, GitHub credentials and model usage move out of
DuckDB (P-TR-295, TR-1610).

`usecases` (the projects), `usecase_objects` (the parts a project uses), `usecase_log`, `docs`,
`skills` (the per-project skills from before skills were shared), `shared_skills`,
`catalog_sources`, `github_credentials` and `model_usage` are the cell's project data. On SQLite
they are new here: the tables are made and copy.py fills them from DuckDB. On Postgres they are
already the cell's live tables, made by the old DDL with rows in them, so each is taken over as it is
(`takeover`): no row is touched, a column the definition has and the table lacks is added, and a
column the table has and the definition lacks stops the migration with its name.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.
The JSON columns (`params`, `setup`, `linear`, `github_issues`, `config`) are TEXT on both engines,
as the old DDL made them on Postgres.

Downgrade: nothing is dropped. The tables stay on both engines: on Postgres they are the live
tables the code before this revision reads, and on SQLite they hold what was written since the
copy.

Revision ID: 0017_project_tables
Revises: 0016_session_usage_hourly
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.takeover import takeover

revision = "0017_project_tables"
down_revision = "0016_session_usage_hourly"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _usecases() -> sa.Table:
    return sa.Table(
        "usecases", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("recipe", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("params", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("last_error", sa.Text),
        sa.Column("goal", sa.Text),
        sa.Column("setup", sa.Text),
        sa.Column("linear", sa.Text),
        sa.Column("github_issues", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("lead_person_id", sa.Text),
        sa.Column("scope", sa.Text),
        sa.Column("icon", sa.Text))


def _usecase_objects() -> sa.Table:
    return sa.Table(
        "usecase_objects", sa.MetaData(),
        sa.Column("usecase_id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("customized", sa.Boolean),
        sa.Column("created_at", _TS))


def _usecase_log() -> sa.Table:
    return sa.Table(
        "usecase_log", sa.MetaData(),
        sa.Column("usecase_id", sa.Text),
        sa.Column("logged_at", _TS),
        sa.Column("action", sa.Text),
        sa.Column("detail", sa.Text))


def _docs() -> sa.Table:
    return sa.Table(
        "docs", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("made_by", sa.Text),
        sa.Column("updated_by", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("owner", sa.Text))


def _skills() -> sa.Table:
    return sa.Table(
        "skills", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("description", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS))


def _shared_skills() -> sa.Table:
    return sa.Table(
        "shared_skills", sa.MetaData(),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("description", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("made_by", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("updated_by", sa.Text))


def _catalog_sources() -> sa.Table:
    return sa.Table(
        "catalog_sources", sa.MetaData(),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("type", sa.Text),
        sa.Column("connector", sa.Text),
        sa.Column("poll", sa.Text),
        sa.Column("config", sa.Text),
        sa.Column("paused", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("ingest_key", sa.Text),
        sa.Column("owned_by", sa.Text),
        sa.Column("customized", sa.Boolean))


def _github_credentials() -> sa.Table:
    return sa.Table(
        "github_credentials", sa.MetaData(),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text),
        sa.Column("token", sa.Text),
        sa.Column("api_url", sa.Text),
        sa.Column("account", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("config", sa.Text))


def _model_usage() -> sa.Table:
    t = sa.Table(
        "model_usage", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("ts", _TS),
        sa.Column("surface", sa.Text),
        sa.Column("agent", sa.Text),
        sa.Column("run_id", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("calls", sa.Integer),
        sa.Column("input_tokens", sa.BigInteger),
        sa.Column("output_tokens", sa.BigInteger),
        sa.Column("cache_creation_input_tokens", sa.BigInteger),
        sa.Column("cache_read_input_tokens", sa.BigInteger),
        sa.Column("cost_usd", sa.Double),
        sa.Column("key_source", sa.Text),
        sa.Column("provider", sa.Text))
    sa.Index("ix_model_usage_ts", t.c.ts)
    return t


def upgrade() -> None:
    conn = op.get_bind()
    for table in (_usecases(), _usecase_objects(), _usecase_log(), _docs(), _skills(),
                  _shared_skills(), _catalog_sources(), _github_credentials(), _model_usage()):
        takeover(conn, table)


def downgrade() -> None:
    pass
