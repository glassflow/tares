"""The session store's tables, as SQLAlchemy Core declares them (P-TR-295).

These are the five session tables exactly as they are today in DuckDB and in the state Postgres:
same names, same columns, same primary keys, and the column types `state_db.state_ddl` gives them on
Postgres (TEXT, BIGINT, DOUBLE PRECISION, TIMESTAMPTZ). JSON that callers read as text is `Text`.
A table made by the old DDL and a table made from these declarations are the same table, which is
how revision 0002 can take over a Postgres database that already has them.

The Store's session methods (store.py, factory_store.py, claude_usage_store.py) read and write
these tables, and nothing else does outside `copy.py` and the tests' own seeding.
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, Boolean, Column, Double, Index, Integer, MetaData, Table, Text,
                        false)

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
# tables and are joined by the readers. `lines_complete` is set by the ticket that fills it
# (TR-1478); until then it stays false.
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
# The result can come first (a later batch, another POST): the row is made by whichever line
# arrives first, and `command` stays empty until the call does. A row without a `command` is not
# a command yet and no reader lists it.
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
)
Index("session_commands_session_at", session_commands.c.session, session_commands.c.at)

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
