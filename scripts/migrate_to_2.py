#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1", "sqlalchemy>=2.0", "psycopg[binary]>=3.1", "alembic>=1.13"]
# ///
"""Move one Tares 1.61.0 cell's data to the Tares 2.0 baseline (TR-1821). Run once per cell.

    uv run scripts/migrate_to_2.py --check DATA_DIR [--state-dsn DSN] [--archive-url URL]
    uv run scripts/migrate_to_2.py --apply DATA_DIR [--state-dsn DSN] [--archive-url URL] --to OUT
    uv run scripts/migrate_to_2.py --verify DATA_DIR [--state-dsn DSN] [--archive-url URL] --to OUT

The input is a stopped 1.61.0 cell: its data directory (`tares.sqlite` and `tares.duckdb`), or, for a
cell with TARES_STATE_DSN, the state Postgres plus the data directory that holds `tares.duckdb`. The
input is only read: the SQLite file is copied first and the copy is read, DuckDB is opened read-only,
and every Postgres session runs with `default_transaction_read_only`.

`--check` checks the input and prints, per table, what `--apply` would copy, rename, rebuild or
drop, with row counts. It writes nothing.

`--apply` writes a NEW 2.0 store, never the input: OUT is a folder that does not exist yet (or is
empty), which becomes the 2.0 data directory (`tares.sqlite`), or a Postgres DSN whose schema (the
first one of its search_path, made when missing) holds no table. The 2.0 tables come from this
repo's Alembic revision (tares/session_store/migrations, run by path), the rows are copied, the
verification below runs, and only then is the store stamped `0001_baseline` (a SQLite folder is
built as OUT.migrating and renamed to OUT at that point). A failed apply leaves an unstamped store
that a 2.0 daemon does not start on: delete it and run again.

`--verify` runs the verification again on an applied output.

The input check refuses a cell that 1.61.0 has not finished upgrading (it finishes these on start):
- the session store is not at revision 0024_merge_ledger, or its tables are not 1.61.0's;
- a session move is pending (`session_moves`), or failed (unless `--rebuild-failed-moves`, which
  rebuilds those sessions from DuckDB's lines here);
- the move was never planned while DuckDB holds claude_code lines;
- on a SQLite cell, a table of the move from DuckDB has no `session_store_copies` row.

What happens to each table is printed by --check. In short: every factory table is copied column
by column (the columns both versions have; a 2.0 column with no source keeps its default, and one
that is NOT NULL without a default fails the run); `usecases` becomes `projects`, `usecase_objects`
`project_docs` (kind doc) and `project_skills` (kind skill), `usecase_log` `project_log`;
`integration_keys` is made from the github_app catalog sources and `pr_events` from DuckDB's pull
request events of the last 30 days; every session line is stored under the source `claude_code`
with the default render flags; a session whose lines are not all in `session_lines` and the archive
gets the missing ones from DuckDB; challenger lines and model replies DuckDB holds and the store
does not are added; `session_usage_hourly` is built again. The platform tables, the move ledgers,
`catalog_sources`, `skills` and the DuckDB events are left behind.

The rebuild step. What a 2.0 cell writes as each line is stored (the facts and turns of a session,
the price of a reply, the hourly usage totals) is rebuilt with the 2.0 code itself, imported from
this checkout (`tares.session_ingest`, `tares.pricing`, `tares.claude_usage_hourly`,
`tares.session_store`), only on the output, after the copy. The rest of the script imports nothing
from `tares/`: it names the 1.61.0 and 2.0 tables itself. So run it from a checkout of the 2.0
release the cell will run.

The archive (gzip JSON lines under TARES_ARCHIVE_URL, file:// or s3://) does not change. A SQLite
cell on its default archive (`<data dir>/archive`) gets the files linked (or copied) into OUT's
`archive` folder, byte for byte. Elsewhere the 2.0 cell keeps the same TARES_ARCHIVE_URL. Pass the
cell's archive with `--archive-url` when it is not the default; reading an s3:// one needs boto3
(`uv run --with boto3 scripts/migrate_to_2.py ...`) and the TARES_ARCHIVE_* variables.

Verification (after --apply, and --verify): the 2.0 tables are exactly the baseline's; per table
the row count is the one the input gives; for projects, tickets, reviews, decisions and the session
lines an MD5 of the ordered ids with their timestamps equals the input's; every webhook key is the
catalog source's key, byte for byte; a sample of archived parts reads back with the size, MD5 and
lines its row records. Any difference fails, naming the table.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import sqlalchemy as sa
from sqlalchemy import MetaData, func, insert, select

REPO = Path(__file__).resolve().parent.parent
INPUT = json.loads((Path(__file__).resolve().parent / "migrate_to_2_input.json").read_text())
HEAD_1 = INPUT["head_revision"]                      # 0024_merge_ledger
VERSION_TABLE = INPUT["version_table"]               # session_store_version
MIGRATIONS = REPO / "tares" / "session_store" / "migrations"
BASELINE = "0001_baseline"

# Every table of a 2.0 cell (tests/test_baseline.py holds the same list; the test of this script
# checks that the two agree).
TABLES_2 = [
    "answers", "api_keys", "assumptions", "brain_calls", "brain_repo_folders",
    "brain_repo_updates", "brain_repos", "challenges", "crews", "decisions",
    "desk_items", "docs", "github_credentials", "grants", "guardrail_pairs",
    "guardrail_rules", "handover_overrides", "handovers", "integration_choices", "integration_connections",
    "integration_keys", "merge_queue", "merge_records", "milestone_acceptances", "milestone_check_results",
    "milestones", "model_usage", "pending_memory", "pending_writeback", "people",
    "pr_events", "project_docs", "project_integrations", "project_log", "project_members",
    "project_merge_method", "project_required_checks", "project_secrets", "project_sessions", "project_skills",
    "projects", "repo_map_history", "repo_maps", "report_texts", "report_weeks",
    "review_findings", "reviews", "rule_proposals", "session_archive_parts", "session_commands",
    "session_files", "session_lines", "session_messages", "session_meters", "session_rollup_state",
    "session_said", "session_states", "session_stations", "session_turns", "session_usage",
    "session_usage_hourly", "sessions", "settings", "shared_skills", "signoffs",
    "spec_questions", "ticket_checks", "ticket_history", "ticket_numbers", "tickets",
    "waiting_told",
]

# The 1.61.0 tables of the move from DuckDB into the session store (tables.MOVED_TABLES at v1.61.0):
# on a SQLite cell each one has its row in session_store_copies once 1.61.0 copied it.
MOVED_1 = [
    "session_stations", "session_states", "project_sessions", "session_usage", "session_meters",
    "settings", "people", "api_keys", "project_members", "usecases", "usecase_objects", "usecase_log",
    "docs", "skills", "shared_skills", "catalog_sources", "github_credentials", "model_usage",
    "tickets", "ticket_history", "ticket_numbers", "milestones", "milestone_check_results",
    "milestone_acceptances", "ticket_checks", "challenges", "reviews", "review_findings",
    "rule_proposals", "assumptions", "desk_items", "decisions", "handovers", "grants",
    "pending_memory", "crews", "spec_questions", "signoffs", "handover_overrides", "answers",
    "waiting_told", "guardrail_rules", "guardrail_pairs", "integration_choices",
    "integration_connections", "project_integrations", "pending_writeback", "project_secrets",
    "project_required_checks", "merge_queue", "project_merge_method", "repo_maps",
    "repo_map_history", "brain_repos", "brain_repo_folders", "brain_repo_updates", "brain_calls",
]

RENAMED = {"projects": "usecases", "project_log": "usecase_log"}
FROM_OBJECTS = {"project_docs": "doc", "project_skills": "skill"}
MADE = ("integration_keys", "pr_events")               # 2.0 tables with no 1.61.0 table
REBUILT = ("session_usage_hourly", "session_rollup_state")
LEDGERS = ("session_moves", "session_ingest_marks", "session_store_copies", "tares_state_meta")
PROJECT_COLUMNS = ("id", "name", "status", "created_at", "updated_at", "goal", "linear",
                   "github_issues", "kind", "lead_person_id", "scope", "icon")
FACTORY_RECIPES = ("custom", "default", "factory")
# The tables that hold one project's own rows, cleared with --drop-non-factory: (table, column)
PROJECT_OWNED = (("projects", "id"), ("project_docs", "project"), ("project_skills", "project"),
                 ("project_log", "project"), ("project_members", "project"),
                 ("project_integrations", "project_uid"), ("project_secrets", "project"),
                 ("project_required_checks", "project"), ("project_merge_method", "project"),
                 ("ticket_numbers", "project"), ("handovers", "project"))

SESSION_SOURCE = "claude_code"                       # 2.0 stores every session line under it
DEFAULT_RENDER = json.dumps({"include_thinking": False, "redact": True}, sort_keys=True)
CHALLENGE_TYPES = ("challenge_commit", "challenge_waived", "challenge_plan")
SYNTHETIC = "<synthetic>"
PR_EVENTS_DAYS = 30
CHUNK = 2000
DUCKDB_MEMORY = os.environ.get("MIGRATE_DUCKDB_MEMORY", "2GB")   # DuckDB's memory limit while it is read
ARCHIVE_SAMPLE = 25
FACT_TABLES = ("session_messages", "session_commands", "session_turns", "session_said", "session_files")
# Checksums: (table, key columns in order, timestamp column)
CHECKSUMS = (("projects", ("id",), "updated_at"), ("tickets", ("id",), "updated_at"),
             ("reviews", ("id",), "reviewed_at"), ("decisions", ("id",), "decided_at"))


class Refused(Exception):
    """The input is not a 1.61.0 cell this script can move as it is."""


class Mismatch(Exception):
    """The output differs from what the input gives."""


# ── small helpers ────────────────────────────────────────────────────────────────────────────
def say(msg: str = "") -> None:
    print(msg, flush=True)


def n(v) -> str:
    return f"{v:,}"


def utc(v):
    """A datetime as an aware UTC one (a naive one is taken to be UTC); anything else as it is."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)
    return v


