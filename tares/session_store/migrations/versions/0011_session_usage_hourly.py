"""Kept hourly totals of the usage rows (P-TR-296, TR-1632).

`station_rollup`'s spend and `usage_by_project` summed every `session_usage` row on each page
request. They read `session_usage_hourly` now: one row per session, hour and project. A second table,
`session_rollup_state`, holds the fill's cursor and its ready mark. The index on
`session_usage(replied_at)` serves the partial first hour of a window.

Additive only: two new tables and one index, no row touched. The tables start empty; the fill in
the daemon builds them from the usage rows, and the readers keep the old scan until it is done.
A table or index that is already there (a second run against a database that stopped half way) is
taken over as it is.

Downgrade: drops the two tables and the index. The usage rows are not touched.

Revision ID: 0011_session_usage_hourly
Revises: 0010_core_people
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_session_usage_hourly"
down_revision = "0010_core_people"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())
    if "session_usage_hourly" not in have:
        t, b = sa.Text, sa.BigInteger
        op.create_table(
            "session_usage_hourly",
            sa.Column("session", t), sa.Column("hour", _TS),
            sa.Column("project", t, server_default=""),
            sa.Column("replies", b, nullable=False), sa.Column("input_tokens", b, nullable=False),
            sa.Column("output_tokens", b, nullable=False),
            sa.Column("cache_write_tokens", b, nullable=False),
            sa.Column("cache_read_tokens", b, nullable=False),
            sa.Column("cost_usd", sa.Double), sa.Column("unpriced", b, nullable=False),
            sa.PrimaryKeyConstraint("session", "hour", "project", name="session_usage_hourly_pkey"))
    if "session_usage_hourly_hour" not in {i["name"] for i in sa.inspect(op.get_bind())
                                           .get_indexes("session_usage_hourly")}:
        op.create_index("session_usage_hourly_hour", "session_usage_hourly", ["hour"])
    if "session_rollup_state" not in have:
        op.create_table(
            "session_rollup_state", sa.Column("name", sa.Text, primary_key=True),
            sa.Column("cursor", sa.Text), sa.Column("ready_at", _TS))
    if "session_usage_replied_at" not in {i["name"] for i in sa.inspect(op.get_bind())
                                          .get_indexes("session_usage")}:
        op.create_index("session_usage_replied_at", "session_usage", ["replied_at"])


def downgrade() -> None:
    op.drop_index("session_usage_replied_at", table_name="session_usage")
    op.drop_table("session_rollup_state")
    op.drop_table("session_usage_hourly")
