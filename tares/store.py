"""DuckDB store — the collapsed Bronze+Silver. One events table; lossless via the JSON payload.

taresd is the sole owner of this connection (DuckDB is single-writer). All reads and writes go
through here; the MCP server never touches the DB directly.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime

import duckdb

from .envelope import Envelope, now_utc

# the URL a Tares agent's subscription had before the wiring moved onto projects (config.agent_url)
AGENT_PREFIX = "tares://agent/"
from .reads import parse_window

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  source       TEXT,
  source_type  TEXT,
  key_value    TEXT,
  event_type   TEXT,
  text         TEXT,
  payload      JSON,
  event_time   TIMESTAMPTZ,
  ingest_time  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS cursors (
  source TEXT PRIMARY KEY,
  cursor TEXT
);
CREATE TABLE IF NOT EXISTS source_stats (
  source      TEXT PRIMARY KEY,
  events      BIGINT,
  last_ingest TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS entity_counts (
  source      TEXT,
  label       TEXT,
  value       TEXT,
  events      BIGINT,
  last_ingest TIMESTAMPTZ,
  PRIMARY KEY (source, label, value)
);
CREATE INDEX IF NOT EXISTS ix_entity_counts_label ON entity_counts(label);
CREATE TABLE IF NOT EXISTS entity_label_state (
  source    TEXT,
  label     TEXT,
  truncated BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (source, label)
);
CREATE TABLE IF NOT EXISTS trigger_state (
  trigger    TEXT,
  key_value  TEXT,
  last_fired TIMESTAMPTZ,
  PRIMARY KEY (trigger, key_value)
);
CREATE TABLE IF NOT EXISTS subscriptions (
  subscription_id TEXT PRIMARY KEY,
  trigger         TEXT,
  url             TEXT,
  created_at      TIMESTAMPTZ,
  created_by      TEXT
);
CREATE TABLE IF NOT EXISTS dispatch_deliveries (
  dispatch_id     TEXT,
  subscription_id TEXT,
  url             TEXT,
  ok              BOOLEAN,
  delivered_at    TIMESTAMPTZ,
  error           TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
  id           TEXT PRIMARY KEY,
  name         TEXT,
  prefix       TEXT,
  hash         TEXT,
  scopes       JSON,
  created_at   TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS catalog_sources (
  name       TEXT PRIMARY KEY,
  type       TEXT,
  connector  TEXT,
  poll       TEXT,
  config     JSON,
  paused     BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ,
  ingest_key TEXT
);
CREATE TABLE IF NOT EXISTS catalog_triggers (
  name       TEXT PRIMARY KEY,
  sources    JSON,
  filters    JSON,
  key_field  TEXT,
  condition  JSON,
  emit       JSON,
  cooldown   TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ,
  paused     BOOLEAN DEFAULT FALSE
);
-- A Tares agent: the DEFINITION only (prompt + optional Slack). Whether it's enabled is not a
-- column — an agent is enabled exactly when it has a subscription to its trigger, the same wiring
-- an external agent has. That keeps one source of truth for "will it be woken" (subscriptions).
CREATE TABLE IF NOT EXISTS catalog_agents (
  name          TEXT PRIMARY KEY,
  trigger       TEXT,
  prompt        TEXT,
  slack_webhook TEXT,
  created_at    TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id          TEXT PRIMARY KEY,
  agent       TEXT,
  trigger     TEXT,
  dispatch_id TEXT,
  key_value   TEXT,
  status      TEXT,
  rounds      INTEGER,
  tool_calls  INTEGER,
  prompt_hash TEXT,
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  duration_ms INTEGER,
  finding     TEXT,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_agent ON agent_runs(agent);
-- Model-usage ledger: one row per model interaction (an agent run's whole loop, an Ask turn),
-- with token counts straight from the API's usage block and the USD cost priced at write time
-- (tares/pricing.py). This is the cell's own meter for what it spends on Anthropic. A hosted
-- control plane reads the aggregate over /api/usage/model to enforce credits, but the cell
-- attaches no meaning to the total. cost_usd is NULL when the model has no known price.
-- (No semicolons in these comments: _SCHEMA is split on them.)
CREATE TABLE IF NOT EXISTS model_usage (
  id       TEXT PRIMARY KEY,
  ts       TIMESTAMPTZ,
  surface  TEXT,
  agent    TEXT,
  run_id   TEXT,
  model    TEXT,
  calls    INTEGER,
  input_tokens  BIGINT,
  output_tokens BIGINT,
  cache_creation_input_tokens BIGINT,
  cache_read_input_tokens     BIGINT,
  cost_usd DOUBLE,
  key_source TEXT
);
CREATE INDEX IF NOT EXISTS ix_model_usage_ts ON model_usage(ts);
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS mcp_servers (
  name        TEXT PRIMARY KEY,
  url         TEXT,
  auth_header TEXT,
  auth_value  TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS github_credentials (
  name        TEXT PRIMARY KEY,
  kind        TEXT,
  token       TEXT,
  api_url     TEXT,
  account     TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ask_sessions (
  id         TEXT PRIMARY KEY,
  title      TEXT,
  state      TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
-- scope: what was read, the trigger or agent tool, "(read)" for a raw read
CREATE TABLE IF NOT EXISTS query_log (
  id            TEXT PRIMARY KEY,
  scope         TEXT,
  key_value     TEXT,
  time_window   TEXT,
  rows_returned INTEGER,
  client        TEXT,
  queried_at    TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS dispatch_log (
  dispatch_id TEXT PRIMARY KEY,
  trigger     TEXT,
  key_value   TEXT,
  kind        TEXT,
  fired_at    TIMESTAMPTZ,
  subscribers INTEGER,
  delivered   INTEGER,
  payload     TEXT
);
-- Projects: a template (code) instantiated with params. Table and column names predate the
-- rename (use case, recipe) while the API says project and template. Every trigger, agent and MCP
-- server belongs to exactly one project (owned_by on those tables). A source is shared, and
-- usecase_objects says which projects it is in (its owned_by only records which created it).
-- usecase_objects also maps the template plan keys to the real object names so a re-plan can
-- diff against what exists. The default project (settings key default_project) holds whatever
-- no other project does.
CREATE TABLE IF NOT EXISTS usecases (
  id         TEXT PRIMARY KEY,
  recipe     TEXT,
  name       TEXT,
  params     JSON,
  status     TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS usecase_objects (
  usecase_id TEXT,
  kind       TEXT,
  key        TEXT,
  name       TEXT,
  customized BOOLEAN,
  created_at TIMESTAMPTZ,
  PRIMARY KEY (usecase_id, kind, key)
);
CREATE TABLE IF NOT EXISTS usecase_log (
  usecase_id TEXT,
  logged_at  TIMESTAMPTZ,
  action     TEXT,
  detail     TEXT
);
-- Skills (TR-332): instructions a project's agents load by name when a task matches the
-- description. `project` is the project id. Deleted with the project (tares/skills.py).
-- The wiring a project holds (P-TR-216): parts belong to the cell, and each project says how it
-- uses them. kind 'wake': in `project`, trigger `trigger` wakes agent `agent` (`enabled`: on in
-- this project). kind 'handoff': in `project`, when `from_agent` concludes `verdict`, `agent`
-- digs in, at most once per `cooldown` per entity. An agent wired in two projects runs once per
-- firing, for both.
CREATE TABLE IF NOT EXISTS project_wiring (
  project    TEXT,
  kind       TEXT,
  trigger    TEXT,
  agent      TEXT,
  from_agent TEXT,
  verdict    TEXT,
  cooldown   TEXT,
  enabled    BOOLEAN,
  created_at TIMESTAMPTZ
);
-- Which projects a run and a firing belong to: the projects whose wiring started them. The
-- `project` column on agent_runs and dispatch_log stays as the first of them.
CREATE TABLE IF NOT EXISTS run_projects (
  run_id  TEXT,
  project TEXT,
  PRIMARY KEY (run_id, project)
);
CREATE TABLE IF NOT EXISTS dispatch_projects (
  dispatch_id TEXT,
  project     TEXT,
  PRIMARY KEY (dispatch_id, project)
);
-- Skills shared by the cell (P-TR-216): one row per skill, and the projects that use one list it
-- in usecase_objects (kind 'skill'). `made_by`: the project that wrote it. The per-project
-- `skills` table below is what they were before: read once by the upgrade, then left alone.
CREATE TABLE IF NOT EXISTS shared_skills (
  name        TEXT PRIMARY KEY,
  description TEXT,
  body        TEXT,
  made_by     TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
);
-- Docs (TR-403): markdown that belongs to the cell, included by projects through usecase_objects
-- (kind 'doc', key 'doc:<id>'), like skills. `made_by` is the project that wrote it, `updated_by`
-- who changed it last (a key's name, "console", a session). Kinds in tares/docs.py.
CREATE TABLE IF NOT EXISTS docs (
  id          TEXT PRIMARY KEY,
  kind        TEXT,
  title       TEXT,
  body        TEXT,
  made_by     TEXT,
  updated_by  TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
);
-- Tickets (TR-403): a project's list of work. owner 'linear' is a record of a Linear issue, written
-- only by the Linear sync (external_id = the issue id, identifier = e.g. ENG-12). owner 'tares'
-- means the project has no Linear and Tares is the source of truth. `working_doc` is a doc id.
CREATE TABLE IF NOT EXISTS tickets (
  id          TEXT PRIMARY KEY,
  project     TEXT,
  owner       TEXT,
  external_id TEXT,
  identifier  TEXT,
  url         TEXT,
  title       TEXT,
  status      TEXT,
  position    DOUBLE,
  working_doc TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
);
-- Claude Code sessions that worked in a project (TR-405): the plugin tells Tares which project a
-- session works in with a `session_project` line, and the whole session (its claude_code events,
-- keyed by session id, the lines from before the project existed included) reads as the project's
CREATE TABLE IF NOT EXISTS project_sessions (
  project    TEXT,
  session    TEXT,
  repo       TEXT,
  linked_at  TIMESTAMPTZ,
  PRIMARY KEY (project, session)
);
-- What a Claude Code session is doing now (TR-405 follow-up), from the plugin's session_state
-- lines: working, waiting (for the person, `reason` says why) or ended. One row per session.
CREATE TABLE IF NOT EXISTS session_states (
  session    TEXT PRIMARY KEY,
  state      TEXT,
  reason     TEXT,
  state_at   TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS skills (
  project     TEXT,
  name        TEXT,
  description TEXT,
  body        TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ,
  PRIMARY KEY (project, name)
);
"""

# Columns added after the first release; bring pre-existing DBs up to the current schema.
_MIGRATIONS = [
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS labels JSON",
    "ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS ingest_key TEXT",
    # a GitHub App or broker credential's fields (app id, key, installations); token kind: empty
    "ALTER TABLE github_credentials ADD COLUMN IF NOT EXISTS config JSON",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_by TEXT",
    # No DEFAULT here on purpose: DuckDB re-applies an ADD COLUMN … DEFAULT on every boot even when
    # the column already exists, which would reset paused=TRUE back to FALSE each restart. Existing
    # rows get NULL (read as False); the upsert always writes an explicit value going forward.
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS paused BOOLEAN",
    "ALTER TABLE dispatch_deliveries ADD COLUMN IF NOT EXISTS error TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS model TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS slack_channel TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS webhook_url TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS webhook_token TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS mcp_servers JSON",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS external_tools JSON",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS max_rounds INTEGER",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS budget_usd DOUBLE",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS daily_cap INTEGER",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS max_rounds INTEGER",
    # extra, non-secret headers an MCP server wants on every request (toolset selection, read-only
    # mode); the auth header stays its own column because it is the secret
    "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS headers JSON",
    # `reviews` were renamed to Tares agents before release; drop the old-named tables if a dev
    # DB still carries them (the definitions are re-created under the new names).
    "DROP TABLE IF EXISTS catalog_reviews",
    "DROP TABLE IF EXISTS review_runs",
    # Retired: tares no longer auto-extracts numeric fields. Numbers you aggregate are declared
    # as number-typed labels (stored in `labels`); the raw values remain in `payload`. Metadata-only
    # drop in DuckDB, so this is instant even on a large table.
    "ALTER TABLE events DROP COLUMN IF EXISTS fields",
    # Project ownership (see usecases tables). No DEFAULT for the same reason as paused above;
    # NULL reads as "not owned" / "not customized".
    "ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS owned_by TEXT",
    "ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS customized BOOLEAN",
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS owned_by TEXT",
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS customized BOOLEAN",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS owned_by TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS customized BOOLEAN",
    "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS owned_by TEXT",
    "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS customized BOOLEAN",
    # Per-run model usage (tokens from the API's usage block, cost priced at write time). Runs
    # from before these columns keep NULL everywhere — their cost is unknown, never backfilled.
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS input_tokens BIGINT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS output_tokens BIGINT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cache_creation_input_tokens BIGINT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cache_read_input_tokens BIGINT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE",
    # the write-back's outcome, so a run can say whether its finding reached the customer's
    # endpoint: "ok", "http 4xx", "failed after 3 attempts: ...", NULL when no webhook
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS delivery TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS delivery_error TEXT",
    # the label whose value the write-back reports as `key` (TR-285: Rius attributes reports by
    # delivery id while the entity, the cooldown axis, is the service)
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS webhook_key_label TEXT",
    # TR-302: an agent names the provider it runs on ("" = the cell default); a run records which
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS provider TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS provider TEXT",
    # TR-318: how a run ended on purpose ("finding" | "no_op"; NULL before) and the verdict label
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS outcome TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS verdict TEXT",
    # TR-220: what the run produced, [{kind, label, url?}], read off its tool calls and deliveries
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS results JSON",
    # TR-332: the skills a run loaded, in order, each once
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS skills JSON",
    # TR-334: [{verdict, agent, cooldown}], the agents this one hands a finding to by verdict
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS handoffs JSON",
    # how a run ends: always with the conclude tool (`concludes`), and the verdicts it may give,
    # [{verdict, when}], the only ones the tool accepts when there are any
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS concludes BOOLEAN",
    # the GitHub credential the agent acts with (check runs; its GitHub MCP server is in mcp_servers)
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS github TEXT",
    "ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS verdicts JSON",
    # Which key paid for a ledger row ("env:ANTHROPIC_API_KEY" | "console") — the boundary a
    # hosted trial's enforcement counts against. Rows from before attribution stay NULL (unknown).
    "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS key_source TEXT",
    "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS provider TEXT",
    # Triggers name their own sources and filters (views were removed): the view's fields move
    # onto the trigger in _fold_views, which then drops the view column and catalog_views.
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS sources JSON",
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS filters JSON",
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS key_field TEXT",
    # TR-330 run lineage: what woke a run (trigger | schedule | manual | rerun | bootstrap |
    # handoff; NULL before, shown as unknown), the run it repeats or was handed off from, and the
    # project it ran in. A firing records its trigger's project and, when a finding tripped it,
    # the run that wrote the finding. Backfilled and indexed in _lineage_upgrade.
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS woken_by TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS project TEXT",
    "ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS project TEXT",
    "ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS parent_run_id TEXT",
    # Project keys and joining a project (TR-335, TR-336): a key may belong to one project, and a
    # subscription may be to a whole project (every trigger of it, including ones added later)
    # instead of to one trigger. NULL on both is what every row before meant.
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS project TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS project TEXT",
    # The goal-first project page: what a project is for, in the user's words (NULL = not said
    # yet), and on a run the one-line headline and next step of what it concluded, plus when a
    # person marked the result handled and who. NULL on every earlier row. Headline and next
    # step are derived from the note at read time for those (tares/goal.py).
    "ALTER TABLE usecases ADD COLUMN IF NOT EXISTS goal TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS headline TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS next_step TEXT",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS handled_by TEXT",
    # The guided project setup: the plan a project was set up from and how far the person got
    # ({plan, step, practice_run}, NULL for any other project), and a practice mark on the runs
    # and firings "Try it" makes. A practice result is shown, never counted: today's totals and
    # health leave it out. NULL on every earlier row reads as not practice.
    "ALTER TABLE usecases ADD COLUMN IF NOT EXISTS setup JSON",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS practice BOOLEAN",
    "ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS practice BOOLEAN",
    # A trigger's own words for what wakes it ("an alert fires in the demo service"), used by
    # every generated sentence instead of the rule's wording. NULL = say it from the rule.
    "ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS description TEXT",
    # TR-408: the Linear project a project's tickets live in, and how the last sync went
    # ({id, name, url, synced_at, cursor, error}); NULL = the project does not use Linear
    "ALTER TABLE usecases ADD COLUMN IF NOT EXISTS linear JSON",
    # What kind of project it is, shown as a tag and choosing its page (NULL = an ordinary
    # project). 'software_factory': specced in Claude Code, built from docs and tickets.
    "ALTER TABLE usecases ADD COLUMN IF NOT EXISTS kind TEXT",
]