def stamp(v) -> str:
    v = utc(v)
    return v.isoformat() if isinstance(v, datetime) else ("" if v is None else str(v))


def pg_url(dsn: str) -> str:
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return "postgresql+psycopg://" + dsn[len(scheme):]
    return dsn


def is_dsn(s: str) -> bool:
    return s.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))


def ordered(col, dialect: str):
    """ORDER BY a text column in byte order on both engines (a Postgres collation such as
    en_US.UTF-8 orders otherwise)."""
    return col.collate("C") if dialect == "postgresql" else col


def _json(v):
    """A DuckDB JSON value as Python: its text parsed ({} for none or for text that is not JSON)."""
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v) if v else {}
        except ValueError:
            return {}
    return {} if v is None else v


def _obj(v) -> dict:
    v = _json(v)
    return v if isinstance(v, dict) else {}


def _text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(v, separators=(",", ":"), sort_keys=True)


def line_key(payload) -> str:
    """What names one line in its session: the payload's uuid, else the SHA-256 of its JSON (the rule
    of 1.61.0 and 2.0 alike, session_ingest.line_key)."""
    o = _obj(payload)
    u = o.get("uuid")
    if isinstance(u, str) and u:
        return u
    return hashlib.sha256(json.dumps(o, sort_keys=True).encode("utf-8", "replace")).hexdigest()


def no_nul(v):
    return v.replace("\x00", "") if isinstance(v, str) else v


def md5_rows(rows) -> str:
    h = hashlib.md5()
    for r in rows:
        h.update(("\x1f".join(stamp(x) for x in r) + "\x1e").encode("utf-8", "surrogatepass"))
    return h.hexdigest()


# ── the input ────────────────────────────────────────────────────────────────────────────────
class Cell:
    """A stopped 1.61.0 cell, read only."""

    def __init__(self, data_dir: str, state_dsn: str | None, archive_url: str | None, work: str):
        self.dir = os.path.abspath(data_dir)
        self.state_dsn = state_dsn
        self.archive_url = archive_url
        if not os.path.isdir(self.dir):
            raise Refused(f"{self.dir} is not a folder")
        if state_dsn:
            self.engine = sa.create_engine(pg_url(state_dsn), connect_args={"prepare_threshold": None},
                                           pool_size=2, max_overflow=2)

            @sa.event.listens_for(self.engine, "connect")
            def _ro(dbapi_conn, _rec):
                with dbapi_conn.cursor() as cur:
                    cur.execute("SET default_transaction_read_only = on")
                dbapi_conn.commit()
            self.where = "state Postgres"
        else:
            path = os.path.join(self.dir, "tares.sqlite")
            if not os.path.exists(path):
                raise Refused(f"{path} does not exist (a 1.61.0 data directory holds tares.sqlite)")
            # SQLite cannot open a WAL database read-only without writing beside it, so the file
            # (and its WAL, if any) is copied and the copy is read
            self.copy = os.path.join(work, "input-copy")
            os.makedirs(self.copy, exist_ok=True)
            for suffix in ("", "-wal"):
                if os.path.exists(path + suffix):
                    shutil.copy2(path + suffix, os.path.join(self.copy, "tares.sqlite" + suffix))
            self.engine = sa.create_engine("sqlite:///" + os.path.join(self.copy, "tares.sqlite"))
            self.where = "tares.sqlite"
        self.dialect = self.engine.dialect.name
        self.md = MetaData()
        self.md.reflect(self.engine)
        self.tables = set(self.md.tables)
        duck_path = os.path.join(self.dir, "tares.duckdb")
        if not os.path.exists(duck_path):
            raise Refused(f"{duck_path} does not exist (a 1.61.0 cell keeps its events there)")
        self.duck = duckdb.connect(duck_path, read_only=True)
        self.duck.execute("SET TimeZone = 'UTC'")
        # bounded, spilling into the work folder: the volume may be read-only, the pod small
        self.duck.execute(f"SET memory_limit = '{DUCKDB_MEMORY}'")
        self.duck.execute("SET threads = 2")
        self.duck.execute("SET preserve_insertion_order = false")
        os.makedirs(os.path.join(work, "duckdb-tmp"), exist_ok=True)
        self.duck.execute(f"SET temp_directory = '{os.path.join(work, 'duckdb-tmp')}'")
        self.duck_tables = {r[0] for r in self.duck.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
            .fetchall()}

    def t(self, name):
        return self.md.tables[name]

    def count(self, name, where=None) -> int:
        if name not in self.tables:
            return 0
        t = self.t(name)
        q = select(func.count()).select_from(t)
        if where is not None:
            q = q.where(where(t))
        with self.engine.connect() as c:
            return int(c.execute(q).scalar_one())

    def rows(self, q):
        with self.engine.connect() as c:
            return c.execute(q).all()

    def stream(self, q, chunk=CHUNK):
        with self.engine.connect() as c:
            res = c.execution_options(stream_results=True, yield_per=chunk).execute(q)
            while True:
                part = res.fetchmany(chunk)
                if not part:
                    return
                yield part

    def setting(self, key):
        if "settings" not in self.tables:
            return None
        t = self.t("settings")
        r = self.rows(select(t.c.value).where(t.c.key == key))
        return r[0][0] if r else None

    def duck_rows(self, sql, args=()):
        return self.duck.execute(sql, list(args)).fetchall()

    def close(self):
        self.engine.dispose()
        self.duck.close()


# ── the archive ──────────────────────────────────────────────────────────────────────────────
class Archive:
    """Reads parts from a folder or an S3 bucket (writes nothing)."""

    def __init__(self, url: str):
        p = urllib.parse.urlparse(url)
        self.url = url
        if p.scheme == "file":
            self.root, self.s3 = urllib.parse.unquote(p.path), None
        elif p.scheme == "s3":
            try:
                import boto3
            except ImportError as e:
                raise Refused("reading an s3:// archive needs boto3: "
                              "uv run --with boto3 scripts/migrate_to_2.py ...") from e
            env = os.environ
            self.s3 = boto3.client("s3", endpoint_url=env.get("TARES_ARCHIVE_ENDPOINT") or None,
                                   region_name=env.get("TARES_ARCHIVE_REGION") or None,
                                   aws_access_key_id=env.get("TARES_ARCHIVE_ACCESS_KEY_ID") or None,
                                   aws_secret_access_key=env.get("TARES_ARCHIVE_SECRET_ACCESS_KEY") or None)
            self.bucket, self.prefix = p.netloc, urllib.parse.unquote(p.path).strip("/")
        else:
            raise Refused(f"archive {url!r}: only file:///path and s3://bucket/prefix/ are known")

    def get(self, key: str) -> bytes:
        if self.s3 is None:
            root = os.path.abspath(self.root)
            path = os.path.abspath(os.path.join(root, *key.split("/")))
            if os.path.commonpath([root, path]) != root:
                raise Mismatch(f"archive key {key!r} leaves the archive folder")
            with open(path, "rb") as f:
                return f.read()
        k = f"{self.prefix}/{key}" if self.prefix else key
        return self.s3.get_object(Bucket=self.bucket, Key=k)["Body"].read()

    def read_part(self, part) -> list[dict]:
        """One part's rows, checked against its row: size, MD5, line count and seq range."""
        data = self.get(part["key"])
        if len(data) != part["bytes"] or hashlib.md5(data).hexdigest() != part["md5"]:
            raise Mismatch(f"session_archive_parts: {part['key']} does not match its row "
                           f"(size {len(data)} of {part['bytes']})")
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            rows = [json.loads(ln.decode("ascii")) for ln in gz]
        seqs = [r.get("seq") for r in rows]
        if (len(rows) != part["lines"] or not rows or seqs[0] != part["first_seq"]
                or seqs[-1] != part["last_seq"] or seqs != sorted(seqs)):
            raise Mismatch(f"session_archive_parts: {part['key']} holds {len(rows)} lines, "
                           f"not the {part['lines']} its row records")
        return rows


def archive_of(cell: Cell) -> Archive | None:
    if cell.archive_url:
        return Archive(cell.archive_url)
    if cell.dialect == "sqlite":
        return Archive("file://" + urllib.parse.quote(os.path.join(cell.dir, "archive")))
    return None


# ── what the input holds, and the plan ───────────────────────────────────────────────────────
class Plan:
    def __init__(self):
        self.checks: list[tuple[bool, str]] = []
        self.rows: list[tuple[str, str, str, int, str]] = []      # (2.0 table, action, from, rows, note)
        self.dropped: list[tuple[str, int, str]] = []
        self.notes: list[str] = []
        self.refusals: list[str] = []


def cc_sources(cell: Cell) -> dict[str, dict]:
    """{name: config} of the claude_code catalog sources, and `claude_code` itself."""
    out = {SESSION_SOURCE: {}}
    if "catalog_sources" in cell.tables:
        t = cell.t("catalog_sources")
        for name, cfg in cell.rows(select(t.c.name, t.c.config).where(t.c.connector == "claude_code")):
            out[name] = _obj(cfg)
    return out


