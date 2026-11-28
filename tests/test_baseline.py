"""The session store starts from one revision, 0001_baseline, with the 2.0 tables (TR-1816).

A fresh SQLite store and a fresh Postgres schema are built from it: the revision table holds
0001_baseline, the tables are exactly the list below, and each one is what tables.py declares
(columns, types, nullability, defaults, primary keys, unique constraints and indexes, as Alembic's
own comparison sees them). No 1.x name is left (`usecases`, `usecase_objects`, `usecase_log`).
A downgrade drops every table and an upgrade builds them again.

Plain:  .venv/bin/python tests/test_baseline.py
With Postgres:
    TARES_TEST_PG=postgresql://postgres:pg@127.0.0.1:55499/postgres .venv/bin/python tests/test_baseline.py
Without TARES_TEST_PG the Postgres half is skipped.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import metadata  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []

# Every table of a 2.0 cell. A table added or renamed changes this list, tables.py and the revision
# that makes it, in the same change.
TABLES = [
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
VERSION_TABLE = "session_store_version"


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def versions(sdb):
    with sdb.begin() as c:
        return [r[0] for r in c.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).all()]


def _same_default(context, inspected_column, metadata_column, inspected_default, metadata_default,
                  rendered_metadata_default):
    """SQLite reports an empty text default as `('')` where the declaration renders it empty: the
    same default. Anything else goes to Alembic's own comparison (None)."""
    if inspected_default in ("''", "('')") and rendered_metadata_default in ("", "''"):
        return False
    return None


def differences(sdb):
    """What Alembic finds between the database and tables.py; [] when they agree."""
    with sdb.begin() as c:
        mc = MigrationContext.configure(c, opts={"compare_type": True,
                                                 "compare_server_default": _same_default})
        diffs = compare_metadata(mc, metadata)
    return [d for d in diffs if not (d[0] == "remove_table" and d[1].name == VERSION_TABLE)]


def check(sdb, where):
    sdb.upgrade()
    ck(f"{where}: the revision table holds 0001_baseline", versions(sdb) == ["0001_baseline"], versions(sdb))
    names = sorted(inspect(sdb.engine).get_table_names())
    ck(f"{where}: the tables are the 2.0 list", names == sorted(TABLES + [VERSION_TABLE]),
       (sorted(set(names) - set(TABLES)), sorted(set(TABLES) - set(names))))
    ck(f"{where}: no 1.x name is left", not [n for n in names if "usecase" in n], names)
    diffs = differences(sdb)
    ck(f"{where}: every table is what tables.py declares", diffs == [], diffs[:5])
    with sdb.begin() as conn:
        cfg = sdb._config()
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    ck(f"{where}: a downgrade drops every table",
       inspect(sdb.engine).get_table_names() == [VERSION_TABLE], inspect(sdb.engine).get_table_names())
    sdb.upgrade()
    ck(f"{where}: an upgrade builds them again", versions(sdb) == ["0001_baseline"]
       and sorted(inspect(sdb.engine).get_table_names()) == sorted(TABLES + [VERSION_TABLE])
       and differences(sdb) == [])


def one_revision():
    print("== one revision ==")
    sdb = SessionStore(":memory:")
    revs = list(ScriptDirectory.from_config(sdb._config()).walk_revisions())
    ck("the migrations hold one revision, 0001_baseline", [r.revision for r in revs] == ["0001_baseline"],
       [r.revision for r in revs])
    ck("it has no parent", revs and revs[0].down_revision is None)
    ck("tables.py declares the same tables", sorted(metadata.tables) == sorted(TABLES),
       sorted(set(metadata.tables) ^ set(TABLES)))
    sdb.close()


def sqlite():
    print("== a fresh SQLite store ==")
    sdb = SessionStore(tempfile.mkdtemp())
    check(sdb, "SQLite")
    sdb.close()


def postgres():
    print("== a fresh Postgres schema ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    schema = "bl_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    try:
        sdb = SessionStore(tempfile.mkdtemp(), f"{PG}?options=-csearch_path%3D{schema}")
        ck("the store is Postgres", sdb.dialect == "postgresql")
        check(sdb, "Postgres")
        sdb.close()
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def old_store():
    print("== a Tares 1.x database is refused, not run against ==")
    from tares.session_store import VERSION_TABLE as VT
    from tares.store import Store, StoreUnavailable
    home = tempfile.mkdtemp()
    sdb = SessionStore(home)
    with sdb.engine.begin() as conn:       # a 1.61.0 cell: its version table at its head
        conn.execute(text(f"CREATE TABLE {VT} (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"))
        conn.execute(text(f"INSERT INTO {VT} VALUES ('0024_merge_ledger')"))
    sdb.close()
    try:
        Store(home)
        ck("the store does not open on it", False, "it opened")
    except StoreUnavailable as e:
        ck("the store does not open on it, and says to convert it first",
           "Tares 1.x" in e.reason and "migration script" in e.reason, e.reason)
    sdb = SessionStore(home)
    ck("and the database is left as it was", versions(sdb) == ["0024_merge_ledger"], versions(sdb))
    sdb.close()


if __name__ == "__main__":
    one_revision()
    sqlite()
    postgres()
    old_store()
    print("FAIL " + ", ".join(FAILS) if FAILS else "OK")
    sys.exit(1 if FAILS else 0)
