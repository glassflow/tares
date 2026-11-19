"""The base revision: the version table and nothing else.

How revisions work here: one revision per ticket, in one straight line (no branches), ids
`NNNN_<slug>`, each with a downgrade that works. An existing database is stamped at the revision it
already matches, never rebuilt. Later tickets add their tables in a revision of their own.

Revision ID: 0001_base
Revises:
"""
revision = "0001_base"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
