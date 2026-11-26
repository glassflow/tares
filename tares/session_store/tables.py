"""The session store's tables, as SQLAlchemy Core declares them (P-TR-295).

These are the five session tables exactly as they are today in DuckDB and in the state Postgres:
same names, same columns, same primary keys, and the column types `state_db.state_ddl` gives them on
Postgres (TEXT, BIGINT, DOUBLE PRECISION, TIMESTAMPTZ). JSON that callers read as text is `Text`.
A table made by the old DDL and a table made from these declarations are the same table, which is
how revision 0002 can take over a Postgres database that already has them.

The Store's session methods (store.py, factory_store.py, claude_usage_store.py) read and write
these tables, and nothing else does outside `copy.py`, the tests' own seeding and report.py, which
owns `report_weeks`.
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, Boolean, Column, Date, Double, Index, Integer, MetaData, Table,
                        Text, false)

from .types import UTCDateTime

metadata = MetaData()

# Which station a Claude Code session plays (factory_store.py)
session_stations = Table(
    "session_stations", metadata,
    Column("session", Text, primary_key=True),
    Column("station", Text),
    Column("role", Text),
    Column("parent", Text),
    Column("project", Text),
    Column("seen_at", UTCDateTime),
    Column("crew_id", Text),
    Column("runtime", Text),   # `codex` for a Codex station's session; empty for Claude Code (TR-1600)
    Column("model", Text),     # the model a Codex station was started on; empty for its account's default
)

# What a session is doing now (store.py)
session_states = Table(
    "session_states", metadata,
    Column("session", Text, primary_key=True),
    Column("state", Text),
    Column("reason", Text),
    Column("state_at", UTCDateTime),
)

# The sessions that worked in a project (store.py)
project_sessions = Table(
    "project_sessions", metadata,
    Column("project", Text, primary_key=True),
    Column("session", Text, primary_key=True),
    Column("repo", Text),
    Column("linked_at", UTCDateTime),
)
# Not in the old DDL: the lookups by session ("which projects did this session work in") read this
Index("project_sessions_session", project_sessions.c.session)

# One row per model reply (claude_usage_store.py)
session_usage = Table(
    "session_usage", metadata,
    Column("session", Text, primary_key=True),
    Column("message", Text, primary_key=True),
    Column("replied_at", UTCDateTime),
    Column("model", Text),
    Column("project", Text),
    Column("subagent", Text),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    Column("cache_write_tokens", BigInteger),
    Column("cache_read_tokens", BigInteger),
    Column("cost_usd", Double),                 # DOUBLE PRECISION
    Column("cache_write_1h_tokens", BigInteger),
)

# The status line's and /context's latest readings per session (claude_usage_store.py).
# `status` and `context` hold JSON that callers parse themselves, so they are Text.
session_meters = Table(
    "session_meters", metadata,
    Column("session", Text, primary_key=True),
    Column("status", Text),
    Column("status_at", UTCDateTime),
    Column("context", Text),
    Column("context_at", UTCDateTime),
    Column("context_asked_at", UTCDateTime),
    Column("status_person", Text),
)

# What a session's lines add up to (session_ingest.py): written as each claude_code line is
# stored, and caught up from DuckDB at start. Station, role, project and state stay in their own
# tables and are joined by the readers. `lines_complete` says whether `session_lines` holds every
# line DuckDB holds for the session (TR-1478): false for a new row, set by the reconcile.
sessions = Table(
    "sessions", metadata,
    Column("session", Text, primary_key=True),
    Column("source", Text),
    Column("started_at", UTCDateTime),
    Column("last_line_at", UTCDateTime),
    Column("line_count", Integer, nullable=False, server_default="0"),
    Column("tool_calls", Integer, nullable=False, server_default="0"),
    Column("coordination_calls", Integer, nullable=False, server_default="0"),
    Column("lines_complete", Boolean, nullable=False, server_default=false()),
    Column("updated_at", UTCDateTime),
)
Index("sessions_last_line_at", sessions.c.last_line_at)

# The facts the factory reads from a session's lines, written as each line is stored and filled
# from DuckDB at start (session_ingest.py, TR-1477). One row per tool call, by its tool_use id, so
# a resumed session that copies old lines, or a fill that runs twice, adds nothing.
# A protocol message the crew sent (a SendMessage call whose text starts `[TF:<TYPE>]`).
session_messages = Table(
    "session_messages", metadata,
    Column("tool_use_id", Text, primary_key=True),
    Column("source", Text),
    Column("session", Text),
    Column("at", UTCDateTime),
    Column("to_station", Text),
    Column("type", Text),
    Column("text", Text),
)
Index("session_messages_session_at", session_messages.c.session, session_messages.c.at)

# A shell command a session ran (the Bash call) with what it printed once its result arrived.
# The call makes the row; a result sets the exit code, output and time of the row of the call it
# answers and makes none (it does not name its tool). A result that came first is written by the
# fill. The columns can be null (a row from a table this revision found already made).
session_commands = Table(
    "session_commands", metadata,
    Column("tool_use_id", Text, primary_key=True),
    Column("source", Text),
    Column("session", Text),
    Column("at", UTCDateTime),
    Column("command", Text),
    Column("exit_code", Integer),
    Column("output_tail", Text),
    Column("result_at", UTCDateTime),
    # Set by a Codex station's lines only (TR-1595): the folder the command ran in, and how it ended
    # (started, ended, failed, declined, unknown). Empty on a row a Claude Code line made.
    Column("cwd", Text),
    Column("outcome", Text),
)
Index("session_commands_session_at", session_commands.c.session, session_commands.c.at)

# What a person typed, what a session said last, and the files it touched (session_ingest.py,
# TR-1505): written as each line is stored and filled from DuckDB at start, so the factory's
# readers do not scan events.
# A turn the person typed: a `user` line that passes `turn_fact`'s filters. The key is (session,
# line_key) on purpose: a resumed session copies its old lines under a new session id, and the
# events table keeps both copies, so a read that asks for both sessions lists both. `person` is
# the recording's `tares_person` label (null when it carries none).
session_turns = Table(
    "session_turns", metadata,
    Column("session", Text, primary_key=True),
    Column("line_key", Text, primary_key=True),
    Column("source", Text),
    Column("at", UTCDateTime),
    Column("person", Text),
    Column("text", Text),
)
Index("session_turns_session_at", session_turns.c.session, session_turns.c.at)
Index("session_turns_at", session_turns.c.at)

# The last three things a session said (assistant text that is not a tool call, not empty), by
# line; older rows are deleted in the transaction that adds a newer one.
session_said = Table(
    "session_said", metadata,
    Column("session", Text, primary_key=True),
    Column("line_key", Text, primary_key=True),
    Column("source", Text),
    Column("at", UTCDateTime),
    Column("text", Text),
)
Index("session_said_session_at", session_said.c.session, session_said.c.at)

# A file a session edited or wrote, once, with when it last did. `source` is here so a purge of a
# source can delete its rows.
session_files = Table(
    "session_files", metadata,
    Column("session", Text, primary_key=True),
    Column("path", Text, primary_key=True),
    Column("source", Text),
    Column("last_at", UTCDateTime),
)
Index("session_files_session_last_at", session_files.c.session, session_files.c.last_at)

# A claude_code line as it was stored, one row per line, numbered within its session (TR-1478).
# `seq` comes from `sessions.line_count` in the transaction that adds the line, so it is dense and
# unique per session: the lines of a batch get `line_count - n + 1 .. line_count` in order. It is
# the primary key because DuckDB does not deduplicate (a line sent twice is stored twice) and this
# table mirrors it, so the two counts can be compared. `line_key` names the line (its uuid, else
# a hash of its payload) and is not unique. `render` holds the flags the line's text was built with
# (`{"include_thinking": ..., "redact": ...}`), and `payload` the JSON text exactly as DuckDB keeps
# it. The text is not kept: it is rebuilt from the payload with those flags.
session_lines = Table(
    "session_lines", metadata,
    Column("session", Text, primary_key=True),
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("line_key", Text),
    Column("at", UTCDateTime),
    Column("type", Text),
    Column("labels", Text),
    Column("render", Text),
    Column("payload", Text),
)
Index("session_lines_session_at", session_lines.c.session, session_lines.c.at)

# One gzip JSON-lines part of a session's lines that the hourly archive wrote and checked, and then
# deleted from `session_lines` (TR-1506). `first_seq` to `last_seq` are the numbers the part held,
# `lines` how many rows it holds, `bytes` the size of the file, `md5` its MD5 as a hex string and
# `key` where the archive keeps it. A late line makes a later part: `seq` only grows.
session_archive_parts = Table(
    "session_archive_parts", metadata,
    Column("session", Text, primary_key=True),
    Column("part", Integer, primary_key=True, autoincrement=False),
    Column("first_seq", Integer),
    Column("last_seq", Integer),
    Column("lines", Integer),
    Column("bytes", BigInteger),
    Column("md5", Text),
    Column("key", Text),
    Column("written_at", UTCDateTime),
)

# The factory's figures of one project in one ISO week (report.py, TR-1666): worked out from the
# factory's records and `session_usage`, so a report page reads no raw rows. `week` is the week's
# Monday (UTC) and `project` "" holds what belongs to no project. The Text columns are JSON, their
# shapes in report.py; every duration is in hours. A week with nothing to say has no row.
report_weeks = Table(
    "report_weeks", metadata,
    Column("project", Text, primary_key=True),
    Column("week", Date, primary_key=True),
    Column("merged", Integer),
    Column("start_to_merge", Text),
    Column("stage_hours", Text),
    Column("waited_hours", Double),
    Column("milestones_accepted", Integer),
    Column("reviewed", Integer),
    Column("passed_first", Integer),
    Column("rounds", Text),
    Column("findings", Text),
    Column("questions", Text),
    Column("cost_usd", Double),
    Column("unpriced", Integer),
    Column("cost_by_role", Text),
    Column("updated_at", UTCDateTime),
)

# A short text Tares wrote from its figures, once per kind and key (TR-1645): `milestone` and the
# milestone's id, later `week`. `source` is where it came from (report_words.py), `figures` the JSON
# it was written from. Writing it again replaces the row.
report_texts = Table(
    "report_texts", metadata,
    Column("kind", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("text", Text),
    Column("source", Text),
    Column("figures", Text),
    Column("written_at", UTCDateTime),
)

# How far a fill from DuckDB has read: one row per fill, by name (session_ingest.py)
session_ingest_marks = Table(
    "session_ingest_marks", metadata,
    Column("name", Text, primary_key=True),
    Column("at", UTCDateTime),
)

# One row per table copied from DuckDB by copy.py, so a second start copies nothing
session_store_copies = Table(
    "session_store_copies", metadata,
    Column("table_name", Text, primary_key=True),
    Column("rows", Integer),
    Column("copied_at", UTCDateTime),
)

# The tables copy_from_duckdb moves, in the order it moves them
COPIED = (session_stations, session_states, project_sessions, session_usage, session_meters)
