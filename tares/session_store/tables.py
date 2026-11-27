"""The session store's tables, as SQLAlchemy Core declares them (P-TR-295).

These are the five session tables, `settings`, the accounts (`people`, `api_keys`, `project_members`),
the project tables (`usecases`, `usecase_objects`, `usecase_log`, `docs`, `skills`,
`shared_skills`, `catalog_sources`, `github_credentials`, `model_usage`), the tickets (`tickets`,
`ticket_history`, `ticket_numbers`), the milestones, checks and reviews (`milestones`,
`milestone_check_results`, `milestone_acceptances`, `ticket_checks`, `challenges`, `reviews`,
`review_findings`, `rule_proposals`) and the decisions, questions and crew tables (`assumptions`,
`desk_items`, `decisions`, `handovers`, `grants`, `pending_memory`, `crews`, `spec_questions`,
`signoffs`, `handover_overrides`, `answers`, `waiting_told`) and the merge queue, integration,
secret and guardrail tables (`merge_queue`, `project_merge_method`, `project_required_checks`,
`integration_choices`, `integration_connections`, `project_integrations`, `pending_writeback`,
`project_secrets`, `guardrail_rules`, `guardrail_pairs`) and the repo map and brain tables
(`repo_maps`, `repo_map_history`, `brain_repos`, `brain_repo_folders`, `brain_repo_updates`,
`brain_calls`), exactly as they are today in DuckDB and in the state Postgres:
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
                        Text, UniqueConstraint, false)

from .types import JSONText, UTCDateTime

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

# The index the readers' boundary slice uses (TR-1632): the replies of the first, partial hour of a
# window are read from the raw rows by `replied_at`. Not in the old DDL.
Index("session_usage_replied_at", session_usage.c.replied_at)

# What the replies of one session add up to in one hour, by the project each reply was stamped with
# (TR-1632). The usage readers sum these instead of every reply: `station_rollup`'s spend and
# `usage_by_project`. `hour` is the start of the hour in UTC and `project` is '' for a reply with
# none (a primary key cannot hold NULL). `cost_usd` is NULL while every reply of the bucket is
# unpriced, so a sum over buckets orders as a sum over replies does. A bucket is rebuilt from the
# raw rows in the transaction that writes a reply (claude_usage_hourly.py), never changed by a delta.
session_usage_hourly = Table(
    "session_usage_hourly", metadata,
    Column("session", Text, primary_key=True),
    Column("hour", UTCDateTime, primary_key=True),
    Column("project", Text, primary_key=True, server_default=""),
    Column("replies", BigInteger, nullable=False),
    Column("input_tokens", BigInteger, nullable=False),
    Column("output_tokens", BigInteger, nullable=False),
    Column("cache_write_tokens", BigInteger, nullable=False),
    Column("cache_read_tokens", BigInteger, nullable=False),
    Column("cost_usd", Double),
    Column("unpriced", BigInteger, nullable=False),
)
Index("session_usage_hourly_hour", session_usage_hourly.c.hour)

# How far the fill of `session_usage_hourly` has got (TR-1632): one row, `usage_hourly`. `cursor` is
# the last session rebuilt (sessions go in order; null before the first chunk), `ready_at` is set
# once the buckets were checked against the raw rows. The readers use the buckets only when it is set.
session_rollup_state = Table(
    "session_rollup_state", metadata,
    Column("name", Text, primary_key=True),
    Column("cursor", Text),
    Column("ready_at", UTCDateTime),
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
# `lines_origin` (TR-1606) says how that is judged: null (a row counted from the events) compares
# with DuckDB's count; 'table' (a session born in `session_lines`) is complete when the table and
# the archive hold `line_count` lines, with no DuckDB read.
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
    Column("lines_origin", Text),
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

# The one-time move of recent sessions' lines into the session store (session_move.py, TR-1609): one
# row per claude_code session DuckDB holds lines for. `state` is pending, verified or failed;
# `target` is table or archive; `lines_events` is DuckDB's line count, `lines_after` the rows the
# move wrote, `error` why a move failed. Not in MOVED_TABLES: nothing is copied from DuckDB into it.
session_moves = Table(
    "session_moves", metadata,
    Column("session", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("target", Text),
    Column("lines_events", Integer),
    Column("lines_after", Integer),
    Column("started_at", UTCDateTime),
    Column("verified_at", UTCDateTime),
    Column("error", Text),
    Column("attempts", Integer),
)
Index("session_moves_state", session_moves.c.state)

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

# The instance's settings (store.py get_setting / set_setting), the first table of the move out of
# DuckDB (TR-1480). `value` is the text the caller stored (often JSON it parses itself).
settings = Table(
    "settings", metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
    Column("updated_at", UTCDateTime),
)

# Who is on the cell (store.py create_person and the rest, TR-1608). `emails` is the extra
# addresses as JSON; a person who left keeps the row with `removed_at` set.
people = Table(
    "people", metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("email", Text),
    Column("emails", JSONText),
    Column("github", Text),
    Column("is_admin", Boolean, server_default=false()),
    Column("created_at", UTCDateTime),
    Column("created_via", Text),
    Column("picture", Text),
    Column("removed_at", UTCDateTime),
)

# The cell's scoped credentials (store.py insert_api_key and the rest, TR-1608): only the SHA-256
# of the secret is kept. `scopes` is a JSON list; `project` makes a project key and `person_id` binds
# the key to a person.
api_keys = Table(
    "api_keys", metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text),
    Column("prefix", Text),
    Column("hash", Text),
    Column("scopes", JSONText),
    Column("created_at", UTCDateTime),
    Column("last_used_at", UTCDateTime),
    Column("revoked_at", UTCDateTime),
    Column("project", Text),
    Column("person_id", Text),
)

# Who is on a project besides its lead (store.py add_project_member, TR-1608): reason `manual` or
# `ticket`. The lead is not a row.
project_members = Table(
    "project_members", metadata,
    Column("project", Text, primary_key=True),
    Column("person_id", Text, primary_key=True),
    Column("added_by", Text),
    Column("added_at", UTCDateTime),
    Column("reason", Text),
)

# Projects (store.py create_project, TR-1610). The tables and columns predate the rename (use case,
# recipe): a row here is a project, `recipe` its template. `params`, `setup`, `linear` and
# `github_issues` are JSON the Store parses, so they are `JSONText`.
usecases = Table(
    "usecases", metadata,
    Column("id", Text, primary_key=True),
    Column("recipe", Text),
    Column("name", Text),
    Column("params", JSONText),
    Column("status", Text),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("last_error", Text),
    Column("goal", Text),
    Column("setup", JSONText),
    Column("linear", JSONText),
    Column("github_issues", JSONText),
    Column("kind", Text),
    Column("lead_person_id", Text),
    Column("scope", Text),
    Column("icon", Text),
)

# The parts a project uses: kind source, trigger, agent, mcp_server, skill or doc, and the plan key
# of a template project.
usecase_objects = Table(
    "usecase_objects", metadata,
    Column("usecase_id", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("name", Text),
    Column("customized", Boolean),
    Column("created_at", UTCDateTime),
)

# What happened to a project (create, update, repair), newest first when read
usecase_log = Table(
    "usecase_log", metadata,
    Column("usecase_id", Text),
    Column("logged_at", UTCDateTime),
    Column("action", Text),
    Column("detail", Text),
)

# Markdown that belongs to the cell, included by projects through usecase_objects (kind doc)
docs = Table(
    "docs", metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text),
    Column("title", Text),
    Column("body", Text),
    Column("made_by", Text),
    Column("updated_by", Text),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("owner", Text),
)

# The per-project skills from before skills were shared: nothing reads it any more, it moves with
# the rest so no table is left behind
skills = Table(
    "skills", metadata,
    Column("project", Text, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("description", Text),
    Column("body", Text),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
)

# Skills that belong to the cell, used by projects through usecase_objects (kind skill)
shared_skills = Table(
    "shared_skills", metadata,
    Column("name", Text, primary_key=True),
    Column("description", Text),
    Column("body", Text),
    Column("made_by", Text),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# The cell's sources. `config` is the connector's settings, JSON the Store parses.
catalog_sources = Table(
    "catalog_sources", metadata,
    Column("name", Text, primary_key=True),
    Column("type", Text),
    Column("connector", Text),
    Column("poll", Text),
    Column("config", JSONText),
    Column("paused", Boolean, server_default=false()),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("ingest_key", Text),
    Column("owned_by", Text),
    Column("customized", Boolean),
)

# A GitHub token or App stored once and named by sources and MCP servers. `config` is JSON text the
# Store reads leniently (a row with text that is not JSON reads as {}), so it stays `Text`.
github_credentials = Table(
    "github_credentials", metadata,
    Column("name", Text, primary_key=True),
    Column("kind", Text),
    Column("token", Text),
    Column("api_url", Text),
    Column("account", Text),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("config", Text),
)

# The model-usage ledger: one row per model interaction, priced when written
model_usage = Table(
    "model_usage", metadata,
    Column("id", Text, primary_key=True),
    Column("ts", UTCDateTime),
    Column("surface", Text),
    Column("agent", Text),
    Column("run_id", Text),
    Column("model", Text),
    Column("calls", Integer),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    Column("cache_creation_input_tokens", BigInteger),
    Column("cache_read_input_tokens", BigInteger),
    Column("cost_usd", Double),
    Column("key_source", Text),
    Column("provider", Text),
)
Index("ix_model_usage_ts", model_usage.c.ts)

# A project's list of work (store.py). `owner` says where a ticket lives (linear, github, tares);
# `depends_on` is [ticket id], `pr` the pull request as Tares last read it, `pr_repo` and `pr_number`
# its repo and number as columns (pr_columns.py), never part of the ticket a caller reads
tickets = Table(
    "tickets", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("owner", Text),
    Column("external_id", Text),
    Column("identifier", Text),
    Column("url", Text),
    Column("title", Text),
    Column("status", Text),
    Column("position", Double),
    Column("working_doc", Text),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("milestone", Text),
    Column("depends_on", JSONText),
    Column("number", Integer),
    Column("holder", Text),
    Column("stage", Text),
    Column("stage_reason", Text),
    Column("pr", JSONText),
    Column("repo", Text),
    Column("assignee_person_id", Text),
    Column("assignee_raw", Text),
    Column("pr_repo", Text),
    Column("pr_number", BigInteger),
)
Index("tickets_project", tickets.c.project)
Index("tickets_pr", tickets.c.pr_repo, tickets.c.pr_number)

# Every change to a ticket's holder or stage (factory_store.py). No key: two lines can be alike
ticket_history = Table(
    "ticket_history", metadata,
    Column("ticket", Text),
    Column("project", Text),
    Column("changed_at", UTCDateTime),
    Column("field", Text),
    Column("value", Text),
    Column("reason", Text),
    Column("by_station", Text),
    Column("by_role", Text),
    Column("person_id", Text),
)
Index("ticket_history_ticket", ticket_history.c.ticket)

# The last ticket number a project gave out: numbers are never given twice
ticket_numbers = Table(
    "ticket_numbers", metadata,
    Column("project", Text, primary_key=True),
    Column("last", Integer),
)

# A project's milestones (factory_store.py). `owner` says where one lives (linear, github, tares);
# `checks` is [{check, for}]
milestones = Table(
    "milestones", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("owner", Text),
    Column("external_id", Text),
    Column("name", Text),
    Column("goal", Text),
    Column("checks", JSONText),
    Column("position", Double),
    Column("created_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
)

# Every recorded try of a milestone's acceptance check; the latest of an index counts
milestone_check_results = Table(
    "milestone_check_results", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("milestone", Text),
    Column("idx", Integer),
    Column("result", Text),
    Column("note", Text),
    Column("by_kind", Text),
    Column("by_name", Text),
    Column("person_id", Text),
    Column("checked_at", UTCDateTime),
)

# A person accepted a milestone as it is: one row per milestone
milestone_acceptances = Table(
    "milestone_acceptances", metadata,
    Column("milestone", Text, primary_key=True),
    Column("project", Text),
    Column("by_name", Text),
    Column("person_id", Text),
    Column("accepted_at", UTCDateTime),
)

# The checks a builder ran on a ticket
ticket_checks = Table(
    "ticket_checks", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("ticket", Text),
    Column("command", Text),
    Column("result", Text),
    Column("commit_sha", Text),
    Column("broke_test", Text),
    Column("by_station", Text),
    Column("by_session", Text),
    Column("checked_at", UTCDateTime),
    Column("person_id", Text),
)

# The challenger's lines, copied from the events as they arrive and looked up by branch
challenges = Table(
    "challenges", metadata,
    Column("source", Text, primary_key=True),
    Column("key_value", Text, primary_key=True),
    Column("event_time", UTCDateTime, primary_key=True),
    Column("event_type", Text, primary_key=True),
    Column("branch", Text, primary_key=True),
    Column("tares_project", Text),
    # the line's payload as it arrived. Text, not JSONText: a line that is not JSON must not fail
    # a write, a copy or a read, so factory_store parses it and reads such a one as {} (TR-1705)
    Column("payload", Text),
)
Index("ix_challenges_branch", challenges.c.source, challenges.c.branch)

# A ticket's review rounds, and each round's findings (numbered on per ticket)
reviews = Table(
    "reviews", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("ticket", Text),
    Column("round", Integer),
    Column("pr", Text),
    Column("head", Text),
    Column("verdict", Text),
    Column("verified", Text),
    Column("not_verified", Text),
    Column("by_station", Text),
    Column("reviewed_at", UTCDateTime),
    Column("person_id", Text),
)

review_findings = Table(
    "review_findings", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("ticket", Text),
    Column("review", Text),
    Column("number", Integer),
    Column("severity", Text),
    Column("file", Text),
    Column("line", Integer),
    Column("blocking", Boolean),
    Column("text", Text),
    Column("kind", Text),
    Column("state", Text),
    Column("fixed_in", Text),
    Column("found_at", UTCDateTime),
)

# A rule offered because a mistake came back; `tickets` is [ticket label]
rule_proposals = Table(
    "rule_proposals", metadata,
    Column("id", Text, primary_key=True),
    Column("repo", Text),
    Column("kind", Text),
    Column("project", Text),
    Column("scope", Text),
    Column("text", Text),
    Column("tickets", JSONText),
    Column("state", Text),
    Column("created_at", UTCDateTime),
    Column("decided_at", UTCDateTime),
)

# What a builder assumed instead of asking, and what the person decided; `tickets` is [ticket]
assumptions = Table(
    "assumptions", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("number", Integer),
    Column("question", Text),
    Column("choice", Text),
    Column("why", Text),
    Column("affects", Text),
    Column("tickets", JSONText),
    Column("state", Text),
    Column("words", Text),
    Column("batch", Text),
    Column("made_by", Text),
    Column("made_role", Text),
    Column("made_at", UTCDateTime),
    Column("decided_at", UTCDateTime),
    Column("person_id", Text),
)

# The desk: the questions waiting for a person
desk_items = Table(
    "desk_items", metadata,
    Column("id", Text, primary_key=True),
    Column("number", Integer),
    Column("project", Text),
    Column("question", Text),
    Column("context", Text),
    Column("options", JSONText),
    Column("recommendation", Text),
    Column("why", Text),
    Column("blocks", Text),
    Column("tickets", JSONText),
    Column("blocking", Boolean),
    Column("asked_by", Text),
    Column("assumptions", JSONText),
    Column("state", Text),
    Column("reason", Text),
    Column("created_at", UTCDateTime),
    Column("closed_at", UTCDateTime),
    Column("station", Text),
    Column("command", Text),
    Column("person_id", Text),
    Column("to_person_id", Text),
)

# The person's answers, in their words
decisions = Table(
    "decisions", metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text),
    Column("desk_item", Text),
    Column("question", Text),
    Column("words", Text),
    Column("choice", Text),
    Column("standing", Boolean),
    Column("scope", Text),
    Column("matched", Boolean),
    Column("session", Text),
    Column("granted", Boolean),
    Column("tickets", JSONText),
    Column("decided_at", UTCDateTime),
    Column("via", Text),
    Column("person_id", Text),
)

# A project handed to the crew, and what became of it; `repos` is [{path, name}]
handovers = Table(
    "handovers", metadata,
    Column("project", Text, primary_key=True),
    Column("repo", Text),
    Column("handed_by", Text),
    Column("handed_at", UTCDateTime),
    Column("repos", JSONText),
    Column("autonomy", Text),
    Column("finished_at", UTCDateTime),
    Column("finished_words", Text),
    Column("finished_decision", Text),
    Column("finished_by", Text),
    Column("taken_at", UTCDateTime),
    Column("taken_words", Text),
    Column("taken_by", Text),
    Column("released_at", UTCDateTime),
    Column("person_id", Text),
    Column("finished_person_id", Text),
    Column("taken_person_id", Text),
    Column("crew", Text),
    Column("override_reason", Text),
    Column("override_at", UTCDateTime),
    Column("override_person_id", Text),
)

# Standing permissions; `areas` and `consents` are lists. `needs_lead` keeps the default the old DDL
# gave it, as `attempts` does below: the code before revision 0020 reads the same table
grants = Table(
    "grants", metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text),
    Column("project", Text),
    Column("holder_person_id", Text),
    Column("words", Text),
    Column("given_by", Text),
    Column("given_at", UTCDateTime),
    Column("areas", JSONText),
    Column("consents", JSONText),
    Column("state", Text),
    Column("needs_lead", Boolean, server_default=false()),
)

# Memory lines that could not be written yet; `scope` is a project or `global`
pending_memory = Table(
    "pending_memory", metadata,
    Column("id", Text, primary_key=True),
    Column("scope", Text),
    Column("line", Text),
    Column("attempts", Integer, server_default="0"),
    Column("last_error", Text),
    Column("created_at", UTCDateTime),
    Column("tried_at", UTCDateTime),
)

# One person's crew on one machine
crews = Table(
    "crews", metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text),
    Column("person_id", Text),
    Column("machine", Text),
    Column("first_seen", UTCDateTime),
    Column("last_seen", UTCDateTime),
    UniqueConstraint("person_id", "name", "machine", name="crews_person_id_name_machine_key"),
)

# Questions the spec session asks a person; numbered per project (Q1, Q2)
spec_questions = Table(
    "spec_questions", metadata,
    Column("project", Text, primary_key=True),
    Column("id", Text, primary_key=True),
    Column("to_person_id", Text),
    Column("repo", Text),
    Column("text", Text),
    Column("asked_by", Text),
    Column("asked_at", UTCDateTime),
    Column("answer", Text),
    Column("answered_by", Text),
    Column("answered_at", UTCDateTime),
    Column("state", Text),
    Column("via", Text),
)

# Repo owners' sign-offs. Nothing reads or writes them any more; the rows are kept
signoffs = Table(
    "signoffs", metadata,
    Column("project", Text, primary_key=True),
    Column("repo", Text, primary_key=True),
    Column("person_id", Text),
    Column("at", UTCDateTime),
    Column("via", Text),
)

# Every hand-over that went ahead over a refusal, oldest kept. It has no key: two rows may be equal
handover_overrides = Table(
    "handover_overrides", metadata,
    Column("project", Text),
    Column("at", UTCDateTime),
    Column("reason", Text),
    Column("person_id", Text),
    Column("handed_by", Text),
)

# The answers given to what waited for a person
answers = Table(
    "answers", metadata,
    Column("id", Text, primary_key=True),
    Column("item", Text),
    Column("kind", Text),
    Column("project", Text),
    Column("ask", Text),
    Column("choice", Text),
    Column("said", Text),
    Column("via", Text),
    Column("person_id", Text),
    Column("answered_at", UTCDateTime),
)
Index("idx_answers_at", answers.c.answered_at)

# Which waiting items each person was told of, and the Slack message that told them
waiting_told = Table(
    "waiting_told", metadata,
    Column("person_id", Text, primary_key=True),
    Column("item", Text, primary_key=True),
    Column("told_at", UTCDateTime),
    Column("sent", Boolean),
    Column("ask", Text),
    Column("channel", Text),
    Column("ts", Text),
    Column("seq", Integer),
    Column("closed", Text),
)

# Your own deny rules for the crew's guardrails (TR-862): a sentence a station's soft_deny list
# gets, for every project (scope factory, project NULL) or one project. Allow rules are the
# grants (the `grants` table), read in place; there is no allow row here.
guardrail_rules = Table(
    "guardrail_rules", metadata,
    Column("id", Text, primary_key=True),
    Column("scope", Text),
    Column("project", Text),
    Column("kind", Text),
    Column("text", Text),
    Column("created_by", Text),
    Column("created_at", UTCDateTime),
)

# Guardrail pairs (TR-863): a hook point paired with a guardrail integration, for every project
# (scope factory, project_uid NULL) or one project. `settings` is not used any more (it is {},
# TR-1380): a pair runs with the integration's connection at the factory. on_fail says whether a
# guardrail that denies, or does not answer, blocks or only warns.
guardrail_pairs = Table(
    "guardrail_pairs", metadata,
    Column("id", Text, primary_key=True),
    Column("scope", Text),
    Column("project_uid", Text),
    Column("hook", Text),
    Column("integration", Text),
    Column("settings", JSONText),
    Column("on_fail", Text, server_default="block"),
    Column("created_at", UTCDateTime),
    Column("created_by", Text),
)

# The default integration of each slot (TR-839, TR-1380): one row per slot, a pointer to one of
# the slot's connections (below). No row means the slot's built-in default. `settings` is not used
# any more (it is {}): an earlier version kept the integration settings here, and the upgrade moves
# them to a connection.
integration_choices = Table(
    "integration_choices", metadata,
    Column("slot", Text, primary_key=True),
    Column("name", Text),
    Column("settings", JSONText),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# The integrations the factory has connected (TR-1380): one row per slot and integration, with its
# integration settings, secrets included. Credentials live only here. memory, monitoring and
# runtime keep one connection each.
integration_connections = Table(
    "integration_connections", metadata,
    Column("slot", Text, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("settings", JSONText),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# A project's own choice for the tickets, review and ci slots: the name of a connected integration
# plus the project's own fields. It holds no secret and no address (TR-1380); those come from the
# factory's connection.
project_integrations = Table(
    "project_integrations", metadata,
    Column("project_uid", Text, primary_key=True),
    Column("slot", Text, primary_key=True),
    Column("name", Text),
    Column("settings", JSONText),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# A status change a ticket system refused or could not be reached for (TR-845). One row per ticket
# and integration, the status wanted last; the row is retried on each refresh and goes when the
# write lands. While a row exists a sync leaves the ticket's status alone.
pending_writeback = Table(
    "pending_writeback", metadata,
    Column("ticket", Text, primary_key=True),
    Column("integration", Text, primary_key=True),
    Column("status", Text),
    Column("attempts", Integer),
    Column("last_error", Text),
    Column("updated_at", UTCDateTime),
)

# A project's secrets (TR-775): the value sealed with the cell's TARES_SECRETS_KEY, never in the
# clear; who set it and when; who last fetched it for a command and when.
project_secrets = Table(
    "project_secrets", metadata,
    Column("project", Text, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("value", Text),
    Column("set_by", Text),
    Column("set_at", UTCDateTime),
    Column("used_by", Text),
    Column("used_at", UTCDateTime),
)

# The commands a project's local ci integration wants recorded as passing on a pull request's head
# (TR-854): one row per project, the commands as a JSON list.
project_required_checks = Table(
    "project_required_checks", metadata,
    Column("project", Text, primary_key=True),
    Column("commands", JSONText),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# The merge queue (TR-856): one row per pull request Tares itself will merge, in order per
# (repo, base). state: queued, waiting_ci, waiting_approval, merging, merged, sent_back, blocked.
# before_files (TR-858) is the files (and head) the pull request had before its branch was last
# brought up to date, as JSON text the caller writes and reads; unknown_ticks is how many ticks in
# a row GitHub could not say whether it merges.
merge_queue = Table(
    "merge_queue", metadata,
    Column("id", Text, primary_key=True),
    Column("repo", Text),
    Column("base", Text),
    Column("pr", Integer),
    Column("head", Text),
    Column("ticket", Text),
    Column("project_uid", Text),
    Column("state", Text),
    Column("reason", Text),
    Column("desk_item", Text),
    Column("approved_head", Text),
    Column("entered_at", UTCDateTime),
    Column("updated_at", UTCDateTime),
    Column("before_files", Text),
    Column("unknown_ticks", Integer),
)
Index("idx_merge_queue_repo", merge_queue.c.repo, merge_queue.c.base)

# How a project's pull requests are merged: squash, merge or rebase. No row means squash.
project_merge_method = Table(
    "project_merge_method", metadata,
    Column("project", Text, primary_key=True),
    Column("method", Text),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# A map of one GitHub repo (TR-681), shared by every project that names it
repo_maps = Table(
    "repo_maps", metadata,
    Column("repo", Text, primary_key=True),
    Column("body", Text),
    Column("updated_at", UTCDateTime),
    Column("updated_by", Text),
)

# A dated line for each change of a repo map; no primary key, as the old DDL made it
repo_map_history = Table(
    "repo_map_history", metadata,
    Column("repo", Text),
    Column("changed_at", UTCDateTime),
    Column("by_station", Text),
    Column("project", Text),
    Column("note", Text),
)

# The repos a cell connected to the brain (TR-1197, ee): owner/name in lowercase, the id of the
# repo source lore made for it, and how far its index is. The ee routes in
# ee/tares_ee/brain/repos.py read and write it; the table exists on every cell. `place` is the
# place in the control plane's queue while an index job waits, and `job_started_at` when the cell
# asked for it (NULL until it did, so a repo is asked for once) (TR-1198). `sync_failures` and
# `sync_error` say how the keep-up sync of a ready repo is doing; a success clears both (TR-1201).
brain_repos = Table(
    "brain_repos", metadata,
    Column("repo", Text, primary_key=True),
    Column("repo_source_id", Text),
    Column("state", Text),
    Column("files_done", Integer, server_default="0"),
    Column("files_total", Integer, server_default="0"),
    Column("indexed_commit", Text),
    Column("last_synced", UTCDateTime),
    Column("error", Text),
    Column("added_by", Text),
    Column("added_at", UTCDateTime),
    Column("place", Integer),
    Column("job_started_at", UTCDateTime),
    Column("sync_failures", Integer, server_default="0"),
    Column("sync_error", Text),
)

# The decision folders an admin chose for a connected repo (TR-1209, ee): one row per folder, with
# who chose it and when. The files in them are the repo's decisions in the brain.
brain_repo_folders = Table(
    "brain_repo_folders", metadata,
    Column("repo", Text, primary_key=True),
    Column("path", Text, primary_key=True),
    Column("chosen_by", Text),
    Column("chosen_at", UTCDateTime),
)

# Each first index and each later update of a connected repo (TR-1463, ee): when, which commit, how
# many files it changed or deleted, the repo's file count after it, and whether it worked (and why
# not). The newest 50 per repo are kept; the Repos page shows the last one and a history.
brain_repo_updates = Table(
    "brain_repo_updates", metadata,
    Column("id", Text, primary_key=True),
    Column("repo", Text),
    Column("at", UTCDateTime),
    Column("kind", Text),
    Column("ok", Integer),
    Column("indexed_commit", Text),
    Column("files_changed", Integer),
    Column("files_deleted", Integer),
    Column("files_total", Integer),
    Column("error", Text),
)
Index("idx_brain_repo_updates_repo", brain_repo_updates.c.repo, brain_repo_updates.c["at"])

# Every question, search and document read a cell sent to the brain (TR-1200, ee): who asked (the
# station and person the call came with, `via` mcp or console), what, which repos it used, how long
# it took and the tokens lore reported. The daily caps count the ask rows. `repos` is a JSON string
# the Store parses itself: a text that is not JSON reads as no repos.
brain_calls = Table(
    "brain_calls", metadata,
    Column("id", Text, primary_key=True),
    Column("at", UTCDateTime),
    Column("tool", Text),
    Column("deep", Integer, server_default="0"),
    Column("question", Text),
    Column("repos", Text),
    Column("duration_ms", Integer),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("ok", Integer),
    Column("error", Text),
    Column("station", Text),
    Column("person_id", Text),
    Column("via", Text),
)

# The tables that live in the session store and no longer in DuckDB, in the order copy_from_duckdb
# moves them. A table here is created by its Alembic revision, taken over in place on Postgres,
# copied from DuckDB and verified on SQLite, and left out of the old DDL, its ALTERs and
# `state_db.move`. Moving a table means adding it here and a revision that takes it over.
MOVED_TABLES = (session_stations, session_states, project_sessions, session_usage, session_meters,
                settings, people, api_keys, project_members, usecases, usecase_objects, usecase_log,
                docs, skills, shared_skills, catalog_sources, github_credentials, model_usage,
                tickets, ticket_history, ticket_numbers,
                milestones, milestone_check_results, milestone_acceptances, ticket_checks,
                challenges, reviews, review_findings, rule_proposals,
                assumptions, desk_items, decisions, handovers, grants, pending_memory, crews,
                spec_questions, signoffs, handover_overrides, answers, waiting_told,
                guardrail_rules, guardrail_pairs, integration_choices, integration_connections,
                project_integrations, pending_writeback, project_secrets, project_required_checks,
                merge_queue, project_merge_method,
                repo_maps, repo_map_history, brain_repos, brain_repo_folders, brain_repo_updates,
                brain_calls)
MOVED_TABLE_NAMES = frozenset(t.name for t in MOVED_TABLES)
