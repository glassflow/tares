"""The short texts Tares writes from its figures (TR-1645).

`report_texts` has one row per text: `kind` says what it is (`milestone`: the summary of an accepted
milestone; `week`: the weekly paragraph) and `key` which one (the milestone's id). `text` is what
people read, `source` where it came from (`model`, `template`, `template:no-key` or
`template:rejected`), `figures` the JSON it was written from and `written_at` when. A text is written
once and kept; asking for it again replaces the row.

The table is new: a table that is already there (a second run against a database that stopped half
way) is taken over as it is. The columns are written out here, not imported from tables.py: a
revision is a record of what ran.

Downgrade: drops the table, and the texts with it. They are written again when somebody next reads
the milestone, and may then differ.

Revision ID: 0009_report_texts
Revises: 0008_report_weeks
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_report_texts"
down_revision = "0008_report_weeks"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "report_texts" not in have:
        op.create_table(
            "report_texts",
            sa.Column("kind", sa.Text),
            sa.Column("key", sa.Text),
            sa.Column("text", sa.Text),
            sa.Column("source", sa.Text),
            sa.Column("figures", sa.Text),
            sa.Column("written_at", _TS),
            sa.PrimaryKeyConstraint("kind", "key", name="report_texts_pkey"))


def downgrade() -> None:
    op.drop_table("report_texts")
