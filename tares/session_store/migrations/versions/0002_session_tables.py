"""The session tables: session_stations, session_states, project_sessions, session_usage,
session_meters, and session_store_copies (P-TR-295, TR-1497).

On Postgres the five tables already exist in the state database (the Store's own DDL made them), so
each one is created only when it is missing and an existing one is taken over as it is: no row and
no column is touched. The columns here are the ones `state_db.state_ddl` gives them on Postgres, and
`tares/session_store/tables.py` declares the same ones (a test compares the two).

The columns are written out here, not imported from tables.py: a revision is a record of what ran,
and a later change to tables.py must not change what this one builds.

Downgrade: drops the copy ledger and the new index. The five tables are dropped only on SQLite,
where the session store made them. On Postgres they are the cell's live session tables, shared with
the code before this revision, so a downgrade leaves them.

Revision ID: 0002_session_tables
Revises: 0001_base
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_session_tables"
down_revision = "0001_base"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _tables():
    """(name, columns, primary key) of each table, built fresh on each call."""
    t, b = sa.Text, sa.BigInteger
    return [
        ("session_stations", [
            sa.Column("session", t), sa.Column("station", t), sa.Column("role", t),
            sa.Column("parent", t), sa.Column("project", t), sa.Column("seen_at", _TS),
            sa.Column("crew_id", t)], ["session"]),
        ("session_states", [
            sa.Column("session", t), sa.Column("state", t), sa.Column("reason", t),
            sa.Column("state_at", _TS)], ["session"]),
        ("project_sessions", [
            sa.Column("project", t), sa.Column("session", t), sa.Column("repo", t),
            sa.Column("linked_at", _TS)], ["project", "session"]),
        ("session_usage", [
            sa.Column("session", t), sa.Column("message", t), sa.Column("replied_at", _TS),
            sa.Column("model", t), sa.Column("project", t), sa.Column("subagent", t),
            sa.Column("input_tokens", b), sa.Column("output_tokens", b),
            sa.Column("cache_write_tokens", b), sa.Column("cache_read_tokens", b),
            sa.Column("cost_usd", sa.Double), sa.Column("cache_write_1h_tokens", b)],
         ["session", "message"]),
        ("session_meters", [
            sa.Column("session", t), sa.Column("status", t), sa.Column("status_at", _TS),
            sa.Column("context", t), sa.Column("context_at", _TS),
            sa.Column("context_asked_at", _TS), sa.Column("status_person", t)], ["session"]),
    ]


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())
    for name, cols, pk in _tables():
        if name not in have:
            op.create_table(name, *cols, sa.PrimaryKeyConstraint(*pk, name=f"{name}_pkey"))
    if "project_sessions_session" not in {i["name"] for i in sa.inspect(op.get_bind())
                                          .get_indexes("project_sessions")}:
        op.create_index("project_sessions_session", "project_sessions", ["session"])
    if "session_store_copies" not in have:
        op.create_table(
            "session_store_copies",
            sa.Column("table_name", sa.Text, primary_key=True),
            sa.Column("rows", sa.Integer),
            sa.Column("copied_at", _TS))


def downgrade() -> None:
    op.drop_table("session_store_copies")
    op.drop_index("project_sessions_session", table_name="project_sessions")
    if op.get_bind().dialect.name == "sqlite":
        for name, _cols, _pk in reversed(_tables()):
            op.drop_table(name)
