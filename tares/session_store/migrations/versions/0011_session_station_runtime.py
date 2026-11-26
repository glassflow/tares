"""Which runtime a station's session runs on, and its model (TR-1600).

`session_stations` gets two columns, both nullable:

- `runtime`: `codex` for a Codex station's session (its lines say so, `tares/connectors/codex.py`). Empty for a
  Claude Code session, which is read as Claude Code, as every row was before.
- `model`: the model a Codex station was started on. Empty when it runs on its account's default, and for a
  Claude Code session, whose model the console reads from its status line.

A column that is already there (a second run against a database that stopped half way) is left alone.
The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: drops the two columns. The lines the rows were made from stay where they are.

Revision ID: 0011_session_station_runtime
Revises: 0010_session_command_outcome
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_session_station_runtime"
down_revision = "0010_session_command_outcome"
branch_labels = None
depends_on = None

COLUMNS = ("runtime", "model")


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("session_stations")}
    for name in COLUMNS:
        if name not in have:
            op.add_column("session_stations", sa.Column(name, sa.Text))


def downgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("session_stations")}
    with op.batch_alter_table("session_stations") as batch:
        for name in COLUMNS:
            if name in have:
                batch.drop_column(name)
