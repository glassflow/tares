"""The database schema as it was when the first tables moved out of DuckDB (TR-1480), frozen.

A table ticket that moves a table in a later revision tests its takeover against a database made
the old way. `Store._ddl()` stops making a moved table, so this module keeps the statements as
they stood on main (`OLD_DDL` is `Store._ddl()` as text), and a later change to the real DDL never
alters them.

    old_ddl_duckdb(con)             the old DDL on a DuckDB connection, then the sample rows
    old_ddl_postgres(dsn)           the same on Postgres: the statements `state_db.state_ddl` keeps
                                    (every table but the event tables), then the sample rows

`SEED` holds the sample rows: {table: (columns, [row, ...])}. Pass `seed=` to use others, or
`seed=None` for none. Both return the rows they inserted, {table: [row, ...]}.

This is a test helper, not a tool of the product.
"""
from __future__ import annotations

from datetime import datetime, timezone

_T = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

OLD_DDL = [
    r'''CREATE TABLE IF NOT EXISTS events (
  source       TEXT,
  source_type  TEXT,
  key_value    TEXT,
  event_type   TEXT,
  text         TEXT,
  payload      JSON,
  event_time   TIMESTAMPTZ,
  ingest_time  TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS cursors (
  source TEXT PRIMARY KEY,
  cursor TEXT
)''',
    r'''
CREATE TABLE IF NOT EXISTS source_stats (
  source      TEXT PRIMARY KEY,
  events      BIGINT,
  last_ingest TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS entity_counts (
  source      TEXT,
  label       TEXT,
  value       TEXT,
  events      BIGINT,
  last_ingest TIMESTAMPTZ,
  PRIMARY KEY (source, label, value)
)''',
    r'''
CREATE INDEX IF NOT EXISTS ix_entity_counts_label ON entity_counts(label)''',
    r'''
CREATE TABLE IF NOT EXISTS entity_label_state (
  source    TEXT,
  label     TEXT,
  truncated BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (source, label)
)''',
    r'''
CREATE TABLE IF NOT EXISTS trigger_state (
  trigger    TEXT,
  key_value  TEXT,
  last_fired TIMESTAMPTZ,
  PRIMARY KEY (trigger, key_value)
)''',
    r'''
CREATE TABLE IF NOT EXISTS subscriptions (
  subscription_id TEXT PRIMARY KEY,
  trigger         TEXT,
  url             TEXT,
  created_at      TIMESTAMPTZ,
  created_by      TEXT
)''',
    r'''
CREATE TABLE IF NOT EXISTS dispatch_deliveries (
  dispatch_id     TEXT,
  subscription_id TEXT,
  url             TEXT,
  ok              BOOLEAN,
  delivered_at    TIMESTAMPTZ,
  error           TEXT
)''',
    r'''
CREATE TABLE IF NOT EXISTS api_keys (
  id           TEXT PRIMARY KEY,
  name         TEXT,
  prefix       TEXT,
  hash         TEXT,
  scopes       JSON,
  created_at   TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS people (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  email      TEXT,
  emails     TEXT,
  github     TEXT,
  is_admin   BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ,
  created_via TEXT
)''',
    r'''
-- Who is on a project besides its lead (usecases.lead_person_id): reason 'manual' (the lead or
-- the system added them) or 'ticket' (they hold one of its tickets). The lead is not a row.
CREATE TABLE IF NOT EXISTS project_members (
  project   TEXT,
  person_id TEXT,
  added_by  TEXT,
  added_at  TIMESTAMPTZ,
  reason    TEXT,
  PRIMARY KEY (project, person_id)
)''',
    r'''
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
)''',
    r'''
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
)''',
    r'''
-- A Tares agent: the DEFINITION only (prompt + optional Slack). Whether it's enabled is not a
-- column: an agent is enabled exactly when it has a subscription to its trigger, the same wiring
-- an external agent has. That keeps one source of truth for "will it be woken" (subscriptions).
CREATE TABLE IF NOT EXISTS catalog_agents (
  name          TEXT PRIMARY KEY,
  trigger       TEXT,
  prompt        TEXT,
  slack_webhook TEXT,
  created_at    TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ
)''',
    r'''
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
)''',
    r'''
CREATE INDEX IF NOT EXISTS ix_agent_runs_agent ON agent_runs(agent)''',
    r'''
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
)''',
    r'''
CREATE INDEX IF NOT EXISTS ix_model_usage_ts ON model_usage(ts)''',
    r'''
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS mcp_servers (
  name        TEXT PRIMARY KEY,
  url         TEXT,
  auth_header TEXT,
  auth_value  TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS github_credentials (
  name        TEXT PRIMARY KEY,
  kind        TEXT,
  token       TEXT,
  api_url     TEXT,
  account     TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS ask_sessions (
  id         TEXT PRIMARY KEY,
  title      TEXT,
  state      TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
)''',
    r'''
-- scope: what was read, the trigger or agent tool, "(read)" for a raw read
CREATE TABLE IF NOT EXISTS query_log (
  id            TEXT PRIMARY KEY,
  scope         TEXT,
  key_value     TEXT,
  time_window   TEXT,
  rows_returned INTEGER,
  client        TEXT,
  queried_at    TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS dispatch_log (
  dispatch_id TEXT PRIMARY KEY,
  trigger     TEXT,
  key_value   TEXT,
  kind        TEXT,
  fired_at    TIMESTAMPTZ,
  subscribers INTEGER,
  delivered   INTEGER,
  payload     TEXT
)''',
    r'''
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
)''',
    r'''
CREATE TABLE IF NOT EXISTS usecase_objects (
  usecase_id TEXT,
  kind       TEXT,
  key        TEXT,
  name       TEXT,
  customized BOOLEAN,
  created_at TIMESTAMPTZ,
  PRIMARY KEY (usecase_id, kind, key)
)''',
    r'''
CREATE TABLE IF NOT EXISTS usecase_log (
  usecase_id TEXT,
  logged_at  TIMESTAMPTZ,
  action     TEXT,
  detail     TEXT
)''',
    r'''
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
)''',
    r'''
-- Which projects a run and a firing belong to: the projects whose wiring started them. The
-- `project` column on agent_runs and dispatch_log stays as the first of them.
CREATE TABLE IF NOT EXISTS run_projects (
  run_id  TEXT,
  project TEXT,
  PRIMARY KEY (run_id, project)
)''',
    r'''
CREATE TABLE IF NOT EXISTS dispatch_projects (
  dispatch_id TEXT,
  project     TEXT,
  PRIMARY KEY (dispatch_id, project)
)''',
    r'''
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
)''',
    r'''
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
)''',
    r'''
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
)''',
    r'''
-- Claude Code sessions that worked in a project (TR-405): the plugin tells Tares which project a
-- session works in with a `session_project` line, and the whole session (its claude_code events,
-- keyed by session id, the lines from before the project existed included) reads as the project's
CREATE TABLE IF NOT EXISTS project_sessions (
  project    TEXT,
  session    TEXT,
  repo       TEXT,
  linked_at  TIMESTAMPTZ,
  PRIMARY KEY (project, session)
)''',
    r'''
-- What a Claude Code session is doing now (TR-405 follow-up), from the plugin's session_state
-- lines: working, idle (a turn ended), waiting (for the person, `reason` says why) or ended. One row per session.
CREATE TABLE IF NOT EXISTS session_states (
  session    TEXT PRIMARY KEY,
  state      TEXT,
  reason     TEXT,
  state_at   TIMESTAMPTZ
)''',
    r'''
CREATE TABLE IF NOT EXISTS skills (
  project     TEXT,
  name        TEXT,
  description TEXT,
  body        TEXT,
  created_at  TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ,
  PRIMARY KEY (project, name)
)''',
    r'''ALTER TABLE events ADD COLUMN IF NOT EXISTS labels JSON''',
    r'''ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS ingest_key TEXT''',
    r'''ALTER TABLE github_credentials ADD COLUMN IF NOT EXISTS config JSON''',
    r'''ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_by TEXT''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS paused BOOLEAN''',
    r'''ALTER TABLE dispatch_deliveries ADD COLUMN IF NOT EXISTS error TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS model TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS slack_channel TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS webhook_url TEXT''',
    r'''ALTER TABLE ask_sessions ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE query_log ADD COLUMN IF NOT EXISTS project TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS webhook_token TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS mcp_servers JSON''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS external_tools JSON''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS max_rounds INTEGER''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS budget_usd DOUBLE''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS budget_period TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS daily_cap INTEGER''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS max_rounds INTEGER''',
    r'''ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS headers JSON''',
    r'''DROP TABLE IF EXISTS catalog_reviews''',
    r'''DROP TABLE IF EXISTS review_runs''',
    r'''ALTER TABLE events DROP COLUMN IF EXISTS fields''',
    r'''ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS owned_by TEXT''',
    r'''ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS customized BOOLEAN''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS owned_by TEXT''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS customized BOOLEAN''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS owned_by TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS customized BOOLEAN''',
    r'''ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS owned_by TEXT''',
    r'''ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS customized BOOLEAN''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS input_tokens BIGINT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS output_tokens BIGINT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cache_creation_input_tokens BIGINT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cache_read_input_tokens BIGINT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS delivery TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS delivery_error TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS webhook_key_label TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS provider TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS provider TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS outcome TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS verdict TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS results JSON''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS skills JSON''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS handoffs JSON''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS concludes BOOLEAN''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS github TEXT''',
    r'''ALTER TABLE catalog_agents ADD COLUMN IF NOT EXISTS verdicts JSON''',
    r'''ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS key_source TEXT''',
    r'''ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS provider TEXT''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS sources JSON''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS filters JSON''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS key_field TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS woken_by TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS project TEXT''',
    r'''ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS project TEXT''',
    r'''ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS parent_run_id TEXT''',
    r'''ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS project TEXT''',
    r'''ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS project TEXT''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS goal TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS headline TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS next_step TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS handled_by TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS what_changed TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS since_at TIMESTAMPTZ''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS failing TEXT''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS likely_cause TEXT''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS setup JSON''',
    r'''ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS practice BOOLEAN''',
    r'''ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS practice BOOLEAN''',
    r'''ALTER TABLE catalog_triggers ADD COLUMN IF NOT EXISTS description TEXT''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS linear JSON''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS github_issues JSON''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS kind TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS milestone TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS depends_on JSON''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS number INTEGER''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS holder TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS stage TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS stage_reason TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS pr JSON''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS repo TEXT''',
    r'''ALTER TABLE shared_skills ADD COLUMN IF NOT EXISTS updated_by TEXT''',
    r'''ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS lead_person_id TEXT''',
    r'''ALTER TABLE people ADD COLUMN IF NOT EXISTS picture TEXT''',
    r'''ALTER TABLE people ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS scope TEXT''',
    r'''ALTER TABLE usecases ADD COLUMN IF NOT EXISTS icon TEXT''',
    r'''ALTER TABLE docs ADD COLUMN IF NOT EXISTS owner TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assignee_person_id TEXT''',
    r'''ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assignee_raw TEXT''',
    r'''CREATE TABLE IF NOT EXISTS challenges (
      source        TEXT,
      key_value     TEXT,
      event_time    TIMESTAMPTZ,
      event_type    TEXT,
      branch        TEXT,
      tares_project TEXT,
      payload       JSON,
      PRIMARY KEY (source, key_value, event_time, event_type, branch)
    )''',
    r'''CREATE INDEX IF NOT EXISTS ix_challenges_branch ON challenges(source, branch)''',
    r'''CREATE TABLE IF NOT EXISTS milestones (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      owner       TEXT,
      external_id TEXT,
      name        TEXT,
      goal        TEXT,
      checks      JSON,
      position    DOUBLE,
      created_at  TIMESTAMPTZ,
      updated_at  TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS session_stations (
      session     TEXT PRIMARY KEY,
      station     TEXT,
      role        TEXT,
      parent      TEXT,
      project     TEXT,
      seen_at     TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS ticket_numbers (
      project     TEXT PRIMARY KEY,
      last        INTEGER
    )''',
    r'''CREATE TABLE IF NOT EXISTS ticket_history (
      ticket      TEXT,
      project     TEXT,
      changed_at  TIMESTAMPTZ,
      field       TEXT,
      value       TEXT,
      reason      TEXT,
      by_station  TEXT,
      by_role     TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS assumptions (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      number      INTEGER,
      question    TEXT,
      choice      TEXT,
      why         TEXT,
      affects     TEXT,
      tickets     JSON,
      state       TEXT,
      words       TEXT,
      batch       TEXT,
      made_by     TEXT,
      made_role   TEXT,
      made_at     TIMESTAMPTZ,
      decided_at  TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS milestone_check_results (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      milestone   TEXT,
      idx         INTEGER,
      result      TEXT,
      note        TEXT,
      by_kind     TEXT,
      by_name     TEXT,
      person_id   TEXT,
      checked_at  TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS milestone_acceptances (
      milestone   TEXT PRIMARY KEY,
      project     TEXT,
      by_name     TEXT,
      person_id   TEXT,
      accepted_at TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS ticket_checks (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      ticket      TEXT,
      command     TEXT,
      result      TEXT,
      commit_sha  TEXT,
      broke_test  TEXT,
      by_station  TEXT,
      by_session  TEXT,
      checked_at  TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS reviews (
      id           TEXT PRIMARY KEY,
      project      TEXT,
      ticket       TEXT,
      round        INTEGER,
      pr           TEXT,
      head         TEXT,
      verdict      TEXT,
      verified     TEXT,
      not_verified TEXT,
      by_station   TEXT,
      reviewed_at  TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS review_findings (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      ticket      TEXT,
      review      TEXT,
      number      INTEGER,
      severity    TEXT,
      file        TEXT,
      line        INTEGER,
      blocking    BOOLEAN,
      text        TEXT,
      kind        TEXT,
      state       TEXT,
      fixed_in    TEXT,
      found_at    TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS rule_proposals (
      id          TEXT PRIMARY KEY,
      repo        TEXT,
      kind        TEXT,
      project     TEXT,
      scope       TEXT,
      text        TEXT,
      tickets     JSON,
      state       TEXT,
      created_at  TIMESTAMPTZ,
      decided_at  TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS repo_maps (
      repo        TEXT PRIMARY KEY,
      body        TEXT,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS repo_map_history (
      repo        TEXT,
      changed_at  TIMESTAMPTZ,
      by_station  TEXT,
      project     TEXT,
      note        TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS desk_items (
      id             TEXT PRIMARY KEY,
      number         INTEGER,
      project        TEXT,
      question       TEXT,
      context        TEXT,
      options        JSON,
      recommendation TEXT,
      why            TEXT,
      blocks         TEXT,
      tickets        JSON,
      blocking       BOOLEAN,
      asked_by       TEXT,
      assumptions    JSON,
      state          TEXT,
      reason         TEXT,
      created_at     TIMESTAMPTZ,
      closed_at      TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS decisions (
      id          TEXT PRIMARY KEY,
      project     TEXT,
      desk_item   TEXT,
      question    TEXT,
      words       TEXT,
      choice      TEXT,
      standing    BOOLEAN,
      scope       TEXT,
      matched     BOOLEAN,
      session     TEXT,
      granted     BOOLEAN,
      tickets     JSON,
      decided_at  TIMESTAMPTZ
    )''',
    r'''ALTER TABLE desk_items ADD COLUMN IF NOT EXISTS station TEXT''',
    r'''ALTER TABLE desk_items ADD COLUMN IF NOT EXISTS command TEXT''',
    r'''ALTER TABLE decisions ADD COLUMN IF NOT EXISTS via TEXT''',
    r'''CREATE TABLE IF NOT EXISTS handovers (
      project     TEXT PRIMARY KEY,
      repo        TEXT,
      handed_by   TEXT,
      handed_at   TIMESTAMPTZ
    )''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS repos JSON''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS autonomy TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS finished_words TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS finished_decision TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS finished_by TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS taken_at TIMESTAMPTZ''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS taken_words TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS taken_by TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ''',
    r'''ALTER TABLE ticket_history ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE decisions ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE desk_items ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE desk_items ADD COLUMN IF NOT EXISTS to_person_id TEXT''',
    r'''ALTER TABLE assumptions ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE ticket_checks ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE reviews ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS person_id TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS finished_person_id TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS taken_person_id TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS crew TEXT''',
    r'''CREATE TABLE IF NOT EXISTS grants (
      id               TEXT PRIMARY KEY,
      kind             TEXT,
      project          TEXT,
      holder_person_id TEXT,
      words            TEXT,
      given_by         TEXT,
      given_at         TIMESTAMPTZ,
      areas            TEXT,
      consents         TEXT,
      state            TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS guardrail_rules (
      id         TEXT PRIMARY KEY,
      scope      TEXT,
      project    TEXT,
      kind       TEXT,
      text       TEXT,
      created_by TEXT,
      created_at TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS guardrail_pairs (
      id          TEXT PRIMARY KEY,
      scope       TEXT,
      project_uid TEXT,
      hook        TEXT,
      integration TEXT,
      settings    TEXT,
      on_fail     TEXT DEFAULT 'block',
      created_at  TIMESTAMPTZ,
      created_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS pending_memory (
      id         TEXT PRIMARY KEY,
      scope      TEXT,
      line       TEXT,
      attempts   INTEGER DEFAULT 0,
      last_error TEXT,
      created_at TIMESTAMPTZ,
      tried_at   TIMESTAMPTZ
    )''',
    r'''ALTER TABLE grants ADD COLUMN IF NOT EXISTS needs_lead BOOLEAN DEFAULT FALSE''',
    r'''CREATE TABLE IF NOT EXISTS crews (
      id          TEXT PRIMARY KEY,
      name        TEXT,
      person_id   TEXT,
      machine     TEXT,
      first_seen  TIMESTAMPTZ,
      last_seen   TIMESTAMPTZ,
      UNIQUE (person_id, name, machine)
    )''',
    r'''ALTER TABLE session_stations ADD COLUMN IF NOT EXISTS crew_id TEXT''',
    r'''CREATE TABLE IF NOT EXISTS spec_questions (
      project       TEXT,
      id            TEXT,
      to_person_id  TEXT,
      repo          TEXT,
      text          TEXT,
      asked_by      TEXT,
      asked_at      TIMESTAMPTZ,
      answer        TEXT,
      answered_by   TEXT,
      answered_at   TIMESTAMPTZ,
      state         TEXT,
      PRIMARY KEY (project, id)
    )''',
    r'''ALTER TABLE spec_questions ADD COLUMN IF NOT EXISTS via TEXT''',
    r'''CREATE TABLE IF NOT EXISTS brain_repos (
      repo           TEXT PRIMARY KEY,
      repo_source_id TEXT,
      state          TEXT,
      files_done     INTEGER DEFAULT 0,
      files_total    INTEGER DEFAULT 0,
      indexed_commit TEXT,
      last_synced    TIMESTAMPTZ,
      error          TEXT,
      added_by       TEXT,
      added_at       TIMESTAMPTZ
    )''',
    r'''ALTER TABLE brain_repos ADD COLUMN IF NOT EXISTS place INTEGER''',
    r'''ALTER TABLE brain_repos ADD COLUMN IF NOT EXISTS job_started_at TIMESTAMPTZ''',
    r'''ALTER TABLE brain_repos ADD COLUMN IF NOT EXISTS sync_failures INTEGER DEFAULT 0''',
    r'''ALTER TABLE brain_repos ADD COLUMN IF NOT EXISTS sync_error TEXT''',
    r'''CREATE TABLE IF NOT EXISTS brain_repo_folders (
      repo      TEXT,
      path      TEXT,
      chosen_by TEXT,
      chosen_at TIMESTAMPTZ,
      PRIMARY KEY (repo, path)
    )''',
    r'''CREATE TABLE IF NOT EXISTS brain_repo_updates (
      id            TEXT PRIMARY KEY,
      repo          TEXT,
      "at"          TIMESTAMPTZ,
      kind          TEXT,
      ok            INTEGER,
      indexed_commit TEXT,
      files_changed INTEGER,
      files_deleted INTEGER,
      files_total   INTEGER,
      error         TEXT
    )''',
    r'''CREATE INDEX IF NOT EXISTS idx_brain_repo_updates_repo ON brain_repo_updates (repo, "at")''',
    r'''CREATE TABLE IF NOT EXISTS brain_calls (
      id            TEXT PRIMARY KEY,
      "at"          TIMESTAMPTZ,
      tool          TEXT,
      deep          INTEGER DEFAULT 0,
      question      TEXT,
      repos         TEXT,
      duration_ms   INTEGER,
      input_tokens  INTEGER,
      output_tokens INTEGER,
      ok            INTEGER,
      error         TEXT,
      station       TEXT,
      person_id     TEXT,
      via           TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS signoffs (
      project    TEXT,
      repo       TEXT,
      person_id  TEXT,
      "at"       TIMESTAMPTZ,
      PRIMARY KEY (project, repo)
    )''',
    r'''ALTER TABLE signoffs ADD COLUMN IF NOT EXISTS via TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS override_reason TEXT''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS override_at TIMESTAMPTZ''',
    r'''ALTER TABLE handovers ADD COLUMN IF NOT EXISTS override_person_id TEXT''',
    r'''CREATE TABLE IF NOT EXISTS handover_overrides (
      project    TEXT,
      "at"       TIMESTAMPTZ,
      reason     TEXT,
      person_id  TEXT,
      handed_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS integration_choices (
      slot        TEXT PRIMARY KEY,
      name        TEXT,
      settings    JSON,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS integration_connections (
      slot        TEXT,
      name        TEXT,
      settings    JSON,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT,
      PRIMARY KEY (slot, name)
    )''',
    r'''CREATE TABLE IF NOT EXISTS project_integrations (
      project_uid TEXT,
      slot        TEXT,
      name        TEXT,
      settings    JSON,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT,
      PRIMARY KEY (project_uid, slot)
    )''',
    r'''CREATE TABLE IF NOT EXISTS pending_writeback (
      ticket       TEXT,
      integration  TEXT,
      status       TEXT,
      attempts     INTEGER,
      last_error   TEXT,
      updated_at   TIMESTAMPTZ,
      PRIMARY KEY (ticket, integration)
    )''',
    r'''CREATE TABLE IF NOT EXISTS project_secrets (
      project     TEXT,
      name        TEXT,
      value       TEXT,
      set_by      TEXT,
      set_at      TIMESTAMPTZ,
      used_by     TEXT,
      used_at     TIMESTAMPTZ,
      PRIMARY KEY (project, name)
    )''',
    r'''CREATE TABLE IF NOT EXISTS project_required_checks (
      project     TEXT PRIMARY KEY,
      commands    JSON,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS merge_queue (
      id            TEXT PRIMARY KEY,
      repo          TEXT,
      base          TEXT,
      pr            INTEGER,
      head          TEXT,
      ticket        TEXT,
      project_uid   TEXT,
      state         TEXT,
      reason        TEXT,
      desk_item     TEXT,
      approved_head TEXT,
      entered_at    TIMESTAMPTZ,
      updated_at    TIMESTAMPTZ
    )''',
    r'''CREATE INDEX IF NOT EXISTS idx_merge_queue_repo ON merge_queue (repo, base)''',
    r'''ALTER TABLE merge_queue ADD COLUMN IF NOT EXISTS before_files TEXT''',
    r'''ALTER TABLE merge_queue ADD COLUMN IF NOT EXISTS unknown_ticks INTEGER''',
    r'''CREATE TABLE IF NOT EXISTS project_merge_method (
      project     TEXT PRIMARY KEY,
      method      TEXT,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS answers (
      id          TEXT PRIMARY KEY,
      item        TEXT,
      kind        TEXT,
      project     TEXT,
      ask         TEXT,
      choice      TEXT,
      said        TEXT,
      via         TEXT,
      person_id   TEXT,
      answered_at TIMESTAMPTZ
    )''',
    r'''CREATE INDEX IF NOT EXISTS idx_answers_at ON answers (answered_at)''',
    r'''CREATE TABLE IF NOT EXISTS waiting_told (
      person_id  TEXT,
      item       TEXT,
      told_at    TIMESTAMPTZ,
      sent       BOOLEAN,
      PRIMARY KEY (person_id, item)
    )''',
    r'''ALTER TABLE waiting_told ADD COLUMN IF NOT EXISTS ask TEXT''',
    r'''ALTER TABLE waiting_told ADD COLUMN IF NOT EXISTS channel TEXT''',
    r'''ALTER TABLE waiting_told ADD COLUMN IF NOT EXISTS ts TEXT''',
    r'''ALTER TABLE waiting_told ADD COLUMN IF NOT EXISTS seq INTEGER''',
    r'''ALTER TABLE waiting_told ADD COLUMN IF NOT EXISTS closed TEXT''',
    r'''CREATE TABLE IF NOT EXISTS session_usage (
      session            TEXT,
      message            TEXT,
      replied_at         TIMESTAMPTZ,
      model              TEXT,
      project            TEXT,
      subagent           TEXT,
      input_tokens       BIGINT,
      output_tokens      BIGINT,
      cache_write_tokens BIGINT,
      cache_read_tokens  BIGINT,
      cost_usd           DOUBLE,
      PRIMARY KEY (session, message)
    )''',
    r'''ALTER TABLE session_usage ADD COLUMN IF NOT EXISTS cache_write_1h_tokens BIGINT''',
    r'''CREATE TABLE IF NOT EXISTS session_meters (
      session          TEXT PRIMARY KEY,
      status           JSON,
      status_at        TIMESTAMPTZ,
      context          JSON,
      context_at       TIMESTAMPTZ,
      context_asked_at TIMESTAMPTZ
    )''',
    r'''ALTER TABLE session_meters ADD COLUMN IF NOT EXISTS status_person TEXT''',
    r'''CREATE TABLE IF NOT EXISTS services (
      name        TEXT PRIMARY KEY,
      display     TEXT,
      hidden      BOOLEAN,
      merged_into TEXT,
      repos       JSON,
      updated_at  TIMESTAMPTZ,
      updated_by  TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS watch_state (
      service    TEXT PRIMARY KEY,
      watched    BOOLEAN,
      reason     TEXT,
      since      TIMESTAMPTZ,
      agent      TEXT,
      project    TEXT,
      told_at    TIMESTAMPTZ,
      told_where TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS incidents (
      id           TEXT PRIMARY KEY,
      service      TEXT,
      project      TEXT,
      state        TEXT,
      opened_at    TIMESTAMPTZ,
      last_note_at TIMESTAMPTZ,
      owner        TEXT,
      owner_name   TEXT,
      owner_at     TIMESTAMPTZ,
      resolved_at  TIMESTAMPTZ,
      resolved_by  TEXT,
      resolved_by_name TEXT,
      resolution   TEXT,
      slack        JSON,
      updated_at   TIMESTAMPTZ
    )''',
    r'''CREATE TABLE IF NOT EXISTS incident_notes (
      run_id        TEXT PRIMARY KEY,
      incident_id   TEXT,
      joined_at     TIMESTAMPTZ,
      slack_channel TEXT,
      slack_ts      TEXT
    )''',
    r'''CREATE TABLE IF NOT EXISTS incident_log (
      incident_id TEXT, at_time TIMESTAMPTZ, action TEXT, by_id TEXT, by_name TEXT, via TEXT
    )''',
    r'''ALTER TABLE incidents ADD COLUMN IF NOT EXISTS slack_url TEXT''',
    r'''CREATE TABLE IF NOT EXISTS finding_feedback (
      run_id      TEXT,
      person      TEXT,
      person_name TEXT,
      worth       BOOLEAN,
      note        TEXT,
      via         TEXT,
      watcher     TEXT,
      watcher_run TEXT,
      dispatch_id TEXT,
      project     TEXT,
      answered_at TIMESTAMPTZ,
      PRIMARY KEY (run_id, person)
    )''',
]

