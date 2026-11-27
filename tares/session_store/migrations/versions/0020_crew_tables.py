"""Decisions, questions and crew tables move out of DuckDB (P-TR-295, TR-1616).

`assumptions`, `desk_items`, `decisions`, `handovers`, `grants`, `pending_memory`, `crews`,
`spec_questions`, `signoffs`, `handover_overrides`, `answers` and `waiting_told`. On SQLite they
are new here: the tables are made and copy.py fills them from DuckDB. On Postgres they are already
the cell's live tables, made by the old DDL with rows in them, so each is taken over as it is: no
row is touched, a column the definition has and the table lacks is added, and a column the table has
and the definition lacks stops the migration with its name.

`idx_answers_at` is the one index the old DDL made on these tables, and `crews` has its one unique
constraint, (person_id, name, machine). `grants.needs_lead` and `pending_memory.attempts` keep the
defaults the old DDL gave them. The JSON columns are TEXT on both engines, as the old DDL made them
on Postgres.

This revision is frozen: it imports only the frozen take-over, and the columns are written out here.

Downgrade: nothing is dropped. The tables stay on both engines: on Postgres they are the live
tables the code before this revision reads, and on SQLite they hold what was written since the
copy. The index stays too: on Postgres the code before this revision makes it itself.

Revision ID: 0020_crew_tables
Revises: 0019_review_tables
"""
import sqlalchemy as sa
from alembic import op

from tares.session_store.migrations.frozen.takeover import takeover