def github_sources(cell: Cell, connectors=("github_app",)) -> list[dict]:
    if "catalog_sources" not in cell.tables:
        return []
    t = cell.t("catalog_sources")
    return [{"name": r[0], "connector": r[1], "config": _obj(r[2]), "ingest_key": r[3], "created_at": r[4]}
            for r in cell.rows(select(t.c.name, t.c.connector, t.c.config, t.c.ingest_key, t.c.created_at)
                               .where(t.c.connector.in_(connectors)).order_by(t.c.name))]


def held_counts(cell: Cell) -> dict[str, int]:
    """{session: lines the table and the archive hold}."""
    out: Counter = Counter()
    t = cell.t("session_lines")
    for sid, k in cell.rows(select(t.c.session, func.count()).group_by(t.c.session)):
        out[sid] += int(k)
    p = cell.t("session_archive_parts")
    for sid, k in cell.rows(select(p.c.session, func.coalesce(func.sum(p.c.lines), 0)).group_by(p.c.session)):
        out[sid] += int(k)
    return out


def duck_counts(cell: Cell, sources) -> dict[str, int]:
    if "events" not in cell.duck_tables:
        return {}
    marks = ", ".join("?" for _ in sources)
    return {sid: int(k) for sid, k in cell.duck_rows(
        f"SELECT key_value, count(*) FROM events WHERE source IN ({marks}) AND key_value IS NOT NULL "
        "AND key_value <> '' GROUP BY key_value", sorted(sources))}


def session_rows(cell: Cell) -> list:
    s = cell.t("sessions")
    return cell.rows(select(s.c.session, s.c.source, s.c.line_count, s.c.lines_complete, s.c.lines_origin))


def move_states(cell: Cell) -> dict[str, str]:
    if "session_moves" not in cell.tables:
        return {}
    m = cell.t("session_moves")
    return dict(cell.rows(select(m.c.session, m.c.state)))


def marks(cell: Cell) -> dict:
    if "session_ingest_marks" not in cell.tables:
        return {}
    t = cell.t("session_ingest_marks")
    return dict(cell.rows(select(t.c.name, t.c.at)))


def sessions_to_repair(cell: Cell, held: dict, moves: dict, duck: dict) -> dict[str, str]:
    """{session: why} for the sessions whose lines may not all be in the table and the archive: not
    born and complete in the table, a failed move, or a legacy complete flag on fewer lines than the
    count. A session with no lines in DuckDB only gets its flag put right."""
    out = {}
    for sid, _src, count, complete, origin in session_rows(cell):
        h = held.get(sid, 0)
        if moves.get(sid) == "failed":
            out[sid] = "its move failed"
        elif not complete:
            out[sid] = "lines_complete is false"
        elif origin != "table":
            out[sid] = "counted by an older version (no lines_origin)"
        elif h != (count or 0):
            out[sid] = f"lines_complete is true on {h} of {count} lines"
    return out


def non_factory_projects(cell: Cell) -> list[tuple]:
    u = cell.t("usecases")
    rows = cell.rows(select(u.c.id, u.c.name, u.c.recipe, u.c.kind).order_by(u.c.id))
    return [r for r in rows if r[2] != "default"
            and (r[2] not in FACTORY_RECIPES or r[3] != "software_factory")]


def inspect_cell(cell: Cell, args) -> Plan:
    plan = Plan()
    ck = plan.checks.append
    # the revision and the tables
    rev = None
    if VERSION_TABLE in cell.tables:
        rev = [r[0] for r in cell.rows(select(cell.t(VERSION_TABLE).c.version_num))]
    if rev == [HEAD_1]:
        ck((True, f"revision {HEAD_1}"))
    else:
        rev = ", ".join(rev or []) or "none"
        ck((False, f"revision {rev}, not {HEAD_1}"))
        plan.refusals.append(f"the session store is at revision {rev}: only a 1.61.0 cell "
                             f"({HEAD_1}) is moved. Run Tares 1.61.0 on it once, stop it, then run this again")
    want = set(INPUT["postgres" if cell.dialect == "postgresql" else "sqlite"]["tables"])
    missing, extra = sorted(want - cell.tables), sorted(cell.tables - want)
    if not missing and not extra:
        ck((True, f"{len(cell.tables)} tables, as 1.61.0 has them"))
    else:
        ck((False, f"tables differ from 1.61.0's: missing {missing or 'none'}, extra {extra or 'none'}"))
        if missing:
            plan.refusals.append(f"tables 1.61.0 has are missing: {', '.join(missing)}")
        if extra:
            plan.refusals.append(f"tables 1.61.0 does not have are present: {', '.join(extra)}")
    if plan.refusals:
        return plan
    # DuckDB
    sources = cc_sources(cell)
    if "events" not in cell.duck_tables:
        ck((False, "DuckDB has no events table"))
        plan.refusals.append("tares.duckdb has no events table")
        return plan
    by_source = dict(cell.duck_rows("SELECT source, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC"))
    total = sum(by_source.values())
    ck((True, f"tares.duckdb: {len(cell.duck_tables)} tables, {n(total)} events ("
              + ", ".join(f"{k} {n(v)}" for k, v in list(by_source.items())[:6])
              + (", ..." if len(by_source) > 6 else "") + ")"))
    if cell.dialect == "postgresql":
        moved = None
        if "settings" in cell.duck_tables:
            r = cell.duck_rows("SELECT value FROM settings WHERE key = 'state_moved_to_postgres'")
            moved = r[0][0] if r else None
        ck((True, f"state moved to Postgres on {moved}" if moved else
            "state not marked as moved to Postgres: the platform tables may be in DuckDB (they are dropped)"))
    # the move ledger
    moves = move_states(cell)
    states = Counter(moves.values())
    mk = marks(cell)
    cc_lines = sum(v for k, v in by_source.items() if k in sources)
    desc = f"session move: {'planned' if 'session_move_planned' in mk else 'not planned'}, " + \
        ", ".join(f"{states.get(s, 0)} {s}" for s in ("verified", "pending", "failed"))
    ok = not states.get("pending") and not (states.get("failed") and not args.rebuild_failed_moves)
    if cc_lines and "session_move_planned" not in mk:
        ok = False
        plan.refusals.append("DuckDB holds claude_code lines and the session move was never planned: "
                             "run Tares 1.61.0 until its session move is done, then stop it")
    ck((ok, desc))
    if states.get("pending"):
        plan.refusals.append(f"{states['pending']} session move(s) pending: run Tares 1.61.0 until "
                             "the move is done (its log says so), then stop it")
    if states.get("failed") and not args.rebuild_failed_moves:
        plan.refusals.append(f"{states['failed']} session move(s) failed: pass --rebuild-failed-moves "
                             "to rebuild those sessions from DuckDB's lines here")
    # the copies ledger
    copied = set()
    if "session_store_copies" in cell.tables:
        copied = {r[0] for r in cell.rows(select(cell.t("session_store_copies").c.table_name))}
    if cell.dialect == "sqlite":
        lack = [t for t in MOVED_1 if t not in copied]
        ck((not lack, f"session_store_copies: {len(MOVED_1) - len(lack)} of {len(MOVED_1)} tables copied"))
        if lack:
            plan.refusals.append(f"1.61.0 has not copied these tables from DuckDB: {', '.join(lack)}. "
                                 "Start Tares 1.61.0 once, stop it, then run this again")
    else:
        ck((True, f"session_store_copies: {len(copied)} rows (on Postgres the tables are the live "
                  "ones, taken over in place; the ledger is not needed)"))
    # what the plan copies
    plan_tables(cell, plan, sources, moves, mk, args)
    return plan


def schema_2() -> MetaData:
    """The 2.0 tables as this checkout's revision makes them, in an in-memory SQLite."""
    from alembic import command
    from alembic.config import Config
    engine = sa.create_engine("sqlite://")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    md = MetaData()
    md.reflect(engine)
    engine.dispose()
    return md


def column_notes(src_table, dst_table, rename: dict | None = None) -> tuple[str, list[str]]:
    """What changes column by column, and the 2.0 NOT NULL columns with no source and no default
    (a copy cannot fill them)."""
    rename = rename or {}
    have = {c.name for c in src_table.columns}
    used = {rename.get(c.name, c.name) for c in dst_table.columns}
    dropped = [c.name for c in src_table.columns if c.name not in used]
    new, blocked = [], []
    for c in dst_table.columns:
        if rename.get(c.name, c.name) in have:
            continue
        if not c.nullable and c.server_default is None:
            blocked.append(c.name)
        else:
            new.append(f"{c.name} ({'its default' if c.server_default is not None else 'NULL'})")
    parts = []
    if rename:
        parts.append(", ".join(f"{v} becomes {k}" for k, v in rename.items()))
    if dropped:
        parts.append("columns dropped: " + ", ".join(dropped))
    if new:
        parts.append("new columns: " + ", ".join(new))
    return "; ".join(parts), blocked