SEED = {
    "settings": (("key", "value", "updated_at"), [
        ("edition", "factory", _T),
        ("default_project", "uc_demo", _T),
        ("gateway_url", "https://gw.example.test/v1", _T),
        ("lineage_backfilled", "1", _T),
    ]),
    "session_stations": (("session", "station", "role", "parent", "project", "seen_at", "crew_id"), [
        ("s1", "crew-builder", "builder", None, "uc_demo", _T, "cr_1"),
        ("s2", "crew-reviewer", "reviewer", None, "uc_demo", _T, "cr_1"),
    ]),
    "session_states": (("session", "state", "reason", "state_at"), [
        ("s1", "working", None, _T),
        ("s2", "waiting", "needs a person", _T),
    ]),
    "project_sessions": (("project", "session", "repo", "linked_at"), [
        ("uc_demo", "s1", "glassflow/tares", _T),
    ]),
}


def _insert(execute, seed) -> dict:
    done = {}
    for table, (cols, rows) in (seed or {}).items():
        marks = ", ".join("?" * len(cols))
        for row in rows:
            execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", list(row))
        done[table] = [tuple(r) for r in rows]
    return done


def old_ddl_duckdb(con, seed=SEED) -> dict:
    """The old DDL on a DuckDB connection, then the sample rows."""
    for stmt in OLD_DDL:
        con.execute(stmt)
    return _insert(con.execute, seed)


def old_ddl_postgres(dsn: str, seed=SEED) -> dict:
    """The old DDL on the Postgres at `dsn` (a schema is chosen by `?options=-csearch_path%3Dx` in
    it), the way a cell that ran before the move has it, then the sample rows."""
    from tares import state_db
    pg = state_db.PgState(dsn, wait=0)
    try:
        for stmt in state_db.state_ddl(OLD_DDL):
            pg.execute(stmt)
        pg.execute("CREATE INDEX IF NOT EXISTS tickets_project ON tickets(project)")
        pg.execute("CREATE INDEX IF NOT EXISTS ticket_history_ticket ON ticket_history(ticket)")
        return _insert(pg.execute, seed)
    finally:
        pg.close()
