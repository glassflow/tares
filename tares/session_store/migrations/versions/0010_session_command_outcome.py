"""How a recorded command ended, and where it ran (TR-1595).

`session_commands` gets two columns, both nullable:

- `cwd`: the folder the command ran in.
- `outcome`: `started` (the command was let through and no end is recorded yet), `ended`, `failed` or
  `declined` (the runtime's own word for a command it gave no exit code for), `unknown` (it started and
  its end will never be known: the runtime died while it ran).

Only a Codex station's lines set them. A row a Claude Code line made keeps both empty and is read as
before; with `outcome` set, `exit_code` holds the real exit code, not the 0 or 1 of "failed or not".

A column that is already there (a second run against a database that stopped half way) is left alone.
The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops the two columns. The lines the rows were made from stay where they are.

Revision ID: 0010_session_command_outcome
Revises: 0009_report_texts
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_session_command_outcome"
down_revision = "0009_report_texts"
branch_labels = None
depends_on = None

COLUMNS = ("cwd", "outcome")


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("session_commands")}
    for name in COLUMNS:
        if name not in have:
            op.add_column("session_commands", sa.Column(name, sa.Text))


def downgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("session_commands")}
    with op.batch_alter_table("session_commands") as batch:
        for name in COLUMNS:
            if name in have:
                batch.drop_column(name)