def plan_tables(cell: Cell, plan: Plan, sources: dict, moves: dict, mk: dict, args) -> None:
    out_tables = TABLES_2
    md2 = schema_2()
    drop_ids = {r[0] for r in non_factory_projects(cell)} if args.drop_non_factory else set()
    for name in out_tables:
        if name in RENAMED:
            src = RENAMED[name]
            note, blocked = column_notes(cell.t(src), md2.tables[name],
                                         {"project": "usecase_id"} if name == "project_log" else None)
            if blocked:
                plan.refusals.append(f"{name}: the 2.0 columns {', '.join(blocked)} are NOT NULL with "
                                     f"no source in {src} and no default")
            k = cell.count(src)
            if name == "projects" and drop_ids:
                k -= len(drop_ids)
                note += f"; {len(drop_ids)} non-factory project(s) dropped"
            plan.rows.append((name, "rename", src, k, note))
        elif name in FROM_OBJECTS:
            kind = FROM_OBJECTS[name]
            o = cell.t("usecase_objects")
            k = len(cell.rows(select(o.c.usecase_id, o.c.name).where(o.c.kind == kind).distinct()))
            plan.rows.append((name, "rename", "usecase_objects", k, f"kind {kind}"))
        elif name == "integration_keys":
            apps = github_sources(cell)
            keyed = [a for a in apps if a["ingest_key"]]
            plan.rows.append((name, "build", "catalog_sources", len(keyed),
                              "github_app sources: " + (", ".join(a["name"] for a in keyed) or "none")))
        elif name == "pr_events":
            k = len(pr_event_rows(cell, args.pr_events_since))
            plan.rows.append((name, "build", "DuckDB events", k,
                              f"pull_request events since {args.pr_events_since.date().isoformat()}"))
        elif name in REBUILT:
            plan.rows.append((name, "rebuild", "session_usage", cell.count(name),
                              "built again from session_usage (rows not copied)"))
        elif name in cell.tables:
            cols, blocked = column_notes(cell.t(name), md2.tables[name])
            if blocked:
                plan.refusals.append(f"{name}: the 2.0 columns {', '.join(blocked)} are NOT NULL with "
                                     f"no source in 1.61.0's {name} and no default")
            note = ""
            if name == "sessions":
                srcs = Counter(r[1] for r in session_rows(cell))
                others = {s: v for s, v in srcs.items() if s != SESSION_SOURCE}
                note = ("sources moved to claude_code: " + ", ".join(f"{s} ({v})" for s, v in others.items())
                        if others else "all under claude_code")
            elif name == "session_lines":
                note = "render flags set to the defaults (redact on, thinking off)"
            elif name == "settings" and cell.setting("skills_shared") is None:
                note = "skills_shared is not set: the old skills are merged here"
            elif name in ("challenges", "session_usage"):
                note = "plus what DuckDB holds and the table does not"
            note = "; ".join(x for x in (note, cols) if x)
            plan.rows.append((name, "copy", name, cell.count(name), note))
        else:
            plan.rows.append((name, "new", "-", 0, "empty"))
    for name in sorted(cell.tables - set(out_tables) - set(RENAMED.values()) - {"usecase_objects"}):
        k = cell.count(name)
        why = {"catalog_sources": "the github_app rows become integration_keys; no catalog in 2.0",
               "skills": "2.0 keeps skills in shared_skills" + (
                   "" if cell.setting("skills_shared") is None else " (1.61.0 merged them already)"),
               VERSION_TABLE: f"replaced by {BASELINE}"}.get(name) or \
            ("a ledger of 1.x moves" if name in LEDGERS else "a platform table")
        plan.dropped.append((name, k, why))
    o = cell.t("usecase_objects")
    other = cell.rows(select(o.c.kind, func.count()).where(o.c.kind.not_in(("doc", "skill"))).group_by(o.c.kind))
    for kind, k in other:
        plan.dropped.append((f"usecase_objects kind {kind}", int(k), "platform objects"))
    total = cell.duck_rows("SELECT count(*) FROM events")[0][0]
    plan.dropped.append(("DuckDB events", int(total), "read for lines, challenger lines, replies and "
                         "pull requests; tares.duckdb is not opened by 2.0"))
    rest = sorted(cell.duck_tables - {"events"})
    if rest:
        plan.dropped.append((f"DuckDB's other {len(rest)} tables", 0, "not read: platform tables, "
                             "and the copies 1.61.0 left there when it moved a table"))
    # notes
    for pid, pname, recipe, kind in non_factory_projects(cell):
        plan.notes.append(f"not a factory project: {pid} {pname!r} (recipe {recipe}, kind {kind}): "
                          + ("dropped (--drop-non-factory)" if args.drop_non_factory else
                             "kept as a plain project (--drop-non-factory drops it)"))
    defaults = default_project_ids(cell)
    if defaults:
        have = cell.setting("default_project")
        plan.notes.append(f"default_project: {defaults[0]}" + (
            "" if have == defaults[0] else f" (was {have!r})"))
    for name, cfg in sources.items():
        if name != SESSION_SOURCE or cfg:
            flags = {"include_thinking": bool(cfg.get("include_thinking", False)),
                     "redact": bool(cfg.get("redact", True))}
            plan.notes.append(f"claude_code source {name!r}: render {json.dumps(flags, sort_keys=True)}"
                              + ("" if json.dumps(flags, sort_keys=True) == DEFAULT_RENDER else
                                 "; its table lines get the default flags (archived parts keep theirs)"))
    tokens = [s for s in github_sources(cell, ("github",))]
    if tokens:
        plan.notes.append("github token sources (polling) are not carried over: "
                          + ", ".join(s["name"] for s in tokens))
    for a in github_sources(cell):
        if not a["ingest_key"]:
            plan.notes.append(f"github_app source {a['name']!r} has no ingest key: no webhook key is made")
    held = held_counts(cell)
    duck = duck_counts(cell, sources)
    repair = sessions_to_repair(cell, held, moves, duck)
    if repair:
        why = Counter(repair.values())
        plan.notes.append(f"sessions whose lines are checked against DuckDB: {len(repair)} ("
                          + "; ".join(f"{v} {k}" for k, v in why.items()) + ")")
    if "sessions_caught_up" not in mk:
        plan.notes.append("the sessions table never caught up: coordination_calls is counted again "
                          "from DuckDB")


def default_project_ids(cell: Cell) -> list[str]:
    u = cell.t("usecases")
    return [r[0] for r in cell.rows(select(u.c.id).where(u.c.recipe == "default")
                                    .order_by(u.c.created_at, u.c.id))]


def pr_event_rows(cell: Cell, since: datetime) -> list[dict]:
    """The `pr_events` rows of DuckDB's pull_request events since `since` (the rule of 2.0's
    github_webhook.pr_rows), one per delivery."""
    names = [s["name"] for s in github_sources(cell, ("github_app", "github"))]
    if not names or "events" not in cell.duck_tables:
        return []
    marks = ", ".join("?" for _ in names)
    out = {}
    for payload, labels, at in cell.duck_rows(
            f"SELECT payload, labels, event_time FROM events WHERE event_type = 'pull_request' "
            f"AND source IN ({marks}) AND event_time >= ? ORDER BY event_time", [*names, since]):
        lab, p = _obj(labels), _obj(payload)
        try:
            number = int(lab.get("number"))
        except (TypeError, ValueError):
            continue
        repo, branch = lab.get("repo"), lab.get("branch")
        if not repo or not branch:
            continue
        at = utc(at)
        did = p.get("_github_delivery") or f"{repo}#{number}@{at.isoformat()}"
        out.setdefault(str(did), {"delivery_id": str(did), "repo": str(repo), "number": number,
                                  "branch": str(branch),
                                  "action": str(lab.get("action") or p.get("action") or ""), "at": at})
    return list(out.values())


def print_plan(cell: Cell, plan: Plan) -> None:
    say(f"Tares 1.61.0 cell: {cell.dir} (session store: {cell.where}"
        + (f", {cell.engine.url.render_as_string(hide_password=True)}" if cell.state_dsn else "") + ")")
    say("Checks")
    for ok, text in plan.checks:
        say(f"  {'ok  ' if ok else 'FAIL'}  {text}")
    if plan.rows:
        say("Plan: 2.0 table, what happens, from, rows")
        for name, action, src, k, note in plan.rows:
            say(f"  {name:<25} {action:<8} {src:<24} {n(k):>9}  {note}")
    if plan.dropped:
        say("Left behind")
        for name, k, why in plan.dropped:
            say(f"  {name:<34} {n(k):>10}  {why}")
    if plan.notes:
        say("Notes")
        for note in plan.notes:
            say(f"  {note}")
    if plan.refusals:
        say("Refused")
        for r in plan.refusals:
            say(f"  {r}")


