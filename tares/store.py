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
from .views import parse_window

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
CREATE TABLE IF NOT EXISTS catalog_views (
  name       TEXT PRIMARY KEY,
  key_field  TEXT,
  sources    JSON,
  filters    JSON,
  created_by TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS catalog_triggers (
  name       TEXT PRIMARY KEY,
  view       TEXT,
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
  cost_usd DOUBLE
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
CREATE TABLE IF NOT EXISTS query_log (
  id            TEXT PRIMARY KEY,
  view          TEXT,
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
-- Use cases: a recipe (code) instantiated with params. The instance owns the ordinary catalog
-- objects it created (owned_by on those tables). usecase_objects maps the recipe plan
-- keys to the real object names so a re-plan can diff against what exists.
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
"""

# Columns added after the first release; bring pre-existing DBs up to the current schema.
_MIGRATIONS = [
    "ALTER TABLE catalog_views ADD COLUMN IF NOT EXISTS filters JSON",
    "ALTER TABLE catalog_views ADD COLUMN IF NOT EXISTS created_by TEXT",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS labels JSON",
    "ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS ingest_key TEXT",
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
    # Use-case ownership (see usecases tables). No DEFAULT for the same reason as paused above;
    # NULL reads as "not owned" / "not customized".
    "ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS owned_by TEXT",
    "ALTER TABLE catalog_sources ADD COLUMN IF NOT EXISTS customized BOOLEAN",
    "ALTER TABLE catalog_views ADD COLUMN IF NOT EXISTS owned_by TEXT",
    "ALTER TABLE catalog_views ADD COLUMN IF NOT EXISTS customized BOOLEAN",
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
    """View filters -> ('AND ...' SQL fragment, params). A field name resolves against the source's
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
        elif op in _FILTER_OPS:
            clauses.append(f"{expr} {_FILTER_OPS[op]} ?")
            params.append(float(value) if numeric else str(value))
        else:
            raise ValueError(f"bad filter op {op!r}")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


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
            self._init_source_stats()
            self._init_entity_counts()
        except Exception as e:
            raise StoreUnavailable(f"cannot initialize the database at {path}: {e}", path) from e

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
    def read_view_window(self, sources: list[str], key: str | None, since: datetime, cap: int = 12,
                         filters: list | None = None, where: dict | None = None,
                         include_payload: bool = False):
        """Rows for an entity across sources, time-ordered: (event_time, source, text, labels), plus
        the raw lossless `payload` as a 5th column when `include_payload` is set.

        The entity is selected by `key` (legacy primary key_value) and/or `where` (a
        {label: value} map matching named labels). Passing key=None with a `where` is the
        label-native read ("everything where env=prod"); passing a key keeps the old behaviour.

        Caps each source to its most-recent `cap` events so a lossless store doesn't return a
        bloated payload (e.g. thousands of identical log lines or every 5s metric sample). Ingest
        stays lossless; this bound is a read-path summary, matching what an SRE actually wants.
        `include_payload` pulls the full stored record per row (bounded by the same cap) for callers
        that need fidelity beyond the summary `text`.
        """
        cols = "event_time, source, text, labels" + (", payload" if include_payload else "")
        ph = ", ".join(["?"] * len(sources))
        fsql, fparams = _filter_sql(filters)
        wsql, wparams = _where_sql(where)
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
                  group_by="key_value") -> dict:
        """{group: value} for an aggregate over `field` in the window, grouped by one or more
        labels. `group_by` is a label name (scalar keys, key_value by default) or a list of
        names (tuple keys — a trigger grouping per (env, app)). NULL group values are dropped
        (a row lacking a grouping label isn't a real entity); NULL field values are ignored by
        the aggregate."""
        names = [group_by] if isinstance(group_by, str) else list(group_by)
        gexprs = [_label_expr(n) for n in names]
        ph = ", ".join(["?"] * len(sources))
        fsql, fparams = _filter_sql(filters)
        wsql, wparams = _where_sql(where)
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
                f"WHERE source IN ({ph}) AND event_time >= ?{fsql}{wsql} "
                f"GROUP BY {grp_g} HAVING {having}",
                [*sources, since, *fparams, *wparams],
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
        with self._lock:
            self.con.execute("DELETE FROM cursors WHERE source = ?", [source])

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

    def set_fired(self, trigger: str, key: str, ts: datetime) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO trigger_state VALUES (?, ?, ?) "
                "ON CONFLICT (trigger, key_value) DO UPDATE SET last_fired = excluded.last_fired",
                [trigger, key, ts],
            )

    # ── catalog (DB-backed; YAML is import/export) ────────────────────────────
    def catalog_empty(self) -> bool:
        with self._lock:
            n = self.con.execute(
                "SELECT (SELECT COUNT(*) FROM catalog_sources)"
                " + (SELECT COUNT(*) FROM catalog_views)"
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

    def set_source_paused(self, name: str, paused: bool) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE catalog_sources SET paused = ?, updated_at = ? WHERE name = ?",
                [paused, now_utc(), name],
            )

    def upsert_catalog_view(self, name: str, key_field: str, sources: list,
                            filters: list | None = None, created_by: str = "human") -> None:
        # Explicit column list: migrated DBs have filters/created_by appended after created_at.
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO catalog_views (name, key_field, sources, filters, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET key_field = excluded.key_field, "
                "sources = excluded.sources, filters = excluded.filters, "
                "created_by = excluded.created_by, updated_at = excluded.updated_at",
                [name, key_field, json.dumps(sources), json.dumps(filters or []),
                 created_by, ts, ts],
            )

    def list_catalog_views(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, key_field, sources, filters, created_by, owned_by, customized "
                "FROM catalog_views ORDER BY name"
            ).fetchall()
        return [
            {"name": r[0], "key_field": r[1], "sources": json.loads(r[2]),
             "filters": json.loads(r[3]) if r[3] else [],
             "created_by": r[4] or "human", "owned_by": r[5], "customized": bool(r[6])}
            for r in rows
        ]

    def delete_catalog_view(self, name: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_views WHERE name = ?", [name])

    def upsert_catalog_trigger(self, name: str, view: str, condition: dict,
                               emit: dict, cooldown: str) -> None:
        # Explicit column list: `paused` was appended by migration, so positional VALUES no longer
        # match. A new trigger starts active (FALSE); an edit preserves the current paused state
        # (paused is intentionally NOT in the DO UPDATE SET — it's toggled via set_trigger_paused).
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO catalog_triggers "
                "(name, view, condition, emit, cooldown, created_at, updated_at, paused) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, FALSE) "
                "ON CONFLICT (name) DO UPDATE SET view = excluded.view, "
                "condition = excluded.condition, emit = excluded.emit, "
                "cooldown = excluded.cooldown, updated_at = excluded.updated_at",
                [name, view, json.dumps(condition), json.dumps(emit), cooldown, ts, ts],
            )

    def list_catalog_triggers(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, view, condition, emit, cooldown, paused, owned_by, customized "
                "FROM catalog_triggers ORDER BY name"
            ).fetchall()
        return [
            {"name": r[0], "view": r[1], "condition": json.loads(r[2]),
             "emit": json.loads(r[3]), "cooldown": r[4], "paused": bool(r[5]),
             "owned_by": r[6], "customized": bool(r[7])}
            for r in rows
        ]

    def set_trigger_paused(self, name: str, paused: bool) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE catalog_triggers SET paused = ?, updated_at = ? WHERE name = ?",
                [paused, now_utc(), name],
            )

    def delete_catalog_trigger(self, name: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_triggers WHERE name = ?", [name])

    def clear_catalog(self) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_sources")
            self.con.execute("DELETE FROM catalog_views")
            self.con.execute("DELETE FROM catalog_triggers")
            self.con.execute("DELETE FROM catalog_agents")
            self.con.execute("DELETE FROM mcp_servers")

    # ── Tares agents (a prompt attached to a trigger; enabled ⟺ subscribed) ──
    def upsert_catalog_agent(self, name: str, trigger: str, prompt: str,
                             slack_webhook: str | None = None, model: str | None = None,
                             slack_channel: str | None = None, webhook_url: str | None = None,
                             webhook_token: str | None = None,
                             mcp_servers: list[str] | None = None,
                             max_rounds: int | None = None) -> None:
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO catalog_agents "
                "(name, trigger, prompt, slack_webhook, model, slack_channel, "
                "webhook_url, webhook_token, mcp_servers, max_rounds, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET trigger = excluded.trigger, "
                "prompt = excluded.prompt, slack_webhook = excluded.slack_webhook, "
                "model = excluded.model, slack_channel = excluded.slack_channel, "
                "webhook_url = excluded.webhook_url, webhook_token = excluded.webhook_token, "
                "mcp_servers = excluded.mcp_servers, max_rounds = excluded.max_rounds, "
                "updated_at = excluded.updated_at",
                [name, trigger, prompt, slack_webhook or "", model or "",
                 slack_channel or "", webhook_url or "", webhook_token or "",
                 json.dumps(mcp_servers or []), max_rounds, ts, ts],
            )

    def list_catalog_agents(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, trigger, prompt, slack_webhook, model, slack_channel, "
                "webhook_url, webhook_token, mcp_servers, updated_at, max_rounds, owned_by, customized "
                "FROM catalog_agents ORDER BY name"
            ).fetchall()
        return [
            {"name": r[0], "trigger": r[1], "prompt": r[2], "slack_webhook": r[3] or "",
             "model": r[4] or "", "slack_channel": r[5] or "",
             "webhook_url": r[6] or "", "webhook_token": r[7] or "",
             "mcp_servers": json.loads(r[8]) if r[8] else [], "updated_at": r[9],
             "max_rounds": r[10], "owned_by": r[11], "customized": bool(r[12])}
            for r in rows
        ]

    def get_catalog_agent(self, name: str) -> dict | None:
        return next((a for a in self.list_catalog_agents() if a["name"] == name), None)

    def delete_catalog_agent(self, name: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM catalog_agents WHERE name = ?", [name])
            self.con.execute("DELETE FROM agent_runs WHERE agent = ?", [name])

    # ── agent runs (the operational record; the finding is an event, not this) ──
    def start_agent_run(self, run_id: str, agent: str, trigger: str, dispatch_id: str,
                        key: str, prompt_hash: str, max_rounds: int | None = None) -> None:
        # max_rounds is the cap this run will be held to (the effective value, not the agent's
        # nullable setting), so the history stays honest if defaults change later.
        with self._lock:
            self.con.execute(
                "INSERT INTO agent_runs (id, agent, trigger, dispatch_id, key_value, status, "
                "rounds, tool_calls, prompt_hash, started_at, max_rounds) "
                "VALUES (?, ?, ?, ?, ?, 'running', 0, 0, ?, ?, ?)",
                [run_id, agent, trigger, dispatch_id, key, prompt_hash, now_utc(), max_rounds],
            )

    def finish_agent_run(self, run_id: str, status: str, rounds: int = 0, tool_calls: int = 0,
                         finding: str | None = None, error: str | None = None,
                         external_tools: list[str] | None = None) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE agent_runs SET status = ?, rounds = ?, tool_calls = ?, finding = ?, "
                "error = ?, external_tools = ?, finished_at = ?, "
                "duration_ms = CAST(date_diff('millisecond', started_at, ?) AS INTEGER) "
                "WHERE id = ?",
                [status, rounds, tool_calls, finding, error,
                 json.dumps(external_tools or []), now_utc(), now_utc(), run_id],
            )

    def record_run_usage(self, run_id: str, model: str, input_tokens: int, output_tokens: int,
                         cache_creation_input_tokens: int = 0,
                         cache_read_input_tokens: int = 0,
                         cost_usd: float | None = None) -> None:
        """Stamp a run with what its model loop consumed. Separate from finish_agent_run on
        purpose: usage exists for every outcome (ok, empty, exhausted, and failed after burning
        tokens), so it is written by the loop's finally rather than each outcome path."""
        with self._lock:
            self.con.execute(
                "UPDATE agent_runs SET model = ?, input_tokens = ?, output_tokens = ?, "
                "cache_creation_input_tokens = ?, cache_read_input_tokens = ?, cost_usd = ? "
                "WHERE id = ?",
                [model, input_tokens, output_tokens, cache_creation_input_tokens,
                 cache_read_input_tokens, cost_usd, run_id],
            )

    def list_agent_runs(self, agent: str | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT id, agent, trigger, dispatch_id, key_value, status, rounds, tool_calls, "
               "started_at, duration_ms, finding, error, external_tools, max_rounds, "
               "model, input_tokens, output_tokens, cache_creation_input_tokens, "
               "cache_read_input_tokens, cost_usd "
               "FROM agent_runs ")
        params: list = []
        if agent:
            sql += "WHERE agent = ? "
            params.append(agent)
        sql += "ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.con.execute(sql, params).fetchall()
        return [
            {"id": r[0], "agent": r[1], "trigger": r[2], "dispatch_id": r[3], "key": r[4],
             "status": r[5], "rounds": r[6], "tool_calls": r[7], "started_at": r[8],
             "duration_ms": r[9], "finding": r[10], "error": r[11],
             "external_tools": json.loads(r[12]) if r[12] else [], "max_rounds": r[13],
             "model": r[14], "input_tokens": r[15], "output_tokens": r[16],
             "cache_creation_input_tokens": r[17], "cache_read_input_tokens": r[18],
             "cost_usd": r[19]}
            for r in rows
        ]

    def agent_stats(self) -> dict[str, dict]:
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
                "FROM agent_runs GROUP BY agent"
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
                           cost_usd: float | None = None) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO model_usage (id, ts, surface, agent, run_id, model, calls, "
                "input_tokens, output_tokens, cache_creation_input_tokens, "
                "cache_read_input_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["mu_" + uuid.uuid4().hex[:12], now_utc(), surface, agent, run_id, model,
                 calls, input_tokens, output_tokens, cache_creation_input_tokens,
                 cache_read_input_tokens, cost_usd],
            )

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
            daily = self.con.execute(
                f"SELECT CAST(ts AS DATE) AS day, {agg} FROM model_usage "
                f"WHERE ts > now() - INTERVAL {int(days)} DAY "
                "GROUP BY day ORDER BY day").fetchall()
        return {
            "total": shape(total),
            "by_surface": {r[0]: shape(r[1:]) for r in surfaces},
            "days": [{"day": str(r[0]), **shape(r[1:])} for r in daily],
            "window_days": int(days),
        }

    def agent_runs_today(self, agent: str, exclude_run_id: str | None = None) -> int:
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
        with self._lock:
            self.con.execute("DELETE FROM mcp_servers WHERE name = ?", [name])

    # ── use cases (recipes instantiated with params; they own ordinary catalog objects) ──
    _OWNED_TABLES = {"source": "catalog_sources", "view": "catalog_views",
                     "trigger": "catalog_triggers", "agent": "catalog_agents",
                     "mcp_server": "mcp_servers"}

    def set_owned_by(self, kind: str, name: str, usecase_id: str | None) -> None:
        """Mark an ordinary object as created by a use case (or clear it). Ownership is a badge and
        a diff key, not a lock: the object stays editable and deletable everywhere."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            self.con.execute(f"UPDATE {table} SET owned_by = ?, customized = FALSE WHERE name = ?",
                             [usecase_id, name])

    def mark_customized(self, kind: str, name: str) -> bool:
        """Called by the normal update paths: if the object is owned by a use case, flag it so the
        engine keeps the user's version on the next re-plan. Returns whether it was owned."""
        table = self._OWNED_TABLES[kind]
        with self._lock:
            row = self.con.execute(f"SELECT owned_by FROM {table} WHERE name = ?", [name]).fetchone()
            if not row or not row[0]:
                return False
            self.con.execute(f"UPDATE {table} SET customized = TRUE WHERE name = ?", [name])
            self.con.execute("UPDATE usecase_objects SET customized = TRUE "
                             "WHERE usecase_id = ? AND kind = ? AND name = ?", [row[0], kind, name])
        return True

    def create_usecase(self, uid: str, recipe: str, name: str, params: dict,
                       status: str = "active") -> None:
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO usecases (id, recipe, name, params, status, created_at, updated_at, "
                "last_error) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                [uid, recipe, name, json.dumps(params), status, ts, ts])

    def update_usecase(self, uid: str, params: dict | None = None, status: str | None = None,
                       last_error: str | None = "", name: str | None = None) -> None:
        """last_error: "" (default) leaves it unchanged; None clears it; a string sets it."""
        sets, vals = ["updated_at = ?"], [now_utc()]
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
    def _usecase_row(r) -> dict:
        return {"id": r[0], "recipe": r[1], "name": r[2], "params": json.loads(r[3] or "{}"),
                "status": r[4], "created_at": r[5], "updated_at": r[6], "last_error": r[7]}

    def list_usecases(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, recipe, name, params, status, created_at, updated_at, last_error "
                "FROM usecases ORDER BY created_at").fetchall()
        return [self._usecase_row(r) for r in rows]

    def get_usecase(self, uid: str) -> dict | None:
        with self._lock:
            r = self.con.execute(
                "SELECT id, recipe, name, params, status, created_at, updated_at, last_error "
                "FROM usecases WHERE id = ?", [uid]).fetchone()
        return self._usecase_row(r) if r else None

    def get_usecase_by_name(self, name: str) -> dict | None:
        return next((u for u in self.list_usecases() if u["name"] == name), None)

    def delete_usecase(self, uid: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ?", [uid])
            self.con.execute("DELETE FROM usecase_log WHERE usecase_id = ?", [uid])
            self.con.execute("DELETE FROM usecases WHERE id = ?", [uid])

    def upsert_usecase_object(self, uid: str, kind: str, key: str, name: str) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO usecase_objects (usecase_id, kind, key, name, customized, created_at) "
                "VALUES (?, ?, ?, ?, FALSE, ?) ON CONFLICT (usecase_id, kind, key) DO UPDATE SET "
                "name = excluded.name", [uid, kind, key, name, now_utc()])

    def list_usecase_objects(self, uid: str) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT kind, key, name, customized, created_at FROM usecase_objects "
                "WHERE usecase_id = ? ORDER BY created_at, kind, key", [uid]).fetchall()
        return [{"kind": r[0], "key": r[1], "name": r[2], "customized": bool(r[3]),
                 "created_at": r[4]} for r in rows]

    def delete_usecase_object(self, uid: str, kind: str, key: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM usecase_objects WHERE usecase_id = ? AND kind = ? "
                             "AND key = ?", [uid, kind, key])

    def log_usecase(self, uid: str, action: str, detail: str = "") -> None:
        with self._lock:
            self.con.execute("INSERT INTO usecase_log (usecase_id, logged_at, action, detail) "
                             "VALUES (?, ?, ?, ?)", [uid, now_utc(), action, detail[:2000]])

    def list_usecase_log(self, uid: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT logged_at, action, detail FROM usecase_log WHERE usecase_id = ? "
                "ORDER BY logged_at DESC LIMIT ?", [uid, limit]).fetchall()
        return [{"at": r[0], "action": r[1], "detail": r[2]} for r in rows]
    # ── GitHub credentials: a token stored once, referenced by sources and MCP servers ──
    # The token is held verbatim like every other connector secret; redaction is the API's job.
    def list_github_credentials(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT name, kind, token, api_url, account, created_at, updated_at "
                "FROM github_credentials ORDER BY name").fetchall()
        return [{"name": r[0], "kind": r[1] or "token", "token": r[2] or "",
                 "api_url": r[3] or "", "account": r[4] or "",
                 "created_at": r[5], "updated_at": r[6]} for r in rows]

    def get_github_credential(self, name: str) -> dict | None:
        return next((c for c in self.list_github_credentials() if c["name"] == name), None)

    def upsert_github_credential(self, name: str, token: str, kind: str = "token",
                                 api_url: str = "", account: str = "") -> None:
        ts = now_utc()
        with self._lock:
            self.con.execute(
                "INSERT INTO github_credentials (name, kind, token, api_url, account, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET kind = excluded.kind, token = excluded.token, "
                "api_url = excluded.api_url, account = excluded.account, "
                "updated_at = excluded.updated_at",
                [name, kind, token or "", api_url or "", account or "", ts, ts])

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
    def log_query(self, qid: str, view: str, key: str, window: str,
                  rows_returned: int, client: str) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO query_log VALUES (?, ?, ?, ?, ?, ?, ?)",
                [qid, view, key, window, rows_returned, client, now_utc()],
            )

    def list_queries(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, view, key_value, time_window, rows_returned, client, queried_at "
                "FROM query_log ORDER BY queried_at DESC LIMIT ?", [int(limit)],
            ).fetchall()
        return [
            {"id": r[0], "view": r[1], "key": r[2], "window": r[3],
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
                     subscribers: int, delivered: int, payload: str) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO dispatch_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [dispatch_id, trigger, key, kind, now_utc(), subscribers, delivered, payload],
            )

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

    def view_usage(self) -> dict:
        """{view: {queries, last_used_at}} from the query log — feeds usage-driven deprecation."""
        with self._lock:
            rows = self.con.execute(
                "SELECT view, COUNT(*), MAX(queried_at) FROM query_log GROUP BY view"
            ).fetchall()
        return {r[0]: {"queries": r[1], "last_used_at": r[2]} for r in rows}

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
    def add_subscription(self, sid: str, trigger: str, url: str, created_by: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (subscription_id) DO UPDATE SET trigger = excluded.trigger, url = excluded.url",
                [sid, trigger, url, now_utc(), created_by],
            )

    def list_subscriptions(self, trigger: str):
        with self._lock:
            return self.con.execute(
                "SELECT subscription_id, trigger, url FROM subscriptions WHERE trigger = ?",
                [trigger],
            ).fetchall()

    def list_all_subscriptions(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT subscription_id, trigger, url, created_at FROM subscriptions "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [{"subscription_id": r[0], "trigger": r[1], "url": r[2], "created_at": r[3]}
                for r in rows]

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

    def all_subscriptions(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT subscription_id, trigger, url, created_at, created_by "
                "FROM subscriptions ORDER BY created_at").fetchall()
        return [{"subscription_id": r[0], "trigger": r[1], "url": r[2],
                 "created_at": r[3], "created_by": r[4]} for r in rows]

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
    def insert_api_key(self, kid: str, name: str, prefix: str, hash_: str, scopes: list[str]) -> None:
        with self._lock:
            self.con.execute("INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                             [kid, name, prefix, hash_, json.dumps(scopes), now_utc()])

    def list_api_keys(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT id, name, prefix, scopes, created_at, last_used_at, revoked_at "
                "FROM api_keys ORDER BY created_at DESC").fetchall()
        return [{"id": r[0], "name": r[1], "prefix": r[2], "scopes": json.loads(r[3]),
                 "created_at": r[4], "last_used_at": r[5], "revoked_at": r[6]} for r in rows]

    def find_api_key(self, hash_: str) -> dict | None:
        """Active (non-revoked) key by secret hash, or None."""
        with self._lock:
            r = self.con.execute(
                "SELECT id, name, scopes, last_used_at FROM api_keys "
                "WHERE hash = ? AND revoked_at IS NULL", [hash_]).fetchone()
        if not r:
            return None
        return {"id": r[0], "name": r[1], "scopes": json.loads(r[2]), "last_used_at": r[3]}

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