_FILTER_COLS = {"event_type", "source", "text", "key_value"}
_FILTER_OPS = {"eq": "=", "neq": "!=", "gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DOTTED_FIELD_RE = re.compile(r"^[A-Za-z0-9_.]+$")

# Max distinct values a (source, label) may materialize in entity_counts. A well-formed label is a
# low/medium-cardinality axis (service, env, status); a label accidentally bound to a near-unique
# field (request_id, a timestamp) would otherwise make entity_counts ≈ the events table. Past the
# cap we drop that (source, label)'s rows and mark it truncated: reads fall back to a live scan and
# the UI can flag it as high-cardinality (not a useful entity axis anyway).
_ENTITY_CARDINALITY_CAP = int(os.getenv("TARES_ENTITY_CARDINALITY_CAP", "10000"))


# A finding's labels name the run and firing it came from (TR-330). They are for tracing a
# finding back, one value per run, so they are never counted as an entity axis.
_PROVENANCE_LABELS = {"run_id", "dispatch_id"}


def _accum_entity(ent: dict, source: str, label: str, value, ingest_time) -> None:
    """Fold one (source, label, value) observation into the batch's entity_counts deltas. Skips
    empty/null, boolean, and numeric values — a number-typed label is a *measurement* you aggregate
    (max/avg/sum), not an entity axis to facet by, and faceting it would materialize one bucket per
    distinct number and blow the cardinality cap. The value is stringified to match how the read
    path reads it back (json_extract_string), so live deltas merge with seeded rows."""
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return
    v = str(value)
    if not v:
        return
    d = ent.get((source, label, v))
    if d is None:
        ent[(source, label, v)] = [1, ingest_time]
    else:
        d[0] += 1
        if ingest_time > d[1]:
            d[1] = ingest_time


def _label_expr(name: str) -> str:
    """SQL expression for label `name`: its own column if it's the legacy primary key, else
    read from the labels JSON. Lets a query slice by key_value or any named label uniformly."""
    if not _FIELD_RE.match(name):
        raise ValueError(f"bad label name {name!r}")
    if name == "key_value":
        return "key_value"
    return f"json_extract_string(labels, '$.{name}')"


def _where_sql(where) -> tuple[str, list]:
    """{label: value} -> ('AND ...' equality SQL, params). Matches on key_value or any label."""
    clauses, params = [], []
    for name, value in (where or {}).items():
        clauses.append(f"{_label_expr(name)} = ?")
        params.append(str(value))
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _filter_sql(filters) -> tuple[str, list]:
    """Trigger filters -> ('AND ...' SQL fragment, params). A field name resolves against the source's
    extracted labels (the string/number axes a user defined). JSON paths are quoted so dotted names
    address one flat key, not a nested object. Numeric ops cast to DOUBLE (TRY_CAST: rows without
    the label — or non-numeric values — simply don't match)."""
    clauses, params = [], []
    for f in filters or []:
        name, op, value = f["field"], f["op"], f["value"]
        if not _DOTTED_FIELD_RE.match(name):
            raise ValueError(f"bad filter field {name!r}")
        numeric = op in ("gt", "lt", "gte", "lte")
        if name in _FILTER_COLS:
            expr = f"TRY_CAST({name} AS DOUBLE)" if numeric else name
        else:
            expr = f"json_extract_string(labels, '$.\"{name}\"')"
            if numeric:
                expr = f"TRY_CAST({expr} AS DOUBLE)"
        if op == "contains":
            clauses.append(f"{expr} ILIKE ?")
            params.append(f"%{value}%")
        elif op == "in":
            values = [str(v) for v in (value if isinstance(value, list) else [value])]
            if not values:
                raise ValueError("filter op 'in' needs at least one value")
            # case-insensitive: GitHub names (owner/repo) are, and a typed list rarely matches case
            clauses.append(f"lower({expr}) IN ({', '.join('?' * len(values))})")
            params.extend(v.lower() for v in values)
        elif op in _FILTER_OPS:
            clauses.append(f"{expr} {_FILTER_OPS[op]} ?")
            params.append(float(value) if numeric else str(value))
        else:
            raise ValueError(f"bad filter op {op!r}")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _scope_sql(scope) -> tuple[str, list]:
    """A project's view of the shared findings and memory sources -> ('AND ...' SQL, params).
    `scope` is {"sources": [the shared sources], "project": id, "agents": [its agents]}: rows of
    other sources pass; rows of a shared source only when they were recorded in the project
    (payload `project`) or, for rows from before findings named their project, by one of its
    agents (the `agent` label)."""
    if not scope or not scope.get("sources"):
        return "", []
    shared = list(scope["sources"])
    agents = list(scope.get("agents") or [])
    sql = (f" AND (source NOT IN ({', '.join(['?'] * len(shared))}) "
           "OR json_extract_string(payload, '$.project') = ?")
    params = [*shared, scope.get("project") or ""]
    if agents:
        sql += (" OR (json_extract_string(payload, '$.project') IS NULL AND "
                f"json_extract_string(labels, '$.agent') IN ({', '.join(['?'] * len(agents))}))")
        params += agents
    return sql + ")", params


def _cgroup_mem_bytes() -> int | None:
    """The container's memory limit in bytes (cgroup v2, then v1), or None if unlimited/unknown."""
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(p).read().strip()
        except OSError:
            continue
        if raw.isdigit():
            n = int(raw)
            if 0 < n < (1 << 62):        # "max" or a huge sentinel = effectively unlimited
                return n
    return None


def _file_size(path: str) -> int:
    """Bytes on disk, 0 if the file isn't there (an in-memory db, or a WAL that's checkpointed)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _bound_duckdb_memory(con, path: str) -> None:
    """Bound DuckDB to the CONTAINER, not the host. Without a memory_limit DuckDB sizes itself to
    the node's RAM, so one heavy scan over a grown dataset blows past the cgroup limit and the
    process is OOMKilled (this took the dev cell down at ~1M events). With a limit set, DuckDB
    spills intermediates to temp_directory (on the data volume) instead of dying. Override with
    TARES_DUCKDB_MEMORY_LIMIT (e.g. "800MB"); no-op locally when there's no cgroup limit."""
    limit = os.getenv("TARES_DUCKDB_MEMORY_LIMIT")
    if not limit:
        cg = _cgroup_mem_bytes()
        if cg:
            limit = f"{max(256, int(cg * 0.6) // (1024 * 1024))}MB"   # 60% of the container
    if not limit:
        return
    try:
        con.execute(f"PRAGMA memory_limit='{limit}'")
        con.execute(f"PRAGMA threads={os.getenv('TARES_DUCKDB_THREADS', '2')}")
        tmp = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", ".duckdb_tmp")
        os.makedirs(tmp, exist_ok=True)
        con.execute(f"PRAGMA temp_directory='{tmp}'")
    except Exception as e:                 # never let tuning block startup
        print(f"taresd: could not bound DuckDB memory ({e})")


class StoreUnavailable(RuntimeError):
    """The database could not be opened or initialized — locked by another daemon, permissions,
    a full disk, a corrupt file. Raised instead of the raw DuckDB error so taresd can start in
    degraded mode (serve the console and explain itself) rather than exit with a traceback."""

    def __init__(self, reason: str, path: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.path = path


class Store:
    def __init__(self, path: str = "tares.duckdb"):
        # All access is from taresd's event loop thread; the lock is belt-and-suspenders since
        # FastAPI may run sync work in a threadpool.
        self._lock = threading.Lock()
        self.path = path          # kept so usage() can size the db file and its WAL
        # Opening the DB is the one startup step that routinely fails for reasons outside the
        # process (another daemon holds the lock, the file is unreadable, the volume is full).
        # Fail as StoreUnavailable so the caller can degrade; the original error is kept as the
        # cause so the traceback still reaches the process log.
        try:
            self.con = duckdb.connect(path)
        except Exception as e:
            raise StoreUnavailable(f"cannot open the database at {path}: {e}", path) from e
        try:
            _bound_duckdb_memory(self.con, path)
            for stmt in _SCHEMA.strip().split(";"):
                if stmt.strip():
                    self.con.execute(stmt)
            for stmt in _MIGRATIONS:
                self.con.execute(stmt)
            self._rename_query_log_scope()
            self._fold_views()
            self._migrate_claude_code_repo_label()
            self._normalize_projects()
            self._lineage_upgrade()
            self._wiring_upgrade()
            self._skills_upgrade()
            self._factory_kind_upgrade()
            self._init_source_stats()
            self._init_entity_counts()
            # Write the upgrade into the database file now. Left in the WAL, the new columns on
            # agent_runs (a table with an index) replay after a hard stop into a corrupted index:
            # "Corrupted ART index", and every query after it fails until a restart.
            self.con.execute("CHECKPOINT")
        except Exception as e:
            raise StoreUnavailable(f"cannot initialize the database at {path}: {e}", path) from e

    def _columns(self, table: str) -> set:
        return {r[0] for r in self.con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}

    def _tables(self) -> set:
        return {r[0] for r in self.con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}

    def _rename_query_log_scope(self) -> None:
        """query_log.view named what an agent read through when views existed; it is now the
        read's scope (a trigger, an agent tool, "(read)"). Renamed in place so the log is kept."""
        if "view" in self._columns("query_log") and "scope" not in self._columns("query_log"):
            self.con.execute("ALTER TABLE query_log RENAME COLUMN view TO scope")

    def _fold_views(self) -> None:
        """Views were removed: a trigger names its own sources, filters and key_field. Copy them
        from the view each trigger named, then drop the view column and the catalog_views table.
        Idempotent: a trigger that already has sources is left alone, and once the column and the
        table are gone there is nothing to do. A trigger whose view no longer exists could never
        fire (evaluating it failed); it keeps no sources and is paused, so it shows up in the
        console to be fixed or deleted instead of vanishing."""
        tables = self._tables()
        if "view" in self._columns("catalog_triggers"):
            views = {}
            if "catalog_views" in tables:
                vcols = self._columns("catalog_views")
                sel = ", ".join(c if c in vcols else "NULL" for c in
                                ("name", "sources", "filters", "key_field"))
                for name, sources, filters, key_field in self.con.execute(
                        f"SELECT {sel} FROM catalog_views").fetchall():
                    views[name] = (json.loads(sources or "[]"), json.loads(filters or "[]"),
                                   key_field or "")
            for name, view, sources, cond in self.con.execute(
                    "SELECT name, view, sources, condition FROM catalog_triggers").fetchall():
                if json.loads(sources or "[]"):
                    continue
                v = views.get(view)
                if v is None:
                    self.con.execute("UPDATE catalog_triggers SET sources = '[]', filters = '[]', "
                                     "key_field = '', paused = TRUE WHERE name = ?", [name])
                    print(f"taresd: trigger {name!r} named view {view!r}, which is gone; "
                          "paused it with no sources")
                    continue
                self.con.execute(
                    "UPDATE catalog_triggers SET sources = ?, filters = ?, key_field = ? "
                    "WHERE name = ?", [json.dumps(v[0]), json.dumps(v[1]), v[2], name])
                # a schedule trigger's last tick was recorded under its view's name; it is under
                # the trigger's own name now, so carry it over or the first tick comes early
                if (json.loads(cond or "{}") or {}).get("every"):
                    self.con.execute("UPDATE trigger_state SET key_value = ? WHERE trigger = ? "
                                     "AND key_value = ? AND NOT EXISTS (SELECT 1 FROM "
                                     "trigger_state WHERE trigger = ? AND key_value = ?)",
                                     [name, name, view, name, name])
            # emit.attach_view asked for the view's timeline in the payload, which it always
            # carries; nothing reads it, so it goes with the view
            for name, emit in self.con.execute("SELECT name, emit FROM catalog_triggers").fetchall():
                e = json.loads(emit or "{}") or {}
                if isinstance(e, dict) and "attach_view" in e:
                    e.pop("attach_view")
                    self.con.execute("UPDATE catalog_triggers SET emit = ? WHERE name = ?",
                                     [json.dumps(e), name])
            self.con.execute("ALTER TABLE catalog_triggers DROP COLUMN view")
        if "catalog_views" in tables:
            self.con.execute("DROP TABLE catalog_views")

    def migrate_claude_code_repo_label(self) -> None:
        """1.14: the claude_code label `project` became `repo`. Rewrites what the catalog says
        about the label: the declaration on every claude_code source, and the key or filters of
        triggers that read only claude_code sources (a trigger mixing in other sources is left alone,
        `project` may be another source's label there). Events already stored keep their old
        label, as any label change does: new events only. Idempotent, cheap (catalog tables
        only), run at every open and again after a catalog import, since an old catalog file can
        bring the old label back."""
        with self._lock:
            self._migrate_claude_code_repo_label()

    def _migrate_claude_code_repo_label(self) -> None:
        rows = self.con.execute(
            "SELECT name, config FROM catalog_sources WHERE connector = 'claude_code'").fetchall()
        names = []
        for name, raw in rows:
            names.append(name)
            cfg = json.loads(raw or "{}")
            hit = False
            for l in cfg.get("labels") or []:
                if isinstance(l, dict) and l.get("name") == "project":
                    l["name"] = "repo"
                    if l.get("field") == "project":
                        l["field"] = "repo"
                    hit = True
            if hit:
                self.con.execute("UPDATE catalog_sources SET config = ? WHERE name = ?",
                                 [json.dumps(cfg), name])
        for name, key_field, sources, filters in self.con.execute(
                "SELECT name, key_field, sources, filters FROM catalog_triggers").fetchall():
            srcs = set(json.loads(sources or "[]"))
            if not srcs or not srcs <= set(names):
                continue
            fl = json.loads(filters or "[]")
            for f in fl:
                if f.get("field") == "project":
                    f["field"] = "repo"
            if key_field == "project" or json.loads(filters or "[]") != fl:
                self.con.execute("UPDATE catalog_triggers SET key_field = ?, filters = ? WHERE name = ?",
                                 ["repo" if key_field == "project" else key_field, json.dumps(fl), name])

    def _lineage_upgrade(self) -> None:
        """Runs and firings from before lineage have no project: take it from the agent's or the
        trigger's owner when that still exists (a run of a deleted agent stays unplaced). Then the
        indexes the project timeline reads through. Idempotent: only NULL rows are touched, and
        the backfill runs once per database (a settings marker), not on every start."""
        done = self.con.execute("SELECT 1 FROM settings WHERE key = 'lineage_backfilled'").fetchone()
        if not done:
            self._lineage_backfill()
        for stmt in ("CREATE INDEX IF NOT EXISTS ix_agent_runs_dispatch ON agent_runs(dispatch_id)",
                     "CREATE INDEX IF NOT EXISTS ix_agent_runs_parent ON agent_runs(parent_run_id)",
                     "CREATE INDEX IF NOT EXISTS ix_agent_runs_project ON agent_runs(project, started_at)",
                     "CREATE INDEX IF NOT EXISTS ix_dispatch_log_project ON dispatch_log(project, fired_at)",
                     "CREATE INDEX IF NOT EXISTS ix_dispatch_log_parent ON dispatch_log(parent_run_id)"):
            self.con.execute(stmt)

    def _lineage_backfill(self) -> None:
        self.con.execute(
            "UPDATE agent_runs SET project = a.owned_by FROM catalog_agents a "
            "WHERE agent_runs.project IS NULL AND a.name = agent_runs.agent "
            "AND a.owned_by IS NOT NULL")
        self.con.execute(
            "UPDATE dispatch_log SET project = t.owned_by FROM catalog_triggers t "
            "WHERE dispatch_log.project IS NULL AND t.name = dispatch_log.trigger "
            "AND t.owned_by IS NOT NULL")
        # and the project rows the timeline reads (P-TR-216)
        self.con.execute("INSERT INTO run_projects (run_id, project) SELECT id, project "
                         "FROM agent_runs WHERE project IS NOT NULL ON CONFLICT DO NOTHING")
        self.con.execute("INSERT INTO dispatch_projects (dispatch_id, project) SELECT dispatch_id, "
                         "project FROM dispatch_log WHERE project IS NOT NULL ON CONFLICT DO NOTHING")
        self.con.execute("INSERT INTO settings (key, value, updated_at) VALUES "
                         "('lineage_backfilled', '1', ?) ON CONFLICT (key) DO NOTHING", [now_utc()])

    def _wiring_upgrade(self) -> None:
        """Once per database (P-TR-216): the wiring an agent carried itself (its trigger, its
        handoffs, and on/off as a tares://agent/ subscription) becomes the wiring of the project
        that made it; those subscriptions go. Runs and firings get their project rows. Called
        with no lock needed (the store is being opened)."""
        if self.con.execute("SELECT 1 FROM settings WHERE key = 'wiring_moved'").fetchone():
            return
        # one transaction: a crash part-way leaves the subscriptions (the on/off record) as they
        # were, and the next start does the whole upgrade again
        self.con.execute("BEGIN TRANSACTION")
        try:
            self._wiring_move()
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def _skills_upgrade(self) -> None:
        """Once per database: the skills each project kept become skills of the cell that those
        projects use. Two projects' skills of the same name and text are one skill; the same name
        with different text keeps both, the second named name-2 (and so on)."""
        if self.con.execute("SELECT 1 FROM settings WHERE key = 'skills_shared'").fetchone():
            return
        self.con.execute("BEGIN TRANSACTION")
        try:
            for project, name, desc, body, c, u in self.con.execute(
                    "SELECT project, name, description, body, created_at, updated_at FROM skills "
                    "ORDER BY created_at, project").fetchall():
                use, n = name, 2
                while True:
                    have = self.con.execute("SELECT body FROM shared_skills WHERE name = ?",
                                            [use]).fetchone()
                    if have is None or have[0] == body:
                        break
                    use, n = f"{name}-{n}", n + 1
                if have is None:
                    self.con.execute("INSERT INTO shared_skills (name, description, body, made_by, "
                                     "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                                     [use, desc, body, project, c, u])
                if use != name:   # the project's own row follows the new name
                    self.con.execute("UPDATE usecase_objects SET name = ? WHERE usecase_id = ? "
                                     "AND kind = 'skill' AND name = ?", [use, project, name])
                self.con.execute(
                    "INSERT INTO usecase_objects (usecase_id, kind, key, name, customized, "
                    "created_at) SELECT ?, 'skill', ?, ?, FALSE, ? WHERE NOT EXISTS (SELECT 1 FROM "
                    "usecase_objects WHERE usecase_id = ? AND kind = 'skill' AND name = ?)",
                    [project, f"skill:{use}", use, c or now_utc(), project, use])
            self.con.execute("INSERT INTO settings (key, value, updated_at) VALUES "
                             "('skills_shared', '1', ?) ON CONFLICT (key) DO NOTHING", [now_utc()])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def _wiring_move(self) -> None:
        default = self._lazy_default_project()
        projects = {r[0] for r in self.con.execute("SELECT id FROM usecases").fetchall()}
        on = {r[0][len(AGENT_PREFIX):] for r in self.con.execute(
            "SELECT url FROM subscriptions WHERE url LIKE ?", [AGENT_PREFIX + "%"]).fetchall()}
        ts = now_utc()
        for name, trig, owner, raw in self.con.execute(
                "SELECT name, trigger, owned_by, handoffs FROM catalog_agents").fetchall():
            project = owner if owner in projects else default()
            # the store's placement pass may have wired it already (off): keep that row, carry
            # on/off over
            if trig:
                if self.con.execute("SELECT 1 FROM project_wiring WHERE kind = 'wake' AND "
                                    "project = ? AND trigger = ? AND agent = ?",
                                    [project, trig, name]).fetchone():
                    self.con.execute("UPDATE project_wiring SET enabled = ? WHERE kind = 'wake' "
                                     "AND project = ? AND trigger = ? AND agent = ?",
                                     [name in on, project, trig, name])
                else:
                    self.con.execute(
                        "INSERT INTO project_wiring (project, kind, trigger, agent, enabled, "
                        "created_at) VALUES (?, 'wake', ?, ?, ?, ?)",
                        [project, trig, name, name in on, ts])
            has_handoffs = self.con.execute(
                "SELECT 1 FROM project_wiring WHERE kind = 'handoff' AND project = ? AND "
                "from_agent = ?", [project, name]).fetchone()
            for h in (json.loads(raw) if raw else []) if not has_handoffs else []:
                if isinstance(h, dict) and h.get("agent") and h.get("verdict"):
                    self.con.execute(
                        "INSERT INTO project_wiring (project, kind, from_agent, agent, verdict, "
                        "cooldown, created_at) VALUES (?, 'handoff', ?, ?, ?, ?, ?)",
                        [project, name, h["agent"], str(h["verdict"]).lower(),
                         h.get("cooldown") or None, ts])
        self.con.execute("INSERT INTO run_projects (run_id, project) SELECT id, project "
                         "FROM agent_runs WHERE project IS NOT NULL ON CONFLICT DO NOTHING")
        self.con.execute("INSERT INTO dispatch_projects (dispatch_id, project) SELECT dispatch_id, "
                         "project FROM dispatch_log WHERE project IS NOT NULL ON CONFLICT DO NOTHING")
        for stmt in ("CREATE INDEX IF NOT EXISTS ix_wiring_trigger ON project_wiring(trigger)",
                     "CREATE INDEX IF NOT EXISTS ix_wiring_project ON project_wiring(project)",
                     "CREATE INDEX IF NOT EXISTS ix_run_projects_project ON run_projects(project)",
                     "CREATE INDEX IF NOT EXISTS ix_dispatch_projects_project ON dispatch_projects(project)"):
            self.con.execute(stmt)
        self.con.execute("INSERT INTO settings (key, value, updated_at) VALUES "
                         "('wiring_moved', '1', ?) ON CONFLICT (key) DO NOTHING", [ts])
        # last: the old on/off record goes only with the marker that says it was carried over
        self.con.execute("DELETE FROM subscriptions WHERE url LIKE ?", [AGENT_PREFIX + "%"])

    # ── wiring: what each project wakes, and who digs in on whose verdict (P-TR-216) ──
    def list_wakes(self, project: str | None = None, trigger: str | None = None,
                   agent: str | None = None) -> list[dict]:
        """Wake rows ({project, trigger, agent, enabled}), filtered by any of the arguments."""
        where, args = [], []
        for col, v in (("project", project), ("trigger", trigger), ("agent", agent)):
            if v is not None:
                where.append(f"{col} = ?")
                args.append(v)
        sql = "SELECT project, trigger, agent, enabled FROM project_wiring WHERE kind = 'wake'"
        sql += "".join(f" AND {w}" for w in where) + " ORDER BY created_at, agent"
        with self._lock:
            rows = self.con.execute(sql, args).fetchall()
        return [{"project": r[0], "trigger": r[1], "agent": r[2], "enabled": bool(r[3])}
                for r in rows]

    def set_wake(self, project: str, trigger: str, agent: str, enabled: bool | None = None) -> None:
        """In `project`, `trigger` wakes `agent`. `enabled` None keeps what the row had (a new row
        starts off): wiring an agent is not turning it on."""
        with self._lock:
            row = self.con.execute(
                "SELECT enabled FROM project_wiring WHERE kind = 'wake' AND project = ? "
                "AND trigger = ? AND agent = ?", [project, trigger, agent]).fetchone()
            if row is not None:
                if enabled is not None:
                    self.con.execute(
                        "UPDATE project_wiring SET enabled = ? WHERE kind = 'wake' AND project = ? "
                        "AND trigger = ? AND agent = ?", [enabled, project, trigger, agent])
                return
            self.con.execute(
                "INSERT INTO project_wiring (project, kind, trigger, agent, enabled, created_at) "
                "VALUES (?, 'wake', ?, ?, ?, ?)", [project, trigger, agent, bool(enabled), now_utc()])

    def remove_wakes(self, project: str | None = None, trigger: str | None = None,
                     agent: str | None = None) -> int:
        """Drop wake rows matching every given argument (at least one). Returns how many."""
        if project is None and trigger is None and agent is None:
            raise ValueError("remove_wakes needs a project, trigger or agent")
        where, args = ["kind = 'wake'"], []
        for col, v in (("project", project), ("trigger", trigger), ("agent", agent)):
            if v is not None:
                where.append(f"{col} = ?")
                args.append(v)
        with self._lock:
            n = self.con.execute(f"SELECT count(*) FROM project_wiring WHERE {' AND '.join(where)}",
                                 args).fetchone()[0]
            self.con.execute(f"DELETE FROM project_wiring WHERE {' AND '.join(where)}", args)
        return int(n)

    def set_agent_enabled(self, agent: str, enabled: bool, project: str | None = None) -> int:
        """Turn an agent on or off: in one project, or in every project that wires it. Returns
        how many wake rows it touched (0: the agent is wired nowhere, so there is nothing to
        turn on)."""
        where, args = "kind = 'wake' AND agent = ?", [agent]
        if project is not None:
            where += " AND project = ?"
            args.append(project)
        with self._lock:
            n = self.con.execute(f"SELECT count(*) FROM project_wiring WHERE {where}",
                                 args).fetchone()[0]
            self.con.execute(f"UPDATE project_wiring SET enabled = ? WHERE {where}",
                             [enabled, *args])
        return int(n)

    def agent_enabled(self, agent: str, project: str | None = None) -> bool:
        """Whether some wake-up (in `project`, or in any project) wakes the agent."""
        return any(w["enabled"] for w in self.list_wakes(project=project, agent=agent))

    def list_handoffs(self, project: str | None = None,
                      from_agent: str | None = None) -> list[dict]:
        """Handoff rows ({project, from_agent, agent, verdict, cooldown})."""
        sql = ("SELECT project, from_agent, agent, verdict, cooldown FROM project_wiring "
               "WHERE kind = 'handoff'")
        args = []
        if project is not None:
            sql += " AND project = ?"
            args.append(project)
        if from_agent is not None:
            sql += " AND from_agent = ?"
            args.append(from_agent)
        with self._lock:
            rows = self.con.execute(sql + " ORDER BY created_at, verdict", args).fetchall()
        return [{"project": r[0], "from_agent": r[1], "agent": r[2], "verdict": r[3],
                 "cooldown": r[4]} for r in rows]

    def set_handoffs(self, project: str, from_agent: str, handoffs: list[dict]) -> None:
        """Replace what `from_agent` hands off to in `project` ({verdict, agent, cooldown})."""
        ts = now_utc()
        with self._lock:
            self.con.execute("DELETE FROM project_wiring WHERE kind = 'handoff' AND project = ? "
                             "AND from_agent = ?", [project, from_agent])
            for h in handoffs or []:
                if h.get("agent") and h.get("verdict"):
                    self.con.execute(
                        "INSERT INTO project_wiring (project, kind, from_agent, agent, verdict, "
                        "cooldown, created_at) VALUES (?, 'handoff', ?, ?, ?, ?, ?)",
                        [project, from_agent, h["agent"], str(h["verdict"]).lower(),
                         h.get("cooldown") or None, ts])

    def remove_wiring(self, project: str | None = None, agent: str | None = None,
                      trigger: str | None = None) -> None:
        """Forget wiring: a project's (deleted), an agent's (deleted: its wakes and every handoff
        from or to it), a trigger's (deleted: its wakes)."""
        with self._lock:
            if project is not None:
                self.con.execute("DELETE FROM project_wiring WHERE project = ?", [project])
            if agent is not None:
                self.con.execute("DELETE FROM project_wiring WHERE agent = ? OR from_agent = ?",
                                 [agent, agent])
            if trigger is not None:
                self.con.execute("DELETE FROM project_wiring WHERE kind = 'wake' AND trigger = ?",
                                 [trigger])

    def rename_in_wiring(self, kind: str, old: str, new: str) -> None:
        with self._lock:
            if kind == "trigger":
                self.con.execute("UPDATE project_wiring SET trigger = ? WHERE trigger = ?", [new, old])
            else:
                self.con.execute("UPDATE project_wiring SET agent = ? WHERE agent = ?", [new, old])
                self.con.execute("UPDATE project_wiring SET from_agent = ? WHERE from_agent = ?",
                                 [new, old])

    # which projects a run or a firing belongs to
    def add_run_projects(self, run_id: str, projects) -> None:
        with self._lock:
            for p in dict.fromkeys(x for x in projects or [] if x):
                self.con.execute("INSERT INTO run_projects (run_id, project) VALUES (?, ?) "
                                 "ON CONFLICT DO NOTHING", [run_id, p])

    def run_projects(self, run_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT project FROM run_projects WHERE run_id = ? ORDER BY project",
                [run_id]).fetchall()]

    def add_dispatch_projects(self, dispatch_id: str, projects) -> None:
        with self._lock:
            for p in dict.fromkeys(x for x in projects or [] if x):
                self.con.execute("INSERT INTO dispatch_projects (dispatch_id, project) VALUES (?, ?) "
                                 "ON CONFLICT DO NOTHING", [dispatch_id, p])

    def dispatch_projects(self, dispatch_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT project FROM dispatch_projects WHERE dispatch_id = ? ORDER BY project",
                [dispatch_id]).fetchall()]

    def active_projects_using(self, kind: str, name: str) -> list[str]:
        """projects_using, without the paused projects and the drafts: the ones a firing of the
        part reaches."""
        with self._lock:
            status = {r[0]: r[1] for r in self.con.execute(
                "SELECT id, status FROM usecases").fetchall()}
        return [p for p in self.projects_using(kind, name)
                if status.get(p) not in ("paused", "draft", None)]

    def projects_using(self, kind: str, name: str) -> list[str]:
        """The projects whose object list has this part (any kind), oldest membership first."""
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT usecase_id FROM usecase_objects WHERE kind = ? AND name = ? "
                "GROUP BY usecase_id ORDER BY min(created_at)", [kind, name]).fetchall()]

    def ping(self) -> None:
        """Cheapest possible liveness probe for /health — proves the connection still answers.
        Raises whatever DuckDB raises when the store has gone away underneath us."""
        with self._lock:
            self.con.execute("SELECT 1").fetchone()

    def disk_bytes(self) -> int:
        """db + WAL bytes, from stat() only — no query, so /health can call it on every probe."""
        return _file_size(self.path) + _file_size(self.path + ".wal")

    def _init_source_stats(self) -> None:
        """`source_stats` is a maintained per-source counter (event count + last ingest) so the
        Sources list — polled every few seconds and hit by every agent `list_sources` — reads O(#sources)
        instead of scanning the whole events table with a GROUP BY. It's kept in sync incrementally by
        append()/purge_events(); this seeds it once from existing data (empty table, or a DB that
        predates the counter). COUNT(*) with no filter is a metadata read in DuckDB, so the guard is
        cheap; the GROUP BY runs only when the counter is empty."""
        with self._lock:
            if self.con.execute("SELECT COUNT(*) FROM source_stats").fetchone()[0]:
                return
            self.con.execute(
                "INSERT INTO source_stats "
                "SELECT source, COUNT(*), MAX(ingest_time) FROM events GROUP BY source")

    def _init_entity_counts(self) -> None:
        """`entity_counts` is a maintained per-(source, label, value) counter backing list_entities /
        the Explore facets, replacing a full-table `GROUP BY json_extract_string(labels, …)` (which
        JSON-parses every row — ~74ms per label at 5M events) with a small-table read. Only DECLARED
        labels + key_value are counted (never the open-ended raw field set), so cardinality is bounded
        by the user's curation; a misconfigured high-cardinality label is caught by the cap (see
        append). Kept in sync by append()/purge_events(); this seeds it once from existing data."""
        with self._lock:
            if self.con.execute("SELECT COUNT(*) FROM entity_counts").fetchone()[0]:
                return
            # Named labels: unnest each row's labels JSON into (key, value) and count per source.
            self.con.execute(
                "INSERT INTO entity_counts (source, label, value, events, last_ingest) "
                "SELECT source, label, value, COUNT(*), MAX(ingest_time) FROM ("
                "  SELECT source, k.key AS label, "
                "         json_extract_string(labels, '$.\"' || k.key || '\"') AS value, ingest_time "
                "  FROM events, UNNEST(json_keys(labels)) AS k(key) WHERE labels IS NOT NULL"
                "  AND NOT (event_type = 'finding' AND k.key IN ('run_id', 'dispatch_id'))"
                ") WHERE value IS NOT NULL AND value <> '' GROUP BY source, label, value")
            # The primary key axis (key_value), stored under the reserved label name 'key_value'.
            self.con.execute(
                "INSERT INTO entity_counts (source, label, value, events, last_ingest) "
                "SELECT source, 'key_value', key_value, COUNT(*), MAX(ingest_time) FROM events "
                "WHERE key_value IS NOT NULL AND key_value <> '' GROUP BY source, key_value "
                "ON CONFLICT (source, label, value) DO NOTHING")
            # Enforce the cap on the seeded data: any (source, label) over it is dropped + truncated.
            over = self.con.execute(
                "SELECT source, label FROM entity_counts GROUP BY source, label "
                "HAVING COUNT(*) > ?", [_ENTITY_CARDINALITY_CAP]).fetchall()
            for src, lab in over:
                self._truncate_entity_label(src, lab)

    def _truncate_entity_label(self, source: str, label: str) -> None:
        """Stop materializing a high-cardinality (source, label): drop its rows and flag it so reads
        fall back to a live scan. Caller holds the lock."""
        self.con.execute(
            "INSERT INTO entity_label_state (source, label, truncated) VALUES (?, ?, TRUE) "
            "ON CONFLICT (source, label) DO UPDATE SET truncated = TRUE", [source, label])
        self.con.execute(
            "DELETE FROM entity_counts WHERE source = ? AND label = ?", [source, label])

    def replace_source_events(self, source: str, envelopes: list[Envelope]) -> None:
        """A declarative source (reference) mirrors its config exactly: replace ALL of its rows in
        one transaction, keeping source_stats + entity_counts consistent. Cheap and idempotent — a
        reference source holds a handful of documents, and re-materializing on every edit is fine."""
        rows = [
            (e.source, e.source_type, e.key_value, e.event_type, e.text,
             json.dumps(e.payload), json.dumps(e.labels), e.event_time, e.ingest_time)
            for e in envelopes
        ]
        ent: dict[tuple, list] = {}
        last = None
        for e in envelopes:
            _accum_entity(ent, e.source, "key_value", e.key_value, e.ingest_time)
            for lname, lval in (e.labels or {}).items():
                if e.event_type == "finding" and lname in _PROVENANCE_LABELS:
                    continue
                _accum_entity(ent, e.source, lname, lval, e.ingest_time)
            if last is None or e.ingest_time > last:
                last = e.ingest_time
        with self._lock:
            self.con.execute("BEGIN TRANSACTION")
            try:
                for tbl in ("events", "source_stats", "entity_counts", "entity_label_state"):
                    self.con.execute(f"DELETE FROM {tbl} WHERE source = ?", [source])
                if rows:
                    self.con.executemany(
                        "INSERT INTO events (source, source_type, key_value, event_type, text, "
                        "payload, labels, event_time, ingest_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows)
                    self.con.execute(
                        "INSERT INTO source_stats (source, events, last_ingest) VALUES (?, ?, ?)",
                        [source, len(rows), last])
                    for (src, label, val), (n, ing) in ent.items():
                        self.con.execute(
                            "INSERT INTO entity_counts (source, label, value, events, last_ingest) "
                            "VALUES (?, ?, ?, ?, ?)", [src, label, val, n, ing])
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    # ── ingest ──────────────────────────────────────────────────────────────
    def append(self, envelopes: list[Envelope]) -> None:
        if not envelopes:
            return
        # Explicit column list: the `labels` column was added by migration after first release,
        # so positional INSERT order is no longer guaranteed.
        rows = [
            (e.source, e.source_type, e.key_value, e.event_type, e.text,
             json.dumps(e.payload), json.dumps(e.labels),
             e.event_time, e.ingest_time)
            for e in envelopes
        ]
        # Per-source deltas for the maintained counter (see event_stats): how many rows this batch
        # adds per source and the latest ingest_time among them. Plus per-(source, label, value)
        # deltas for entity_counts (see list_entities) — the declared labels and key_value only.
        deltas: dict[str, list] = {}
        ent: dict[tuple, list] = {}
        for e in envelopes:
            d = deltas.get(e.source)
            if d is None:
                deltas[e.source] = [1, e.ingest_time]
            else:
                d[0] += 1
                if e.ingest_time > d[1]:
                    d[1] = e.ingest_time
            _accum_entity(ent, e.source, "key_value", e.key_value, e.ingest_time)
            for lname, lval in (e.labels or {}).items():
                if e.event_type == "finding" and lname in _PROVENANCE_LABELS:
                    continue
                _accum_entity(ent, e.source, lname, lval, e.ingest_time)
        # Insert in bounded chunks. DuckDB's executemany binds rows one at a time, so a single huge
        # batch (e.g. a connector catching up a large backlog) would bind millions of parameters at
        # once and stall the daemon. Chunking caps each bind regardless of how much a caller passes.
        # The event inserts and every derived counter update run in one transaction so a counter can
        # never drift from the rows it counts (a crash rolls back all of them together).
        chunk = 2000
        with self._lock:
            self.con.execute("BEGIN TRANSACTION")
            try:
                for i in range(0, len(rows), chunk):
                    self.con.executemany(
                        "INSERT INTO events (source, source_type, key_value, event_type, text, "
                        "payload, labels, event_time, ingest_time) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows[i:i + chunk]
                    )
                for src, (n, last) in deltas.items():
                    self.con.execute(
                        "INSERT INTO source_stats (source, events, last_ingest) VALUES (?, ?, ?) "
                        "ON CONFLICT (source) DO UPDATE SET "
                        "events = source_stats.events + EXCLUDED.events, "
                        "last_ingest = greatest(source_stats.last_ingest, EXCLUDED.last_ingest)",
                        [src, n, last])
                self._apply_entity_deltas(ent)
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    def _apply_entity_deltas(self, ent: dict) -> None:
        """Fold this batch's (source, label, value) deltas into entity_counts. Already-truncated
        labels are skipped (they read via live scan). After upserting, any (source, label) that just
        crossed the cardinality cap is truncated. Caller holds the lock and an open transaction."""
        if not ent:
            return
        truncated = {(r[0], r[1]) for r in self.con.execute(
            "SELECT source, label FROM entity_label_state WHERE truncated = TRUE").fetchall()}
        rows = [(s, l, v, c[0], c[1]) for (s, l, v), c in ent.items() if (s, l) not in truncated]
        if rows:
            self.con.executemany(
                "INSERT INTO entity_counts (source, label, value, events, last_ingest) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (source, label, value) DO UPDATE SET "
                "events = entity_counts.events + EXCLUDED.events, "
                "last_ingest = greatest(entity_counts.last_ingest, EXCLUDED.last_ingest)", rows)
        for src, lab in {(s, l) for (s, l, _) in ent} - truncated:
            n = self.con.execute("SELECT COUNT(*) FROM entity_counts WHERE source = ? AND label = ?",
                                  [src, lab]).fetchone()[0]
            if n > _ENTITY_CARDINALITY_CAP:
                self._truncate_entity_label(src, lab)

    # ── reads ───────────────────────────────────────────────────────────────
    def read_window(self, sources: list[str], key: str | None, since: datetime, cap: int = 12,
                         filters: list | None = None, where: dict | None = None,
                         include_payload: bool = False, scope: dict | None = None):
        """Rows for an entity across sources, time-ordered: (event_time, source, text, labels), plus
        the raw lossless `payload` as a 5th column when `include_payload` is set.

        The entity is selected by `key` (legacy primary key_value) and/or `where` (a
        {label: value} map matching named labels). Passing key=None with a `where` is the
        label-native read ("everything where env=prod"); passing a key keeps the old behaviour.

        Caps each source to its most-recent `cap` events so a lossless store doesn't return a
        bloated payload (e.g. thousands of identical log lines or every 5s metric sample). Ingest
        stays lossless; this bound is a read-path summary, matching what an SRE actually wants.
        `include_payload` pulls the full stored record per row (bounded by the same cap) for callers
        that need fidelity beyond the summary `text`. `scope` narrows the shared findings and
        memory sources to one project's rows (see _scope_sql).
        """
        cols = "event_time, source, text, labels" + (", payload" if include_payload else "")
        ph = ", ".join(["?"] * len(sources))
        fsql, fparams = _filter_sql(filters)
        wsql, wparams = _where_sql(where)
        ssql, sparams = _scope_sql(scope)
        wsql, wparams = wsql + ssql, wparams + sparams
        ksql, kparams = (" AND key_value = ?", [key]) if key is not None else ("", [])
        with self._lock:
            return self.con.execute(
                f"SELECT {cols} FROM ("
                f"  SELECT {cols}, "
                f"  ROW_NUMBER() OVER (PARTITION BY source ORDER BY event_time DESC) AS rn "
                # reference sources are declarative context — always surfaced for the matched
                # entity, regardless of the read window (a project note doesn't 'age out').
                f"  FROM events WHERE source IN ({ph}) "
                f"    AND (source_type = 'reference' OR event_time >= ?){ksql}{fsql}{wsql}"
                f") WHERE rn <= {int(cap)} ORDER BY event_time",
                [*sources, since, *kparams, *fparams, *wparams],
            ).fetchall()

    def aggregate(self, sources: list[str], field: str | None, agg: str, since: datetime,
                  filters: list | None = None, where: dict | None = None,
                  group_by="key_value", until: datetime | None = None,
                  scope: dict | None = None) -> dict:
        """{group: value} for an aggregate over `field` in the window, grouped by one or more
        labels. `group_by` is a label name (scalar keys, key_value by default) or a list of
        names (tuple keys — a trigger grouping per (env, app)). NULL group values are dropped
        (a row lacking a grouping label isn't a real entity); NULL field values are ignored by
        the aggregate. `until` closes the window (exclusive), for comparing with an earlier one."""
        names = [group_by] if isinstance(group_by, str) else list(group_by)
        gexprs = [_label_expr(n) for n in names]
        ph = ", ".join(["?"] * len(sources))
        fsql, fparams = _filter_sql(filters)
        wsql, wparams = _where_sql(where)
        ssql, sparams = _scope_sql(scope)   # a project's view of the shared sources
        wsql, wparams = wsql + ssql, wparams + sparams
        if field and not re.match(r"^[A-Za-z0-9_.]+$", str(field)):
            raise ValueError(f"bad aggregate field {field!r}")
        # quoted JSON path so dotted field names (http.status_code) resolve as one flat key
        valexpr = (f"CAST(json_extract_string(labels, '$.\"{field}\"') AS DOUBLE)"
                   if field else "1")
        aggexpr = {
            "count": "COUNT(*)",
            "sum": f"SUM({valexpr})",
            "avg": f"AVG({valexpr})",
            "max": f"MAX({valexpr})",
            "min": f"MIN({valexpr})",
            "any": f"MAX({valexpr})",
        }[agg]
        sel_g = ", ".join(f"{e} AS g{i}" for i, e in enumerate(gexprs))
        grp_g = ", ".join(f"g{i}" for i in range(len(gexprs)))
        having = " AND ".join(f"g{i} IS NOT NULL" for i in range(len(gexprs)))
        with self._lock:
            rows = self.con.execute(
                f"SELECT {sel_g}, {aggexpr} FROM events "
                f"WHERE source IN ({ph}) AND event_time >= ?"
                f"{' AND event_time < ?' if until is not None else ''}{fsql}{wsql} "
                f"GROUP BY {grp_g} HAVING {having}",
                [*sources, since, *([until] if until is not None else []), *fparams, *wparams],
            ).fetchall()
        out = {}
        for r in rows:
            gvals, val = r[:len(gexprs)], r[-1]
            key = gvals[0] if isinstance(group_by, str) else tuple(gvals)
            out[key] = val if val is not None else 0.0
        return out

    # ── cursors (incremental connectors) ──────────────────────────────────────
    def get_cursor(self, source: str):
        with self._lock:
            r = self.con.execute("SELECT cursor FROM cursors WHERE source = ?", [source]).fetchone()
        return r[0] if r else None

    def delete_cursor(self, source: str) -> None:
        # a source may keep secondary cursors as `<source>#<what>` (GitHub: `#prs`); they go too
        with self._lock:
            self.con.execute("DELETE FROM cursors WHERE source = ? OR starts_with(source, ?)",
                             [source, source + "#"])

    def set_cursor(self, source: str, cursor: str) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO cursors VALUES (?, ?) "
                "ON CONFLICT (source) DO UPDATE SET cursor = excluded.cursor",
                [source, cursor],
            )

    # ── trigger cooldown state ────────────────────────────────────────────────
    def last_fired(self, trigger: str, key: str):
        with self._lock:
            r = self.con.execute(
                "SELECT last_fired FROM trigger_state WHERE trigger = ? AND key_value = ?",
                [trigger, key],
            ).fetchone()
        return r[0] if r else None

    def last_fired_any(self, trigger: str):
        """When the trigger last fired for any key (a custom project's trigger card)."""
        with self._lock:
            r = self.con.execute("SELECT MAX(last_fired) FROM trigger_state WHERE trigger = ?",
                                 [trigger]).fetchone()
        return r[0] if r else None

    def set_fired(self, trigger: str, key: str, ts: datetime) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO trigger_state VALUES (?, ?, ?) "
                "ON CONFLICT (trigger, key_value) DO UPDATE SET last_fired = excluded.last_fired",
                [trigger, key, ts],
            )

    def clear_fired(self, trigger: str, key: str) -> None:
        """Forget one key's last firing, so the next evaluation is not in cooldown for it. The
        firing that follows writes a fresh `set_fired`, which re-arms the cooldown as usual."""
        with self._lock:
            self.con.execute(
                "DELETE FROM trigger_state WHERE trigger = ? AND key_value = ?", [trigger, key])

    # ── catalog (DB-backed; YAML is import/export) ────────────────────────────
    def catalog_empty(self) -> bool:
        with self._lock:
            n = self.con.execute(
                "SELECT (SELECT COUNT(*) FROM catalog_sources)"
                " + (SELECT COUNT(*) FROM catalog_triggers)"
            ).fetchone()[0]
        return n == 0

    def upsert_catalog_source(self, name: str, type_: str, connector: str, poll: str, config: dict,
                              paused: bool = False, ingest_key: str | None = None) -> None:
        ts = now_utc()
        # the ingest_key is the stable, unguessable path segment for push endpoints (/ingest/<key>).
        # Generated once at creation, preserved across updates; backfilled if an older row lacks one.
        ik = ingest_key or f"{connector}-{secrets.token_hex(4)}"
        with self._lock:
            self.con.execute(
                "INSERT INTO catalog_sources "
                "(name, type, connector, poll, config, paused, created_at, updated_at, ingest_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET type = excluded.type, "
                "connector = excluded.connector, poll = excluded.poll, config = excluded.config, "
                "paused = excluded.paused, updated_at = excluded.updated_at, "
                "ingest_key = COALESCE(catalog_sources.ingest_key, excluded.ingest_key)",
                [name, type_, connector, poll, json.dumps(config), paused, ts, ts, ik],
            )

    def set_source_ingest_key(self, name: str, ingest_key: str) -> None:
        """Replace a push source's ingest key (a GitHub App recreated for an existing source)."""
        with self._lock:
            self.con.execute("UPDATE catalog_sources SET ingest_key = ? WHERE name = ?",
                             [ingest_key, name])

    def list_catalog_sources(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, type, connector, poll, config, paused, created_at, updated_at, "
                "ingest_key, owned_by, customized FROM catalog_sources ORDER BY name"
            ).fetchall()
        return [
            {"name": r[0], "type": r[1], "connector": r[2], "poll": r[3],
             "config": json.loads(r[4]), "paused": bool(r[5]),
             "created_at": r[6], "updated_at": r[7], "ingest_key": r[8],
             "owned_by": r[9], "customized": bool(r[10])}
            for r in rows
        ]

    def delete_catalog_source(self, name: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_sources WHERE name = ?", [name])
            self._forget_part("source", name)

    def set_source_paused(self, name: str, paused: bool) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE catalog_sources SET paused = ?, updated_at = ? WHERE name = ?",
                [paused, now_utc(), name],
            )

    def upsert_catalog_trigger(self, name: str, sources: list, condition: dict,
                               emit: dict, cooldown: str, filters: list | None = None,
                               key_field: str = "", description: str | None = None) -> None:
        # Explicit column list: `paused` was appended by migration, so positional VALUES no longer
        # match. A new trigger starts active (FALSE); an edit preserves the current paused state
        # (paused is intentionally NOT in the DO UPDATE SET — it's toggled via set_trigger_paused).
        # description: None keeps what is stored (a caller that does not know it), "" clears it.
        ts = now_utc()
        keep = description is None
        with self._lock:
            self.con.execute(
                "INSERT INTO catalog_triggers "
                "(name, sources, filters, key_field, condition, emit, cooldown, created_at, "
                "updated_at, paused, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?) "
                "ON CONFLICT (name) DO UPDATE SET sources = excluded.sources, "
                "filters = excluded.filters, key_field = excluded.key_field, "
                "condition = excluded.condition, emit = excluded.emit, "
                "cooldown = excluded.cooldown, updated_at = excluded.updated_at"
                + ("" if keep else ", description = excluded.description"),
                [name, json.dumps(list(sources or [])), json.dumps(list(filters or [])),
                 key_field or "", json.dumps(condition), json.dumps(emit), cooldown, ts, ts,
                 description or None],
            )

    def set_trigger_filters(self, name: str, filters: list) -> None:
        with self._lock:
            self.con.execute("UPDATE catalog_triggers SET filters = ? WHERE name = ?",
                             [json.dumps(filters or []), name])

    def set_trigger_description(self, name: str, description: str | None) -> None:
        with self._lock:
            self.con.execute("UPDATE catalog_triggers SET description = ? WHERE name = ?",
                             [description or None, name])

    def list_catalog_triggers(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, sources, filters, key_field, condition, emit, cooldown, paused, "
                "owned_by, customized, description FROM catalog_triggers ORDER BY name"
            ).fetchall()
        return [
            {"name": r[0], "sources": json.loads(r[1] or "[]"), "filters": json.loads(r[2] or "[]"),
             "key_field": r[3] or "", "condition": json.loads(r[4]),
             "emit": json.loads(r[5]), "cooldown": r[6], "paused": bool(r[7]),
             "project": r[8], "owned_by": r[8], "customized": bool(r[9]),
             "description": r[10] or ""}
            for r in rows
        ]

    def set_trigger_paused(self, name: str, paused: bool) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE catalog_triggers SET paused = ?, updated_at = ? WHERE name = ?",
                [paused, now_utc(), name],
            )

    def delete_catalog_trigger(self, name: str) -> list[str]:
        """Delete a trigger: what it woke stops being woken by it, it leaves every project's list
        of parts, and an agent that had it as its own trigger keeps going without one (a handoff
        still starts it). Returns those agents."""
        with self._lock:
            self.con.execute("DELETE FROM catalog_triggers WHERE name = ?", [name])
            self.con.execute("DELETE FROM project_wiring WHERE kind = 'wake' AND trigger = ?",
                             [name])
            self._forget_part("trigger", name)
            agents = [r[0] for r in self.con.execute(
                "SELECT name FROM catalog_agents WHERE trigger = ?", [name]).fetchall()]
            self.con.execute("UPDATE catalog_agents SET trigger = '', updated_at = ? "
                             "WHERE trigger = ?", [now_utc(), name])
        return agents

    def clear_catalog(self) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_sources")
            self.con.execute("DELETE FROM catalog_triggers")
            self.con.execute("DELETE FROM catalog_agents")
            self.con.execute("DELETE FROM mcp_servers")
            self.con.execute("DELETE FROM project_wiring")

    # ── Tares agents (a prompt attached to a trigger; enabled ⟺ subscribed) ──
    def upsert_catalog_agent(self, name: str, trigger: str, prompt: str,
                             slack_webhook: str | None = None, model: str | None = None,
                             slack_channel: str | None = None, webhook_url: str | None = None,
                             webhook_token: str | None = None,
                             mcp_servers: list[str] | None = None,
                             max_rounds: int | None = None,
                             budget_usd: float | None = None,
                             webhook_key_label: str | None = None,
                             provider: str | None = None,
                             daily_cap: int | None = None,
                             handoffs: list[dict] | None = None,
                             concludes: bool | None = None,
                             verdicts: list[dict] | None = None,
                             github: str | None = None) -> None:
        # handoffs, concludes, verdicts, github: None keeps what is stored (a caller that does
        # not know about them, such as a template re-plan, must not wipe them); [] / False / ""
        # clears
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO catalog_agents "
                "(name, trigger, prompt, slack_webhook, model, slack_channel, "
                "webhook_url, webhook_token, mcp_servers, max_rounds, budget_usd, "
                "webhook_key_label, provider, daily_cap, handoffs, concludes, verdicts, github, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET trigger = excluded.trigger, "
                "prompt = excluded.prompt, slack_webhook = excluded.slack_webhook, "
                "model = excluded.model, slack_channel = excluded.slack_channel, "
                "webhook_url = excluded.webhook_url, webhook_token = excluded.webhook_token, "
                "mcp_servers = excluded.mcp_servers, max_rounds = excluded.max_rounds, "
                "budget_usd = excluded.budget_usd, "
                "webhook_key_label = excluded.webhook_key_label, "
                "provider = excluded.provider, daily_cap = excluded.daily_cap, "
                "handoffs = COALESCE(excluded.handoffs, catalog_agents.handoffs), "
                "concludes = COALESCE(excluded.concludes, catalog_agents.concludes), "
                "verdicts = COALESCE(excluded.verdicts, catalog_agents.verdicts), "
                "github = COALESCE(excluded.github, catalog_agents.github), "
                "updated_at = excluded.updated_at",
                [name, trigger, prompt, slack_webhook or "", model or "",
                 slack_channel or "", webhook_url or "", webhook_token or "",
                 json.dumps(mcp_servers or []), max_rounds, budget_usd,
                 webhook_key_label or "", provider or "", daily_cap,
                 None if handoffs is None else json.dumps(handoffs),
                 None if concludes is None else bool(concludes),
                 None if verdicts is None else json.dumps(verdicts),
                 None if github is None else github, ts, ts],
            )
            # the agent's trigger and handoffs are the wiring of the project that made it
            # (P-TR-216); one not placed yet is wired when it is (_wire_owner)
            row = self.con.execute("SELECT owned_by FROM catalog_agents WHERE name = ?",
                                   [name]).fetchone()
            if row and row[0]:
                self._wire_owner(name, row[0], trigger, handoffs)

    def wire_agent(self, name: str, project: str, trigger: str | None,
                   handoffs: list[dict] | None) -> None:
        """`project`'s wiring of the agent: the wake-up that wakes it there (on/off kept) and,
        when given, what it hands off to there."""
        with self._lock:
            self._wire_owner(name, project, trigger, handoffs)

    def _wire_owner(self, name: str, project: str, trigger: str | None,
                    handoffs: list[dict] | None) -> None:
        """The agent's own trigger and handoffs as `project`'s wiring: the project's wake row for
        the agent follows the trigger (keeping on/off), handoffs (None: keep) replace the
        project's. Called with the lock held."""
        if trigger == "":
            # no trigger of its own any more: nothing wakes it in this project (a handoff still
            # starts it). None means "leave the wake-up as it is".
            self.con.execute("DELETE FROM project_wiring WHERE kind = 'wake' AND project = ? "
                             "AND agent = ?", [project, name])
        elif trigger:
            rows = self.con.execute(
                "SELECT trigger, enabled FROM project_wiring WHERE kind = 'wake' AND project = ? "
                "AND agent = ?", [project, name]).fetchall()
            if not rows:
                self.con.execute(
                    "INSERT INTO project_wiring (project, kind, trigger, agent, enabled, created_at) "
                    "VALUES (?, 'wake', ?, ?, FALSE, ?)", [project, trigger, name, now_utc()])
            elif not any(t == trigger for t, _on in rows):
                # one wake-up in this project: the agent's trigger changed, its row follows
                if len(rows) == 1:
                    self.con.execute(
                        "UPDATE project_wiring SET trigger = ? WHERE kind = 'wake' AND project = ? "
                        "AND agent = ?", [trigger, project, name])
                else:
                    self.con.execute(
                        "INSERT INTO project_wiring (project, kind, trigger, agent, enabled, "
                        "created_at) VALUES (?, 'wake', ?, ?, FALSE, ?)",
                        [project, trigger, name, now_utc()])
        if handoffs is not None:
            self.con.execute("DELETE FROM project_wiring WHERE kind = 'handoff' AND project = ? "
                             "AND from_agent = ?", [project, name])
            for h in handoffs:
                if isinstance(h, dict) and h.get("agent") and h.get("verdict"):
                    self.con.execute(
                        "INSERT INTO project_wiring (project, kind, from_agent, agent, verdict, "
                        "cooldown, created_at) VALUES (?, 'handoff', ?, ?, ?, ?, ?)",
                        [project, name, h["agent"], str(h["verdict"]).lower(),
                         h.get("cooldown") or None, now_utc()])

    def list_catalog_agents(self, project: str | None = None) -> list[dict]:
        """Every agent. `trigger`, `handoffs` and `enabled` are its wiring in `project`, or by
        default in the project that made it (P-TR-216: the wiring belongs to the project; an
        agent wired nowhere keeps the trigger and handoffs it was defined with)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT name, trigger, prompt, slack_webhook, model, slack_channel, "
                "webhook_url, webhook_token, mcp_servers, updated_at, max_rounds, budget_usd, owned_by, customized, "
                "webhook_key_label, provider, daily_cap, handoffs, concludes, verdicts, github "
                "FROM catalog_agents ORDER BY name"
            ).fetchall()
            wiring = self.con.execute(
                "SELECT project, kind, trigger, agent, from_agent, verdict, cooldown, enabled "
                "FROM project_wiring ORDER BY created_at").fetchall()
        wakes: dict[tuple, list] = {}
        hands: dict[tuple, list] = {}
        for p, kind, trig, agent, frm, verdict, cooldown, enabled in wiring:
            if kind == "wake":
                wakes.setdefault((p, agent), []).append((trig, bool(enabled)))
            else:
                hands.setdefault((p, frm), []).append(
                    {"verdict": verdict, "agent": agent, **({"cooldown": cooldown} if cooldown else {})})
        out = []
        for r in rows:
            where = project or r[12]
            w = wakes.get((where, r[0])) or []
            on = [t for t, e in w if e]
            out.append({
                "name": r[0], "trigger": (on or [t for t, _e in w] or [r[1]])[0],
                "triggers": [t for t, _e in w], "prompt": r[2], "slack_webhook": r[3] or "",
                "model": r[4] or "", "slack_channel": r[5] or "",
                "webhook_url": r[6] or "", "webhook_token": r[7] or "",
                "mcp_servers": json.loads(r[8]) if r[8] else [], "updated_at": r[9],
                "max_rounds": r[10], "budget_usd": r[11], "owned_by": r[12], "customized": bool(r[13]),
                "webhook_key_label": r[14] or "", "provider": r[15] or "", "daily_cap": r[16],
                "handoffs": hands.get((where, r[0]), [] if w or (where, r[0]) in hands
                                      else (json.loads(r[17]) if r[17] else [])),
                "concludes": bool(r[18]), "verdicts": json.loads(r[19]) if r[19] else [],
                "github": r[20] or "", "enabled": bool(on)})
        return out

    def get_catalog_agent(self, name: str, project: str | None = None) -> dict | None:
        return next((a for a in self.list_catalog_agents(project) if a["name"] == name), None)

    def delete_catalog_agent(self, name: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_agents WHERE name = ?", [name])
            # its wiring in every project: what wakes it, and handoffs from and to it
            self.con.execute("DELETE FROM project_wiring WHERE agent = ? OR from_agent = ?",
                             [name, name])
            self.con.execute("DELETE FROM agent_runs WHERE agent = ?", [name])
            self._forget_part("agent", name)
            # a handoff to the agent goes with it (TR-334): the agents that handed off to it
            # keep their other handoffs
            rows = self.con.execute("SELECT name, handoffs FROM catalog_agents "
                                    "WHERE handoffs IS NOT NULL").fetchall()
            for other, raw in rows:
                entries = json.loads(raw) if raw else []
                kept = [h for h in entries if h.get("agent") != name]
                if len(kept) != len(entries):
                    self.con.execute("UPDATE catalog_agents SET handoffs = ? WHERE name = ?",
                                     [json.dumps(kept), other])

    # ── agent runs (the operational record; the finding is an event, not this) ──
    def start_agent_run(self, run_id: str, agent: str, trigger: str, dispatch_id: str,
                        key: str, prompt_hash: str, max_rounds: int | None = None,
                        woken_by: str | None = None, parent_run_id: str | None = None,
                        project: str | None = None, practice: bool = False) -> None:
        # max_rounds is the cap this run will be held to (the effective value, not the agent's
        # nullable setting), so the history stays honest if defaults change later. woken_by,
        # parent_run_id and project are the run's lineage (TR-330), fixed when it starts.
        with self._lock:
            self.con.execute(
                "INSERT INTO agent_runs (id, agent, trigger, dispatch_id, key_value, status, "
                "rounds, tool_calls, prompt_hash, started_at, max_rounds, woken_by, "
                "parent_run_id, project, practice) "
                "VALUES (?, ?, ?, ?, ?, 'running', 0, 0, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, agent, trigger, dispatch_id, key, prompt_hash, now_utc(), max_rounds,
                 woken_by, parent_run_id or None, project or None, bool(practice)],
            )
            if project:   # the run belongs to its project (more join with add_run_projects)
                self.con.execute("INSERT INTO run_projects (run_id, project) VALUES (?, ?) "
                                 "ON CONFLICT DO NOTHING", [run_id, project])

    def finish_agent_run(self, run_id: str, status: str, rounds: int = 0, tool_calls: int = 0,
                         finding: str | None = None, error: str | None = None,
                         external_tools: list[str] | None = None, outcome: str | None = None,
                         verdict: str | None = None, headline: str | None = None,
                         next_step: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE agent_runs SET status = ?, rounds = ?, tool_calls = ?, finding = ?, "
                "error = ?, external_tools = ?, outcome = ?, verdict = ?, headline = ?, "
                "next_step = ?, finished_at = ?, "
                "duration_ms = CAST(date_diff('millisecond', started_at, ?) AS INTEGER) "
                "WHERE id = ?",
                [status, rounds, tool_calls, finding, error,
                 json.dumps(external_tools or []), outcome, verdict, headline or None,
                 next_step or None, now_utc(), now_utc(), run_id],
            )

    def record_run_usage(self, run_id: str, model: str, input_tokens: int, output_tokens: int,
                         cache_creation_input_tokens: int = 0,
                         cache_read_input_tokens: int = 0,
                         cost_usd: float | None = None, provider: str | None = None) -> None:
        """Stamp a run with what its model loop consumed. Separate from finish_agent_run on
        purpose: usage exists for every outcome (ok, empty, exhausted, and failed after burning
        tokens), so it is written by the loop's finally rather than each outcome path."""
        with self._lock:
            self.con.execute(
                "UPDATE agent_runs SET model = ?, input_tokens = ?, output_tokens = ?, "
                "cache_creation_input_tokens = ?, cache_read_input_tokens = ?, cost_usd = ?, "
                "provider = COALESCE(?, provider) WHERE id = ?",
                [model, input_tokens, output_tokens, cache_creation_input_tokens,
                 cache_read_input_tokens, cost_usd, provider, run_id],
            )

    def count_agent_runs(self, agent: str) -> tuple[int, int]:
        """(all runs, runs that ended ok) for one agent."""
        with self._lock:
            r = self.con.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'ok') "
                                 "FROM agent_runs WHERE agent = ?", [agent]).fetchone()
        return (int(r[0]), int(r[1])) if r else (0, 0)

    def list_agent_runs(self, agent: str | None = None, limit: int = 50, offset: int = 0,
                        status: str | None = None, where_sql: str = "",
                        where_params: list | None = None) -> list[dict]:
        """Newest first. `status` narrows to one run status (ok, failed, capped, ...); `offset`
        pages, so a console can show more without re-reading what it has. `where_sql` is an extra
        condition for the store's own lookups (by id, dispatch, parent run)."""
        sql = ("SELECT id, agent, trigger, dispatch_id, key_value, status, rounds, tool_calls, "
               "started_at, duration_ms, finding, error, external_tools, max_rounds, "
               "model, input_tokens, output_tokens, cache_creation_input_tokens, "
               "cache_read_input_tokens, cost_usd, delivery, delivery_error, provider, "
               "outcome, verdict, results, woken_by, parent_run_id, project, skills, "
               "headline, next_step, handled_at, handled_by, practice "
               "FROM agent_runs ")
        where, params = [], []
        if where_sql:
            where.append(where_sql)
            params += list(where_params or [])
        if agent:
            where.append("agent = ?")
            params.append(agent)
        if status:
            where.append("status = ?")
            params.append(status)
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params += [int(limit), max(0, int(offset))]
        with self._lock:
            rows = self.con.execute(sql, params).fetchall()
        return [
            {"id": r[0], "agent": r[1], "trigger": r[2], "dispatch_id": r[3], "key": r[4],
             "status": r[5], "rounds": r[6], "tool_calls": r[7], "started_at": r[8],
             "duration_ms": r[9], "finding": r[10], "error": r[11],
             "external_tools": json.loads(r[12]) if r[12] else [], "max_rounds": r[13],
             "model": r[14], "input_tokens": r[15], "output_tokens": r[16],
             "cache_creation_input_tokens": r[17], "cache_read_input_tokens": r[18],
             "cost_usd": r[19], "delivery": r[20], "delivery_error": r[21],
             "provider": r[22] or "", "outcome": r[23], "verdict": r[24],
             "results": json.loads(r[25]) if r[25] else [],
             "woken_by": r[26], "parent_run_id": r[27], "project": r[28],
             "skills": json.loads(r[29]) if r[29] else [],
             "headline": r[30], "next_step": r[31], "handled_at": r[32], "handled_by": r[33],
             "practice": bool(r[34])}
            for r in rows
        ]

    def runs_where(self, column: str, values: list, project: str | None = None) -> list[dict]:
        """Runs whose dispatch_id or parent_run_id is one of `values` (the timeline's indexed
        lookups), oldest first, optionally only those of one project."""
        if column not in ("dispatch_id", "parent_run_id", "id") or not values:
            return []
        sql = f"{column} IN ({', '.join(['?'] * len(values))})"
        params = list(values)
        if project is not None:
            # the runs that belong to the project (P-TR-216: its wiring started them)
            sql += " AND id IN (SELECT run_id FROM run_projects WHERE project = ?)"
            params.append(project)
        return list(reversed(self.list_agent_runs(limit=100000, where_sql=sql,
                                                  where_params=params)))

    def project_findings(self, project: str, entity: str = "", agent: str = "",
                         limit: int = 50) -> list[dict]:
        """The findings recorded in a project, newest first: its Tares agents' runs that
        concluded with one, and external agents' findings (TR-336)."""
        sql = ("id IN (SELECT run_id FROM run_projects WHERE project = ?) AND status = 'ok' "
               "AND outcome = 'finding'")
        params = [project]
        if entity:
            sql += " AND key_value = ?"
            params.append(entity)
        return self.list_agent_runs(agent or None, limit=limit, where_sql=sql,
                                    where_params=params)

    def set_run_results(self, run_id: str, results: list) -> None:
        """What the run produced (TR-220), stamped when it ends."""
        with self._lock:
            self.con.execute("UPDATE agent_runs SET results = ? WHERE id = ?",
                             [json.dumps(results), run_id])

    def set_run_skills(self, run_id: str, names: list[str]) -> None:
        """The skills the run loaded (TR-332), stamped when it ends."""
        with self._lock:
            self.con.execute("UPDATE agent_runs SET skills = ? WHERE id = ?",
                             [json.dumps(names), run_id])

    def set_run_handled(self, run_id: str, by: str | None) -> None:
        """Mark a run's result handled by `by` now, or clear the mark when `by` is None."""
        with self._lock:
            self.con.execute("UPDATE agent_runs SET handled_at = ?, handled_by = ? WHERE id = ?",
                             [now_utc() if by else None, by or None, run_id])

    def set_run_practice(self, run_id: str) -> None:
        """Mark a run as practice: an outside agent's finding answering a practice firing."""
        with self._lock:
            self.con.execute("UPDATE agent_runs SET practice = TRUE WHERE id = ?", [run_id])

    def project_threads_since(self, project: str, since) -> int:
        """How many timeline threads of a project started at or after `since` something looked
        at: the roots timeline_roots pages through, counted, without firings no agent was on
        (a trigger whose agents are all off still fires). Practice threads are left out."""
        with self._lock:
            r = self.con.execute(
                "WITH pr AS (SELECT run_id FROM run_projects WHERE project = ?), "
                "pd AS (SELECT dispatch_id FROM dispatch_projects WHERE project = ?) "
                "SELECT (SELECT count(*) FROM dispatch_log d WHERE dispatch_id IN (SELECT * FROM pd) "
                "        AND fired_at >= ? "
                "        AND NOT COALESCE(practice, FALSE) AND COALESCE(subscribers, 0) > 0 "
                "        AND NOT COALESCE(d.parent_run_id IN (SELECT * FROM pr), FALSE)) + "
                "       (SELECT count(*) FROM agent_runs r WHERE id IN (SELECT * FROM pr) "
                "        AND started_at >= ? "
                "        AND COALESCE(dispatch_id, '') = '' AND NOT COALESCE(practice, FALSE) "
                "        AND NOT COALESCE(r.parent_run_id IN (SELECT * FROM pr), FALSE))",
                [project, project, since, since]).fetchone()
        return int(r[0] or 0) if r else 0

    def project_spend_since(self, project: str, since) -> float:
        """What the runs of a project started at or after `since` cost, in USD. Practice runs are
        left out."""
        with self._lock:
            r = self.con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs "
                                 "WHERE id IN (SELECT run_id FROM run_projects WHERE project = ?) "
                                 "AND started_at >= ? "
                                 "AND NOT COALESCE(practice, FALSE)", [project, since]).fetchone()
        return float(r[0] or 0) if r else 0.0

    def recent_ingest_gaps(self, source: str, since, limit: int = 1000) -> list[float]:
        """Seconds between consecutive ingests of a source since `since`, over at most its last
        `limit` events: how often the source usually hears something (project health)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT epoch(ingest_time) FROM events WHERE source = ? AND ingest_time >= ? "
                "ORDER BY ingest_time DESC LIMIT ?", [source, since, int(limit)]).fetchall()
        ts = sorted(float(r[0]) for r in rows if r[0] is not None)
        return [b - a for a, b in zip(ts, ts[1:])]

    def recent_ingest_hours(self, source: str, since) -> list:
        """The distinct hours a source received events in since `since` (project health: a
        source with a steady rhythm can go silent, a bursty one cannot)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT DISTINCT date_trunc('hour', ingest_time) FROM events "
                "WHERE source = ? AND ingest_time >= ?", [source, since]).fetchall()
        return [r[0] for r in rows]

    def set_run_delivery(self, run_id: str, delivery: str, error: str | None = None) -> None:
        """The write-back's outcome for a run, recorded after the finding is stored: a failed
        delivery never loses the finding, it only marks the run."""
        with self._lock:
            self.con.execute("UPDATE agent_runs SET delivery = ?, delivery_error = ? WHERE id = ?",
                             [delivery, error, run_id])

    def agent_stats(self, project: str | None = None) -> dict[str, dict]:
        """Per-agent lifetime aggregates for the console, one grouped query. `finished` excludes
        `running` and `capped` (a capped run made no model call), so the success rate reflects
        runs that actually concluded or tried to. Cost sums skip NULL rows (historical runs and
        unpriced models); `uncosted_runs` says how many finished runs the sum could not see, so
        a total reads as a floor rather than a fact."""
        with self._lock:
            rows = self.con.execute(
                "SELECT agent, count(*), "
                "count(*) FILTER (WHERE status = 'ok'), "
                "count(*) FILTER (WHERE status IN ('ok', 'empty', 'failed', 'exhausted')), "
                "avg(duration_ms) FILTER (WHERE status IN ('ok', 'empty', 'failed', 'exhausted')), "
                "sum(cost_usd), sum(input_tokens), sum(output_tokens), "
                "count(*) FILTER (WHERE status IN ('ok', 'empty', 'failed', 'exhausted') "
                "                 AND cost_usd IS NULL) "
                "FROM agent_runs "
                # with a project: only the runs that belong to it (P-TR-216)
                + ("WHERE id IN (SELECT run_id FROM run_projects WHERE project = ?) "
                   if project else "") + "GROUP BY agent", [project] if project else []
            ).fetchall()
        return {
            r[0]: {"runs": int(r[1]), "ok": int(r[2]), "finished": int(r[3]),
                   "avg_duration_ms": int(r[4]) if r[4] is not None else None,
                   "cost_usd": float(r[5]) if r[5] is not None else None,
                   "input_tokens": int(r[6]) if r[6] is not None else 0,
                   "output_tokens": int(r[7]) if r[7] is not None else 0,
                   "uncosted_runs": int(r[8])}
            for r in rows
        }

    # ── model-usage ledger (the cell's Anthropic spend meter) ─────────────────
    def record_model_usage(self, surface: str, agent: str, run_id: str, model: str, calls: int,
                           input_tokens: int, output_tokens: int,
                           cache_creation_input_tokens: int = 0,
                           cache_read_input_tokens: int = 0,
                           cost_usd: float | None = None,
                           key_source: str | None = None,
                           provider: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO model_usage (id, ts, surface, agent, run_id, model, calls, "
                "input_tokens, output_tokens, cache_creation_input_tokens, "
                "cache_read_input_tokens, cost_usd, key_source, provider) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["mu_" + uuid.uuid4().hex[:12], now_utc(), surface, agent, run_id, model,
                 calls, input_tokens, output_tokens, cache_creation_input_tokens,
                 cache_read_input_tokens, cost_usd, key_source or None, provider or None],
            )
        from . import metrics
        metrics.model_usage(provider, surface, calls, input_tokens, output_tokens, cost_usd)

    def model_usage_summary(self, days: int = 30) -> dict:
        """All-time totals plus a per-day tail over the ledger. The shape a credits poller needs:
        totals to enforce against, days to draw a burn-down. `uncosted_calls` counts rows whose
        model had no known price, so the cost total is understood as a floor."""
        agg = ("coalesce(sum(calls), 0), coalesce(sum(input_tokens), 0), "
               "coalesce(sum(output_tokens), 0), coalesce(sum(cache_creation_input_tokens), 0), "
               "coalesce(sum(cache_read_input_tokens), 0), sum(cost_usd), "
               "coalesce(sum(calls) FILTER (WHERE cost_usd IS NULL), 0)")

        def shape(r) -> dict:
            return {"calls": int(r[0]), "input_tokens": int(r[1]), "output_tokens": int(r[2]),
                    "cache_creation_input_tokens": int(r[3]),
                    "cache_read_input_tokens": int(r[4]),
                    "cost_usd": float(r[5]) if r[5] is not None else None,
                    "uncosted_calls": int(r[6])}

        with self._lock:
            total = self.con.execute(f"SELECT {agg} FROM model_usage").fetchone()
            surfaces = self.con.execute(
                f"SELECT surface, {agg} FROM model_usage GROUP BY surface").fetchall()
            # Which key paid: the boundary a hosted trial is enforced against ('unknown' holds
            # rows from before attribution existed).
            key_sources = self.con.execute(
                f"SELECT coalesce(key_source, 'unknown'), {agg} FROM model_usage "
                "GROUP BY 1").fetchall()
            # Which provider served it: rows from before providers existed were all Anthropic.
            providers = self.con.execute(
                f"SELECT coalesce(provider, 'anthropic'), {agg} FROM model_usage "
                "GROUP BY 1").fetchall()
            daily = self.con.execute(
                f"SELECT CAST(ts AS DATE) AS day, {agg} FROM model_usage "
                f"WHERE ts > now() - INTERVAL {int(days)} DAY "
                "GROUP BY day ORDER BY day").fetchall()
        return {
            "total": shape(total),
            "by_surface": {r[0]: shape(r[1:]) for r in surfaces},
            "by_key_source": {r[0]: shape(r[1:]) for r in key_sources},
            "by_provider": {r[0]: shape(r[1:]) for r in providers},
            "days": [{"day": str(r[0]), **shape(r[1:])} for r in daily],
            "window_days": int(days),
        }

    def get_agent_run(self, run_id: str) -> dict | None:
        """One run by id, in the list_agent_runs shape."""
        rows = self.list_agent_runs(limit=1, where_sql="id = ?", where_params=[run_id])
        return rows[0] if rows else None

    def agent_cost_total(self, agent: str) -> float:
        """Lifetime spend of one agent, from the run log (the budget_usd cap counts against it)."""
        with self._lock:
            r = self.con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs "
                                 "WHERE agent = ?", [agent]).fetchone()
        return float(r[0] or 0)

    def agent_runs_today(self, agent: str, exclude_run_id: str | None = None,
                         include_practice: bool = True) -> int:
        """Runs started in the last 24h — the cost ceiling's counter. `exclude_run_id` leaves out the
        run being checked: the row is inserted before the cap is evaluated, so the cap must count the
        runs that came BEFORE this one or it lets one extra through.

        `capped` rows are NOT counted. A capped run returns before any model call, so it costs
        nothing, and counting it makes the cap self-sustaining: past the ceiling every further
        trigger fire writes another capped row, which holds the count at the ceiling, so the agent
        stays disabled for 24h after the last *attempt* instead of after the 50th real run. A
        trigger in a hot loop — the exact case this cap exists for — would silence its agent
        indefinitely. `failed` rows ARE counted: some of them failed after spending tokens, and
        this is a cost ceiling, so the conservative direction is to count them."""
        sql = ("SELECT count(*) FROM agent_runs WHERE agent = ? "
               "AND started_at > now() - INTERVAL 1 DAY AND status <> 'capped'")
        params: list = [agent]
        if exclude_run_id:
            sql += " AND id <> ?"
            params.append(exclude_run_id)
        if not include_practice:   # project health: a practice run is not the agent's workload
            sql += " AND NOT COALESCE(practice, FALSE)"
        with self._lock:
            row = self.con.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def reap_stale_agent_runs(self, older_than: str | None = None) -> int:
        """Close out orphaned `running` rows, returning how many were reaped.

        A run lives in the daemon process (`AgentRunner._tasks`), so if the daemon is killed
        mid-run its row stays `status='running'` forever — reading as in-flight on the runs list and
        still counting toward the daily cap. Called at startup, where nothing can legitimately be in
        flight yet, so the default reaps every `running` row; pass a window ("1h") to only reap rows
        older than it."""
        cutoff = None if older_than is None else now_utc() - parse_window(older_than)
        where = "status = 'running'" + ("" if cutoff is None else " AND started_at < ?")
        args: list = [] if cutoff is None else [cutoff]
        with self._lock:
            row = self.con.execute(f"SELECT count(*) FROM agent_runs WHERE {where}",
                                   args).fetchone()
            n = int(row[0]) if row else 0
            if n:
                ts = now_utc()
                self.con.execute(
                    "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ?, "
                    "duration_ms = CAST(date_diff('millisecond', started_at, ?) AS INTEGER) "
                    f"WHERE {where}",
                    ["interrupted: taresd stopped while this run was in flight", ts, ts] + args)
        return n

    # ── settings (instance config set from the console) ───────────────────────
    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self.con.execute("SELECT value FROM settings WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.con.execute("DELETE FROM settings WHERE key = ?", [key])
            else:
                self.con.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at", [key, value, now_utc()])

    # ── MCP connections (external tool servers a Tares agent may opt into) ─────
    # `auth_value` is a secret (an API key or full header value); redaction is the API layer's
    # job, the store holds it verbatim like connector secrets.
    def list_mcp_servers(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, url, auth_header, auth_value, updated_at, owned_by, customized, headers "
                "FROM mcp_servers ORDER BY name").fetchall()
        return [{"name": r[0], "url": r[1], "auth_header": r[2] or "",
                 "auth_value": r[3] or "", "updated_at": r[4],
                 "owned_by": r[5], "customized": bool(r[6]),
                 "headers": json.loads(r[7]) if r[7] else {}} for r in rows]

    def get_mcp_server(self, name: str) -> dict | None:
        return next((m for m in self.list_mcp_servers() if m["name"] == name), None)

    def upsert_mcp_server(self, name: str, url: str, auth_header: str | None = None,
                          auth_value: str | None = None, headers: dict | None = None) -> None:
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO mcp_servers (name, url, auth_header, auth_value, headers, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET url = excluded.url, "
                "auth_header = excluded.auth_header, auth_value = excluded.auth_value, "
                "headers = excluded.headers, updated_at = excluded.updated_at",
                [name, url, auth_header or "", auth_value or "", json.dumps(headers or {}),
                 ts, ts])

    def delete_mcp_server(self, name: str) -> None:
        """Delete an MCP server: it leaves every project's list of parts and every agent's
        tools, so no agent is left naming a server that is gone."""
        with self._lock:
            self.con.execute("DELETE FROM mcp_servers WHERE name = ?", [name])
            self._forget_part("mcp_server", name)
            for agent, raw in self.con.execute(
                    "SELECT name, mcp_servers FROM catalog_agents "
                    "WHERE mcp_servers IS NOT NULL").fetchall():
                listed = json.loads(raw) if raw else []
                if name in listed:
                    self.con.execute("UPDATE catalog_agents SET mcp_servers = ? WHERE name = ?",
                                     [json.dumps([x for x in listed if x != name]), agent])

    # ── projects (templates instantiated with params; they own ordinary catalog objects) ──
    _OWNED_TABLES = {"source": "catalog_sources",
                     "trigger": "catalog_triggers", "agent": "catalog_agents",
                     "mcp_server": "mcp_servers"}

    def set_owned_by(self, kind: str, name: str, project_id: str | None) -> None:
        """Mark an ordinary object as created by a project (or clear it). Ownership is a badge and
        a diff key, not a lock: the object stays editable and deletable everywhere."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            self.con.execute(f"UPDATE {table} SET owned_by = ?, customized = FALSE WHERE name = ?",
                             [project_id, name])
            if kind == "agent" and project_id:
                self._wire_from_definition(name, project_id)

    def _wire_from_definition(self, name: str, project: str) -> None:
        """An agent placed in a project that does not wire it yet takes the trigger and handoffs
        it was defined with as that project's wiring (off until turned on). Lock held."""
        r = self.con.execute("SELECT trigger, handoffs FROM catalog_agents WHERE name = ?",
                             [name]).fetchone()
        if r is None:
            return
        has = self.con.execute("SELECT 1 FROM project_wiring WHERE project = ? AND "
                               "(agent = ? AND kind = 'wake' OR from_agent = ?) LIMIT 1",
                               [project, name, name]).fetchone()
        if not has:
            # the project that made it starts from its own handoffs; another project that takes
            # it in wires its own (it may not have the agents those name)
            maker = self.con.execute("SELECT owned_by FROM catalog_agents WHERE name = ?",
                                     [name]).fetchone()
            mine = bool(maker) and maker[0] == project
            self._wire_owner(name, project, r[0], (json.loads(r[1]) if r[1] else []) if mine else None)

    def claim_owned_by(self, kind: str, name: str, project_id: str) -> bool:
        """Take ownership only if the object is unowned or already this project's; returns whether
        the claim holds afterwards. The check and the write share the store lock, so two projects
        adopting the same object cannot both win."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            self.con.execute(f"UPDATE {table} SET owned_by = ?, customized = FALSE "
                             f"WHERE name = ? AND (owned_by IS NULL OR owned_by = ?)",
                             [project_id, name, project_id])
            row = self.con.execute(f"SELECT owned_by FROM {table} WHERE name = ?", [name]).fetchone()
        return bool(row and row[0] == project_id)

    def release_owned_by(self, kind: str, name: str, project_id: str) -> None:
        """Clear ownership only while the object is still this project's (a same-name object
        recreated by hand and claimed by another project is left alone)."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            self.con.execute(f"UPDATE {table} SET owned_by = NULL, customized = FALSE "
                             f"WHERE name = ? AND owned_by = ?", [name, project_id])

    def mark_customized(self, kind: str, name: str) -> bool:
        """Called by the normal update paths: if the object is owned by a project, flag it so the
        engine keeps the user's version on the next re-plan. Returns whether it was owned."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            row = self.con.execute(f"SELECT owned_by FROM {table} WHERE name = ?", [name]).fetchone()
            if not row or not row[0]:
                return False
            owner = self.con.execute("SELECT recipe FROM usecases WHERE id = ?", [row[0]]).fetchone()
            if owner and owner[0] in ("custom", "default"):
                return True   # adopted, not planned: there is no planned version to diverge from
            self.con.execute(f"UPDATE {table} SET customized = TRUE WHERE name = ?", [name])
            self.con.execute("UPDATE usecase_objects SET customized = TRUE "
                             "WHERE usecase_id = ? AND kind = ? AND name = ?", [row[0], kind, name])
        return True

    def create_project(self, uid: str, template: str, name: str, params: dict,
                       status: str = "active", goal: str | None = None) -> None:
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO usecases (id, recipe, name, params, status, created_at, updated_at, "
                "last_error, goal) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                [uid, template, name, json.dumps(params), status, ts, ts, goal or None])

    def update_project(self, uid: str, params: dict | None = None, status: str | None = None,
                       last_error: str | None = "", name: str | None = None,
                       goal: str | None = "") -> None:
        """last_error and goal: "" (default) leaves it unchanged; None clears it; a string sets
        it."""
        sets, vals = ["updated_at = ?"], [now_utc()]
        if goal != "":
            sets.append("goal = ?"); vals.append(goal)
        if params is not None:
            sets.append("params = ?"); vals.append(json.dumps(params))
        if status is not None:
            sets.append("status = ?"); vals.append(status)
        if name is not None:
            sets.append("name = ?"); vals.append(name)
        if last_error != "":
            sets.append("last_error = ?"); vals.append(last_error)
        vals.append(uid)
        with self._lock:
            self.con.execute(f"UPDATE usecases SET {', '.join(sets)} WHERE id = ?", vals)

    @staticmethod
    def _project_row(r) -> dict:
        return {"id": r[0], "template": r[1], "name": r[2], "params": json.loads(r[3] or "{}"),
                "status": r[4], "created_at": r[5], "updated_at": r[6], "last_error": r[7],
                "goal": r[8], "kind": r[9]}

    def list_projects(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, recipe, name, params, status, created_at, updated_at, last_error, "
                "goal, kind FROM usecases ORDER BY created_at").fetchall()
        return [self._project_row(r) for r in rows]

    def get_project(self, uid: str) -> dict | None:
        with self._lock:
            r = self.con.execute(
                "SELECT id, recipe, name, params, status, created_at, updated_at, last_error, "
                "goal, kind FROM usecases WHERE id = ?", [uid]).fetchone()
        return self._project_row(r) if r else None

    def set_project_kind(self, uid: str, kind: str | None) -> None:
        with self._lock:
            self.con.execute("UPDATE usecases SET kind = ? WHERE id = ?", [kind or None, uid])

    def _factory_kind_upgrade(self) -> None:
        """Once: a project from before project kinds that holds docs and tickets and no agent was
        made by a spec session; it is a software factory project. Lock not needed (init)."""
        if self.con.execute("SELECT value FROM settings WHERE key = 'factory_kind_filled'"
                            ).fetchone():
            return
        self.con.execute(
            "UPDATE usecases SET kind = 'software_factory' WHERE kind IS NULL AND recipe = 'custom' "
            "AND id IN (SELECT project FROM tickets) "
            "AND id IN (SELECT usecase_id FROM usecase_objects WHERE kind = 'doc') "
            "AND id NOT IN (SELECT usecase_id FROM usecase_objects WHERE kind = 'agent')")
        self.con.execute("INSERT INTO settings (key, value, updated_at) VALUES "
                         "('factory_kind_filled', '1', ?) ON CONFLICT (key) DO NOTHING",
                         [now_utc()])

    def get_project_by_name(self, name: str) -> dict | None:
        return next((u for u in self.list_projects() if u["name"] == name), None)

    def delete_project(self, uid: str) -> None:
        with self._lock:
            # its keys stop working and nothing is delivered for it any more (TR-335)
            self.con.execute("UPDATE api_keys SET revoked_at = ? WHERE project = ? "
                             "AND revoked_at IS NULL", [now_utc(), uid])
            self.con.execute("DELETE FROM subscriptions WHERE project = ?", [uid])
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ?", [uid])
            self.con.execute("DELETE FROM project_wiring WHERE project = ?", [uid])
            self._forget_unused_skills()   # a skill another project uses stays
            self._forget_unused_docs()     # so does a doc
            self.con.execute("DELETE FROM tickets WHERE project = ?", [uid])
            self.con.execute("DELETE FROM project_sessions WHERE project = ?", [uid])
            self.con.execute("DELETE FROM session_states WHERE session NOT IN (SELECT session "
                             "FROM project_sessions)")
            self.con.execute("DELETE FROM usecase_log WHERE usecase_id = ?", [uid])
            self.con.execute("DELETE FROM usecases WHERE id = ?", [uid])

    def get_project_setup(self, uid: str) -> dict | None:
        """The guided setup of a project ({plan, step, practice_run, ...}), or None when it was
        not set up that way."""
        with self._lock:
            r = self.con.execute("SELECT setup FROM usecases WHERE id = ?", [uid]).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    def set_project_setup(self, uid: str, setup: dict | None) -> None:
        with self._lock:
            self.con.execute("UPDATE usecases SET setup = ?, updated_at = ? WHERE id = ?",
                             [json.dumps(setup, default=str) if setup is not None else None,
                              now_utc(), uid])

    def upsert_project_object(self, uid: str, kind: str, key: str, name: str) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO usecase_objects (usecase_id, kind, key, name, customized, created_at) "
                "VALUES (?, ?, ?, ?, FALSE, ?) ON CONFLICT (usecase_id, kind, key) DO UPDATE SET "
                "name = excluded.name", [uid, kind, key, name, now_utc()])

    def list_project_objects(self, uid: str) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT kind, key, name, customized, created_at FROM usecase_objects "
                "WHERE usecase_id = ? ORDER BY created_at, kind, key", [uid]).fetchall()
        return [{"kind": r[0], "key": r[1], "name": r[2], "customized": bool(r[3]),
                 "created_at": r[4]} for r in rows]

    def delete_project_object(self, uid: str, kind: str, key: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ? AND kind = ? "
                             "AND key = ?", [uid, kind, key])

    # ── skills (TR-332): parts of the cell (P-TR-216), used by projects; validated by
    # tares/skills.py before they get here ──
    def list_skills(self, project: str) -> list[dict]:
        """The skills a project uses, without their bodies: name, description, updated_at, size
        (bytes of the body)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT DISTINCT s.name, s.description, s.updated_at, octet_length(encode(s.body)) "
                "FROM shared_skills s JOIN usecase_objects o ON o.kind = 'skill' AND o.name = s.name "
                "WHERE o.usecase_id = ? ORDER BY s.name", [project]).fetchall()
        return [{"name": r[0], "description": r[1], "updated_at": r[2], "size": int(r[3] or 0)}
                for r in rows]

    def list_all_skills(self) -> list[dict]:
        """Every skill on the cell with its body, the project that made it (`project`) and the
        projects that use it (`projects`)."""
        with self._lock:
            rows = self.con.execute("SELECT name, description, body, made_by FROM shared_skills "
                                    "ORDER BY name").fetchall()
            users = self.con.execute("SELECT name, usecase_id FROM usecase_objects WHERE "
                                     "kind = 'skill' GROUP BY name, usecase_id").fetchall()
        by: dict[str, list] = {}
        for n, u in users:
            by.setdefault(n, []).append(u)
        return [{"project": r[3], "name": r[0], "description": r[1], "body": r[2],
                 "projects": sorted(by.get(r[0], []))} for r in rows]

    def get_skill(self, project: str | None, name: str) -> dict | None:
        """A skill the project uses (any project, with None)."""
        with self._lock:
            if project is not None and not self.con.execute(
                    "SELECT 1 FROM usecase_objects WHERE usecase_id = ? AND kind = 'skill' "
                    "AND name = ?", [project, name]).fetchone():
                return None
            r = self.con.execute(
                "SELECT name, description, body, created_at, updated_at FROM shared_skills "
                "WHERE name = ?", [name]).fetchone()
        return ({"name": r[0], "description": r[1], "body": r[2], "created_at": r[3],
                 "updated_at": r[4]} if r else None)

    def _use_skill(self, project: str, name: str) -> bool:
        """The project uses the skill; True when it did not before. Lock held."""
        if self.con.execute("SELECT 1 FROM usecase_objects WHERE usecase_id = ? AND kind = 'skill' "
                            "AND name = ?", [project, name]).fetchone():
            return False
        self.con.execute("INSERT INTO usecase_objects (usecase_id, kind, key, name, customized, "
                         "created_at) VALUES (?, 'skill', ?, ?, FALSE, ?) ON CONFLICT DO NOTHING",
                         [project, f"skill:{name}", name, now_utc()])
        return True

    def use_skill(self, project: str, name: str) -> bool:
        """A skill already on the cell, used by one more project. False when there is no such
        skill; the project already using it is fine."""
        with self._lock:
            if not self.con.execute("SELECT 1 FROM shared_skills WHERE name = ?", [name]).fetchone():
                return False
            self._use_skill(project, name)
        return True

    def upsert_skill(self, project: str, name: str, description: str, body: str) -> bool:
        """Create or replace the skill (shared: every project that uses it sees the change), and
        have the project use it. Returns True when the project did not have it before."""
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO shared_skills (name, description, body, made_by, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
                "description = excluded.description, body = excluded.body, "
                "updated_at = excluded.updated_at", [name, description, body, project, ts, ts])
            return self._use_skill(project, name)

    def delete_skill(self, project: str, name: str) -> bool:
        """The project stops using the skill; the skill goes when no project uses it any more.
        Returns whether the project had it."""
        with self._lock:
            had = self.con.execute("SELECT 1 FROM usecase_objects WHERE usecase_id = ? AND "
                                   "kind = 'skill' AND name = ?", [project, name]).fetchone()
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ? AND kind = 'skill' "
                             "AND name = ?", [project, name])
            self._forget_unused_skills()
        return had is not None

    def _forget_unused_skills(self) -> None:
        """Skills no project uses any more go. Lock held."""
        self.con.execute("DELETE FROM shared_skills WHERE name NOT IN (SELECT name FROM "
                         "usecase_objects WHERE kind = 'skill')")

    def skill_users(self, name: str) -> list[str]:
        return self.projects_using("skill", name)

    def mark_skill_customized(self, project: str, name: str) -> None:
        """A skill a template planned was edited by hand: a re-plan keeps the edit."""
        with self._lock:
            self.con.execute("UPDATE usecase_objects SET customized = TRUE WHERE usecase_id = ? "
                             "AND kind = 'skill' AND name = ?", [project, name])

    # ── docs and tickets (TR-403): validated by tares/docs.py before they get here ──
    _DOC_COLS = "d.id, d.kind, d.title, d.made_by, d.updated_by, d.created_at, d.updated_at"

    @staticmethod
    def _doc_row(r, body: str | None = None) -> dict:
        out = {"id": r[0], "kind": r[1], "title": r[2], "made_by": r[3], "updated_by": r[4],
               "created_at": r[5], "updated_at": r[6]}
        if body is not None:
            out["body"] = body
        return out

    def create_doc(self, project: str | None, kind: str, title: str, body: str,
                   by: str = "") -> str:
        """A new doc on the cell, included in `project` (None: on the cell only, for every
        project to include). Returns its id."""
        doc_id = "doc_" + uuid.uuid4().hex[:10]
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO docs (id, kind, title, body, made_by, updated_by, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [doc_id, kind, title, body, project, by or None, ts, ts])
            if project:
                self._use_doc(project, doc_id)
        return doc_id

    def update_doc(self, doc_id: str, kind: str | None = None, title: str | None = None,
                   body: str | None = None, by: str = "") -> None:
        sets, vals = ["updated_at = ?", "updated_by = ?"], [now_utc(), by or None]
        for col, v in (("kind", kind), ("title", title), ("body", body)):
            if v is not None:
                sets.append(f"{col} = ?"); vals.append(v)
        vals.append(doc_id)
        with self._lock:
            self.con.execute(f"UPDATE docs SET {', '.join(sets)} WHERE id = ?", vals)

    def get_doc(self, project: str | None, doc_id: str) -> dict | None:
        """A doc the project includes (any doc, with None), with its body and the projects that
        include it."""
        with self._lock:
            if project is not None and not self.con.execute(
                    "SELECT 1 FROM usecase_objects WHERE usecase_id = ? AND kind = 'doc' "
                    "AND name = ?", [project, doc_id]).fetchone():
                return None
            r = self.con.execute(f"SELECT {self._DOC_COLS}, d.body FROM docs d WHERE d.id = ?",
                                 [doc_id]).fetchone()
            if not r:
                return None
            users = [u[0] for u in self.con.execute(
                "SELECT usecase_id FROM usecase_objects WHERE kind = 'doc' AND name = ? "
                "ORDER BY usecase_id", [doc_id]).fetchall()]
        return {**self._doc_row(r, r[7]), "projects": users}

    def list_docs(self, project: str, kind: str | None = None) -> list[dict]:
        """The docs a project includes, without bodies: size in bytes instead."""
        q = (f"SELECT DISTINCT {self._DOC_COLS}, octet_length(encode(d.body)) FROM docs d "
             "JOIN usecase_objects o ON o.kind = 'doc' AND o.name = d.id WHERE o.usecase_id = ?")
        vals: list = [project]
        if kind:
            q += " AND d.kind = ?"; vals.append(kind)
        with self._lock:
            rows = self.con.execute(q, vals).fetchall()
        return [{**self._doc_row(r), "size": int(r[7] or 0)} for r in rows]

    def list_cell_docs(self) -> list[dict]:
        """Every doc on the cell (no bodies), with the projects that include it."""
        with self._lock:
            rows = self.con.execute(f"SELECT {self._DOC_COLS}, octet_length(encode(d.body)) "
                                    "FROM docs d ORDER BY d.title").fetchall()
            users = self.con.execute("SELECT name, usecase_id FROM usecase_objects WHERE "
                                     "kind = 'doc' GROUP BY name, usecase_id").fetchall()
        by: dict[str, list] = {}
        for n, u in users:
            by.setdefault(n, []).append(u)
        return [{**self._doc_row(r), "size": int(r[7] or 0), "projects": sorted(by.get(r[0], []))}
                for r in rows]

    def _use_doc(self, project: str, doc_id: str) -> None:
        """Lock held."""
        self.con.execute("INSERT INTO usecase_objects (usecase_id, kind, key, name, customized, "
                         "created_at) VALUES (?, 'doc', ?, ?, FALSE, ?) ON CONFLICT DO NOTHING",
                         [project, f"doc:{doc_id}", doc_id, now_utc()])

    def use_doc(self, project: str, doc_id: str) -> bool:
        """A doc already on the cell, included in one more project. False when there is no such
        doc."""
        with self._lock:
            if not self.con.execute("SELECT 1 FROM docs WHERE id = ?", [doc_id]).fetchone():
                return False
            self._use_doc(project, doc_id)
        return True

    def remove_doc(self, project: str, doc_id: str) -> bool:
        """The project stops including the doc; the doc is deleted when no project includes it
        any more and it was made by a project (a doc made on the cell stays). A ticket of this
        project whose working doc it was loses the link. Returns whether the project had it."""
        with self._lock:
            had = self.con.execute("SELECT 1 FROM usecase_objects WHERE usecase_id = ? AND "
                                   "kind = 'doc' AND name = ?", [project, doc_id]).fetchone()
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ? AND kind = 'doc' "
                             "AND name = ?", [project, doc_id])
            self.con.execute("UPDATE tickets SET working_doc = NULL WHERE project = ? AND "
                             "working_doc = ?", [project, doc_id])
            self._forget_unused_docs()
        return had is not None

    def _forget_unused_docs(self) -> None:
        """Docs a project made that no project includes any more go. Lock held."""
        self.con.execute("DELETE FROM docs WHERE made_by IS NOT NULL AND id NOT IN (SELECT name "
                         "FROM usecase_objects WHERE kind = 'doc')")

    _TICKET_COLS = ("id, project, owner, external_id, identifier, url, title, status, position, "
                    "working_doc, created_at, updated_at")

    @staticmethod
    def _ticket_row(r) -> dict:
        return {"id": r[0], "project": r[1], "owner": r[2], "external_id": r[3],
                "identifier": r[4], "url": r[5], "title": r[6], "status": r[7],
                "position": r[8], "working_doc": r[9], "created_at": r[10], "updated_at": r[11]}

    def create_ticket(self, project: str, owner: str, title: str, status: str = "todo",
                      position: float | None = None, external_id: str | None = None,
                      identifier: str | None = None, url: str | None = None,
                      working_doc: str | None = None) -> str:
        """A ticket at the end of the project's list unless `position` says where."""
        tid = "tk_" + uuid.uuid4().hex[:10]
        ts = now_utc()
        with self._lock:
            if position is None:
                last = self.con.execute("SELECT max(position) FROM tickets WHERE project = ?",
                                        [project]).fetchone()[0]
                position = (last or 0) + 1
            self.con.execute(
                f"INSERT INTO tickets ({self._TICKET_COLS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [tid, project, owner, external_id, identifier, url, title, status,
                 float(position), working_doc, ts, ts])
        return tid

    def update_ticket(self, tid: str, **fields) -> None:
        """Set the given columns (title, status, position, working_doc, identifier, url,
        external_id); None clears working_doc only when passed explicitly."""
        allowed = {"title", "status", "position", "working_doc", "identifier", "url",
                   "external_id"}
        sets, vals = ["updated_at = ?"], [now_utc()]
        for col, v in fields.items():
            if col in allowed:
                sets.append(f"{col} = ?"); vals.append(v)
        vals.append(tid)
        with self._lock:
            self.con.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", vals)

    def update_ticket_owner(self, tid: str, owner: str) -> None:
        """A project stopped using Linear: its tickets become Tares's (TR-408)."""
        with self._lock:
            self.con.execute("UPDATE tickets SET owner = ?, updated_at = ? WHERE id = ?",
                             [owner, now_utc(), tid])

    def list_tickets(self, project: str) -> list[dict]:
        with self._lock:
            rows = self.con.execute(f"SELECT {self._TICKET_COLS} FROM tickets WHERE project = ? "
                                    "ORDER BY position, created_at", [project]).fetchall()
        return [self._ticket_row(r) for r in rows]

    def get_ticket(self, project: str, ref: str) -> dict | None:
        """A ticket of the project by its id, Linear identifier (ENG-12, any case) or Linear
        issue id."""
        with self._lock:
            r = self.con.execute(
                f"SELECT {self._TICKET_COLS} FROM tickets WHERE project = ? AND (id = ? OR "
                "upper(identifier) = upper(?) OR external_id = ?)", [project, ref, ref, ref]
            ).fetchone()
        return self._ticket_row(r) if r else None

    def delete_ticket(self, tid: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM tickets WHERE id = ?", [tid])

    def link_session(self, project: str, session: str, repo: str | None = None) -> bool:
        """The Claude Code session works in the project. True when it was not linked before."""
        with self._lock:
            had = self.con.execute("SELECT 1 FROM project_sessions WHERE project = ? AND "
                                   "session = ?", [project, session]).fetchone()
            if not had:
                self.con.execute("INSERT INTO project_sessions (project, session, repo, linked_at) "
                                 "VALUES (?, ?, ?, ?)", [project, session, repo, now_utc()])
        return not had

    def project_sessions(self, project: str) -> list[dict]:
        """The sessions that worked in the project, newest activity first, with when they started
        and last did something, how many lines they have, and their state (working, waiting,
        ended; None before the plugin reported one) with its reason and time."""
        with self._lock:
            rows = self.con.execute(
                "SELECT s.session, s.repo, s.linked_at, min(e.event_time), max(e.event_time), "
                "count(e.key_value), st.state, st.reason, st.state_at FROM project_sessions s "
                "LEFT JOIN events e ON e.source = 'claude_code' AND e.key_value = s.session "
                "LEFT JOIN session_states st ON st.session = s.session WHERE s.project = ? "
                "GROUP BY s.session, s.repo, s.linked_at, st.state, st.reason, st.state_at "
                "ORDER BY max(e.event_time) DESC NULLS LAST", [project]).fetchall()
        return [{"session": r[0], "repo": r[1], "linked_at": r[2], "started_at": r[3],
                 "last_at": r[4], "lines": int(r[5] or 0), "state": r[6], "state_reason": r[7],
                 "state_at": r[8]} for r in rows]

    def set_session_state(self, session: str, state: str, reason: str | None, at,
                          force: bool = False) -> None:
        """The session's state now; an older line than the one stored does not win, unless
        `force` (a take-over is a decision, not a report)."""
        when = "" if force else " WHERE excluded.state_at >= session_states.state_at"
        with self._lock:
            self.con.execute(
                "INSERT INTO session_states (session, state, reason, state_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (session) DO UPDATE SET state = excluded.state, "
                "reason = excluded.reason, state_at = excluded.state_at" + when,
                [session, state, reason or None, at])

    def said_lately(self, source: str, session: str, n: int = 3) -> list[dict]:
        """The session's last `n` assistant messages (not tool calls), oldest first."""
        with self._lock:
            rows = self.con.execute(
                "SELECT text, event_time FROM events WHERE source = ? AND key_value = ? AND "
                "event_type = 'assistant' ORDER BY event_time DESC LIMIT 60",
                [source, session]).fetchall()
        out = []
        for t, at in rows:
            t = (t or "").strip()
            if t and not t.startswith("→"):
                out.append({"text": t, "at": at})
            if len(out) >= n:
                break
        return list(reversed(out))

    def files_touched(self, source: str, session: str, limit: int = 20) -> list[str]:
        """The files the session last edited or wrote (its Edit / Write / NotebookEdit calls),
        most recent first, each once."""
        with self._lock:
            rows = self.con.execute(
                "SELECT payload FROM events WHERE source = ? AND key_value = ? AND event_type = "
                "'tool_use' ORDER BY event_time DESC LIMIT 400", [source, session]).fetchall()
        seen: list[str] = []
        for (pj,) in rows:
            try:
                o = json.loads(pj) if pj else {}
            except (TypeError, ValueError):
                continue
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                if str(b.get("name") or "") not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                path = str(inp.get("file_path") or inp.get("notebook_path") or "")
                if path and path not in seen:
                    seen.append(path)
            if len(seen) >= limit:
                break
        return seen

    def last_said(self, source: str, session: str) -> str | None:
        """The last thing the session's assistant wrote (not a tool call), for a waiting
        session: what it stopped on."""
        with self._lock:
            rows = self.con.execute(
                "SELECT text FROM events WHERE source = ? AND key_value = ? AND event_type = "
                "'assistant' ORDER BY event_time DESC LIMIT 20", [source, session]).fetchall()
        for (t,) in rows:
            t = (t or "").strip()
            if t and not t.startswith("→"):
                return t
        return None

    def session_projects(self, session: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT project FROM project_sessions WHERE session = ?", [session]).fetchall()]

    def get_project_linear(self, uid: str) -> dict | None:
        """The Linear project a project's tickets live in, with the last sync's state, or None."""
        with self._lock:
            r = self.con.execute("SELECT linear FROM usecases WHERE id = ?", [uid]).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    def set_project_linear(self, uid: str, linear: dict | None) -> None:
        with self._lock:
            self.con.execute("UPDATE usecases SET linear = ? WHERE id = ?",
                             [json.dumps(linear, default=str) if linear is not None else None,
                              uid])

    def linear_projects(self) -> list[tuple[str, dict]]:
        """(project id, its Linear link) for every project that uses Linear."""
        with self._lock:
            rows = self.con.execute("SELECT id, linear FROM usecases WHERE linear IS NOT NULL"
                                    ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows if r[1]]

    def skill_loads(self, agents: list[str], days: int = 7) -> dict[str, list[str]]:
        """{skill: [agents]} for the runs of `agents` in the last `days` that loaded a skill."""
        if not agents:
            return {}
        from datetime import timedelta
        since = now_utc() - timedelta(days=days)
        marks = ", ".join("?" for _ in agents)
        with self._lock:
            rows = self.con.execute(
                f"SELECT agent, skills FROM agent_runs WHERE agent IN ({marks}) "
                "AND skills IS NOT NULL AND started_at >= ? ORDER BY started_at DESC",
                [*agents, since]).fetchall()
        out: dict[str, list[str]] = {}
        for agent, names in rows:
            for n in json.loads(names or "[]"):
                if agent not in out.setdefault(n, []):
                    out[n].append(agent)
        return out

    # ── project membership: every object is in a project ─────────────────────
    # A trigger, agent or MCP server belongs to exactly one project (owned_by, plus its
    # usecase_objects row); a source is in any number (one usecase_objects row per project). The
    # default project holds what no other project does. These helpers keep the two records in
    # step, and keep a custom project's `objects` param (its whole configuration) in step too.
    DEFAULT_PROJECT_NAME = "Default"

    def default_project_id(self) -> str:
        with self._lock:
            return self._ensure_default_project()

    def default_project_id_if_any(self) -> str | None:
        with self._lock:
            return self._default_project_if_any()

    def _default_project_if_any(self) -> str | None:
        """The default project's id when the cell has one, never creating it. Called with the
        lock held."""
        row = self.con.execute("SELECT value FROM settings WHERE key = 'default_project'").fetchone()
        if row and self.con.execute("SELECT 1 FROM usecases WHERE id = ?", [row[0]]).fetchone():
            return row[0]
        found = self.con.execute("SELECT id FROM usecases WHERE recipe = 'default' "
                                 "ORDER BY created_at LIMIT 1").fetchone()
        return found[0] if found else None

    def _lazy_default_project(self):
        """A function giving the default project's id, made on its first call and only then. The
        default project exists from the first time something has no other project to sit in, so
        a cell where everything starts in a project never has one. Called with the lock held."""
        made: list[str | None] = [None]

        def get() -> str:
            if made[0] is None:
                made[0] = self._ensure_default_project()
            return made[0]
        return get

    def _ensure_default_project(self) -> str:
        """The default project's id, creating the project if the cell has none yet. Called with
        the lock held."""
        row = self.con.execute("SELECT value FROM settings WHERE key = 'default_project'").fetchone()
        if row and self.con.execute("SELECT 1 FROM usecases WHERE id = ?", [row[0]]).fetchone():
            return row[0]
        found = self.con.execute("SELECT id FROM usecases WHERE recipe = 'default' "
                                 "ORDER BY created_at LIMIT 1").fetchone()
        if found:
            uid = found[0]
        else:
            uid = "uc_" + uuid.uuid4().hex[:10]
            taken = {r[0] for r in self.con.execute("SELECT name FROM usecases").fetchall()}
            name = self.DEFAULT_PROJECT_NAME if self.DEFAULT_PROJECT_NAME not in taken \
                else "Default project"
            ts = now_utc()
            self.con.execute(
                "INSERT INTO usecases (id, recipe, name, params, status, created_at, updated_at, "
                "last_error) VALUES (?, 'default', ?, '{}', 'active', ?, ?, NULL)",
                [uid, name, ts, ts])
        self.con.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('default_project', ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at", [uid, now_utc()])
        return uid

    def _recipes(self) -> dict:
        return {r[0]: r[1] for r in self.con.execute("SELECT id, recipe FROM usecases").fetchall()}

    def _custom_objects(self, uid: str, change) -> None:
        """Rewrite a custom project's `objects` param with `change(list) -> list`."""
        row = self.con.execute("SELECT params FROM usecases WHERE id = ?", [uid]).fetchone()
        params = json.loads((row[0] if row else None) or "{}")
        objs = list(params.get("objects") or [])
        new = change(objs)
        if new != objs:
            params["objects"] = new
            self.con.execute("UPDATE usecases SET params = ?, updated_at = ? WHERE id = ?",
                             [json.dumps(params), now_utc(), uid])

    def _add_row(self, uid: str, kind: str, name: str, key: str | None = None) -> None:
        """Record `name` as one of project `uid`'s objects. Without a `key`, an existing row for
        the name is kept as it is (a template's planned key must survive a later move back); a
        new row is keyed `kind:name` in a custom or default project (the key a custom plan uses)
        and `+kind:name` in a template project, where the `+` keeps it apart from the plan."""
        recipe = self._recipes().get(uid)
        if recipe is None:
            raise KeyError(f"unknown project {uid!r}")
        have = self.con.execute("SELECT key FROM usecase_objects WHERE usecase_id = ? AND kind = ? "
                                "AND name = ?", [uid, kind, name]).fetchall()
        if key is None:
            if have:
                return
            key = f"{kind}:{name}" if recipe in ("custom", "default") else f"+{kind}:{name}"
        else:
            for (k,) in have:
                if k != key:
                    self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ? AND kind = ? "
                                     "AND key = ?", [uid, kind, k])
        self.con.execute(
            "INSERT INTO usecase_objects (usecase_id, kind, key, name, customized, created_at) "
            "VALUES (?, ?, ?, ?, FALSE, ?) ON CONFLICT (usecase_id, kind, key) DO UPDATE SET "
            "name = excluded.name", [uid, kind, key, name, now_utc()])
        if recipe == "custom":
            self._custom_objects(uid, lambda objs: objs if any(
                o.get("kind") == kind and o.get("name") == name for o in objs)
                else objs + [{"kind": kind, "name": name}])

    def _drop_rows(self, kind: str, name: str, uids) -> None:
        """Forget `name` as an object of each project in `uids`."""
        recipes = self._recipes()
        for uid in uids:
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ? AND kind = ? "
                             "AND name = ?", [uid, kind, name])
            if recipes.get(uid) == "custom":
                self._custom_objects(uid, lambda objs: [
                    o for o in objs if not (o.get("kind") == kind and o.get("name") == name)])

    def _forget_part(self, kind: str, name: str) -> None:
        """A part deleted from the cell leaves every project's list of parts: deleting it was
        the choice, not something to repair. Called with the lock held."""
        self._drop_rows(kind, name, set(self._rows_for(kind, name))
                        | {u for u, r in self._recipes().items() if r == "custom"})

    def _rows_for(self, kind: str, name: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT usecase_id FROM usecase_objects WHERE kind = ? AND name = ?",
            [kind, name]).fetchall()]

    def put_in_project(self, kind: str, name: str, uid: str, key: str | None = None,
                       creator: bool = False) -> None:
        """Put a part in project `uid` (P-TR-216: parts belong to the cell; a project uses them,
        any number of projects the same part). It stays in its other projects. The default
        project only holds what no other project does: a part that was there for want of
        another project leaves it, and the default project's wiring of it comes along (a source
        stays while a default trigger still reads it). `creator` records `uid` as the project
        that made it; a part with no maker takes `uid`."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            # None on a cell that has none: nothing can be leaving it
            default = self._default_project_if_any()
            row = self.con.execute(f"SELECT owned_by FROM {table} WHERE name = ?",
                                   [name]).fetchone()
            owner = row[0] if row else None
            if kind == "source":
                if creator:
                    self.con.execute(f"UPDATE {table} SET owned_by = ? WHERE name = ?", [uid, name])
                if uid != default and default in self._rows_for("source", name):
                    # a trigger of the default project still reads it: it stays there too
                    readers = [r[0] for r in self.con.execute(
                        "SELECT t.name, t.sources FROM catalog_triggers t JOIN usecase_objects o "
                        "ON o.kind = 'trigger' AND o.name = t.name AND o.usecase_id = ?",
                        [default]).fetchall() if name in json.loads(r[1] or "[]")]
                    if not readers:
                        self._drop_rows("source", name, [default])
            else:
                # the default project holds what no other project does ("whatever was made outside
                # another project"): a part another project takes leaves it, with the default
                # project's wiring of it, so what ran there runs on in the project it joined
                leaving_default = uid != default and default in self._rows_for(kind, name)
                if leaving_default:
                    self._drop_rows(kind, name, [default])
                    if kind == "agent":
                        self.con.execute(
                            "UPDATE project_wiring SET project = ? WHERE project = ? AND "
                            "(agent = ? OR from_agent = ?)", [uid, default, name, name])
                    elif kind == "trigger":
                        self.con.execute(
                            "UPDATE project_wiring SET project = ? WHERE project = ? AND "
                            "kind = 'wake' AND trigger = ?", [uid, default, name])
                if creator or owner is None or (leaving_default and owner in (None, default)):
                    self.con.execute(f"UPDATE {table} SET owned_by = ? WHERE name = ?", [uid, name])
                if kind == "agent":
                    self._wire_from_definition(name, uid)
            self._add_row(uid, kind, name, key)

    def remove_from_project(self, kind: str, name: str, uid: str) -> None:
        """Take a part out of a project, with that project's wiring of it. A part left in no
        project joins the default one; a part whose maker leaves it gets another project that
        uses it as its maker."""
        with self._lock:
            self._drop_rows(kind, name, [uid])
            if kind == "agent":
                self.con.execute("DELETE FROM project_wiring WHERE project = ? AND "
                                 "(agent = ? OR from_agent = ?)", [uid, name, name])
            elif kind == "trigger":
                self.con.execute("DELETE FROM project_wiring WHERE project = ? AND kind = 'wake' "
                                 "AND trigger = ?", [uid, name])
            table = self._OWNED_TABLES[kind]
            exists = self.con.execute(f"SELECT owned_by FROM {table} WHERE name = ?",
                                      [name]).fetchone()
            if exists is None:
                return
            left = self._rows_for(kind, name)
            if not left:
                default = self._ensure_default_project()
                self._add_row(default, kind, name)
                left = [default]
            if kind != "source" and exists[0] == uid:
                self.con.execute(f"UPDATE {table} SET owned_by = ? WHERE name = ?", [left[0], name])

    def source_memberships(self) -> dict[str, list[str]]:
        """{source name: [project ids it is in]}, for every source with a membership."""
        with self._lock:
            rows = self.con.execute(
                "SELECT DISTINCT name, usecase_id FROM usecase_objects WHERE kind = 'source' "
                "ORDER BY name, usecase_id").fetchall()
        out: dict[str, list[str]] = {}
        for name, uid in rows:
            out.setdefault(name, []).append(uid)
        return out

    def project_sources(self, uid: str) -> list[str]:
        """The sources project `uid` is made of: its source rows that name a source that exists."""
        with self._lock:
            rows = self.con.execute(
                "SELECT DISTINCT o.name FROM usecase_objects o JOIN catalog_sources s "
                "ON s.name = o.name WHERE o.usecase_id = ? AND o.kind = 'source' ORDER BY o.name",
                [uid]).fetchall()
        return [r[0] for r in rows]

    def normalize_projects(self) -> None:
        """Make every object sit in a project. Runs at every open (it is the upgrade that folded
        views into triggers placing what it touched) and after a catalog import; idempotent and
        cheap, catalog tables only. The rules: a trigger with no project goes to the default
        project; an agent with none follows its trigger; an MCP server with none goes to the one
        project whose agents use it, else to the default; a trigger's sources are members of its
        project; a source in no project joins the default one. An owner that no longer exists
        counts as none."""
        with self._lock:
            self._normalize_projects()

    def _normalize_projects(self) -> None:
        # made only when something here has no other project to sit in
        default = self._lazy_default_project()
        recipes = self._recipes()
        # views are gone: so are their rows, and their entries in custom projects
        self.con.execute("DELETE FROM usecase_objects WHERE kind = 'view'")
        for uid, recipe in recipes.items():
            if recipe == "custom":
                self._custom_objects(uid, lambda objs: [o for o in objs
                                                        if o.get("kind") != "view"])

        # a deleted part is gone from every project (deleting it was the choice), and an agent
        # whose trigger was deleted keeps going without one; this also mends cells where a delete
        # happened before deletes did both
        self.con.execute("UPDATE catalog_agents SET trigger = '' WHERE trigger IS NULL OR "
                         "(trigger <> '' AND trigger NOT IN (SELECT name FROM catalog_triggers))")
        for kind, table in self._OWNED_TABLES.items():
            gone = [r[0] for r in self.con.execute(
                f"SELECT DISTINCT name FROM usecase_objects WHERE kind = ? "
                f"AND name NOT IN (SELECT name FROM {table})", [kind]).fetchall()]
            for name in gone:
                self._drop_rows(kind, name, set(self._rows_for(kind, name))
                                | {u for u, r in recipes.items() if r == "custom"})

        def valid(owner):
            return owner if owner in recipes else None

        triggers = {r[0]: (valid(r[1]), json.loads(r[2] or "[]")) for r in self.con.execute(
            "SELECT name, owned_by, sources FROM catalog_triggers").fetchall()}
        owners: dict[tuple[str, str], str] = {}
        for name, (owner, _srcs) in triggers.items():
            owners[("trigger", name)] = owner or default()
        agents = {r[0]: (valid(r[1]), r[2], json.loads(r[3] or "[]")) for r in self.con.execute(
            "SELECT name, owned_by, trigger, mcp_servers FROM catalog_agents").fetchall()}
        for name, (owner, trig, _servers) in agents.items():
            owners[("agent", name)] = owner or owners.get(("trigger", trig)) or default()
        users: dict[str, set] = {}
        for name, (_o, _t, servers) in agents.items():
            for srv in servers:
                users.setdefault(srv, set()).add(owners[("agent", name)])
        for name, owner in self.con.execute("SELECT name, owned_by FROM mcp_servers").fetchall():
            by = users.get(name) or set()
            owners[("mcp_server", name)] = valid(owner) or (next(iter(by)) if len(by) == 1
                                                            else default())
        table = self._OWNED_TABLES
        for (kind, name), owner in owners.items():
            current = self.con.execute(f"SELECT owned_by FROM {table[kind]} WHERE name = ?",
                                       [name]).fetchone()
            if current and current[0] != owner:
                self.con.execute(f"UPDATE {table[kind]} SET owned_by = ? WHERE name = ?",
                                 [owner, name])
            # a row elsewhere is left alone: it is how a project shows an object it lost (one
            # deleted by hand and recreated, which lands here unowned), and how an edit that
            # still lists it takes it back
            # an agent placed here for the first time (just imported, in no project yet) takes
            # its own trigger and handoffs as its project's wiring; one already in a project is
            # wired as that project says, even when it says nothing
            first = kind == "agent" and not self._rows_for("agent", name)
            self._add_row(owner, kind, name)
            if first:
                self._wire_from_definition(name, owner)
        existing = {r[0] for r in self.con.execute("SELECT name FROM catalog_sources").fetchall()}
        for name, (_owner, srcs) in triggers.items():
            for src in srcs:
                if src in existing:
                    self._add_row(owners[("trigger", name)], "source", src)
        for src in existing:
            if not self._rows_for("source", src):
                self._add_row(default(), "source", src)
        # the default project holds only what no other project does: a source another project
        # has and no default trigger reads leaves it
        held = self._default_project_if_any()
        if held is None:
            return
        default_reads = {src for (name, (_o, srcs)) in triggers.items()
                         if held in self._rows_for("trigger", name) for src in srcs}
        for src in existing:
            rows = self._rows_for("source", src)
            if held in rows and len(rows) > 1 and src not in default_reads:
                self._drop_rows("source", src, [held])

    def log_project(self, uid: str, action: str, detail: str = "") -> None:
        with self._lock:
            self.con.execute("INSERT INTO usecase_log (usecase_id, logged_at, action, detail) "
                             "VALUES (?, ?, ?, ?)", [uid, now_utc(), action, detail[:2000]])

    def list_project_log(self, uid: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT logged_at, action, detail FROM usecase_log WHERE usecase_id = ? "
                "ORDER BY logged_at DESC LIMIT ?", [uid, limit]).fetchall()
        return [{"at": r[0], "action": r[1], "detail": r[2]} for r in rows]
    # ── GitHub credentials: a token stored once, referenced by sources and MCP servers ──
    # The token (and an App's private key) is held verbatim like every other connector secret;
    # redaction is the API's job. `config` holds the kind-specific fields: for `app` the app id,
    # private key, webhook secret and installations; for `app_broker` the broker URL and secret.
    def list_github_credentials(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, kind, token, api_url, account, created_at, updated_at, config "
                "FROM github_credentials ORDER BY name").fetchall()
        out = []
        for r in rows:
            try:
                cfg = json.loads(r[7]) if r[7] else {}
            except (TypeError, ValueError):
                cfg = {}
            out.append({"name": r[0], "kind": r[1] or "token", "token": r[2] or "",
                        "api_url": r[3] or "", "account": r[4] or "",
                        "created_at": r[5], "updated_at": r[6],
                        "config": cfg if isinstance(cfg, dict) else {}})
        return out

    def get_github_credential(self, name: str) -> dict | None:
        return next((c for c in self.list_github_credentials() if c["name"] == name), None)

    def upsert_github_credential(self, name: str, token: str, kind: str = "token",
                                 api_url: str = "", account: str = "",
                                 config: dict | None = None) -> None:
        """Create or replace a credential. `config` None keeps the stored one (a token rotation
        does not wipe an App's installations); pass {} to clear it."""
        ts = now_utc()
        with self._lock:
            if config is None:
                row = self.con.execute("SELECT config FROM github_credentials WHERE name = ?",
                                       [name]).fetchone()
                cfg_json = row[0] if row and row[0] else None
            else:
                cfg_json = json.dumps(config) if config else None
            self.con.execute(
                "INSERT INTO github_credentials (name, kind, token, api_url, account, "
                "created_at, updated_at, config) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET kind = excluded.kind, token = excluded.token, "
                "api_url = excluded.api_url, account = excluded.account, "
                "updated_at = excluded.updated_at, config = excluded.config",
                [name, kind, token or "", api_url or "", account or "", ts, ts, cfg_json])

    def update_github_credential_config(self, name: str, config: dict) -> None:
        """Replace only the kind-specific fields (an installation added or removed by a webhook)."""
        with self._lock:
            self.con.execute(
                "UPDATE github_credentials SET config = ?, updated_at = ? WHERE name = ?",
                [json.dumps(config) if config else None, now_utc(), name])

    def delete_github_credential(self, name: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM github_credentials WHERE name = ?", [name])

    # ── ask sessions (the in-app agent's chat history) ────────────────────────
    # `state` is an opaque JSON blob owned by the console (messages, tool calls, proposal
    # decisions). The daemon stores and returns it; it never interprets it — parsing it here
    # would couple the store's schema to the UI's message shape for no reader's benefit.
    ASK_SESSIONS_KEEP = 50   # bounded history: enough to scroll back, never a growth vector

    def list_ask_sessions(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, title, created_at, updated_at FROM ask_sessions "
                "ORDER BY updated_at DESC").fetchall()
        return [{"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]} for r in rows]

    def get_ask_session(self, sid: str) -> dict | None:
        with self._lock:
            row = self.con.execute(
                "SELECT id, title, state, created_at, updated_at FROM ask_sessions WHERE id = ?",
                [sid]).fetchone()
        if row is None:
            return None
        return {"id": row[0], "title": row[1], "state": row[2],
                "created_at": row[3], "updated_at": row[4]}

    def upsert_ask_session(self, sid: str, title: str, state: str) -> None:
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO ask_sessions (id, title, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET title = excluded.title, "
                "state = excluded.state, updated_at = excluded.updated_at",
                [sid, title, state, ts, ts])
            self.con.execute(
                "DELETE FROM ask_sessions WHERE id NOT IN "
                "(SELECT id FROM ask_sessions ORDER BY updated_at DESC LIMIT ?)",
                [self.ASK_SESSIONS_KEEP])

    def delete_ask_session(self, sid: str) -> bool:
        with self._lock:
            n = self.con.execute("DELETE FROM ask_sessions WHERE id = ?", [sid]).fetchone()
            # DuckDB returns the deleted-row count as a result row
        return bool(n and n[0])

    # ── activity logs (agent-facing observability) ────────────────────────────
    def log_query(self, qid: str, scope: str, key: str, window: str,
                  rows_returned: int, client: str) -> None:
        """`scope` says what was read: a trigger's name, an agent tool, "(read)" for a raw read."""
        with self._lock:
            self.con.execute(
                "INSERT INTO query_log (id, scope, key_value, time_window, rows_returned, client, "
                "queried_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [qid, scope, key, window, rows_returned, client, now_utc()],
            )

    def list_queries(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, scope, key_value, time_window, rows_returned, client, queried_at "
                "FROM query_log ORDER BY queried_at DESC LIMIT ?", [int(limit)],
            ).fetchall()
        return [
            {"id": r[0], "scope": r[1], "key": r[2], "window": r[3],
             "rows_returned": r[4], "client": r[5], "queried_at": r[6]}
            for r in rows
        ]

    def get_dispatch(self, dispatch_id: str) -> dict | None:
        """One firing by id, with the same failure-reason as list_dispatches. None if unknown."""
        with self._lock:
            r = self.con.execute(
                "SELECT l.dispatch_id, l.trigger, l.key_value, l.kind, l.fired_at, l.subscribers, "
                "(SELECT COUNT(*) FROM dispatch_deliveries d "
                " WHERE d.dispatch_id = l.dispatch_id AND d.ok) AS delivered, "
                "(SELECT COUNT(*) FROM dispatch_deliveries d "
                " WHERE d.dispatch_id = l.dispatch_id AND d.ok IS NULL) AS pending, "
                "l.payload, "
                "(SELECT arg_max(d.error, d.delivered_at) FROM dispatch_deliveries d "
                " WHERE d.dispatch_id = l.dispatch_id AND d.ok = FALSE) AS error "
                "FROM dispatch_log l WHERE l.dispatch_id = ?", [dispatch_id]).fetchone()
        if r is None:
            return None
        return {"dispatch_id": r[0], "trigger": r[1], "key": r[2], "kind": r[3],
                "fired_at": r[4], "subscribers": r[5], "delivered": r[6], "pending": r[7],
                "payload": r[8], "error": r[9]}

    def deliveries_for(self, dispatch_id: str) -> list[dict]:
        """Per-subscriber delivery attempts for one firing — the detail behind 'delivered X of N'."""
        with self._lock:
            rows = self.con.execute(
                "SELECT subscription_id, url, ok, error, delivered_at FROM dispatch_deliveries "
                "WHERE dispatch_id = ? ORDER BY delivered_at", [dispatch_id]).fetchall()
        # ok stays None when the delivery is still pending (a Tares agent mid-run) — the caller
        # distinguishes pending from failed; bool() would collapse both to False.
        return [{"subscription_id": r[0], "url": r[1],
                 "ok": None if r[2] is None else bool(r[2]),
                 "error": r[3], "delivered_at": r[4]} for r in rows]

    def log_dispatch(self, dispatch_id: str, trigger: str, key: str, kind: str,
                     subscribers: int, delivered: int, payload: str,
                     project: str | None = None, parent_run_id: str | None = None,
                     practice: bool = False) -> None:
        # project: the trigger's at firing time. parent_run_id: the run whose finding tripped it.
        with self._lock:
            self.con.execute(
                "INSERT INTO dispatch_log (dispatch_id, trigger, key_value, kind, fired_at, "
                "subscribers, delivered, payload, project, parent_run_id, practice) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [dispatch_id, trigger, key, kind, now_utc(), subscribers, delivered, payload,
                 project or None, parent_run_id or None, bool(practice)],
            )
            if project:   # the firing belongs to its project (more join with add_dispatch_projects)
                self.con.execute("INSERT INTO dispatch_projects (dispatch_id, project) VALUES (?, ?) "
                                 "ON CONFLICT DO NOTHING", [dispatch_id, project])

    # ── project timeline (TR-331): the store's side is indexed lookups only ──
    def timeline_roots(self, project: str, before: datetime | None, limit: int) -> list[tuple]:
        """[(kind, id, at)] newest first: the threads of a project's timeline. A firing starts
        one unless a run of the same project wrote the finding that tripped it (it then sits under
        that run); a run starts one when no firing woke it and it repeats or continues no run of
        the project (a rerun or a handoff sits under its parent)."""
        b = "AND {col} < ? " if before is not None else ""
        args = [before] if before is not None else []
        with self._lock:
            # the firings and runs that belong to the project (P-TR-216)
            return self.con.execute(
                "WITH pr AS (SELECT run_id FROM run_projects WHERE project = ?), "
                "pd AS (SELECT dispatch_id FROM dispatch_projects WHERE project = ?) "
                "SELECT * FROM ("
                "SELECT 'firing' AS kind, dispatch_id AS id, fired_at AS ts FROM dispatch_log d "
                f"WHERE dispatch_id IN (SELECT * FROM pd) {b.format(col='fired_at')}"
                "AND NOT COALESCE(d.parent_run_id IN (SELECT * FROM pr), FALSE) "
                "UNION ALL "
                "SELECT 'run', id, started_at FROM agent_runs r "
                f"WHERE id IN (SELECT * FROM pr) {b.format(col='started_at')}"
                "AND COALESCE(dispatch_id, '') = '' "
                "AND NOT COALESCE(r.parent_run_id IN (SELECT * FROM pr), FALSE)"
                ") ORDER BY ts DESC, id DESC LIMIT ?",
                [project, project, *args, *args, int(limit)]).fetchall()

    def dispatches_where(self, column: str, values: list, project: str | None = None) -> list[dict]:
        """Firings by dispatch_id or by the run that tripped them, oldest first."""
        if column not in ("dispatch_id", "parent_run_id") or not values:
            return []
        sql = (f"SELECT dispatch_id, trigger, key_value, kind, fired_at, subscribers, payload, "
               f"project, parent_run_id FROM dispatch_log "
               f"WHERE {column} IN ({', '.join(['?'] * len(values))})")
        params = list(values)
        if project is not None:
            sql += " AND dispatch_id IN (SELECT dispatch_id FROM dispatch_projects WHERE project = ?)"
            params.append(project)
        with self._lock:
            rows = self.con.execute(sql + " ORDER BY fired_at", params).fetchall()
        return [{"dispatch_id": r[0], "trigger": r[1], "key": r[2], "kind": r[3],
                 "fired_at": r[4], "subscribers": r[5], "payload": r[6], "project": r[7],
                 "parent_run_id": r[8]} for r in rows]

    def deliveries_for_many(self, dispatch_ids: list) -> dict[str, list[dict]]:
        """{dispatch_id: [delivery]} for several firings at once, each in the deliveries_for shape."""
        if not dispatch_ids:
            return {}
        with self._lock:
            rows = self.con.execute(
                "SELECT dispatch_id, subscription_id, url, ok, error, delivered_at "
                f"FROM dispatch_deliveries WHERE dispatch_id IN ({', '.join(['?'] * len(dispatch_ids))}) "
                "ORDER BY delivered_at", list(dispatch_ids)).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r[0], []).append({"subscription_id": r[1], "url": r[2],
                                             "ok": None if r[3] is None else bool(r[3]),
                                             "error": r[4], "delivered_at": r[5]})
        return out

    def finding_run(self, sources: list[str], key: str | None, since: datetime,
                    filters: list | None = None, where: dict | None = None) -> str | None:
        """The run that wrote the newest finding for an entity in the window, when the finding
        names one: how a firing on a findings source knows which run caused it."""
        ph = ", ".join(["?"] * len(sources))
        fsql, fparams = _filter_sql(filters)
        wsql, wparams = _where_sql(where)
        ksql, kparams = (" AND key_value = ?", [key]) if key is not None else ("", [])
        with self._lock:
            row = self.con.execute(
                "SELECT json_extract_string(labels, '$.run_id') AS rid FROM events "
                f"WHERE source IN ({ph}) AND event_type = 'finding' AND event_time >= ?"
                f"{ksql}{fsql}{wsql} AND json_extract_string(labels, '$.run_id') IS NOT NULL "
                "ORDER BY event_time DESC LIMIT 1",
                [*sources, since, *kparams, *fparams, *wparams]).fetchone()
        return row[0] if row else None

    def list_dispatches(self, limit: int = 100) -> list[dict]:
        # `delivered` and `pending` are computed LIVE from deliveries, not read from the snapshot:
        # a Tares agent's in-process run finishes after fire() returns, so its outcome lands late.
        # Counting ok/NULL deliveries keeps the row honest as those runs complete.
        # `error` = the most recent failed delivery's reason (NULL if none failed).
        with self._lock:
            rows = self.con.execute(
                "SELECT l.dispatch_id, l.trigger, l.key_value, l.kind, l.fired_at, l.subscribers, "
                "(SELECT COUNT(*) FROM dispatch_deliveries d "
                " WHERE d.dispatch_id = l.dispatch_id AND d.ok) AS delivered, "
                "(SELECT COUNT(*) FROM dispatch_deliveries d "
                " WHERE d.dispatch_id = l.dispatch_id AND d.ok IS NULL) AS pending, "
                "l.payload, "
                "(SELECT arg_max(d.error, d.delivered_at) FROM dispatch_deliveries d "
                " WHERE d.dispatch_id = l.dispatch_id AND d.ok = FALSE) AS error "
                "FROM dispatch_log l ORDER BY l.fired_at DESC LIMIT ?", [int(limit)],
            ).fetchall()
        return [
            {"dispatch_id": r[0], "trigger": r[1], "key": r[2], "kind": r[3],
             "fired_at": r[4], "subscribers": r[5], "delivered": r[6], "pending": r[7],
             "payload": r[8], "error": r[9]}
            for r in rows
        ]

    # ── event inspection (UI) ─────────────────────────────────────────────────
    def event_stats(self) -> list[dict]:
        """Per-source totals + last ingest, for source health cards. Reads the maintained
        `source_stats` counter (kept in sync by append()/purge_events()) rather than scanning and
        grouping the whole events table — this is polled every few seconds by the Sources list and
        hit by every agent `list_sources`, so an O(#sources) read matters."""
        with self._lock:
            rows = self.con.execute(
                "SELECT source, events, last_ingest FROM source_stats ORDER BY source"
            ).fetchall()
        return [{"source": r[0], "events": r[1], "last_ingest": r[2]} for r in rows]

    def usage(self) -> dict:
        """What this instance is costing on disk, for the metering endpoint. Every number here is
        O(1)-ish: file sizes come from stat(), per-source event counts from the maintained
        `source_stats` counter (never a scan of events), and the two row counts are unfiltered
        COUNT(*)s, which DuckDB answers from table metadata. Per-source bytes are None — DuckDB
        stores every source in one events table and does not attribute storage per value."""
        db_bytes = _file_size(self.path)
        wal_bytes = _file_size(self.path + ".wal")
        with self._lock:
            sources = self.con.execute(
                "SELECT source, events FROM source_stats ORDER BY source").fetchall()
            runs = self.con.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
            deliveries = self.con.execute("SELECT COUNT(*) FROM dispatch_deliveries").fetchone()[0]
        return {
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "events": sum(int(r[1] or 0) for r in sources),
            "sources": [{"name": r[0], "events": int(r[1] or 0), "bytes": None} for r in sources],
            "agent_runs": int(runs),
            "dispatch_deliveries": int(deliveries),
        }

    def newest_key(self, sources: list[str], filters: list | None,
                   since: datetime) -> tuple[str, datetime] | None:
        """(entity, event time) of the newest event since `since` across `sources` that passes
        `filters` (a trigger's), or None."""
        if not sources:
            return None
        ph = ", ".join(["?"] * len(sources))
        fsql, fparams = _filter_sql(filters)
        with self._lock:
            r = self.con.execute(
                f"SELECT key_value, event_time FROM events WHERE source IN ({ph}) AND ingest_time >= ? "
                f"AND COALESCE(key_value, '') <> ''{fsql} ORDER BY ingest_time DESC LIMIT 1",
                [*sources, since, *fparams]).fetchone()
        return (r[0], r[1]) if r else None

    def recent_events(self, source: str | None = None, limit: int = 50) -> list[dict]:
        where = "WHERE source = ?" if source else ""
        params = ([source] if source else []) + [int(limit)]
        with self._lock:
            rows = self.con.execute(
                f"SELECT source, key_value, event_type, text, event_time, ingest_time "
                f"FROM events {where} ORDER BY ingest_time DESC LIMIT ?", params,
            ).fetchall()
        return [
            {"source": r[0], "key": r[1], "event_type": r[2], "text": r[3],
             "event_time": r[4], "ingest_time": r[5]}
            for r in rows
        ]

    def key_events(self, source: str, key: str, limit: int = 200,
                   offset: int = 0) -> list[dict]:
        """One entity's events on one source, oldest first (a session read top to bottom)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT event_type, text, event_time, labels FROM events WHERE source = ? AND "
                "key_value = ? ORDER BY event_time, ingest_time LIMIT ? OFFSET ?",
                [source, key, int(limit), int(offset)]).fetchall()
        return [{"event_type": r[0], "text": r[1], "event_time": r[2],
                 "labels": json.loads(r[3]) if r[3] else {}} for r in rows]

    def last_finding(self, source: str, agent: str, key: str) -> str | None:
        """The newest delivered finding this agent wrote for this key, or None. Feeds the next
        run's opening message so it builds on the earlier conclusion instead of rediscovering."""
        with self._lock:
            rows = self.con.execute(
                "SELECT payload FROM events WHERE source = ? AND key_value = ? "
                "ORDER BY ingest_time DESC LIMIT 20", [source, key]).fetchall()
        for (pj,) in rows:
            try:
                p = json.loads(pj) if pj else {}
            except (TypeError, ValueError):
                continue
            if p.get("agent") == agent and p.get("finding"):
                return str(p["finding"])
        return None

    def recent_payloads(self, source: str, limit: int = 500) -> list[dict]:
        """The lossless payloads of a source's most recent events (for field profiling)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT payload FROM events WHERE source = ? ORDER BY ingest_time DESC LIMIT ?",
                [source, int(limit)]).fetchall()
        out = []
        for (pj,) in rows:
            try:
                out.append(json.loads(pj) if pj else {})
            except (TypeError, ValueError):
                pass
        return out

    def source_schema(self, source: str, sample: int = 200) -> dict:
        """Inferred shape of a source's events, sampled from the most recent `sample` rows: the
        event types seen and the typed labels (with the type of the latest value). Number-typed
        labels are the aggregatable ones — a trigger's `field` picks from these."""
        with self._lock:
            rows = self.con.execute(
                "SELECT event_type, labels FROM events WHERE source = ? "
                "ORDER BY ingest_time DESC LIMIT ?", [source, int(sample)],
            ).fetchall()
        event_types, fields = set(), {}
        for etype, ljson in rows:
            event_types.add(etype)
            for k, v in (json.loads(ljson) or {}).items():
                fields.setdefault(k, "number" if isinstance(v, (int, float))
                                  and not isinstance(v, bool) else "string")
        return {"event_types": sorted(event_types), "fields": fields,
                "sampled_events": len(rows)}

    def backfill_labels(self, source: str, specs: list, context_fn=None) -> int:
        """Recompute a source's stored events' labels from their lossless payload using `specs`.
        This is what makes labels retroactive: a label declared today is computed over data
        ingested before it existed (the value was always in the payload, just unnamed).

        NOTE: not currently wired. Source edits are going-forward only (new events get the new
        specs; existing events are untouched). This is the building block for a planned explicit,
        chunked, cancellable background relabel job — it must not run inline on an edit, since on a
        large source it rewrites millions of rows.

        `context_fn` reconstructs the connector's per-event label context from the stored payload
        (the connector's `label_context`); without it the payload is used as-is, which would drop
        SYNTHESIZED labels (e.g. Vercel's `project`, derived from projectName)."""
        from .config import extract_labels
        with self._lock:
            rows = self.con.execute(
                "SELECT rowid, payload FROM events WHERE source = ?", [source]).fetchall()
        # Compute the new labels for every row OUTSIDE the lock: JSON parsing and the connector's
        # label_context can be heavy, and we must not hold the single DB writer for the whole scan.
        updates = []
        for rid, pj in rows:
            try:
                payload = json.loads(pj) if pj else {}
            except (TypeError, ValueError):
                payload = {}
            ctx = context_fn(payload) if context_fn else payload
            updates.append((rid, json.dumps(extract_labels(specs, ctx))))
        if not updates:
            return 0
        # Apply as ONE set-based UPDATE via a temp-table join. DuckDB is columnar: a single-row
        # `UPDATE ... WHERE rowid = ?` rewrites a whole row group, so N of them on a large source is
        # quadratic (this once wedged the daemon). Staging the new values and joining once is a
        # single rewrite.
        with self._lock:
            self.con.execute(
                "CREATE OR REPLACE TEMP TABLE _backfill (rowid BIGINT, labels VARCHAR)")
            self.con.executemany("INSERT INTO _backfill VALUES (?, ?)", updates)
            self.con.execute(
                "UPDATE events SET labels = b.labels FROM _backfill b WHERE events.rowid = b.rowid")
            self.con.execute("DROP TABLE _backfill")
        return len(updates)

    def list_entities(self, label: str, sources: list[str] | None = None,
                      limit: int = 200) -> list[dict]:
        """Distinct values of `label` (an entity per value) with event count + last seen, most
        active first. `label` may be 'key_value' or any named label. Optionally scoped to sources.

        Reads the maintained entity_counts counter (kept in sync by append()/purge_events()) instead
        of a full-table `GROUP BY json_extract_string(labels, …)`. If any relevant (source, label)
        was truncated for exceeding the cardinality cap, that label isn't materialized — fall back to
        a live scan so the answer stays correct (just slower, for a label that shouldn't be an axis)."""
        if not _FIELD_RE.match(label):
            raise ValueError(f"bad label name {label!r}")
        ph = ", ".join(["?"] * len(sources)) if sources else ""
        with self._lock:
            tq = "SELECT 1 FROM entity_label_state WHERE label = ? AND truncated = TRUE"
            tp: list = [label]
            if sources:
                tq += f" AND source IN ({ph})"
                tp += sources
            if self.con.execute(tq + " LIMIT 1", tp).fetchone() is not None:
                return self._list_entities_scan(label, sources, limit)
            q = "SELECT value, SUM(events), MAX(last_ingest) FROM entity_counts WHERE label = ?"
            params: list = [label]
            if sources:
                q += f" AND source IN ({ph})"
                params += sources
            q += " GROUP BY value ORDER BY SUM(events) DESC LIMIT ?"
            rows = self.con.execute(q, [*params, int(limit)]).fetchall()
        return [{"value": r[0], "events": r[1], "last_ingest": r[2]} for r in rows]

    def is_label_truncated(self, label: str, sources: list[str] | None = None) -> bool:
        """Whether `label` exceeded the cardinality cap (for any relevant source) and is served by a
        live scan rather than the counter. Lets the UI flag it as a high-cardinality axis."""
        if not _FIELD_RE.match(label):
            return False
        q = "SELECT 1 FROM entity_label_state WHERE label = ? AND truncated = TRUE"
        p: list = [label]
        if sources:
            q += f" AND source IN ({', '.join(['?'] * len(sources))})"
            p += sources
        with self._lock:
            return self.con.execute(q + " LIMIT 1", p).fetchone() is not None

    def _list_entities_scan(self, label: str, sources: list[str] | None, limit: int) -> list[dict]:
        """The pre-counter implementation: scan events and GROUP BY the label. Used only as the
        fallback for a truncated (high-cardinality) label. Caller holds the lock."""
        expr = _label_expr(label)
        where = "WHERE " + expr + " IS NOT NULL"
        params: list = []
        if sources:
            where += f" AND source IN ({', '.join(['?'] * len(sources))})"
            params += sources
        rows = self.con.execute(
            f"SELECT {expr} AS v, COUNT(*), MAX(ingest_time) FROM events {where} "
            f"GROUP BY v ORDER BY COUNT(*) DESC LIMIT ?", [*params, int(limit)],
        ).fetchall()
        return [{"value": r[0], "events": r[1], "last_ingest": r[2]} for r in rows]

    def purge_dispatches(self, trigger: str) -> int:
        """Delete a trigger's firing history: its dispatch_log rows, their per-recipient
        deliveries, and its cooldown state. Used when a project is deleted with purge, so a
        re-created demo does not open on last week's firings."""
        with self._lock:
            n = self.con.execute(
                "SELECT COUNT(*) FROM dispatch_log WHERE trigger = ?", [trigger]).fetchone()[0]
            self.con.execute("BEGIN TRANSACTION")
            try:
                self.con.execute(
                    "DELETE FROM dispatch_deliveries WHERE dispatch_id IN "
                    "(SELECT dispatch_id FROM dispatch_log WHERE trigger = ?)", [trigger])
                self.con.execute("DELETE FROM dispatch_log WHERE trigger = ?", [trigger])
                self.con.execute("DELETE FROM trigger_state WHERE trigger = ?", [trigger])
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
        return int(n)

    def purge_events(self, source: str) -> int:
        with self._lock:
            n = self.con.execute(
                "SELECT COUNT(*) FROM events WHERE source = ?", [source]).fetchone()[0]
            self.con.execute("BEGIN TRANSACTION")
            try:
                self.con.execute("DELETE FROM events WHERE source = ?", [source])
                self.con.execute("DELETE FROM cursors WHERE source = ?", [source])
                # Drop the maintained counters for this source (the only decrement path).
                self.con.execute("DELETE FROM source_stats WHERE source = ?", [source])
                self.con.execute("DELETE FROM entity_counts WHERE source = ?", [source])
                self.con.execute("DELETE FROM entity_label_state WHERE source = ?", [source])
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
        return n

    # ── subscriptions ─────────────────────────────────────────────────────────
    def add_subscription(self, sid: str, trigger: str, url: str, created_by: str | None = None,
                         project: str | None = None) -> None:
        """A subscription to one trigger, or with `project` (and trigger "") to every trigger of
        that project, the ones added later included (TR-336)."""
        with self._lock:
            self.con.execute(
                "INSERT INTO subscriptions (subscription_id, trigger, url, created_at, created_by, "
                "project) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (subscription_id) DO UPDATE SET trigger = excluded.trigger, url = excluded.url",
                [sid, trigger, url, now_utc(), created_by, project or None],
            )

    def list_project_subscriptions(self, project: str) -> list[dict]:
        """The subscriptions to a whole project, oldest first."""
        with self._lock:
            rows = self.con.execute(
                "SELECT subscription_id, url, created_at, created_by FROM subscriptions "
                "WHERE project = ? ORDER BY created_at", [project]).fetchall()
        return [{"subscription_id": r[0], "url": r[1], "created_at": r[2], "created_by": r[3]}
                for r in rows]

    def get_subscription(self, sid: str) -> dict | None:
        with self._lock:
            r = self.con.execute(
                "SELECT subscription_id, trigger, url, created_at, created_by, project "
                "FROM subscriptions WHERE subscription_id = ?", [sid]).fetchone()
        if r is None:
            return None
        return {"subscription_id": r[0], "trigger": r[1], "url": r[2], "created_at": r[3],
                "created_by": r[4], "project": r[5]}

    def last_delivery(self, subscription_id: str) -> dict | None:
        """The newest delivery made through one subscription, or None."""
        with self._lock:
            r = self.con.execute(
                "SELECT delivered_at, ok, error, dispatch_id FROM dispatch_deliveries "
                "WHERE subscription_id = ? ORDER BY delivered_at DESC LIMIT 1",
                [subscription_id]).fetchone()
        if r is None:
            return None
        return {"at": r[0], "ok": None if r[1] is None else bool(r[1]), "error": r[2],
                "dispatch_id": r[3]}

    def list_subscriptions(self, trigger: str):
        with self._lock:
            return self.con.execute(
                "SELECT subscription_id, trigger, url FROM subscriptions WHERE trigger = ?",
                [trigger],
            ).fetchall()

    def list_all_subscriptions(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT subscription_id, trigger, url, created_at, project FROM subscriptions "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [{"subscription_id": r[0], "trigger": r[1], "url": r[2], "created_at": r[3],
                 "project": r[4]} for r in rows]

    def remove_subscription(self, sid: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM subscriptions WHERE subscription_id = ?", [sid])

    def subscription_by_url(self, url: str) -> dict | None:
        """A subscription by its exact URL — used to find a Tares agent's internal subscription
        (url = tares://agent/<name>), which is how enabled/disabled is represented."""
        with self._lock:
            r = self.con.execute(
                "SELECT subscription_id, trigger, url FROM subscriptions WHERE url = ? LIMIT 1",
                [url]).fetchone()
        return {"subscription_id": r[0], "trigger": r[1], "url": r[2]} if r else None

    def remove_subscription_by_url(self, url: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM subscriptions WHERE url = ?", [url])

    def log_delivery(self, dispatch_id: str, subscription_id: str, url: str, ok: bool | None,
                     error: str | None = None) -> None:
        # Explicit column list: `error` was appended by migration, so positional VALUES no longer match.
        # ok may be NULL — a Tares agent's delivery is logged pending at fire time and updated when
        # the in-process run finishes (external POSTs log their final ok immediately).
        with self._lock:
            self.con.execute(
                "INSERT INTO dispatch_deliveries "
                "(dispatch_id, subscription_id, url, ok, delivered_at, error) VALUES (?, ?, ?, ?, ?, ?)",
                [dispatch_id, subscription_id, url, ok, now_utc(), error])

    def update_delivery(self, dispatch_id: str, subscription_id: str, ok: bool,
                        error: str | None = None) -> None:
        """Resolve a pending delivery (a Tares agent's in-process run finishing)."""
        with self._lock:
            self.con.execute(
                "UPDATE dispatch_deliveries SET ok = ?, error = ?, delivered_at = ? "
                "WHERE dispatch_id = ? AND subscription_id = ?",
                [ok, error, now_utc(), dispatch_id, subscription_id])

    def remove_subscriptions_by_trigger(self, trigger: str) -> int:
        """Drop every subscription to this trigger — called when the trigger is deleted, so no
        subscriber keeps 'waking on' a trigger that no longer exists."""
        with self._lock:
            n = self.con.execute("SELECT COUNT(*) FROM subscriptions WHERE trigger = ?",
                                 [trigger]).fetchone()[0]
            self.con.execute("DELETE FROM subscriptions WHERE trigger = ?", [trigger])
        return int(n)

    def all_subscriptions(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT subscription_id, trigger, url, created_at, created_by, project "
                "FROM subscriptions ORDER BY created_at").fetchall()
        return [{"subscription_id": r[0], "trigger": r[1], "url": r[2],
                 "created_at": r[3], "created_by": r[4], "project": r[5]} for r in rows]

    def delivery_stats(self, window: str = "24h") -> dict:
        """{url: {ok, fail, ok_total, fail_total, last_at, last_ok, last_error}} per endpoint.

        `ok`/`fail` are counted over `window` only — an all-time total presented next to a "last
        woken" of weeks ago reads as "this agent is busy" when it has been idle for a month. The
        all-time totals come back alongside them (same single pass) so nothing is lost.

        `last_at` / `last_ok` / `last_error` describe the MOST RECENT delivery *ever*, not the most
        recent one in the window: an endpoint that is currently failing must keep saying so even
        when it has been quiet for longer than the window (arg_max picks the value at the latest
        delivered_at)."""
        since = now_utc() - parse_window(window)
        with self._lock:
            # ok IS NULL is a pending Tares-agent run — count it as neither delivered nor failed,
            # and exclude it from the "most recent outcome" (arg_max over resolved deliveries only),
            # so a running agent never reads as a failure.
            rows = self.con.execute(
                "SELECT url, SUM(CASE WHEN ok AND delivered_at > ? THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN ok = FALSE AND delivered_at > ? THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN ok THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN ok = FALSE THEN 1 ELSE 0 END), MAX(delivered_at), "
                "SUM(CASE WHEN ok IS NULL THEN 1 ELSE 0 END), "
                "arg_max(ok, CASE WHEN ok IS NULL THEN NULL ELSE delivered_at END), "
                "arg_max(error, CASE WHEN ok IS NULL THEN NULL ELSE delivered_at END) "
                "FROM dispatch_deliveries GROUP BY url", [since, since]).fetchall()
        return {r[0]: {"ok": int(r[1] or 0), "fail": int(r[2] or 0),
                       "ok_total": int(r[3] or 0), "fail_total": int(r[4] or 0),
                       "last_at": r[5], "pending": int(r[6] or 0),
                       "last_ok": True if r[7] is None else bool(r[7]), "last_error": r[8]}
                for r in rows}

    def recent_deliveries(self, url: str, limit: int = 20) -> list[dict]:
        """Latest deliveries to one endpoint, joined with the firing's trigger/entity."""
        with self._lock:
            rows = self.con.execute(
                "SELECT d.delivered_at, d.ok, l.trigger, l.key_value, d.dispatch_id, d.error "
                "FROM dispatch_deliveries d LEFT JOIN dispatch_log l ON d.dispatch_id = l.dispatch_id "
                "WHERE d.url = ? ORDER BY d.delivered_at DESC LIMIT ?", [url, limit]).fetchall()
        return [{"at": r[0], "ok": None if r[1] is None else bool(r[1]), "trigger": r[2],
                 "key": r[3], "dispatch_id": r[4], "error": r[5]} for r in rows]

    # ── API keys (scoped credentials; only the SHA-256 of the secret is stored) ─
    def insert_api_key(self, kid: str, name: str, prefix: str, hash_: str, scopes: list[str],
                       project: str | None = None) -> None:
        """`project` makes it a project key: it reads that project only (TR-335)."""
        with self._lock:
            self.con.execute(
                "INSERT INTO api_keys (id, name, prefix, hash, scopes, created_at, last_used_at, "
                "revoked_at, project) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                [kid, name, prefix, hash_, json.dumps(scopes), now_utc(), project or None])

    def list_api_keys(self, project: str | None = None) -> list[dict]:
        """Every key, newest first; with `project`, only that project's keys."""
        sql = ("SELECT id, name, prefix, scopes, created_at, last_used_at, revoked_at, project "
               "FROM api_keys ")
        params = []
        if project is not None:
            sql += "WHERE project = ? "
            params.append(project)
        with self._lock:
            rows = self.con.execute(sql + "ORDER BY created_at DESC", params).fetchall()
        return [{"id": r[0], "name": r[1], "prefix": r[2], "scopes": json.loads(r[3]),
                 "created_at": r[4], "last_used_at": r[5], "revoked_at": r[6],
                 "project": r[7]} for r in rows]

    def get_api_key(self, kid: str) -> dict | None:
        return next((k for k in self.list_api_keys() if k["id"] == kid), None)

    def find_api_key(self, hash_: str) -> dict | None:
        """Active (non-revoked) key by secret hash, or None."""
        with self._lock:
            r = self.con.execute(
                "SELECT id, name, scopes, last_used_at, project FROM api_keys "
                "WHERE hash = ? AND revoked_at IS NULL", [hash_]).fetchone()
        if not r:
            return None
        return {"id": r[0], "name": r[1], "scopes": json.loads(r[2]), "last_used_at": r[3],
                "project": r[4]}

    def touch_api_key(self, kid: str) -> None:
        with self._lock:
            self.con.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", [now_utc(), kid])

    def revoke_api_key(self, kid: str) -> bool:
        """Revoke the key and delete its subscriptions (a revoked agent must stop receiving
        trigger dispatches — otherwise its webhook is a post-revocation exfiltration path)."""
        with self._lock:
            hit = self.con.execute("SELECT 1 FROM api_keys WHERE id = ? AND revoked_at IS NULL",
                                   [kid]).fetchone()
            if not hit:
                return False
            self.con.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", [now_utc(), kid])
            self.con.execute("DELETE FROM subscriptions WHERE created_by = ?", [f"key:{kid}"])
        return True