# ── the output ───────────────────────────────────────────────────────────────────────────────
class Out:
    def __init__(self, target: str):
        self.target = target
        self.final = None
        if is_dsn(target):
            self.engine = sa.create_engine(pg_url(target), connect_args={"prepare_threshold": None},
                                           pool_size=2, max_overflow=2)
            self.dir = None
        else:
            self.final = os.path.abspath(target)
            self.dir = self.final
            self.engine = None
        self.dialect = "postgresql" if is_dsn(target) else "sqlite"

    # SQLite: OUT.migrating until the store is stamped
    def prepare(self) -> None:
        if self.dialect == "sqlite":
            if os.path.exists(self.final) and os.listdir(self.final):
                raise Refused(f"{self.final} exists and is not empty: the output must be a new folder")
            self.dir = self.final + ".migrating"
            if os.path.exists(self.dir):
                shutil.rmtree(self.dir)
            os.makedirs(self.dir)
            self.engine = sa.create_engine("sqlite:///" + os.path.join(self.dir, "tares.sqlite"))

            @sa.event.listens_for(self.engine, "connect")
            def _wal(dbapi_conn, _rec):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        else:
            with self.engine.begin() as c:
                # the schema the DSN's connections write to: the first of the search_path that
                # exists, else the first one it names (made here)
                first = c.exec_driver_sql("SELECT current_schema()").scalar_one()
                if first is None:
                    path = c.exec_driver_sql("SHOW search_path").scalar_one()
                    named = [p.strip().strip('"') for p in path.split(",") if p.strip()]
                    first = next((p for p in named if p != "$user"), None)
                    if first is None:
                        raise Refused("the output DSN's search_path names no schema")
                    c.exec_driver_sql(f'CREATE SCHEMA "{first}"')
                self.schema = first
            with self.engine.connect() as c:
                have = sa.inspect(c).get_table_names(schema=self.schema)
            if have:
                raise Refused(f"schema {self.schema} already holds {len(have)} tables: the output "
                              "must be a new schema")

    def open_existing(self) -> None:
        if self.dialect == "sqlite":
            for d in (self.final, self.final + ".migrating"):
                if os.path.exists(os.path.join(d, "tares.sqlite")):
                    self.dir = d
                    break
            else:
                raise Refused(f"{self.final} holds no tares.sqlite")
            self.engine = sa.create_engine("sqlite:///" + os.path.join(self.dir, "tares.sqlite"))
        else:
            with self.engine.connect() as c:
                self.schema = c.exec_driver_sql("SELECT current_schema()").scalar_one()
        self.reflect()

    def reflect(self) -> None:
        self.md = MetaData()
        self.md.reflect(self.engine)

    def t(self, name):
        return self.md.tables[name]

    def dsn(self) -> str | None:
        return self.target if self.dialect == "postgresql" else None

    def build(self) -> None:
        """Run this repo's 2.0 revisions on the empty output, check the tables are the baseline's,
        and take the stamp off until the copy is verified."""
        from alembic import command
        from alembic.config import Config
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        self.reflect()
        names = sorted(t for t in self.md.tables if t != VERSION_TABLE)
        if names != sorted(TABLES_2):
            raise Mismatch(f"the 2.0 revision made other tables than the baseline: "
                           f"extra {sorted(set(names) - set(TABLES_2))}, missing {sorted(set(TABLES_2) - set(names))}")
        with self.engine.begin() as conn:
            got = [r[0] for r in conn.execute(select(self.t(VERSION_TABLE).c.version_num))]
            if got != [BASELINE]:
                raise Mismatch(f"the 2.0 revision stamped {got}, not {BASELINE}")
            conn.execute(self.t(VERSION_TABLE).delete())

    def stamp(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(self.t(VERSION_TABLE).delete())
            conn.execute(insert(self.t(VERSION_TABLE)).values(version_num=BASELINE))
        if self.dialect == "sqlite":
            self.engine.dispose()
            if os.path.exists(self.final):
                os.rmdir(self.final)        # an empty folder was allowed
            os.replace(self.dir, self.final)
            self.dir = self.final
            self.engine = sa.create_engine("sqlite:///" + os.path.join(self.dir, "tares.sqlite"))

    def count(self, name) -> int:
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).select_from(self.t(name))).scalar_one())