revision = "0020_crew_tables"
down_revision = "0019_review_tables"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _assumptions() -> sa.Table:
    return sa.Table(
        "assumptions", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("number", sa.Integer),
        sa.Column("question", sa.Text),
        sa.Column("choice", sa.Text),
        sa.Column("why", sa.Text),
        sa.Column("affects", sa.Text),
        sa.Column("tickets", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("words", sa.Text),
        sa.Column("batch", sa.Text),
        sa.Column("made_by", sa.Text),
        sa.Column("made_role", sa.Text),
        sa.Column("made_at", _TS),
        sa.Column("decided_at", _TS),
        sa.Column("person_id", sa.Text))


def _desk_items() -> sa.Table:
    return sa.Table(
        "desk_items", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("number", sa.Integer),
        sa.Column("project", sa.Text),
        sa.Column("question", sa.Text),
        sa.Column("context", sa.Text),
        sa.Column("options", sa.Text),
        sa.Column("recommendation", sa.Text),
        sa.Column("why", sa.Text),
        sa.Column("blocks", sa.Text),
        sa.Column("tickets", sa.Text),
        sa.Column("blocking", sa.Boolean),
        sa.Column("asked_by", sa.Text),
        sa.Column("assumptions", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("closed_at", _TS),
        sa.Column("station", sa.Text),
        sa.Column("command", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("to_person_id", sa.Text))


def _decisions() -> sa.Table:
    return sa.Table(
        "decisions", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project", sa.Text),
        sa.Column("desk_item", sa.Text),
        sa.Column("question", sa.Text),
        sa.Column("words", sa.Text),
        sa.Column("choice", sa.Text),
        sa.Column("standing", sa.Boolean),
        sa.Column("scope", sa.Text),
        sa.Column("matched", sa.Boolean),
        sa.Column("session", sa.Text),
        sa.Column("granted", sa.Boolean),
        sa.Column("tickets", sa.Text),
        sa.Column("decided_at", _TS),
        sa.Column("via", sa.Text),
        sa.Column("person_id", sa.Text))


def _handovers() -> sa.Table:
    return sa.Table(
        "handovers", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("repo", sa.Text),
        sa.Column("handed_by", sa.Text),
        sa.Column("handed_at", _TS),
        sa.Column("repos", sa.Text),
        sa.Column("autonomy", sa.Text),
        sa.Column("finished_at", _TS),
        sa.Column("finished_words", sa.Text),
        sa.Column("finished_decision", sa.Text),
        sa.Column("finished_by", sa.Text),
        sa.Column("taken_at", _TS),
        sa.Column("taken_words", sa.Text),
        sa.Column("taken_by", sa.Text),
        sa.Column("released_at", _TS),
        sa.Column("person_id", sa.Text),
        sa.Column("finished_person_id", sa.Text),
        sa.Column("taken_person_id", sa.Text),
        sa.Column("crew", sa.Text),
        sa.Column("override_reason", sa.Text),
        sa.Column("override_at", _TS),
        sa.Column("override_person_id", sa.Text))


def _grants() -> sa.Table:
    return sa.Table(
        "grants", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text),
        sa.Column("project", sa.Text),
        sa.Column("holder_person_id", sa.Text),
        sa.Column("words", sa.Text),
        sa.Column("given_by", sa.Text),
        sa.Column("given_at", _TS),
        sa.Column("areas", sa.Text),
        sa.Column("consents", sa.Text),
        sa.Column("state", sa.Text),
        sa.Column("needs_lead", sa.Boolean, server_default=sa.false()))


def _pending_memory() -> sa.Table:
    return sa.Table(
        "pending_memory", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("scope", sa.Text),
        sa.Column("line", sa.Text),
        sa.Column("attempts", sa.Integer, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", _TS),
        sa.Column("tried_at", _TS))


def _crews() -> sa.Table:
    return sa.Table(
        "crews", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("machine", sa.Text),
        sa.Column("first_seen", _TS),
        sa.Column("last_seen", _TS),
        sa.UniqueConstraint("person_id", "name", "machine", name="crews_person_id_name_machine_key"))


def _spec_questions() -> sa.Table:
    return sa.Table(
        "spec_questions", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("to_person_id", sa.Text),
        sa.Column("repo", sa.Text),
        sa.Column("text", sa.Text),
        sa.Column("asked_by", sa.Text),
        sa.Column("asked_at", _TS),
        sa.Column("answer", sa.Text),
        sa.Column("answered_by", sa.Text),
        sa.Column("answered_at", _TS),
        sa.Column("state", sa.Text),
        sa.Column("via", sa.Text))


def _signoffs() -> sa.Table:
    return sa.Table(
        "signoffs", sa.MetaData(),
        sa.Column("project", sa.Text, primary_key=True),
        sa.Column("repo", sa.Text, primary_key=True),
        sa.Column("person_id", sa.Text),
        sa.Column("at", _TS),
        sa.Column("via", sa.Text))


def _handover_overrides() -> sa.Table:
    return sa.Table(
        "handover_overrides", sa.MetaData(),
        sa.Column("project", sa.Text),
        sa.Column("at", _TS),
        sa.Column("reason", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("handed_by", sa.Text))


def _answers() -> sa.Table:
    table = sa.Table(
        "answers", sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("item", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("project", sa.Text),
        sa.Column("ask", sa.Text),
        sa.Column("choice", sa.Text),
        sa.Column("said", sa.Text),
        sa.Column("via", sa.Text),
        sa.Column("person_id", sa.Text),
        sa.Column("answered_at", _TS))
    sa.Index("idx_answers_at", table.c.answered_at)
    return table


def _waiting_told() -> sa.Table:
    return sa.Table(
        "waiting_told", sa.MetaData(),
        sa.Column("person_id", sa.Text, primary_key=True),
        sa.Column("item", sa.Text, primary_key=True),
        sa.Column("told_at", _TS),
        sa.Column("sent", sa.Boolean),
        sa.Column("ask", sa.Text),
        sa.Column("channel", sa.Text),
        sa.Column("ts", sa.Text),
        sa.Column("seq", sa.Integer),
        sa.Column("closed", sa.Text))


def upgrade() -> None:
    conn = op.get_bind()
    for table in (_assumptions(), _desk_items(), _decisions(), _handovers(), _grants(),
                  _pending_memory(), _crews(), _spec_questions(), _signoffs(),
                  _handover_overrides(), _answers(), _waiting_told()):
        takeover(conn, table)


def downgrade() -> None:
    pass