# ── the copy ─────────────────────────────────────────────────────────────────────────────────
class Copier:
    def __init__(self, cell: Cell, out: Out, args):
        self.cell, self.out, self.args = cell, out, args
        self.pg_out = out.dialect == "postgresql"
        self.sources = cc_sources(cell)
        self.expected: dict[str, int] = {}
        self.nul_rows: Counter = Counter()
        self.drop_ids = {r[0] for r in non_factory_projects(cell)} if args.drop_non_factory else set()
        self.rebuilt: dict[str, dict] = {}       # session -> how its lines were rebuilt
        self.facts_from_duck: set[str] = set()
        self.report: list[str] = []

    def clean(self, table: str, row: dict) -> dict:
        out = {}
        for k, v in row.items():
            v = utc(v)
            if self.pg_out and isinstance(v, str) and "\x00" in v:
                v = v.replace("\x00", "")
                self.nul_rows[table] += 1
            out[k] = v
        return out

    def insert(self, conn, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        t = self.out.t(table)
        rows = [self.clean(table, r) for r in rows]
        for i in range(0, len(rows), CHUNK):
            conn.execute(insert(t), rows[i:i + CHUNK])
        return len(rows)

    def copy(self, name: str, src: str | None = None, where=None, fix=None, rename=None) -> int:
        """Copy a table column by column, in chunks, in one output transaction."""
        src = src or name
        st, dt = self.cell.t(src), self.out.t(name)
        rename = rename or {}
        names = {c.name for c in st.columns}
        cols = []
        for c in dt.columns:
            if c.name in names or c.name in rename:
                cols.append(c.name)
            elif not c.nullable and c.server_default is None:
                raise Mismatch(f"{name}: the 2.0 column {c.name} is NOT NULL and has no source in "
                               f"{src} and no default")
        q = select(*[st.c[rename.get(c, c)].label(c) for c in cols])
        if where is not None:
            q = q.where(where(st))
        k = 0
        with self.out.engine.begin() as conn:
            for part in self.cell.stream(q):
                rows = [dict(r._mapping) for r in part]
                if fix:
                    rows = [x for x in (fix(r) for r in rows) if x is not None]
                k += self.insert(conn, name, rows)
        return k

    # each step returns nothing; the counts go in self.expected
    def run(self) -> None:
        cell = self.cell
        plain = [t for t in TABLES_2 if t in cell.tables and t not in RENAMED and t not in REBUILT
                 and t not in ("sessions", "session_lines", "settings", "shared_skills") + FACT_TABLES]
        for name in plain:
            where = None
            col = dict(PROJECT_OWNED).get(name)
            if col and self.drop_ids:
                where = (lambda c: lambda t: t.c[c].not_in(sorted(self.drop_ids)))(col)
            self.expected[name] = self.copy(name, where=where)
        full = [f"{t} {n(self.expected[t])}" for t in plain if self.expected[t]]
        empty = [t for t in plain if not self.expected[t]]
        say(f"  copied {len(plain)} tables: " + ", ".join(full))
        if empty:
            say(f"  ({len(empty)} of them empty: {', '.join(empty)})")
        self.projects()
        self.settings_and_skills()
        self.keys_and_pr_events()
        self.sessions()
        self.fills()
        self.rebuild()
        self.archive_files()

    def projects(self) -> None:
        cell, drop = self.cell, sorted(self.drop_ids)
        keep = (lambda t: t.c.id.not_in(drop)) if drop else None
        self.expected["projects"] = self.copy("projects", "usecases", where=keep)
        keep = (lambda t: t.c.usecase_id.not_in(drop)) if drop else None
        self.expected["project_log"] = self.copy("project_log", "usecase_log", where=keep,
                                                 rename={"project": "usecase_id"})
        o = cell.t("usecase_objects")
        for name, kind in FROM_OBJECTS.items():
            col = "doc" if kind == "doc" else "skill"
            q = select(o.c.usecase_id, o.c.name, func.min(o.c.created_at)).where(
                o.c.kind == kind).group_by(o.c.usecase_id, o.c.name)
            rows = [{"project": p, col: nm, "created_at": c} for p, nm, c in cell.rows(q)
                    if p not in self.drop_ids and nm is not None]
            with self.out.engine.begin() as conn:
                self.expected[name] = self.insert(conn, name, rows)
        say(f"  renamed usecases, usecase_log, usecase_objects ({n(self.expected['projects'])} projects)")

    def settings_and_skills(self) -> None:
        cell = self.cell
        self.expected["settings"] = self.copy("settings")
        defaults = [d for d in default_project_ids(cell) if d not in self.drop_ids]
        st = self.out.t("settings")
        now = datetime.now(timezone.utc)
        with self.out.engine.begin() as conn:
            if defaults:
                had = conn.execute(select(st.c.value).where(st.c.key == "default_project")).first()
                if had is None:
                    conn.execute(insert(st).values(key="default_project", value=defaults[0], updated_at=now))
                    self.expected["settings"] += 1
                elif had[0] != defaults[0]:
                    conn.execute(st.update().where(st.c.key == "default_project").values(
                        value=defaults[0], updated_at=now))
                self.report.append(f"default_project is {defaults[0]}")
        self.expected["shared_skills"] = self.copy("shared_skills")
        if cell.setting("skills_shared") is None and "skills" in cell.tables:
            self.merge_skills()

    def merge_skills(self) -> None:
        """1.61.0's `_skills_upgrade`, for a cell where it never ran: each project's skills become
        skills of the cell its projects use; the same name with other text is kept as name-2."""
        k = self.cell.t("skills")
        s, ps, st = self.out.t("shared_skills"), self.out.t("project_skills"), self.out.t("settings")
        rows = self.cell.rows(select(k.c.project, k.c.name, k.c.description, k.c.body, k.c.created_at,
                                     k.c.updated_at).order_by(k.c.created_at, k.c.project))
        added = used = 0
        with self.out.engine.begin() as conn:
            for project, name, desc, body, c, u in rows:
                if project in self.drop_ids:
                    continue
                use, i = name, 2
                while True:
                    have = conn.execute(select(s.c.body).where(s.c.name == use)).first()
                    if have is None or have[0] == body:
                        break
                    use, i = f"{name}-{i}", i + 1
                if have is None:
                    conn.execute(insert(s).values(**self.clean("shared_skills", dict(
                        name=use, description=desc, body=body, made_by=project, created_at=c, updated_at=u))))
                    added += 1
                if use != name:
                    conn.execute(ps.delete().where(ps.c.project == project, ps.c.skill == name))
                if conn.execute(select(ps.c.skill).where(ps.c.project == project, ps.c.skill == use)).first() is None:
                    conn.execute(insert(ps).values(project=project, skill=use,
                                                   created_at=utc(c) or datetime.now(timezone.utc)))
                    used += 1
            conn.execute(insert(st).values(key="skills_shared", value="1",
                                           updated_at=datetime.now(timezone.utc)))
        self.expected["shared_skills"] += added
        self.expected["settings"] += 1
        with self.out.engine.connect() as conn:
            self.expected["project_skills"] = conn.execute(select(func.count()).select_from(ps)).scalar_one()
        self.report.append(f"skills merged into shared_skills: {added} added, {used} project uses")

    def keys_and_pr_events(self) -> None:
        rows = [{"key": a["ingest_key"], "integration": "ci_github", "credential": a["config"].get("credential"),
                 "source": a["name"], "created_at": a["created_at"]}
                for a in github_sources(self.cell) if a["ingest_key"]]
        with self.out.engine.begin() as conn:
            self.expected["integration_keys"] = self.insert(conn, "integration_keys", rows)
            self.expected["pr_events"] = self.insert(conn, "pr_events",
                                                     pr_event_rows(self.cell, self.args.pr_events_since))
        say(f"  built integration_keys {n(self.expected['integration_keys'])}, "
            f"pr_events {n(self.expected['pr_events'])}")

    # ── sessions ──
    def sessions(self) -> None:
        cell = self.cell
        moved = Counter()

        def fix_session(r):
            if r.get("source") != SESSION_SOURCE:
                moved[r.get("source")] += 1
            r["source"] = SESSION_SOURCE
            return r
        self.expected["sessions"] = self.copy("sessions", fix=fix_session)
        for s, k in moved.items():
            self.report.append(f"sessions moved from source {s!r} to claude_code: {k}")
        rewritten = Counter()

        def fix_line(r):
            if r.get("render") != DEFAULT_RENDER:
                rewritten[r.get("render")] += 1
                r["render"] = DEFAULT_RENDER
            return r
        self.expected["session_lines"] = self.copy("session_lines", fix=fix_line)
        for flags, k in rewritten.items():
            self.report.append(f"render flags {flags} set to the defaults on {k} table line(s)")
        for name in FACT_TABLES:
            def fix_fact(r):
                if "source" in r and r["source"] in self.sources:
                    r["source"] = SESSION_SOURCE
                return r
            self.expected[name] = self.copy(name, fix=fix_fact)
        say(f"  copied sessions {n(self.expected['sessions'])}, session_lines "
            f"{n(self.expected['session_lines'])}, and their facts")
        self.repair_sessions()

    def repair_sessions(self) -> None:
        """Sessions whose lines may not all be held: check them against DuckDB and complete them
        (the rule of 1.61.0's session move: DuckDB's lines in time order, then the lines only the
        table or the archive holds), or put their flag right."""
        cell = self.cell
        held = held_counts(cell)
        moves = move_states(cell)
        duck = duck_counts(cell, self.sources)
        todo = sessions_to_repair(cell, held, moves, duck)
        if not todo:
            return
        archive = archive_of(cell)
        s = self.out.t("sessions")
        lines_t, parts_t = self.out.t("session_lines"), self.out.t("session_archive_parts")
        info = {r[0]: r for r in session_rows(cell)}
        fixed = Counter()
        for sid in sorted(todo):
            count = info[sid][2] or 0
            h = held.get(sid, 0)
            dl = self.duck_lines(sid) if duck.get(sid) else []
            have = self.held_lines(sid, archive)
            seen = Counter((r["line_key"], stamp(r["at"])) for r in have)
            used: Counter = Counter()
            missing = []
            for r in dl:
                k = (r["line_key"], stamp(r["at"]))
                if used[k] < seen[k]:
                    used[k] += 1
                else:
                    missing.append(r)
            with self.out.engine.begin() as conn:
                if not missing:
                    if h >= count:
                        conn.execute(s.update().where(s.c.session == sid).values(
                            line_count=h, lines_complete=True, lines_origin="table"))
                        fixed["complete, flag put right"] += 1
                    else:
                        conn.execute(s.update().where(s.c.session == sid).values(lines_complete=False))
                        fixed["lines missing everywhere, left incomplete"] += 1
                        self.report.append(f"session {sid}: {h} of {count} lines are stored anywhere; "
                                           "the rest cannot be migrated (lines_complete false)")
                    continue
                has_parts = conn.execute(select(func.count()).select_from(parts_t).where(
                    parts_t.c.session == sid)).scalar_one()
                if has_parts:
                    # the archive does not change: the missing lines follow the last stored one
                    top = max([r["seq"] for r in have] or [0])
                    new = [{**r, "session": sid, "seq": top + i + 1} for i, r in enumerate(missing)]
                    self.insert(conn, "session_lines", new)
                    written = len(have) + len(new)
                    self.expected["session_lines"] += len(new)
                    how = f"{len(new)} DuckDB lines added after seq {top} (archived parts kept)"
                else:
                    # every line again, numbered from 1: DuckDB's first, then the ones only the
                    # table held
                    seen_d = Counter((r["line_key"], stamp(r["at"])) for r in dl)
                    used_d: Counter = Counter()
                    extra = []
                    for r in have:
                        k = (r["line_key"], stamp(r["at"]))
                        if used_d[k] < seen_d[k]:
                            used_d[k] += 1
                        else:
                            extra.append(r)
                    conn.execute(lines_t.delete().where(lines_t.c.session == sid))
                    full = dl + [{k: v for k, v in r.items() if k not in ("session", "seq")} for r in extra]
                    full = [{**r, "session": sid, "seq": i + 1, "render": DEFAULT_RENDER}
                            for i, r in enumerate(full)]
                    self.insert(conn, "session_lines", full)
                    written = len(full)
                    self.expected["session_lines"] += written - len(have)
                    how = f"rebuilt: {len(dl)} DuckDB lines, then {len(extra)} only the table held"
                last = max((utc(r["at"]) for r in dl + have if r["at"] is not None), default=None)
                conn.execute(s.update().where(s.c.session == sid).values(
                    line_count=written, last_line_at=last, lines_complete=True, lines_origin="table"))
            self.rebuilt[sid] = {"why": todo[sid], "how": how, "lines": written}
            self.facts_from_duck.add(sid)
            fixed["completed from DuckDB"] += 1
        self.report.append("sessions checked against DuckDB: " + ", ".join(f"{v} {k}" for k, v in fixed.items()))

    def duck_lines(self, sid: str) -> list[dict]:
        """The session's DuckDB lines as rows of session_lines (no session, no seq), in time order,
        the way 1.61.0's session move built them."""
        marks_ = ", ".join("?" for _ in self.sources)
        out = []
        for etype, payload, labels, at in self.cell.duck_rows(
                f"SELECT event_type, payload, labels, event_time FROM events WHERE source IN ({marks_}) "
                "AND key_value = ? ORDER BY event_time, rowid", [*sorted(self.sources), sid]):
            p, lab = _json(payload), _json(labels)
            out.append({"line_key": line_key(p), "at": utc(at), "type": _text(etype),
                        "labels": json.dumps(lab), "render": DEFAULT_RENDER, "payload": json.dumps(p)})
        return out

    def held_lines(self, sid: str, archive: Archive | None) -> list[dict]:
        t = self.cell.t("session_lines")
        out = [dict(r._mapping) for r in self.cell.rows(select(t).where(t.c.session == sid).order_by(t.c.seq))]
        p = self.cell.t("session_archive_parts")
        parts = [dict(r._mapping) for r in self.cell.rows(select(p).where(p.c.session == sid).order_by(p.c.part))]
        if parts:
            if archive is None:
                raise Refused(f"session {sid} has archived parts and no archive is given (--archive-url)")
            for part in parts:
                for r in archive.read_part(part):
                    out.append({"session": sid, "seq": r["seq"], "line_key": r.get("line_key"),
                                "at": utc(datetime.fromisoformat(r["at"])) if r.get("at") else None,
                                "type": r.get("type"), "labels": r.get("labels"),
                                "render": r.get("render"), "payload": r.get("payload")})
            out.sort(key=lambda r: r["seq"])
        return [{**r, "render": DEFAULT_RENDER} if "render" in r else r for r in out]

    # ── what DuckDB holds and the store does not ──
    def fills(self) -> None:
        cell = self.cell
        # challenger lines (1.61.0's _challenges_fill)
        rows = cell.duck_rows(
            "SELECT source, coalesce(key_value, ''), event_time, event_type, "
            "coalesce(json_extract_string(labels, '$.branch'), ''), "
            "json_extract_string(labels, '$.tares_project'), CAST(payload AS VARCHAR) FROM events "
            f"WHERE event_type IN ({', '.join(repr(t) for t in CHALLENGE_TYPES)})")
        ch = self.out.t("challenges")
        with self.out.engine.connect() as conn:
            have = {(a, b, stamp(c), d, e) for a, b, c, d, e in conn.execute(
                select(ch.c.source, ch.c.key_value, ch.c.event_time, ch.c.event_type, ch.c.branch))}
        add = {}
        for source, key, at, typ, branch, project, payload in rows:
            if self.pg_out:
                key, branch, project = no_nul(key), no_nul(branch), no_nul(project) or None
            k = (source, key, stamp(at), typ, branch)
            if k in have or k in add:
                continue
            add[k] = {"source": source, "key_value": key, "event_time": utc(at), "event_type": typ,
                      "branch": branch, "tares_project": project, "payload": payload}
        with self.out.engine.begin() as conn:
            self.expected["challenges"] += self.insert(conn, "challenges", list(add.values()))
        if add:
            self.report.append(f"challenger lines added from DuckDB: {len(add)}")
        # model replies (1.61.0's _claude_usage_fill), priced by the rebuild step
        u = self.out.t("session_usage")
        with self.out.engine.connect() as conn:
            have_u = set(conn.execute(select(u.c.session, u.c.message)).all())
            projects = {}
            p = self.out.t("projects")
            for pid, pname in conn.execute(select(p.c.id, p.c.name).order_by(p.c.created_at)):
                projects.setdefault(pname, pid)
            for pid, _ in conn.execute(select(p.c.id, p.c.name)):
                projects[pid] = pid
        srcs = sorted(self.sources)
        j = "json_extract(e.payload, '$.message.usage')"
        found = cell.duck_rows(
            "SELECT e.key_value, json_extract_string(e.payload, '$.message.id'), min(e.event_time), "
            "any_value(json_extract_string(e.payload, '$.message.model')), "
            "any_value(json_extract_string(e.payload, '$.tares_project')), "
            "any_value(json_extract_string(e.payload, '$.subagent')), "
            f"max(CAST(json_extract({j}, '$.input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.output_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.cache_creation_input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.cache_read_input_tokens') AS BIGINT)), "
            f"max(CAST(json_extract({j}, '$.cache_creation.ephemeral_1h_input_tokens') AS BIGINT)) "
            f"FROM events e WHERE e.source IN ({', '.join('?' * len(srcs))}) "
            f"AND {j} IS NOT NULL AND json_extract_string(e.payload, '$.message.id') IS NOT NULL "
            "AND coalesce(json_extract_string(e.payload, '$.message.model'), '') <> ? "
            "GROUP BY 1, 2", [*srcs, SYNTHETIC])
        cols = ("session", "message", "replied_at", "model", "project", "subagent", "input_tokens",
                "output_tokens", "cache_write_tokens", "cache_read_tokens", "cache_write_1h_tokens")
        new = []
        for r in found:
            row = dict(zip(cols, r))
            if self.pg_out:
                for c in ("session", "message", "model", "project", "subagent"):
                    row[c] = no_nul(row[c]) or (None if c != "session" else row[c])
            if not row["message"] or not row["session"] or (row["session"], row["message"]) in have_u:
                continue
            have_u.add((row["session"], row["message"]))
            row["project"] = projects.get(row["project"]) if row["project"] else None
            row["cost_usd"] = None
            new.append(row)
        with self.out.engine.begin() as conn:
            self.expected["session_usage"] += self.insert(conn, "session_usage", new)
        self.usage_added = [(r["session"], r["message"]) for r in new]
        if new:
            self.report.append(f"model replies added from DuckDB: {len(new)} (priced by the rebuild step)")
        # coordination_calls, when 1.61.0's sessions table never caught up (its readers scanned)
        if "sessions_caught_up" not in marks(cell):
            self.coordination_counts = self.coordination_scan()
        else:
            self.coordination_counts = None

    def coordination_scan(self) -> dict[str, list]:
        """{session: [(text, payload)]} of the tool_use lines that may coordinate, for the rebuild
        step to count (1.61.0's _coordination_calls_scan)."""
        srcs = sorted(self.sources)
        out = defaultdict(list)
        for sid, text, payload in self.cell.duck_rows(
                f"SELECT key_value, text, CAST(payload AS VARCHAR) FROM events WHERE source IN "
                f"({', '.join('?' * len(srcs))}) AND event_type = 'tool_use' AND (text LIKE '%→ mcp__%' "
                "OR text LIKE '%→ SendMessage(%' OR text LIKE '%→ ListAgents(%' OR text LIKE '%→ Skill(%' "
                "OR text LIKE '%→ Cron%')", srcs):
            out[sid].append((text, payload))
        return out

    # ── the rebuild step: 2.0's own code, on the output ──
    def rebuild(self) -> None:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from tares import claude_usage_hourly, pricing, session_ingest
        from tares.session_store import SessionStore
        sdb = SessionStore(self.out.dir if self.out.dialect == "sqlite" else "", self.out.dsn())
        try:
            # the facts and turns of the sessions whose lines came from DuckDB
            srcs = sorted(self.sources)
            marks_ = ", ".join("?" for _ in srcs)
            for sid in sorted(self.facts_from_duck):
                rows = self.cell.duck_rows(
                    f"SELECT event_type, text, payload, labels, event_time FROM events WHERE source IN "
                    f"({marks_}) AND key_value = ? ORDER BY event_time, rowid", [*srcs, sid])
                lines = [(SESSION_SOURCE, sid, utc(at), et, text, _json(p), _json(lab))
                         for et, text, p, lab, at in rows]
                if self.pg_out:
                    lines = [(a, b, c, d, no_nul(e), json.loads(no_nul(json.dumps(f))),
                              json.loads(no_nul(json.dumps(g)))) for a, b, c, d, e, f, g in lines]
                with sdb.begin() as conn:
                    session_ingest._write_facts(conn, sdb, session_ingest.facts_of([x[:6] for x in lines]))
                    session_ingest._write_turns(conn, sdb, session_ingest.turns_of(lines))
            if self.facts_from_duck:
                for name in FACT_TABLES:
                    self.expected[name] = self.out.count(name)
                say(f"  rebuilt the facts and turns of {len(self.facts_from_duck)} session(s) from DuckDB")
            # coordination calls, counted the way 2.0 counts them as a line is stored
            if self.coordination_counts is not None:
                s = self.out.t("sessions")
                counts = {sid: sum(session_ingest.coordination_in(text, payload) for text, payload in lst)
                          for sid, lst in self.coordination_counts.items()}
                with self.out.engine.begin() as conn:
                    for sid, k in counts.items():
                        conn.execute(s.update().where(s.c.session == sid).values(coordination_calls=k))
                say(f"  counted coordination_calls of {len(counts)} session(s) from DuckDB")
            # the price of the replies added from DuckDB
            u = self.out.t("session_usage")
            priced = 0
            with self.out.engine.begin() as conn:
                for sid, msg in self.usage_added:
                    r = conn.execute(select(u.c.model, u.c.input_tokens, u.c.output_tokens,
                                            u.c.cache_write_tokens, u.c.cache_read_tokens,
                                            u.c.cache_write_1h_tokens).where(
                        u.c.session == sid, u.c.message == msg)).first()
                    cost = pricing.cost_usd(r[0] or "", *(x or 0 for x in r[1:]))
                    if cost is not None:
                        conn.execute(u.update().where(u.c.session == sid, u.c.message == msg)
                                     .values(cost_usd=cost))
                        priced += 1
            if self.usage_added:
                say(f"  priced {priced} of {len(self.usage_added)} added replies")
            # the hourly totals, from every usage row
            from types import SimpleNamespace
            k = claude_usage_hourly.fill(SimpleNamespace(sessions_db=sdb))
            self.expected["session_usage_hourly"] = self.out.count("session_usage_hourly")
            self.expected["session_rollup_state"] = self.out.count("session_rollup_state")
            say(f"  built session_usage_hourly for {n(k)} session(s): "
                f"{n(self.expected['session_usage_hourly'])} rows")
        finally:
            sdb.close()

    def archive_files(self) -> None:
        """A SQLite cell on its default archive: the files go along into OUT/archive, linked where
        the file system allows, else copied. Elsewhere the archive stays where it is."""
        if self.cell.dialect != "sqlite" or self.cell.archive_url or self.out.dialect != "sqlite":
            return
        src = os.path.join(self.cell.dir, "archive")
        if not os.path.isdir(src):
            return
        dst = os.path.join(self.out.dir, "archive")
        k = 0
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            for f in files:
                a, b = os.path.join(root, f), os.path.join(dst, rel, f)
                try:
                    os.link(a, b)
                except OSError:
                    shutil.copy2(a, b)
                k += 1
        self.report.append(f"archive: {k} file(s) linked into {os.path.join(self.out.final, 'archive')}")


# ── verification ─────────────────────────────────────────────────────────────────────────────
def checksum(engine, dialect, t, keys, ts, where=None) -> tuple[int, str]:
    q = select(*[t.c[k] for k in keys], t.c[ts]).order_by(*[ordered(t.c[k], dialect) for k in keys])
    if where is not None:
        q = q.where(where(t))
    with engine.connect() as c:
        rows = c.execute(q).all()
    return len(rows), md5_rows(rows)


def lines_checksum(engine, dialect, t, skip: set, nul: bool) -> tuple[int, str]:
    """MD5 over (session, seq, line_key, at, md5 of the payload) in session and seq order, without
    the sessions in `skip`."""
    h = hashlib.md5()
    k = 0
    q = select(t.c.session, t.c.seq, t.c.line_key, t.c.at, t.c.payload).order_by(
        ordered(t.c.session, dialect), t.c.seq)
    with engine.connect() as c:
        res = c.execution_options(stream_results=True, yield_per=5000).execute(q)
        for sid, seq, key, at, payload in res:
            if sid in skip:
                continue
            if nul:
                sid, key, payload = no_nul(sid), no_nul(key), no_nul(payload)
            p = hashlib.md5((payload or "").encode("utf-8", "surrogatepass")).hexdigest()
            h.update(("\x1f".join((sid, str(seq), key or "", stamp(at), p)) + "\x1e")
                     .encode("utf-8", "surrogatepass"))
            k += 1
    return k, h.hexdigest()


def verify(cell: Cell, out: Out, args, expected: dict | None, rebuilt: set) -> list[str]:
    """Every difference, each naming its table ([] when the output is what the input gives)."""
    fails = []
    out.reflect()
    names = sorted(t for t in out.md.tables if t != VERSION_TABLE)
    if names != sorted(TABLES_2):
        fails.append(f"tables: the output's tables are not the baseline's "
                     f"(extra {sorted(set(names) - set(TABLES_2))}, missing {sorted(set(TABLES_2) - set(names))})")
        return fails
    drop = sorted(r[0] for r in non_factory_projects(cell)) if args.drop_non_factory else []
    # row counts
    if expected is None:          # --verify: what a plain copy gives, from the input alone
        expected = {}
        for name in TABLES_2:
            if name in REBUILT or name in MADE or name in FROM_OBJECTS:
                continue
            src = RENAMED.get(name, name)
            if src in cell.tables and name not in ("session_lines", "challenges", "session_usage",
                                                   "settings", "shared_skills") + FACT_TABLES:
                col = dict(PROJECT_OWNED).get(name)
                if name == "projects":
                    col = "id"
                elif name == "project_log":
                    col = "usecase_id"
                w = (lambda c: lambda t: t.c[c].not_in(drop))(col) if col and drop else None
                expected[name] = cell.count(src, w)
    for name in sorted(expected):
        got = out.count(name)
        if got != expected[name]:
            fails.append(f"{name}: {n(got)} rows, the input gives {n(expected[name])}")
    # checksums
    nul = out.dialect == "postgresql" and cell.dialect == "sqlite"
    for name, keys, ts in CHECKSUMS:
        src = RENAMED.get(name, name)
        w_in = (lambda t: t.c.id.not_in(drop)) if (name == "projects" and drop) else None
        a = checksum(cell.engine, cell.dialect, cell.t(src), keys, ts, w_in)
        b = checksum(out.engine, out.dialect, out.t(name), keys, ts)
        if a != b:
            fails.append(f"{name}: checksum of ids and {ts} differs ({a[0]} rows {a[1]}, output {b[0]} rows {b[1]})")
    a = lines_checksum(cell.engine, cell.dialect, cell.t("session_lines"), rebuilt, nul)
    b = lines_checksum(out.engine, out.dialect, out.t("session_lines"), rebuilt, False)
    if a != b:
        fails.append(f"session_lines: checksum of session, seq, key, time and payload differs "
                     f"({a[0]} rows {a[1]}, output {b[0]} rows {b[1]})")
    # every session line of a session is held: rows plus archived lines equal line_count
    s, lt, pt = out.t("sessions"), out.t("session_lines"), out.t("session_archive_parts")
    with out.engine.connect() as c:
        kept = dict(c.execute(select(lt.c.session, func.count()).group_by(lt.c.session)).all())
        arch = dict(c.execute(select(pt.c.session, func.sum(pt.c.lines)).group_by(pt.c.session)).all())
        bad = [sid for sid, count, complete in c.execute(select(s.c.session, s.c.line_count, s.c.lines_complete))
               if complete and (kept.get(sid, 0) + int(arch.get(sid) or 0)) != count]
        srcs = sorted({r[0] for r in c.execute(select(s.c.source).distinct())} - {SESSION_SOURCE})
        flags = c.execute(select(func.count()).select_from(lt).where(lt.c.render != DEFAULT_RENDER)).scalar_one()
    if bad:
        fails.append(f"sessions: {len(bad)} complete session(s) whose stored lines are not line_count "
                     f"(first {bad[0]})")
    if srcs:
        fails.append(f"sessions: sources other than claude_code are left: {srcs}")
    if flags:
        fails.append(f"session_lines: {flags} line(s) without the default render flags")
    # integration keys: byte-equal to the catalog's
    want = sorted((a["ingest_key"], a["config"].get("credential"), a["name"])
                  for a in github_sources(cell) if a["ingest_key"])
    ik = out.t("integration_keys")
    with out.engine.connect() as c:
        got = sorted((r[0], r[1], r[2]) for r in c.execute(
            select(ik.c.key, ik.c.credential, ik.c.source).where(ik.c.integration == "ci_github")))
    if [w[0].encode() for w in want] != [g[0].encode() for g in got] or want != got:
        fails.append(f"integration_keys: {len(got)} keys, not the {len(want)} github_app keys of the "
                     "catalog byte for byte")
    # archived sessions read
    archive = None
    try:
        archive = (Archive("file://" + urllib.parse.quote(os.path.join(out.dir, "archive")))
                   if out.dialect == "sqlite" and cell.dialect == "sqlite" and not cell.archive_url
                   else archive_of(cell))
    except Refused as e:
        fails.append(f"session_archive_parts: {e}")
    with out.engine.connect() as c:
        parts = [dict(r._mapping) for r in c.execute(select(pt))]
    if parts and archive is None:
        say(f"  note: {len(parts)} archived parts, and no archive to read them from (--archive-url)")
    elif parts:
        sample = random.Random(1821).sample(parts, min(ARCHIVE_SAMPLE, len(parts)))
        for part in sample:
            try:
                archive.read_part(part)
            except (Mismatch, OSError) as e:
                fails.append(f"session_archive_parts: {e}" if isinstance(e, Mismatch) else
                             f"session_archive_parts: {part['key']} does not read ({e})")
                break
        else:
            say(f"  archived parts: {len(sample)} of {len(parts)} read back and match their rows")
    return fails


# ── main ─────────────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move a Tares 1.61.0 cell's data to the 2.0 baseline.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", metavar="DATA_DIR", help="check the input and print the plan; writes nothing")
    mode.add_argument("--apply", metavar="DATA_DIR", help="write a new 2.0 store at --to")
    mode.add_argument("--verify", metavar="DATA_DIR", help="verify an applied output at --to again")
    ap.add_argument("--to", metavar="OUT", help="a new folder, or a Postgres DSN of a new schema")
    ap.add_argument("--state-dsn", help="the 1.61.0 cell's TARES_STATE_DSN, for a Postgres cell")
    ap.add_argument("--archive-url", help="the cell's TARES_ARCHIVE_URL, when it is not <data dir>/archive")
    ap.add_argument("--drop-non-factory", action="store_true",
                    help="drop the projects that are not factory projects instead of keeping them")
    ap.add_argument("--rebuild-failed-moves", action="store_true",
                    help="rebuild the sessions whose 1.61.0 move failed from DuckDB's lines")
    ap.add_argument("--pr-events-since", type=lambda s: utc(datetime.fromisoformat(s)),
                    help="keep pull request events from this time on (default: 30 days ago)")
    args = ap.parse_args(argv)
    if args.pr_events_since is None:
        args.pr_events_since = datetime.now(timezone.utc) - timedelta(days=PR_EVENTS_DAYS)
    data_dir = args.check or args.apply or args.verify
    if (args.apply or args.verify) and not args.to:
        ap.error("--apply and --verify need --to")
    # the work folder (the copy of tares.sqlite, DuckDB's spill) goes beside a folder output, whose
    # disk is the one the output needs anyway; else in the temp folder (TMPDIR)
    near = os.path.dirname(os.path.abspath(args.to)) if args.to and not is_dsn(args.to) else None
    work = tempfile.mkdtemp(prefix="migrate-to-2-", dir=near if near and os.path.isdir(near) else None)
    try:
        try:
            cell = Cell(data_dir, args.state_dsn, args.archive_url, work)
        except Refused as e:
            say(f"Refused: {e}")
            return 2
        try:
            plan = inspect_cell(cell, args)
            print_plan(cell, plan)
            if plan.refusals:
                say("Nothing was written." if args.check else "Refused: nothing was written.")
                return 2
            if args.check:
                say("The check passed. Nothing was written.")
                return 0
            out = Out(args.to)
            if args.verify:
                out.open_existing()
                say("Verify")
                repaired = sessions_to_repair(cell, held_counts(cell), move_states(cell),
                                              duck_counts(cell, cc_sources(cell)))
                fails = verify(cell, out, args, None, set(repaired))
                return report_fails(fails)
            say("Apply")
            try:
                out.prepare()
            except Refused as e:
                say(f"Refused: {e}")
                return 2
            out.build()
            say(f"  built the 2.0 tables ({len(TABLES_2)}) from {MIGRATIONS}")
            cp = Copier(cell, out, args)
            cp.run()
            for line in cp.report:
                say(f"  {line}")
            for t, k in cp.nul_rows.items():
                say(f"  {t}: NUL characters dropped from {k} row(s) (Postgres takes none)")
            say("Verify")
            fails = verify(cell, out, args, cp.expected, set(cp.rebuilt))
            if fails:
                return report_fails(fails, out)
            out.stamp()
            say(f"Done: the output is stamped {BASELINE}: "
                + (out.dir if out.dialect == "sqlite" else f"schema {out.schema}"))
            return 0
        finally:
            cell.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def report_fails(fails: list[str], out: Out | None = None) -> int:
    if not fails:
        say("Verified: the output is what the input gives.")
        return 0
    for f in fails:
        say(f"  FAIL  {f}")
    if out is not None:
        where = out.dir if out.dialect == "sqlite" else f"schema {out.schema}"
        say(f"Verification failed. The output ({where}) is not stamped and a 2.0 daemon does not start "
            "on it: delete it, and run again once the cause is fixed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
