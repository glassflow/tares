"""The groundwork for moving tables out of DuckDB (TR-1480): `settings` is the first table moved.

Plain (SQLite):       python tests/test_store_moved_tables.py
With the Postgres harness (every Store keeps its state in Postgres, the takeover cases run):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness \\
        python tests/test_store_moved_tables.py

Cases:
  settings_copy      a cell from before the move: DuckDB's settings rows arrive in SQLite, counts
                     verified, one ledger row per moved table, DuckDB untouched
  legacy_file        sessions.sqlite becomes tares.sqlite on start, with its -wal and -shm files
  cli_edition        tares up reads the edition from tares.sqlite beside the data file, not from
                     DuckDB's copy; a changed edition is the one it sees
  copy_units         _from_duckdb parses a JSONText column, _duckdb_columns skips a column DuckDB
                     lacks, and a rename that stops half way is finished by the next start
  takeover_unit      takeover(): create, add a column, error on an extra column, types ignored,
                     twice is the same as once
  json_text          JSONText round trip, None is NULL
  ddl_skips          Store._ddl() and state_db.move leave the moved tables alone
  methods            get_setting / set_setting answer as before, on whatever the Store runs on
  postgres_takeover  a table made by the old DDL is taken over with its rows untouched; an upgrade,
                     a downgrade and an upgrade again keep the rows
  accounts_copy      people, api_keys and project_members (TR-1608): DuckDB's rows arrive in SQLite with
                     JSON columns parsed (null and empty kept apart), ledger counts, DuckDB untouched
  accounts_methods   the Store's people, key and member methods keep their names and return shapes;
                     revoking a key deletes its subscriptions; deleting a project revokes its keys
  accounts_takeover  Postgres: the three tables the old DDL made are taken over, rows untouched
  projects_copy      the nine project tables (TR-1610): DuckDB's rows arrive in SQLite with JSON columns
                     parsed (null and blank kept apart), ledger counts, DuckDB untouched
  projects_methods   the Store's project, object, log, doc, skill, source, GitHub credential and model
                     usage methods keep their names and return shapes, and each group changes the rows
                     it says and no others
  projects_takeover  Postgres: the nine tables the old DDL made are taken over, rows untouched
  tickets_copy       tickets, ticket_history and ticket_numbers (TR-1612): DuckDB's rows arrive in
                     SQLite, and a ticket from before numbers gets one at that start
  tickets_methods    ticket numbers are never given twice (several writers at once too), NULLs sort
                     last, and the methods that also touch the tables still in DuckDB (deletes,
                     write-backs, reviews) change both sides
  tickets_takeover   Postgres: the three tables the old DDL made are taken over, rows untouched
  tickets_pr_lookup  pr_repo and pr_number (TR-1614) follow every write of pr, are repaired at start
                     when an older release left them stale, take any odd pr without failing a start,
                     and ticket_for_pr reads their index among 1,000 tickets
  tickets_pr_revision  revision 0019 down and up on SQLite and Postgres, filling the rows there are
  reviews_copy       milestones, checks, reviews, findings, challenger lines and rule proposals
                     (TR-1615): DuckDB's rows arrive in SQLite, null and empty kept
  reviews_methods    the Store's milestone, check, review, finding, challenger and rule proposal
                     methods keep their answers; deleting a project removes its rows
  reviews_takeover   Postgres: the eight tables the old DDL made are taken over, rows untouched; a
                     missing column and index are added, an extra column stops the upgrade
  tickets_pr_guard   the fill of pr_repo and pr_number (the live one and revision 0019's frozen copy) leaves
                     a row whose pr changes between its read and its write
  crew_copy          the decision, question and crew tables (TR-1616): DuckDB's rows arrive in SQLite
                     with lists parsed (null and blank read as []), and numbering goes on from them
  crew_methods       the Store's assumption, desk, decision, answer, told, hand-over, question, grant,
                     memory line and crew methods keep their answers and number once each, also with
                     several processes at once; deleting a project removes its rows
  crew_takeover      Postgres: the twelve tables the old DDL made are taken over, rows untouched; a new
                     database has the old defaults, constraint and index
  integr_copy        the merge queue, integration, secret and guardrail tables (TR-1617): DuckDB's rows
                     arrive in SQLite, a secret decrypts after the copy, and the credentials upgrade
                     runs on the copied rows
  integr_methods     the Store's rule, pair, integration, secret, required check, merge queue and
                     write-back methods keep their answers, also with several processes at once;
                     deleting a project removes its rows, its required checks too
  integr_one_snapshot  each integration read answers from one state while another process changes
                     the default or the settings
  integr_takeover    Postgres: the ten tables the old DDL made are taken over, rows untouched; a
                     missing column and index are added
  brain_copy         the repo maps and brain tables (TR-1618): DuckDB's rows arrive in SQLite, null and
                     empty kept, a column the file lacks reads as nothing
  brain_methods      the Store's repo map, connected repo, folder, update history and brain call methods
                     keep their answers; version checks, caps and the kept history hold with several
                     processes at once, each with its own engine
  brain_takeover     Postgres: the six tables the old DDL made are taken over, rows untouched; missing
                     columns and the index are added
"""
import copy
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import duckdb  # noqa: E402
from sqlalchemy import Column, Index, Integer, MetaData, Table, Text, event, inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

import test_session_store as ts  # noqa: E402  (its helpers; its checks share its FAILS list)
from old_ddl import _T, OLD_DDL, SEED, old_ddl_duckdb, old_ddl_postgres  # noqa: E402
from tares import project_secrets, state_db  # noqa: E402
from tares.factory import map_version  # noqa: E402
from tares.factory_store import MapChanged  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store import copy as copy_mod  # noqa: E402
from tares.session_store.engine import LEGACY_SQLITE_FILE, SQLITE_FILE, adopt_legacy_file  # noqa: E402
from tares.session_store.migrations.frozen import pr_columns as frozen_pr  # noqa: E402
from tares.session_store.pr_columns import fill_pr_columns  # noqa: E402
from tares.session_store.tables import MOVED_TABLE_NAMES, MOVED_TABLES  # noqa: E402
from tares.session_store.tables import answers as answers_table  # noqa: E402
from tares.session_store.tables import api_keys as api_keys_table  # noqa: E402
from tares.session_store.tables import challenges as challenges_table  # noqa: E402
from tares.session_store.tables import crews as crews_table  # noqa: E402
from tares.session_store.tables import integration_connections as connections_table  # noqa: E402
from tares.session_store.tables import desk_items as desk_table  # noqa: E402
from tares.session_store.tables import milestones as milestones_table  # noqa: E402
from tares.session_store.tables import people as people_table  # noqa: E402
from tares.session_store.tables import project_members as members_table  # noqa: E402
from tares.session_store.tables import settings as settings_table  # noqa: E402
from tares.session_store.tables import ticket_history as history_table  # noqa: E402
from tares.session_store.tables import ticket_numbers as numbers_table  # noqa: E402
from tares.session_store.tables import tickets as tickets_table  # noqa: E402
from tares.session_store.tables import usecases as projects_table  # noqa: E402
from tares.session_store.tables import waiting_told as told_table  # noqa: E402
from tares.session_store.takeover import TakeoverError, takeover  # noqa: E402
from tares.session_store.types import JSONText  # noqa: E402
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
# the harness (CI's postgres suite) swaps the DSN a Store gets for a schema of its own, so a case
# that names its own DSN cannot run under it; the duckdb suite has TARES_TEST_PG and no harness
HARNESS = Store.__init__.__module__ != "tares.store"
ck = ts.ck


def _norm(row):
    return tuple(ts._norm(v) for v in row)


def _pg_schema():
    import psycopg
    sch = "ss_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {sch}")
    return sch, f"{PG}?options=-csearch_path%3D{sch}"


def _drop_schema(sch):
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")


def _settings_rows(sdb):
    with sdb.begin() as c:       # through the declared table, so times come back as UTC datetimes
        return sorted(_norm(r) for r in c.execute(settings_table.select()).all())


def _legacy_cell():
    """A cell from before the move: the old DDL in DuckDB with the sample rows, no session store file."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    seeded = old_ddl_duckdb(con)
    con.execute("CHECKPOINT")
    con.close()
    return d, path, seeded


def settings_copy():
    print("== settings is copied from DuckDB to SQLite at the first start ==")
    d, path, seeded = _legacy_cell()
    want = sorted(_norm(r) for r in seeded["settings"])
    ck("the old DuckDB file has the sample settings", len(want) == 4, want)
    s = Store(path, state_dsn="")
    ck("the session store is SQLite, in tares.sqlite", s.sessions_db.dialect == "sqlite"
       and os.path.exists(os.path.join(d, SQLITE_FILE)))
    got = {r[0]: r for r in _settings_rows(s.sessions_db)}
    ck("every DuckDB settings row arrived, key, value and time", all(got.get(r[0]) == r for r in want),
       (want, got))
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger has a row for each moved table", sorted(ledger) == sorted(MOVED_TABLE_NAMES), ledger)
    ck("the ledger counts settings as the four DuckDB held (what was read equals what was inserted)",
       ledger["settings"] == 4, ledger)
    ck("and the other moved tables as the sample rows",
       ledger["session_stations"] == 2 and ledger["session_states"] == 2
       and ledger["project_sessions"] == 1 and ledger["session_usage"] == 0, ledger)
    with s._lock:
        duck = s.con.execute("SELECT count(*) FROM settings").fetchone()[0]
    ck("DuckDB still has its four rows: nothing is deleted there", duck == 4, duck)
    ck("get_setting answers from the session store", s.get_setting("edition") == "factory"
       and s.get_setting("gateway_url") == "https://gw.example.test/v1")
    s.set_setting("edition", "sre")
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing: a setting written after the copy is kept",
       s.get_setting("edition") == "sre")
    with s.sessions_db.begin() as c:
        ledger2 = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("and the ledger is as it was", ledger2 == ledger, ledger2)
    s.close()


def legacy_file():
    print("== sessions.sqlite becomes tares.sqlite ==")
    d = tempfile.mkdtemp()
    for suffix, body in (("", b"main"), ("-wal", b"wal frames"), ("-shm", b"shm index")):
        with open(os.path.join(d, LEGACY_SQLITE_FILE + suffix), "wb") as f:
            f.write(body)
    adopt_legacy_file(d)
    names = sorted(os.listdir(d))
    ck("the file and its -wal and -shm are renamed", names == [SQLITE_FILE, SQLITE_FILE + "-shm",
                                                               SQLITE_FILE + "-wal"], names)
    ck("their bytes are as they were", [open(os.path.join(d, SQLITE_FILE + s_), "rb").read()
                                        for s_ in ("", "-wal", "-shm")] == [b"main", b"wal frames", b"shm index"])
    adopt_legacy_file(d)
    ck("a second call changes nothing", sorted(os.listdir(d)) == names)

    # both exist: tares.sqlite wins and the old file is left where it is
    d2 = tempfile.mkdtemp()
    for n, body in ((SQLITE_FILE, b"new"), (LEGACY_SQLITE_FILE, b"old")):
        with open(os.path.join(d2, n), "wb") as f:
            f.write(body)
    adopt_legacy_file(d2)
    ck("with both files there, neither is touched", open(os.path.join(d2, SQLITE_FILE), "rb").read() == b"new"
       and open(os.path.join(d2, LEGACY_SQLITE_FILE), "rb").read() == b"old")

    # a real start on a cell whose store was sessions.sqlite
    d3 = tempfile.mkdtemp()
    path = os.path.join(d3, "t.duckdb")
    s = Store(path, state_dsn="")
    s.set_setting("kept", "yes")
    s.link_session("p1", "s1")
    s.close()
    os.replace(os.path.join(d3, SQLITE_FILE), os.path.join(d3, LEGACY_SQLITE_FILE))
    for f in os.listdir(d3):
        if f.startswith(SQLITE_FILE):
            os.remove(os.path.join(d3, f))      # a stale side file of the new name would not be adopted
    s = Store(path, state_dsn="")
    ck("a Store start adopts the old file: its rows are all there", s.get_setting("kept") == "yes"
       and s.sessions_of_projects(["p1"]) == ["s1"])
    ck("and no sessions.sqlite is left", not [f for f in os.listdir(d3) if f.startswith(LEGACY_SQLITE_FILE)],
       os.listdir(d3))
    s.close()


def _scratch_table(name="tmp_take"):
    md = MetaData()
    return Table(name, md, Column("id", Integer, primary_key=True), Column("a", Text), Column("b", Text),
                 Index(f"{name}_a", "a"))


def _columns(engine, name):
    return sorted(c["name"] for c in inspect(engine).get_columns(name))


def _index_names(engine, name):
    return sorted(i["name"] for i in inspect(engine).get_indexes(name))


def takeover_unit(sdb, label):
    print(f"== takeover() on {label} ==")
    e = sdb.engine
    t = _scratch_table()
    with sdb.begin() as c:
        r = takeover(c, t)
    ck(f"{label}: a missing table is created with its index", r.created and _columns(e, "tmp_take")
       == ["a", "b", "id"] and _index_names(e, "tmp_take") == ["tmp_take_a"], (r, _columns(e, "tmp_take")))

    # a table with fewer columns, rows in it, and a type the definition does not share
    with sdb.begin() as c:
        c.execute(text("CREATE TABLE tmp_old (id INTEGER PRIMARY KEY, a INTEGER)"))
        c.execute(text("INSERT INTO tmp_old (id, a) VALUES (1, 10), (2, NULL)"))
    t2 = _scratch_table("tmp_old")
    with sdb.begin() as c:
        r = takeover(c, t2)
    ck(f"{label}: a missing column is added", _columns(e, "tmp_old") == ["a", "b", "id"]
       and r.added_columns == ["b"] and not r.created, (r, _columns(e, "tmp_old")))
    ck(f"{label}: the missing index is added", _index_names(e, "tmp_old") == ["tmp_old_a"] and r.added_indexes == ["tmp_old_a"])
    with sdb.begin() as c:
        rows = c.execute(text("SELECT id, a, b FROM tmp_old ORDER BY id")).all()
    ck(f"{label}: rows are untouched and the new column is NULL", [tuple(r_) for r_ in rows]
       == [(1, 10, None), (2, None, None)], rows)
    ck(f"{label}: a column's type is not compared (a is INTEGER there, TEXT in the definition)", True)
    with sdb.begin() as c:
        r = takeover(c, t2)
    ck(f"{label}: a second call adds nothing", not r.created and not r.added_columns and not r.added_indexes, r)

    # a column the definition lacks
    with sdb.begin() as c:
        c.execute(text("CREATE TABLE tmp_extra (id INTEGER PRIMARY KEY, a TEXT, b TEXT, surprise TEXT)"))
        c.execute(text("INSERT INTO tmp_extra (id, a, b, surprise) VALUES (1, 'x', 'y', 'z')"))
    try:
        with sdb.begin() as c:
            takeover(c, _scratch_table("tmp_extra"))
        ck(f"{label}: an extra column is an error", False, "no error")
    except TakeoverError as err:
        ck(f"{label}: an extra column is an error naming it", "surprise" in str(err) and "tmp_extra" in str(err), str(err))
    with sdb.begin() as c:
        row = c.execute(text("SELECT id, a, b, surprise FROM tmp_extra")).one()
    ck(f"{label}: and the table is left as it was", tuple(row) == (1, "x", "y", "z")
       and _columns(e, "tmp_extra") == ["a", "b", "id", "surprise"])
    with sdb.begin() as c:
        for n in ("tmp_take", "tmp_old", "tmp_extra"):
            c.execute(text(f"DROP TABLE {n}"))


def json_text(sdb, label):
    print(f"== JSONText on {label} ==")
    md = MetaData()
    t = Table("tmp_json", md, Column("id", Integer, primary_key=True), Column("doc", JSONText))
    values = {1: {"a": [1, 2, {"b": None}], "n": 1.5}, 2: ["x", True], 3: "plain text", 4: 0, 5: None}
    with sdb.begin() as c:
        md.create_all(c)
        c.execute(t.insert(), [{"id": k, "doc": v} for k, v in values.items()])
    with sdb.begin() as c:
        got = {r[0]: r[1] for r in c.execute(t.select()).all()}
        raw = {r[0]: r[1] for r in c.execute(text("SELECT id, doc FROM tmp_json")).all()}
        nulls = c.execute(text("SELECT count(*) FROM tmp_json WHERE doc IS NULL")).scalar()
        kind = inspect(c).get_columns("tmp_json")[1]["type"].compile(dialect=sdb.engine.dialect)
    ck(f"{label}: every value comes back as it went in", got == values, got)
    ck(f"{label}: the column holds JSON text", raw[1] == '{"a": [1, 2, {"b": null}], "n": 1.5}' and raw[3] == '"plain text"', raw)
    ck(f"{label}: None is SQL NULL, not the text null", nulls == 1 and raw[5] is None, (nulls, raw[5]))
    ck(f"{label}: the column type is TEXT", kind.upper() == "TEXT", kind)
    with sdb.begin() as c:
        c.execute(text("DROP TABLE tmp_json"))


def ddl_skips():
    print("== the old DDL, its migrations and the one-time move leave moved tables out ==")
    stmts = [re.sub(r"--[^\n]*", "", x) for x in Store._ddl()]
    for name in sorted(MOVED_TABLE_NAMES):
        pat = re.compile(r"(CREATE\s+TABLE|ALTER\s+TABLE|CREATE\s+(UNIQUE\s+)?INDEX\b[^;]*?\bON)\s+(IF\s+(NOT\s+)?EXISTS\s+)?\"?"
                         + name + r"\b", re.I)
        hits = [x.strip()[:60] for x in stmts if pat.search(x)]
        ck(f"Store._ddl() makes, alters and indexes no {name}", not hits, hits)
    ck("the frozen old DDL still has them, as the record", all(
        any(re.search(r"CREATE TABLE IF NOT EXISTS " + n + r"\b", x) for x in OLD_DDL) for n in MOVED_TABLE_NAMES))
    ck("and the other tables are still made", any("CREATE TABLE IF NOT EXISTS mcp_servers" in x for x in stmts))

    # a fresh DuckDB file has none of them
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="")
    have = s._tables_of(s.con)
    ck("a new cell's DuckDB has no moved table", not (set(have) & MOVED_TABLE_NAMES), sorted(set(have) & MOVED_TABLE_NAMES))
    ck("and _state_tables() lists none", not (set(s._state_tables()) & MOVED_TABLE_NAMES))
    s.close()

    # state_db.move: a spy on the cursor sees every statement it runs
    import contextlib

    class Cur:
        def __init__(self):
            self.sql = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a, **k):
            self.sql.append(sql)
            return []

    class Conn:
        def __init__(self):
            self.cur = Cur()

        def transaction(self):
            return contextlib.nullcontext()

        def cursor(self):
            return self.cur

    class FakePg:
        def __init__(self):
            self.c = Conn()

        def conn(self):
            return self.c

    pg = FakePg()
    d2 = tempfile.mkdtemp()
    con = duckdb.connect(os.path.join(d2, "x.duckdb"))
    old_ddl_duckdb(con)
    names = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    ck("the old DuckDB has the moved tables", MOVED_TABLE_NAMES <= names, sorted(MOVED_TABLE_NAMES - names))
    counts = state_db.move(con, pg, sorted(MOVED_TABLE_NAMES))
    ck("move() given the moved tables moves none of them", counts == {}, counts)
    ck("and runs no statement on any of them", not any(
        re.search(r"\b" + n + r"\b", q) for q in pg.c.cur.sql for n in MOVED_TABLE_NAMES), pg.c.cur.sql)
    ck("only the record of the move is written", len(pg.c.cur.sql) == 1 and "tares_state_meta" in pg.c.cur.sql[0],
       pg.c.cur.sql)
    con.close()


def methods():
    print("== get_setting and set_setting ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    ck("a key never set is None", s.get_setting("nope") is None)
    s.set_setting("k", "v1")
    ck("a value comes back as it was set", s.get_setting("k") == "v1")
    with s.sessions_db.begin() as c:
        t1 = c.execute(text("SELECT updated_at FROM settings WHERE key = 'k'")).scalar()
    s.set_setting("k", "v2")
    with s.sessions_db.begin() as c:
        rows = c.execute(text("SELECT value, updated_at FROM settings WHERE key = 'k'")).all()
    ck("setting it again replaces the value, still one row", len(rows) == 1 and rows[0][0] == "v2", rows)
    ck("and moves the time forward", str(rows[0][1]) >= str(t1), (t1, rows[0][1]))
    s.set_setting("json", '{"a": [1, 2]}')
    ck("a JSON text is returned as the same text", s.get_setting("json") == '{"a": [1, 2]}')
    s.set_setting("empty", "")
    ck("an empty string is a value, not a missing one", s.get_setting("empty") == "")
    if s.sessions_db.dialect == "sqlite":
        # a reader does not queue behind a writer that holds the write lock (WAL, deferred BEGIN)
        import threading
        got = []
        with s.sessions_db.begin() as c:
            c.execute(text("UPDATE settings SET value = 'v3' WHERE key = 'json'"))   # the lock is held
            t = threading.Thread(target=lambda: got.append(s.get_setting("k")))
            t.start()
            t.join(2)
            waited = t.is_alive()
        t.join(10)
        ck("a get_setting answers while a writer holds the lock (the last committed value)",
           not waited and got == ["v2"], (waited, got))
        ck("and the writer's change is there once it commits", s.get_setting("json") == "v3")
        s.set_setting("json", '{"a": [1, 2]}')
    s.set_setting("k", None)
    ck("None removes the key", s.get_setting("k") is None and s.get_setting("json") == '{"a": [1, 2]}')
    s._set_setting_once("once", "first")
    s._set_setting_once("once", "second")
    ck("a one-time marker keeps its first value", s.get_setting("once") == "first")
    ck("a data upgrade's marker is in the session store, not in DuckDB",
       s.get_setting("lineage_backfilled") is not None
       and (HARNESS or "settings" not in s._tables_of(s.con)),   # the harness's move marks DuckDB
       (s.get_setting("lineage_backfilled"), "settings" in s._tables_of(s.con)))
    s.close()


def postgres_takeover():
    print("== Postgres: a settings table the old DDL made is taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        seeded = old_ddl_postgres(dsn)
        want = sorted(tuple(map(str, r)) for r in seeded["settings"])

        def snapshot():
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                 "WHERE table_schema = %s AND table_name = 'settings' ORDER BY column_name",
                                 [sch]).fetchall()
                rows = c.execute("SELECT key, value, updated_at FROM settings ORDER BY key").fetchall()
                pk = c.execute("SELECT count(*) FROM information_schema.table_constraints WHERE table_schema = %s "
                               "AND table_name = 'settings' AND constraint_type = 'PRIMARY KEY'", [sch]).fetchone()
                return cols, [tuple(map(str, r)) for r in rows], pk[0]

        before = snapshot()
        ck("the old DDL made settings, with the sample rows", len(before[1]) == 4 and before[2] == 1, before)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        ck("the database has no revision table: the old code made it", "session_store_version"
           not in inspect(sdb.engine).get_table_names())
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("settings, its columns, types, primary key and every row, is as the old code left it", snapshot() == before)
        ck("a take-over made no copy: the ledger is empty", ts._count(sdb, "session_store_copies") == 0)
        with ts.capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", ts.upgrade_lines(recs) == [])

        # upgrade, then downgrade: the rows stay
        ts.step_down(sdb, "0007_session_archive")
        ck("a downgrade to the revision before settings keeps the table and its rows", snapshot() == before
           and ts.versions(sdb) == ["0007_session_archive"], ts.versions(sdb))
        ts.step_down(sdb, "0001_base")
        ck("a downgrade to the base keeps them too", snapshot() == before)
        sdb.upgrade()
        ck("an upgrade after it takes the table over again, rows untouched", snapshot() == before
           and ts.versions(sdb) == [ts.HEAD])
        sdb.close()

        # a column the old table lacks is added; a column it has extra stops the start
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("ALTER TABLE settings DROP COLUMN updated_at")
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        ts.step_down(sdb, "0007_session_archive")
        sdb.upgrade()
        cols = [r[0] for r in snapshot()[0]]
        ck("a table missing a column gets it back, rows kept", cols == ["key", "updated_at", "value"]
           and sorted(r[:2] for r in snapshot()[1]) == sorted(w[:2] for w in want), cols)
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("ALTER TABLE settings ADD COLUMN owner TEXT")
        ts.step_down(sdb, "0007_session_archive")
        try:
            sdb.upgrade()
            ck("an extra column stops the upgrade", False, "no error")
        except Exception as err:
            ck("an extra column stops the upgrade and names the column", "owner" in str(err), str(err))
        ck("and the rows are still there", len(snapshot()[1]) == 4)
        sdb.close()

        # a Store on top of it answers
        if not HARNESS:
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                c.execute("ALTER TABLE settings DROP COLUMN owner")
                c.execute("CREATE TABLE IF NOT EXISTS tares_state_meta (key TEXT PRIMARY KEY, value TEXT, "
                          "updated_at TIMESTAMPTZ DEFAULT now())")
                c.execute("INSERT INTO tares_state_meta (key, value) VALUES ('moved_from_duckdb', '0') "
                          "ON CONFLICT DO NOTHING")      # the cell already runs on this database
            d = tempfile.mkdtemp()
            s = Store(os.path.join(d, "t.duckdb"), state_dsn=dsn)
            ck("a Store on that database is on Postgres", s.sessions_db.dialect == "postgresql")
            ck("get_setting reads the taken-over rows", s.get_setting("edition") == "factory"
               and s.get_setting("gateway_url") == "https://gw.example.test/v1")
            s.set_setting("edition", "sre")
            ck("set_setting writes them", s.get_setting("edition") == "sre")
            s.close()
    finally:
        _drop_schema(sch)


# The accounts as an old cell holds them (old_ddl's SEED has none): `emails` and `scopes` are JSON
# text, and the blank and null forms are the ones the copy must tell apart
_PEOPLE_COLS = ("id", "name", "email", "emails", "github", "is_admin", "created_at", "created_via",
                "picture", "removed_at")
_KEY_COLS = ("id", "name", "prefix", "hash", "scopes", "created_at", "last_used_at", "revoked_at",
             "project", "person_id")
_MEMBER_COLS = ("project", "person_id", "added_by", "added_at", "reason")
ACCOUNTS = {
    "people": (_PEOPLE_COLS, [
        ("local", "You", None, "[]", None, True, _T, "boot", None, None),
        ("p_kim", "Kim", "kim@example.test", '["kim@work.test", "k@home.test"]', "kimgh", False, _T,
         "admin", None, None),
        ("p_null", "Null", "null@example.test", None, None, False, _T, "admin", None, None),
        ("p_blank", "Blank", None, "", None, False, _T, "admin", "pic.png", _T),
    ]),
    "api_keys": (_KEY_COLS, [
        ("k1", "one", "nvf_k1", "h1", '["read"]', _T, None, None, None, None),
        ("k2", "two", "nvf_k2", "h2", None, _T, _T, None, "uc_demo", "p_kim"),
        ("k3", "three", "nvf_k3", "h3", "[]", _T, None, _T, None, "p_kim"),
    ]),
    "project_members": (_MEMBER_COLS, [
        ("uc_demo", "p_kim", "local", _T, "manual"),
        ("uc_demo", "p_null", None, _T, "ticket"),
    ]),
}


def _account_rows(sdb, table):
    with sdb.begin() as c:
        return sorted(_norm(r) for r in c.execute(table.select().order_by(*table.primary_key.columns)).all())


def accounts_copy():
    print("== people, api_keys and project_members are copied from DuckDB at the first start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old_ddl_duckdb(con, seed={**SEED, **ACCOUNTS})
    con.execute("CHECKPOINT")
    con.close()
    s = Store(path, state_dsn="")
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger counts the accounts as DuckDB held them",
       (ledger["people"], ledger["api_keys"], ledger["project_members"]) == (4, 3, 2), ledger)
    ppl = {r[0]: r for r in _account_rows(s.sessions_db, people_table)}
    ck("every person arrived", sorted(ppl) == ["local", "p_blank", "p_kim", "p_null"], sorted(ppl))
    with s.sessions_db.begin() as c:
        emails = dict(c.execute(text("SELECT id, emails FROM people")).all())
    ck("emails is the same JSON text in the table", emails["p_kim"] == '["kim@work.test", "k@home.test"]'
       and emails["local"] == "[]", emails)
    ck("a null emails stays NULL and an empty string is NULL too, not a crash",
       emails["p_null"] is None and emails["p_blank"] is None, emails)
    ck("a person's flags and times arrived", ppl["local"][5] in (True, 1) and ppl["p_kim"][5] in (False, 0)
       and ppl["p_blank"][9] is not None and ppl["p_kim"][9] is None, ppl)
    ck("list_people reads them", [p["id"] for p in s.list_people()] and
       {p["id"] for p in s.list_people()} >= {"local", "p_kim", "p_null"}, s.list_people())
    kim = s.get_person("p_kim")
    ck("emails comes back as a list", kim and kim["emails"] == ["kim@work.test", "k@home.test"], kim)
    ck("and as an empty list when null or blank", s.get_person("p_null")["emails"] == []
       and (s.get_person("p_blank") or {"emails": []})["emails"] == [])
    k1, k2, k3 = s.get_api_key("k1"), s.get_api_key("k2"), s.get_api_key("k3")
    ck("a key's scopes come back as a list: one, none, empty",
       k1["scopes"] == ["read"] and k2["scopes"] == [] and k3["scopes"] == [], (k1, k2, k3))
    ck("a project key keeps its project and person, and a revoked one its time",
       k2["project"] == "uc_demo" and k2["person_id"] == "p_kim" and k3["revoked_at"] is not None
       and k1["revoked_at"] is None, (k2, k3))
    ck("find_api_key finds an active key and not a revoked one", s.find_api_key("h1")["id"] == "k1"
       and s.find_api_key("h3") is None)
    ck("members arrived", sorted(m["person_id"] for m in s.list_project_members("uc_demo")) == ["p_kim", "p_null"])
    with s._lock:
        n = [s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in ("people", "api_keys", "project_members")]
    ck("DuckDB still has its rows: nothing is deleted there", n == [4, 3, 2], n)
    s.create_person("Later", email="later@example.test")
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing: a person made after the copy is kept",
       s.find_person_by_email("later@example.test") is not None and len(s.list_people()) >= 4)
    s.close()


def accounts_methods():
    print("== people, keys and members through Core ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    local = s.get_person("local")
    ck("a new cell has `local`, an admin", local and local["is_admin"] and local["emails"] == [], local)
    a = s.create_person("Ann", email="Ann@Example.test", emails=["ann2@example.test"], github="@AnnGH")
    ck("create_person lowercases and keeps the extra emails", a["email"] == "ann@example.test"
       and a["emails"] == ["ann2@example.test"] and a["github"] == "anngh", a)
    b = s.create_person("Bob")
    ck("a person with no email has an empty list", b["email"] is None and b["emails"] == [], b)
    try:
        s.create_person("Dup", email="ann2@example.test")
        ck("an email in use is refused", False, "no error")
    except ValueError as e:
        ck("an email in use is refused, naming it", "ann2@example.test" in str(e), str(e))
    ck("a clash left no row", sum(1 for p in s.list_people() if p["name"] == "Dup") == 0)
    up = s.update_person(a["id"], name="Ann B", emails=[])
    ck("update_person changes the name and clears the extra emails", up["name"] == "Ann B"
       and up["emails"] == [] and s.get_person(a["id"])["emails"] == [], up)
    ck("an email freed by an update can be used again", s.create_person("Cy", email="ann2@example.test"))
    ck("update_person of nobody is None", s.update_person("p_missing", name="x") is None)

    print("== keys ==")
    s.insert_api_key("kA", "a", "nvf_a", "hA", ["read", "write"], person_id=a["id"])
    s.insert_api_key("kB", "b", "nvf_b", "hB", [], project="uc_x")
    ck("a key's scopes are a list, empty when none", s.get_api_key("kA")["scopes"] == ["read", "write"]
       and s.get_api_key("kB")["scopes"] == [], (s.get_api_key("kA"), s.get_api_key("kB")))
    ck("list_api_keys for a project lists its keys only", [k["id"] for k in s.list_api_keys("uc_x")] == ["kB"])
    ck("list_api_keys lists both, newest first", {k["id"] for k in s.list_api_keys()} == {"kA", "kB"})
    ck("key_person_id and set_key_person", s.key_person_id("kA") == a["id"]
       and s.set_key_person("kB", b["id"]) and s.key_person_id("kB") == b["id"]
       and s.set_key_person("kB", None) and s.key_person_id("kB") is None and not s.set_key_person("nope", None))
    s.insert_api_key("kRev", "r", "nvf_r", "hRev", [], person_id=a["id"])
    s.revoke_api_key("kRev")
    ck("set_key_person leaves a revoked key alone", s.set_key_person("kRev", b["id"]) is False
       and s.get_api_key("kRev")["person_id"] == a["id"], s.get_api_key("kRev"))
    s.touch_api_key("kA")
    ck("touch_api_key sets the time", s.get_api_key("kA")["last_used_at"] is not None)
    s.add_subscription("sub_kA", "t1", "https://hook.example.test/a", created_by="key:kA")
    s.add_subscription("sub_other", "t1", "https://hook.example.test/o", created_by="key:kB")
    ck("revoke_api_key says it revoked", s.revoke_api_key("kA") is True)
    with s._sread:
        subs = [r[0] for r in s.sc.execute("SELECT subscription_id FROM subscriptions").fetchall()]
    ck("and deleted that key's subscriptions only", "sub_kA" not in subs and "sub_other" in subs, subs)
    ck("a revoked key is not found, and revoking again says no", s.find_api_key("hA") is None
       and s.get_api_key("kA")["revoked_at"] is not None and s.revoke_api_key("kA") is False
       and s.revoke_api_key("kNope") is False)

    print("== a person who leaves ==")
    s.insert_api_key("kC", "c", "nvf_c", "hC", ["read"], person_id=b["id"])
    s.insert_api_key("kD", "d", "nvf_d", "hD", ["read"], person_id=b["id"])
    ck("remove_person says it removed", s.remove_person(b["id"]) is True)
    ck("their keys are revoked", s.find_api_key("hC") is None and s.find_api_key("hD") is None)
    ck("a second remove says no", s.remove_person(b["id"]) is False)
    # the whole cleanup is one transaction: a crash at its last step leaves nothing half done
    cy = s.create_person("Cy2")
    s.create_project("uc_cy", "custom", "cy", {}, lead_person_id=cy["id"])
    theirs = s.create_ticket("uc_cy", "tares", "open work", assignee_person_id=cy["id"])
    s.update_ticket(theirs, stage="doing")
    done = s.create_ticket("uc_cy", "tares", "finished work", status="done", assignee_person_id=cy["id"])
    s.update_ticket(done, stage="merged")

    def assignee(tid):
        return s.get_ticket("uc_cy", tid)["assignee_person_id"]

    import tares.store as store_mod
    real_now = store_mod.now_utc

    def boom():
        raise RuntimeError("crash")
    store_mod.now_utc = boom         # cy has no key, so the first call is the one that marks them removed
    try:
        s.remove_person(cy["id"])
        ck("a crash in the cleanup raises", False, "no error")
    except RuntimeError:
        ck("a crash in the cleanup raises", True)
    finally:
        store_mod.now_utc = real_now
    ck("the person is not marked removed yet, so a retry can finish", not s.get_person(cy["id"]).get("removed_at"))
    ck("and their ticket and lead are as before", assignee(theirs) == cy["id"]
       and s.get_project("uc_cy")["lead_person_id"] == cy["id"], (assignee(theirs), s.get_project("uc_cy")))
    ck("the retry removes them", s.remove_person(cy["id"]) is True and s.get_person(cy["id"])["removed_at"])
    ck("their open ticket and lead are nobody's, a finished ticket keeps their name", assignee(theirs) is None
       and assignee(done) == cy["id"] and s.get_project("uc_cy")["lead_person_id"] is None,
       (assignee(theirs), assignee(done)))

    print("== members and a project's end ==")
    s.create_project("uc_x", "custom", "x", {}, lead_person_id=a["id"])
    ck("add_project_member says added, then already", s.add_project_member("uc_x", "p1", added_by="local")
       is True and s.add_project_member("uc_x", "p1", added_by="other", reason="ticket") is False)
    ck("the first reason stays", [m["reason"] for m in s.list_project_members("uc_x")] == ["manual"])
    try:
        s.add_project_member("uc_x", "p2", reason="odd")
        ck("an unknown reason is refused", False, "no error")
    except ValueError:
        ck("an unknown reason is refused", True)
    s.add_project_member("uc_x", "p2", reason="ticket")
    by = s.members_by_project(["uc_x", "uc_none"])
    ck("members_by_project groups them", sorted(m["person_id"] for m in by.get("uc_x", [])) == ["p1", "p2"]
       and not by.get("uc_none"), by)
    ck("is_project_member: a member, the lead, a stranger", s.is_project_member("uc_x", "p1")
       and s.is_project_member("uc_x", a["id"]) and not s.is_project_member("uc_x", "p_none")
       and not s.is_project_member("uc_x", None))
    ck("remove_project_member says removed, then no", s.remove_project_member("uc_x", "p2") is True
       and s.remove_project_member("uc_x", "p2") is False)
    s.insert_api_key("kE", "e", "nvf_e", "hE", ["read"], project="uc_x")
    s.insert_api_key("kF", "f", "nvf_f", "hF", ["read"], project="uc_other")
    s.delete_project("uc_x")
    ck("deleting the project revokes its keys and drops its members", s.find_api_key("hE") is None
       and s.get_api_key("kE")["revoked_at"] is not None and s.list_project_members("uc_x") == [])
    ck("and left another project's key alone", s.find_api_key("hF") is not None)
    s.close()


def accounts_takeover():
    print("== Postgres: the accounts tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        seeded = old_ddl_postgres(dsn, seed={**SEED, **ACCOUNTS})
        tables = ("people", "api_keys", "project_members")

        def snapshot():
            out = {}
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                for t in tables:
                    cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                     "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
                                     [sch, t]).fetchall()
                    rows = c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                    out[t] = (cols, [tuple(map(str, r)) for r in rows])
            return out

        before = snapshot()
        ck("the old DDL made the three tables, with the sample rows",
           [len(before[t][1]) for t in tables] == [4, 3, 2], {t: len(before[t][1]) for t in tables})
        ck("emails and scopes are TEXT there", dict((c[0], c[1]) for c in before["people"][0])["emails"] == "text"
           and dict((c[0], c[1]) for c in before["api_keys"][0])["scopes"] == "text")
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its columns, types and rows, is as the old code left it", snapshot() == before)
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        with ts.capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", ts.upgrade_lines(recs) == [])
        ts.step_down(sdb, "0013_session_origin")
        ck("a downgrade to the revision before keeps the tables and their rows", snapshot() == before
           and ts.versions(sdb) == ["0013_session_origin"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it takes them over again, rows untouched", snapshot() == before)
        # the Core tables read what the old code wrote
        ppl = {r[0]: r for r in _account_rows(sdb, people_table)}
        ck("Core reads a person the old code wrote", sorted(ppl) == ["local", "p_blank", "p_kim", "p_null"])
        with sdb.begin() as c:
            kim = c.execute(people_table.select().where(people_table.c.id == "p_kim")).first()
            k2 = c.execute(api_keys_table.select().where(api_keys_table.c.id == "k2")).first()
            blank = c.execute(people_table.select().where(people_table.c.id == "p_blank")).first()
        ck("JSON text parses to a list, null to None", kim.emails == ["kim@work.test", "k@home.test"]
           and k2.scopes is None and blank.emails is None, (kim, k2, blank))
        ck("members read too", len(_account_rows(sdb, members_table)) == 2)
        sdb.close()
    finally:
        _drop_schema(sch)

# The project tables as an old cell holds them (old_ddl's SEED has none). `params`, `setup`, `linear`,
# `github_issues` and `config` are JSON text, and the blank and null forms are the ones the copy must
# tell apart. `github_credentials.config` stays text.
PROJECT_TABLES = ("usecases", "usecase_objects", "usecase_log", "docs", "skills", "shared_skills",
                  "catalog_sources", "github_credentials", "model_usage")
_PROJECT_COLS = ("id", "recipe", "name", "params", "status", "created_at", "updated_at", "last_error", "goal",
                 "setup", "linear", "github_issues", "kind", "lead_person_id", "scope", "icon")
PROJECTS = {
    "usecases": (_PROJECT_COLS, [
        ("uc_demo", "custom", "Demo", '{"objects": [{"kind": "source", "name": "logs"}]}', "active", _T, _T,
         None, "Make it fast", '{"step": "plan"}', '{"team": "T1"}', '{"repo": "o/r"}', "software_factory",
         "p_kim", "private", "\U0001f9ed"),
        ("uc_old", "custom", "Old", "{}", "active", _T, _T, "boom", None, None, None, None, None, None, None, None),
        ("uc_blank", "custom", "Blank", None, "draft", _T, _T, None, "", None, None, None, None, None, "team", None),
    ]),
    "usecase_objects": (("usecase_id", "kind", "key", "name", "customized", "created_at"), [
        ("uc_demo", "source", "source:logs", "logs", False, _T),
        ("uc_demo", "doc", "doc:d1", "d1", True, _T),
        ("uc_old", "skill", "skill:runbook", "runbook", None, _T),
    ]),
    "usecase_log": (("usecase_id", "logged_at", "action", "detail"), [
        ("uc_demo", _T, "create", None),
        ("uc_demo", _T, "edit", "renamed"),
    ]),
    "docs": (("id", "kind", "title", "body", "made_by", "updated_by", "created_at", "updated_at", "owner"), [
        ("d1", "spec", "Spec", "# Spec", "uc_demo", "console", _T, _T, None),
        ("d2", "personal_agents", "Yours", "mine", None, None, _T, _T, "p_kim"),
    ]),
    "skills": (("project", "name", "description", "body", "created_at", "updated_at"), [
        ("uc_old", "runbook", "How to restart", "restart it", _T, _T),
    ]),
    "shared_skills": (("name", "description", "body", "made_by", "created_at", "updated_at", "updated_by"), [
        ("runbook", "How to restart", "restart it", "uc_demo", _T, _T, "console"),
        ("plain", "Plain", "do it", None, _T, _T, None),
    ]),
    "catalog_sources": (("name", "type", "connector", "poll", "config", "paused", "created_at", "updated_at",
                         "ingest_key", "owned_by", "customized"), [
        ("logs", "otlp", "otlp", "5s", '{"labels": [1]}', False, _T, _T, "ik_logs", "uc_demo", False),
        ("laptop", "poll", "claude_code", "5m", None, True, _T, _T, None, None, None),
    ]),
    "github_credentials": (("name", "kind", "token", "api_url", "account", "created_at", "updated_at", "config"), [
        ("gh1", "app", "tok1", "https://api.github.com", "org", _T, _T, '{"app_id": 7}'),
        ("gh2", "token", "tok2", None, None, _T, _T, None),
    ]),
    "model_usage": (("id", "ts", "surface", "agent", "run_id", "model", "calls", "input_tokens", "output_tokens",
                     "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd", "key_source",
                     "provider"), [
        ("m1", _T, "ask", None, None, "claude", 1, 100, 50, 0, 0, 0.01, "env", "anthropic"),
        ("m2", _T, "agent", "a1", "r1", "claude", 2, 10, 5, 1, 2, None, None, None),
    ]),
}
PROJECT_COUNTS = {"usecases": 3, "usecase_objects": 3, "usecase_log": 2, "docs": 2, "skills": 1,
                  "shared_skills": 2, "catalog_sources": 2, "github_credentials": 2, "model_usage": 2}


def _table_rows(sdb, name):
    with sdb.begin() as c:
        return c.execute(text(f"SELECT * FROM {name}")).all()


def _n(sdb, name):
    with sdb.begin() as c:
        return c.execute(text(f"SELECT count(*) FROM {name}")).scalar()


def projects_copy():
    print("== the project tables are copied from DuckDB at the first start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old_ddl_duckdb(con, seed={**SEED, **PROJECTS})
    con.execute("CHECKPOINT")
    con.close()
    s = Store(path, state_dsn="")
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger counts each table as DuckDB held it", {t: ledger.get(t) for t in PROJECT_TABLES} == PROJECT_COUNTS,
       ledger)
    have = {t: _n(s.sessions_db, t) for t in PROJECT_TABLES}
    ck("the rows are there (the start may add the default project, a log line or the seeds)",
       all(have[t] >= PROJECT_COUNTS[t] for t in PROJECT_TABLES), have)
    demo, old, blank = s.get_project("uc_demo"), s.get_project("uc_old"), s.get_project("uc_blank")
    # (demo is the cell's default project in SEED: the start adds the loose source `laptop` to it)
    ck("params arrive parsed", demo["params"]["objects"][0] == {"kind": "source", "name": "logs"}
       and old["params"] == {} and blank["params"] == {}, (demo["params"], old["params"], blank["params"]))
    ck("a project keeps its goal, kind, lead, scope and icon", (demo["goal"], demo["kind"], demo["lead_person_id"],
       demo["scope"], demo["icon"]) == ("Make it fast", "software_factory", "p_kim", "private", "\U0001f9ed"), demo)
    ck("a null scope reads as team, and a null goal stays None", old["scope"] == "team" and old["goal"] is None
       and old["last_error"] == "boom", old)
    ck("setup, linear and github_issues are parsed, and null and blank are None",
       s.get_project_setup("uc_demo") == {"step": "plan"} and s.get_project_linear("uc_demo") == {"team": "T1"}
       and s.get_project_github("uc_demo") == {"repo": "o/r"} and s.get_project_setup("uc_old") is None
       and s.get_project_setup("uc_blank") is None and s.get_project_linear("uc_blank") is None,
       (s.get_project_setup("uc_demo"), s.get_project_linear("uc_demo"), s.get_project_github("uc_demo")))
    objs = {o["key"]: o["customized"] for o in s.list_project_objects("uc_demo")}
    ck("a project's objects arrive, with the customized flag", objs["doc:d1"] is True
       and objs["source:logs"] is False and set(objs) <= {"doc:d1", "source:logs", "source:laptop"}, objs)
    ck("the log keeps a null detail and a text one", {e["action"]: e["detail"] for e in s.list_project_log("uc_demo")}
       .get("create") in (None, "") and {e["action"]: e["detail"] for e in s.list_project_log("uc_demo")}
       .get("edit") == "renamed", s.list_project_log("uc_demo"))
    src = {x["name"]: x for x in s.list_catalog_sources()}
    ck("a source's config arrives parsed, null as is", src["logs"]["config"] == {"labels": [1]}
       and src["laptop"]["config"] in (None, {}) and src["laptop"]["paused"] is True
       and src["logs"]["ingest_key"] == "ik_logs" and src["logs"]["owned_by"] == "uc_demo", src)
    gh = {c["name"]: c for c in s.list_github_credentials()}
    ck("a GitHub credential's config is the dict, and a null one is {}", gh["gh1"]["config"] == {"app_id": 7}
       and gh["gh2"]["config"] == {} and gh["gh2"]["api_url"] == "" and gh["gh1"]["token"] == "tok1", gh)
    ck("shared skills and docs arrive", s.get_skill(None, "runbook")["body"] == "restart it"
       and s.get_doc(None, "d1")["body"] == "# Spec" and s.get_doc(None, "d2")["title"] == "Yours")
    tot = s.model_usage_summary()["total"]
    ck("the usage ledger sums as DuckDB's rows did", (tot["calls"], tot["input_tokens"], tot["output_tokens"],
       tot["cache_creation_input_tokens"], tot["cache_read_input_tokens"], tot["cost_usd"], tot["uncosted_calls"])
       == (3, 110, 55, 1, 2, 0.01, 2), tot)
    with s._lock:
        n = [s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in PROJECT_TABLES]
    ck("DuckDB still has its rows: nothing is deleted there", n == [PROJECT_COUNTS[t] for t in PROJECT_TABLES], n)
    s.create_project("uc_later", "custom", "Later", {})
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing: a project made after the copy is kept",
       s.get_project("uc_later") is not None and len(s.list_projects()) >= 4)
    s.close()


def integr_one_snapshot():
    print("== a read of the integrations sees one state while another process writes ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    other = _another_process(s, d)
    s.create_project("uc_t", "custom", "Tee", {})

    def during(read, write):
        """`read()`, with `write()` run by the other process right after the read's first query."""
        ran = []

        def after(conn, cursor, statement, *a):
            if statement.startswith("SELECT") and not ran:
                ran.append(1)
                write()

        event.listen(s.sessions_db.engine, "after_cursor_execute", after)
        try:
            got = read()
        finally:
            event.remove(s.sessions_db.engine, "after_cursor_execute", after)
        assert ran, "the write never ran"
        return got

    def defaults():
        return [c["name"] for c in s.list_connections("tickets") if c["default"]]

    ck("no connection, no default: an unknown one reads as None, its settings as empty",
       (s.list_connections("tickets"), s.get_connection("tickets", "nope"), s.get_choice("tickets"))
       == ([], None, None)
       and s.stored_settings("tickets", "nope") == s.stored_settings("tickets", "nope", "uc_t") == {})
    s.set_choice("ci", "github", {"repo": "o/a"}, "Kim", default=False)
    s.set_choice("ci", "local", {"token": "l"}, "Kim", default=False)
    ck("no default chosen: the built-in one's connection is marked",
       [(c["name"], c["default"]) for c in s.list_connections("ci")] == [("github", False), ("local", True)],
       s.list_connections("ci"))
    s.set_choice("tickets", "tix_a", {"token": "a"}, "Kim", default=False)
    ck("a connection and no default chosen: none is marked",
       defaults() == [] and s.get_connection("tickets", "tix_a")["default"] is False
       and s.get_choice("tickets") is None)
    s.set_default("tickets", "tix_a", "Kim")
    s.set_choice("tickets", "tix_b", {"token": "b"}, "Kim", default=False)

    before = s.list_connections("tickets")

    def connect_a_third():
        other.set_choice("tickets", "tix_c", {"token": "c"}, "Ann", default=False)
        other.set_default("tickets", "tix_c", "Ann")

    got = during(lambda: s.list_connections("tickets"), connect_a_third)
    ck("list_connections: set_default meanwhile, still exactly one default, all from before",
       got == before and [c["name"] for c in got if c["default"]] == ["tix_a"] and defaults() == ["tix_c"],
       (got, defaults()))

    for name, what in (("tix_c", "the default"), ("tix_a", "the next default")):
        s.set_default("tickets", "tix_c", "Kim")
        before = s.get_connection("tickets", name)
        got = during(lambda: s.get_connection("tickets", name),
                     lambda: other.set_default("tickets", "tix_a", "Ann"))
        ck(f"get_connection of {what}: set_default meanwhile, the flag is from before",
           got == before and got["default"] is (name == "tix_c") and defaults() == ["tix_a"],
           (before, got, defaults()))

    s.set_default("tickets", "tix_a", "Kim")
    before = s.get_choice("tickets")

    def move_the_default_and_drop_it():
        other.set_default("tickets", "tix_b", "Ann")
        other.delete_connection("tickets", "tix_a")

    got = during(lambda: s.get_choice("tickets"), move_the_default_and_drop_it)
    ck("get_choice: set_default and a delete meanwhile, the default keeps its settings",
       got == before and got["secrets"] == {"token": {"has_value": True}} and defaults() == ["tix_b"],
       (before, got, defaults()))

    s.set_choice("tickets", "github", {"repo": "o/a"}, "Kim")
    s.set_project_choice("uc_t", "tickets", "github", {"issue_label": "old"}, "Kim")

    def change_both():
        other.set_choice("tickets", "github", {"repo": "o/b"}, "Ann")
        other.set_project_choice("uc_t", "tickets", "github", {"issue_label": "new"}, "Ann")

    got = during(lambda: s.stored_settings("tickets", "github", "uc_t"), change_both)
    ck("stored_settings: both change meanwhile, the connection and the pick are from before",
       got == {"repo": "o/a", "issue_label": "old"}
       and s.stored_settings("tickets", "github", "uc_t") == {"repo": "o/b", "issue_label": "new"}, got)
    other.sessions_db.close()
    s.close()


def _waits_for_write_lock(s, call):
    """Whether `call`, on another thread, stays out while this thread holds the store's write lock."""
    done = threading.Event()
    t = threading.Thread(target=lambda: (call(), done.set()), daemon=True)
    with s._swrite:
        t.start()
        early = done.wait(0.3)
    t.join(10)
    return not early and done.is_set()


def _runs_under_the_write_lock(s, call):
    """Whether `call`, on another thread, finishes while a transaction holds the session store's
    write lock (SQLite has one writer at a time; a page read must not queue behind it)."""
    done = threading.Event()
    t = threading.Thread(target=lambda: (call(), done.set()), daemon=True)
    with s.sessions_db.begin():
        t.start()
        early = done.wait(2)
    t.join(10)
    return early


def projects_methods():
    print("== projects, objects, log, docs, skills, sources, credentials and usage through Core ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    sdb = s.sessions_db
    base = {t: _n(sdb, t) for t in PROJECT_TABLES}

    def grew(**kw):
        now = {t: _n(sdb, t) for t in PROJECT_TABLES}
        out = {t: now[t] - base[t] for t in PROJECT_TABLES if now[t] != base[t]}
        base.update(now)
        return out == kw, out

    print("== projects ==")
    s.create_project("uc_a", "custom", "Alpha", {"k": [1]}, goal="Ship", lead_person_id="local", icon="A")
    a = s.get_project("uc_a")
    ck("create_project writes one row, params a dict, scope team", a["name"] == "Alpha"
       and a["params"] == {"k": [1]} and a["goal"] == "Ship" and a["scope"] == "team" and a["icon"] == "A"
       and a["lead_person_id"] == "local" and a["status"] == "active", a)
    ck("create_project touched only usecases", grew(usecases=1)[0], grew())
    s.update_project("uc_a", params={"k": 2}, status="paused", last_error="bad", name="Alpha2", goal=None)
    a = s.get_project("uc_a")
    ck("update_project sets params, status, error, name, and clears a goal given as None", a["params"] == {"k": 2}
       and a["status"] == "paused" and a["last_error"] == "bad" and a["name"] == "Alpha2" and a["goal"] is None, a)
    s.update_project("uc_a")
    ck("update_project with no arguments leaves the row as it was", s.get_project("uc_a")["last_error"] == "bad")
    s.set_project_setup("uc_a", {"step": 1})
    s.set_project_scope("uc_a", "private")
    s.set_project_kind("uc_a", "software_factory")
    s.set_project_icon("uc_a", None)
    a = s.get_project("uc_a")
    ck("setup, scope, kind and icon each change their own column", s.get_project_setup("uc_a") == {"step": 1}
       and a["scope"] == "private" and a["kind"] == "software_factory" and a["icon"] is None, a)
    s.set_project_setup("uc_a", None)
    ck("a cleared setup reads None", s.get_project_setup("uc_a") is None)
    try:
        s.set_project_scope("uc_a", "bogus")
        ck("a bad scope is refused", False, "no error")
    except ValueError:
        ck("a bad scope is refused", s.get_project("uc_a")["scope"] == "private")
    ck("get_project of nobody is None", s.get_project("uc_missing") is None)
    ck("list_projects has it, filtered by kind", "uc_a" in [p["id"] for p in s.list_projects()]
       and [p["id"] for p in s.list_projects(kind="software_factory")] == ["uc_a"])
    ck("list_projects ids filter", [p["id"] for p in s.list_projects(ids=["uc_a"])] == ["uc_a"])

    print("== log ==")
    s.log_project("uc_a", "edit", "x" * 3000)
    s.log_project("uc_a", "note")
    lg = s.list_project_log("uc_a")
    ck("log_project keeps two lines, detail cut at 2000, empty detail kept", len(lg) >= 2
       and any(len(e["detail"] or "") == 2000 for e in lg) and any(e["action"] == "note" for e in lg), lg)
    ck("the log touched only usecase_log", grew(usecase_log=len(lg))[0], grew())

    print("== docs ==")
    did = s.create_doc("uc_a", "spec", "Spec", "# hi", by="uc_a")
    ck("create_doc writes the doc and its project object", grew(docs=1, usecase_objects=1)[0], grew())
    g = s.get_doc("uc_a", did)
    ck("get_doc returns body and the projects that include it", g["title"] == "Spec" and g["body"] == "# hi"
       and g["projects"] == ["uc_a"] and g["kind"] == "spec", g)
    ck("get_doc of a project that lacks it is None", s.get_doc("uc_missing", did) is None)
    s.update_doc(did, title="Spec2", by="console")
    g = s.get_doc(None, did)
    ck("update_doc changes the title, who, and keeps the body", g["title"] == "Spec2" and g["body"] == "# hi"
       and g["updated_by"] == "console", g)
    ld = s.list_docs("uc_a")
    ck("list_docs gives size in bytes, no body", [x["id"] for x in ld] == [did] and ld[0]["size"] == 4
       and "body" not in ld[0], ld)
    ck("list_docs by kind", s.list_docs("uc_a", kind="other") == [] and len(s.list_docs("uc_a", kind="spec")) == 1)
    pd = s.create_doc(None, "personal_agents", "Mine", "x", owner="local")
    ck("a personal doc is not in a project's list, is in its owner's", pd not in [x["id"] for x in s.list_docs("uc_a")]
       and (s.personal_doc("local", "personal_agents") or {}).get("id") == pd)
    ck("doc_titles reads titles for the ids asked", s.doc_titles("uc_a", [did]) == {did: "Spec2"}, s.doc_titles("uc_a", [did]))
    grew()
    ck("remove_doc returns True and deletes a doc only that project used", s.remove_doc("uc_a", did) is True
       and s.get_doc(None, did) is None)
    ck("remove_doc of one not included is False", s.remove_doc("uc_a", did) is False)
    ck("remove_doc touched docs and objects only", grew(docs=-1, usecase_objects=-1)[0], grew())

    print("== skills ==")
    ck("upsert_skill is True the first time", s.upsert_skill("uc_a", "run", "How", "do it", updated_by="me") is True)
    ck("and False the second", s.upsert_skill("uc_a", "run", "How2", "do it again") is False)
    ck("one shared skill and one object", grew(shared_skills=1, usecase_objects=1)[0], grew())
    sk = s.get_skill("uc_a", "run")
    ck("get_skill has the changed description and body", sk["description"] == "How2" and sk["body"] == "do it again", sk)
    ls = s.list_skills("uc_a")
    ck("list_skills has size, no body", [x["name"] for x in ls] == ["run"] and ls[0]["size"] == 11
       and "body" not in ls[0], ls)
    ck("list_all_skills names its users", [x["projects"] for x in s.list_all_skills() if x["name"] == "run"] == [["uc_a"]])
    s.mark_skill_customized("uc_a", "run")
    ck("mark_skill_customized sets the flag on the object", {o["key"]: o["customized"] for o in
       s.list_project_objects("uc_a")}.get("skill:run") is True, s.list_project_objects("uc_a"))
    ck("delete_skill returns True, removes the use, the unused skill goes with it", s.delete_skill("uc_a", "run") is True
       and s.get_skill(None, "run") is None and s.delete_skill("uc_a", "run") is False)

    print("== sources ==")
    s.upsert_catalog_source("src1", "poll", "http_poll", "1m", {"url": "u"}, paused=False, ingest_key="k1")
    cs = {x["name"]: x for x in s.list_catalog_sources()}
    ck("upsert_catalog_source writes one row, config a dict", cs["src1"]["config"] == {"url": "u"}
       and cs["src1"]["ingest_key"] == "k1" and cs["src1"]["paused"] is False, cs["src1"])
    s.upsert_catalog_source("src1", "poll", "http_poll", "2m", {"url": "v"})
    cs = {x["name"]: x for x in s.list_catalog_sources()}
    ck("a second upsert replaces the config and keeps the ingest key", cs["src1"]["config"] == {"url": "v"}
       and cs["src1"]["poll"] == "2m" and cs["src1"]["ingest_key"] == "k1", cs["src1"])
    s.set_source_paused("src1", True)
    s.set_source_ingest_key("src1", "k2")
    cs = {x["name"]: x for x in s.list_catalog_sources()}
    ck("pause and ingest key change their column only", cs["src1"]["paused"] is True
       and cs["src1"]["ingest_key"] == "k2" and cs["src1"]["config"] == {"url": "v"}, cs["src1"])
    s.put_in_project("source", "src1", "uc_a")
    ck("project_sources joins the project's objects to the sources", list(s.project_sources("uc_a"))
       == ["src1"], s.project_sources("uc_a"))
    ck("projects_using names the project", s.projects_using("source", "src1") == ["uc_a"])
    grew()
    s.delete_catalog_source("src1")
    ck("delete_catalog_source removes the source and its objects", "src1" not in [x["name"] for x in s.list_catalog_sources()]
       and s.projects_using("source", "src1") == [], s.projects_using("source", "src1"))

    print("== GitHub credentials ==")
    s.upsert_github_credential("gh", "tok", kind="app", api_url="https://x", account="org", config={"app_id": 1})
    g = {c["name"]: c for c in s.list_github_credentials()}["gh"]
    ck("the credential is stored with its config dict", g["config"] == {"app_id": 1} and g["token"] == "tok"
       and g["kind"] == "app" and g["account"] == "org", g)
    s.upsert_github_credential("gh", "tok2")
    g = {c["name"]: c for c in s.list_github_credentials()}["gh"]
    ck("a rotation with config None keeps the config", g["token"] == "tok2" and g["config"] == {"app_id": 1}, g)
    s.upsert_github_credential("gh", "tok2", config={})
    ck("config {} clears it", {c["name"]: c for c in s.list_github_credentials()}["gh"]["config"] == {})
    s.update_github_credential_config("gh", {"installs": [3]})
    ck("update_github_credential_config replaces only the config", {c["name"]: c for c in s.list_github_credentials()}
       ["gh"]["config"] == {"installs": [3]})
    s.delete_github_credential("gh")
    ck("delete removes it", "gh" not in [c["name"] for c in s.list_github_credentials()])

    print("== model usage ==")
    grew()
    base_tot = s.model_usage_summary()["total"]["calls"]
    s.record_model_usage("ask", "", "", "m", 2, 100, 20, 3, 4, cost_usd=0.5, key_source="env", provider="anthropic")
    s.record_model_usage("agent", "a", "r1", "m", 1, 10, 2)
    sm = s.model_usage_summary()
    ck("the totals add both rows", sm["total"]["calls"] - base_tot == 3, sm["total"])
    ck("an unpriced row counts as uncosted and the cost total is a float", sm["total"]["uncosted_calls"] >= 1
       and isinstance(sm["total"]["cost_usd"], float), sm["total"])
    ck("by surface, key source and provider split them", sm["by_surface"]["ask"]["calls"] == 2
       and sm["by_surface"]["agent"]["calls"] >= 1 and sm["by_key_source"]["env"]["input_tokens"] == 100
       and "unknown" in sm["by_key_source"] and sm["by_provider"]["anthropic"]["calls"] >= 3, sm)
    ck("days has today with both rows", sm["days"] and sm["days"][-1]["calls"] >= 3 and sm["window_days"] == 30, sm["days"])
    ck("only model_usage grew", *(lambda r: (r[0], r[1]))(grew(model_usage=2)))

    print("== delete_project ==")
    s.create_project("uc_b", "custom", "Beta", {})
    s.log_project("uc_b", "x")
    s.create_doc("uc_b", "spec", "T", "b")
    s.upsert_skill("uc_b", "own", "d", "b")
    s.upsert_skill("uc_a", "shared", "d", "b")
    s.upsert_skill("uc_b", "shared", "d", "b")
    s.insert_api_key("kP", "p", "nvf_p", "hP", [], project="uc_b")
    s.delete_project("uc_b")
    ck("the project, its log, objects and its only doc and skill are gone", s.get_project("uc_b") is None
       and s.list_project_log("uc_b") == [] and s.list_project_objects("uc_b") == []
       and s.get_skill(None, "own") is None, s.list_project_objects("uc_b"))
    ck("a skill another project uses stays", s.get_skill("uc_a", "shared") is not None)
    ck("its key is revoked", s.get_api_key("kP")["revoked_at"] is not None, s.get_api_key("kP"))
    ck("the other project is untouched", s.get_project("uc_a") is not None)

    print("== writes that read first wait for the store's write lock ==")
    s.upsert_catalog_source("src2", "poll", "http_poll", "1m", {})
    # on Postgres two of these threads' transactions interleave; only this lock keeps them apart
    for name, call in (
            ("delete_catalog_source", lambda: s.delete_catalog_source("src2")),
            ("upsert_skill", lambda: s.upsert_skill("uc_a", "locked", "d", "b")),
            ("delete_skill", lambda: s.delete_skill("uc_a", "locked")),
            ("upsert_github_credential", lambda: s.upsert_github_credential("locked", "tok")),
            ("update_github_credential_config", lambda: s.update_github_credential_config("locked", {}))):
        ck(f"{name} waits for the write lock", _waits_for_write_lock(s, call))

    print("== plain writes wait for it too ==")
    # a write that reads a row and writes it back relies on every other writer of the row waiting here
    doc = s.create_doc("uc_a", "working", "locked", "b")
    for name, call in (
            ("upsert_catalog_source", lambda: s.upsert_catalog_source("src3", "poll", "http_poll", "1m", {})),
            ("set_source_ingest_key", lambda: s.set_source_ingest_key("src3", "k")),
            ("set_source_paused", lambda: s.set_source_paused("src3", True)),
            ("record_model_usage", lambda: s.record_model_usage("ask", "a", "r", "m", 1, 1, 1)),
            ("create_project", lambda: s.create_project("uc_c", "custom", "C", {})),
            ("update_project", lambda: s.update_project("uc_a", params={"k": 1})),
            ("set_project_lead", lambda: s.set_project_lead("uc_a", "local")),
            ("set_project_icon", lambda: s.set_project_icon("uc_a", "B")),
            ("set_project_scope", lambda: s.set_project_scope("uc_a", "team")),
            ("set_project_kind", lambda: s.set_project_kind("uc_a", None)),
            ("set_project_setup", lambda: s.set_project_setup("uc_a", {})),
            ("set_project_linear", lambda: s.set_project_linear("uc_a", {})),
            ("set_project_github", lambda: s.set_project_github("uc_a", {})),
            ("upsert_project_object", lambda: s.upsert_project_object("uc_a", "doc", "k1", "n1")),
            ("delete_project_object", lambda: s.delete_project_object("uc_a", "doc", "k1")),
            ("mark_skill_customized", lambda: s.mark_skill_customized("uc_a", "locked")),
            ("create_doc", lambda: s.create_doc("uc_a", "working", "t", "b")),
            ("update_doc", lambda: s.update_doc(doc, title="x")),
            ("log_project", lambda: s.log_project("uc_a", "x")),
            ("delete_github_credential", lambda: s.delete_github_credential("locked"))):
        ck(f"{name} waits for the write lock", _waits_for_write_lock(s, call))
    s.close()


def cli_edition():
    print("== tares up reads the edition from tares.sqlite ==")
    from tares import cli
    keep = os.environ.pop("TARES_STATE_DSN", None)
    try:
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.duckdb")
        s = Store(path, state_dsn="")
        s.set_setting("edition", "factory")
        s.close()
        ck("the edition saved in the session store is the one the CLI reads", cli._stored_edition(path) == "factory",
           cli._stored_edition(path))
        s = Store(path, state_dsn="")
        s.set_setting("edition", "sre")
        s.close()
        ck("and a changed one is read as changed (DuckDB's copy is not what it sees)",
           cli._stored_edition(path) == "sre", cli._stored_edition(path))
        # DuckDB's own copy says something else: the session store wins
        con = duckdb.connect(path)
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)")
        con.execute("INSERT INTO settings (key, value, updated_at) VALUES ('edition', 'platform', now())")
        con.close()
        ck("DuckDB's older copy does not win over the session store", cli._stored_edition(path) == "sre",
           cli._stored_edition(path))
        # no session store file at all: DuckDB's copy is the fallback (a cell that has not moved yet)
        d2, path2, _ = _legacy_cell()
        ck("a cell that has not moved yet answers from DuckDB", cli._stored_edition(path2) == "factory",
           cli._stored_edition(path2))
    finally:
        if keep is not None:
            os.environ["TARES_STATE_DSN"] = keep


def copy_units():
    print("== the copy's helpers ==")
    md = MetaData()
    t = Table("tmp_copy", md, Column("id", Integer, primary_key=True), Column("doc", JSONText),
              Column("note", Text), Column("later", Text))
    row = _from_duckdb_row(t, (1, '{"a": [1, null]}', "n"), ["id", "doc", "note"])
    ck("a JSONText column's text is parsed, so the insert writes it once", row["doc"] == {"a": [1, None]}, row)
    row = _from_duckdb_row(t, (2, None, "n"), ["id", "doc", "note"])
    ck("a NULL JSONText stays None", row["doc"] is None, row)
    row = _from_duckdb_row(t, (3, '"plain"', "text that is not json"), ["id", "doc", "note"])
    ck("only the JSONText column is parsed", row["note"] == "text that is not json" and row["doc"] == "plain", row)
    row = _from_duckdb_row(challenges_table, ('{"a":1,}',), ["payload"])
    ck("a challenger line's payload is copied as it is, so one that is no JSON cannot stop a start (TR-1705)",
       row["payload"] == '{"a":1,}', row)

    con = duckdb.connect()
    con.execute("CREATE TABLE tmp_copy (id INTEGER, doc TEXT, note TEXT)")      # no 'later' column

    class _Store:
        pass
    fake = _Store()
    fake.con = con
    ck("a column DuckDB lacks is left out, in the table's order",
       copy_mod._duckdb_columns(fake, t) == ["id", "doc", "note"], copy_mod._duckdb_columns(fake, t))
    ck("a table DuckDB lacks gives no columns", copy_mod._duckdb_columns(
        fake, Table("tmp_absent", MetaData(), Column("id", Integer))) == [])
    con.close()

    # the rename order: side files first, the main file last, so a stop part way is finished next start
    d = tempfile.mkdtemp()
    for suffix, body in (("", b"main"), ("-wal", b"wal frames"), ("-shm", b"shm index")):
        with open(os.path.join(d, LEGACY_SQLITE_FILE + suffix), "wb") as f:
            f.write(body)
    moves, real = [], os.replace

    class _Stop(Exception):
        pass

    def stopping(a, b):
        moves.append(os.path.basename(a))
        if a.endswith(LEGACY_SQLITE_FILE):
            raise _Stop()          # the start dies on the rename of the main file
        real(a, b)
    os.replace = stopping
    try:
        adopt_legacy_file(d)
    except _Stop:
        pass
    finally:
        os.replace = real
    ck("the -wal and -shm are renamed before the main file", moves[-1] == LEGACY_SQLITE_FILE
       and set(moves[:-1]) == {LEGACY_SQLITE_FILE + "-wal", LEGACY_SQLITE_FILE + "-shm"}, moves)
    ck("a stop before the main file leaves sessions.sqlite in place, with no tares.sqlite",
       os.path.exists(os.path.join(d, LEGACY_SQLITE_FILE)) and not os.path.exists(os.path.join(d, SQLITE_FILE)),
       sorted(os.listdir(d)))
    adopt_legacy_file(d)
    ck("the next start finishes the move: all three files under the new name, none under the old",
       sorted(os.listdir(d)) == [SQLITE_FILE, SQLITE_FILE + "-shm", SQLITE_FILE + "-wal"], sorted(os.listdir(d)))
    ck("and the bytes are as they were", [open(os.path.join(d, SQLITE_FILE + s_), "rb").read()
                                          for s_ in ("", "-wal", "-shm")] == [b"main", b"wal frames", b"shm index"])


def _from_duckdb_row(table, row, cols):
    return copy_mod._from_duckdb(table, row, cols)


def sqlite_and_pg_units():
    sdb = SessionStore(":memory:")
    takeover_unit(sdb, "SQLite")
    json_text(sdb, "SQLite")
    sdb.close()
    if PG:
        sch, dsn = _pg_schema()
        try:
            pdb = SessionStore(tempfile.mkdtemp(), dsn)
            takeover_unit(pdb, "Postgres")
            json_text(pdb, "Postgres")
            pdb.close()
        finally:
            _drop_schema(sch)


def projects_takeover():
    print("== Postgres: the project tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        seeded = old_ddl_postgres(dsn, seed={**SEED, **PROJECTS})

        def snapshot():
            out = {}
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                for t in PROJECT_TABLES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                     "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
                                     [sch, t]).fetchall()
                    rows = c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                    out[t] = (cols, sorted(tuple(map(str, r)) for r in rows))
            return out

        before = snapshot()
        ck("the old DDL made the nine tables, with the sample rows",
           {t: len(before[t][1]) for t in PROJECT_TABLES} == PROJECT_COUNTS, {t: len(before[t][1]) for t in PROJECT_TABLES})
        ck("params and config are TEXT there", dict((c[0], c[1]) for c in before["usecases"][0])["params"] == "text"
           and dict((c[0], c[1]) for c in before["catalog_sources"][0])["config"] == "text")
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its columns, types and rows, is as the old code left it", snapshot() == before)
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        with ts.capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", ts.upgrade_lines(recs) == [])
        ts.step_down(sdb, "0016_session_usage_hourly")
        ck("a downgrade to the revision before keeps the tables and their rows", snapshot() == before
           and ts.versions(sdb) == ["0016_session_usage_hourly"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it takes them over again, rows untouched", snapshot() == before)
        with sdb.begin() as c:
            demo = c.execute(projects_table.select().where(projects_table.c.id == "uc_demo")).first()
            blank = c.execute(projects_table.select().where(projects_table.c.id == "uc_blank")).first()
        ck("Core reads a project the old code wrote, JSON text parsed, null as None",
           demo.params == {"objects": [{"kind": "source", "name": "logs"}]} and demo.setup == {"step": "plan"}
           and blank.params is None and blank.setup is None, (demo, blank))
        sdb.close()
    finally:
        _drop_schema(sch)

# The ticket tables as an old cell holds them. `depends_on` and `pr` are JSON (TEXT on Postgres);
# tk_null has what an old row may lack: no position, no created_at, a JSON null.
TICKET_TABLES = ("tickets", "ticket_history", "ticket_numbers")
_TICKET_COLS = ("id", "project", "owner", "external_id", "identifier", "url", "title", "status", "position",
                "working_doc", "created_at", "updated_at", "milestone", "depends_on", "number", "holder",
                "stage", "stage_reason", "pr", "repo", "assignee_person_id", "assignee_raw")
_HISTORY_COLS = ("ticket", "project", "changed_at", "field", "value", "reason", "by_station", "by_role",
                 "person_id")
_PR = '{"repo": "o/r", "number": 7, "state": "open"}'
TICKETS = {
    "tickets": (_TICKET_COLS, [
        ("tk_a", "uc_demo", "tares", None, None, None, "A", "todo", 1.0, "d1", _T, _T, "ms1", None, 1,
         None, "review", None, _PR, "o/r", "p_kim", None),
        ("tk_b", "uc_demo", "linear", "ext-b", "ENG-2", "https://x.test/2", "B", "todo", 2.0, None, _T, _T,
         None, '["tk_a"]', 2, "ashish", "doing", "why", None, None, None, "kim@x.test"),
        ("tk_null", "uc_demo", "tares", None, None, None, "N", "done", None, None, None, None, None, "null", 3,
         None, None, None, "null", None, None, None),
    ]),
    "ticket_history": (_HISTORY_COLS, [
        # two lines alike: the table has no key, and both must arrive
        ("tk_a", "uc_demo", _T, "stage", "review", None, "s1", "builder", None),
        ("tk_a", "uc_demo", _T, "stage", "review", None, "s1", "builder", None),
        ("tk_b", "uc_demo", _T, "holder", "ashish", "why", None, None, "p_kim"),
    ]),
    "ticket_numbers": (("project", "last"), [("uc_demo", 5)]),
}
TICKET_COUNTS = {"tickets": 3, "ticket_history": 3, "ticket_numbers": 1}


def tickets_copy():
    print("== the ticket tables are copied from DuckDB at the first start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old_ddl_duckdb(con, seed={**SEED, **PROJECTS, **TICKETS})
    con.execute("CHECKPOINT")
    con.close()
    repair = Store._ticket_pr_columns
    Store._ticket_pr_columns = lambda self: None          # what the copy alone leaves behind
    try:
        s = Store(path, state_dsn="")
    finally:
        Store._ticket_pr_columns = repair
    ck("the copy itself sets pr_repo and pr_number from pr", _pr_cols(s.sessions_db)
       == {"tk_a": ("o/r", 7), "tk_b": (None, None), "tk_null": (None, None)}, _pr_cols(s.sessions_db))
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger counts each table as DuckDB held it", {t: ledger.get(t) for t in TICKET_TABLES} == TICKET_COUNTS,
       ledger)
    ck("the rows are there", {t: _n(s.sessions_db, t) for t in TICKET_TABLES} == TICKET_COUNTS)
    got = {t["id"]: t for t in s.list_tickets("uc_demo")}
    ck("JSON arrives parsed: pr a dict, depends_on a list, null as None and []",
       got["tk_a"]["pr"] == {"repo": "o/r", "number": 7, "state": "open"} and got["tk_a"]["depends_on"] == []
       and got["tk_b"]["depends_on"] == ["tk_a"] and got["tk_null"]["pr"] is None
       and got["tk_null"]["depends_on"] == [], got)
    ck("times arrive as UTC datetimes, and a missing one stays None", got["tk_a"]["created_at"] == _T
       and got["tk_null"]["created_at"] is None, (got["tk_a"]["created_at"], got["tk_null"]["created_at"]))
    ck("the other columns arrive", (got["tk_b"]["owner"], got["tk_b"]["external_id"], got["tk_b"]["identifier"],
       got["tk_b"]["holder"], got["tk_b"]["stage_reason"], got["tk_b"]["assignee_raw"], got["tk_a"]["milestone"],
       got["tk_a"]["working_doc"]) == ("linear", "ext-b", "ENG-2", "ashish", "why", "kim@x.test", "ms1", "d1"),
       got["tk_b"])
    ck("both history lines that are alike arrived", [h["value"] for h in s.ticket_history("tk_a")]
       == ["review", "review"], s.ticket_history("tk_a"))
    next_id = s.create_ticket("uc_demo", "tares", "next")
    ck("the next number follows the copied counter, not the tickets", s.get_ticket("uc_demo", next_id)["number"] == 6,
       s.get_ticket("uc_demo", next_id))
    with s._lock:
        n = [s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TICKET_TABLES]
    ck("DuckDB still has its rows: nothing is deleted there", n == [TICKET_COUNTS[t] for t in TICKET_TABLES], n)
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing: a ticket made after the copy is kept",
       s.get_ticket("uc_demo", next_id) is not None and _n(s.sessions_db, "tickets") == 4)
    s.close()

    print("== a DuckDB file from before numbers, stages and pull requests ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old = {"tickets": (("id", "project", "owner", "title", "status", "position", "created_at"), [
        ("tk_2", "uc_demo", "tares", "second", "todo", 2.0, _T),
        ("tk_1", "uc_demo", "tares", "first", "todo", 1.0, _T),
        ("tk_last", "uc_demo", "tares", "no position", "todo", None, _T),
        ("tk_o", "uc_old", "tares", "other project", "todo", 1.0, _T),
    ])}
    old_ddl_duckdb(con, seed={**SEED, **PROJECTS, **old})
    for col in ("number", "stage", "pr", "assignee_person_id", "assignee_raw"):
        con.execute(f"ALTER TABLE tickets DROP COLUMN {col}")
    con.execute("ALTER TABLE ticket_history DROP COLUMN person_id")
    con.execute("CHECKPOINT")
    con.close()
    s = Store(path, state_dsn="")
    nums = {t["id"]: t["number"] for p in ("uc_demo", "uc_old") for t in s.list_tickets(p)}
    ck("each project's tickets are numbered in plan order, one with no position last",
       nums == {"tk_1": 1, "tk_2": 2, "tk_last": 3, "tk_o": 1}, nums)
    ck("a column the old file lacks reads as None", s.get_ticket("uc_demo", "tk_1")["pr"] is None
       and s.get_ticket("uc_demo", "tk_1")["stage"] is None)
    n = s.get_ticket("uc_demo", s.create_ticket("uc_demo", "tares", "new"))["number"]
    ck("a new ticket continues after them", n == 4, n)
    s.close()


def tickets_methods():
    print("== ticket numbers ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    sdb = s.sessions_db
    s.create_project("uc_t", "custom", "T", {})
    s.create_project("uc_u", "custom", "U", {})

    def num(tid, project="uc_t"):
        return s.get_ticket(project, tid)["number"]

    a = s.create_ticket("uc_t", "tares", "A")
    b = s.create_ticket("uc_t", "linear", "B", identifier="ENG-9", external_id="ext-9", depends_on=[a],
                        working_doc="d_x")
    c_ = s.create_ticket("uc_t", "tares", "C")
    ck("tickets are numbered 1, 2, 3 at positions 1, 2, 3", [(t["number"], t["position"])
       for t in s.list_tickets("uc_t")] == [(1, 1.0), (2, 2.0), (3, 3.0)], s.list_tickets("uc_t"))
    s.delete_ticket(c_)
    c_ = s.create_ticket("uc_t", "tares", "C again")
    ck("the number of a deleted ticket is not given out again", num(c_) == 4, num(c_))
    with sdb.begin() as conn:
        conn.execute(numbers_table.delete().where(numbers_table.c.project == "uc_t"))
    e = s.create_ticket("uc_t", "tares", "E")
    ck("with no counter row, numbering continues after the highest ticket", num(e) == 5, num(e))
    with sdb.begin() as conn:
        conn.execute(numbers_table.update().where(numbers_table.c.project == "uc_t").values(last=1))
    f = s.create_ticket("uc_t", "tares", "F")
    ck("a counter behind the tickets catches up", num(f) == 6, num(f))
    for tid in (e, f):
        s.delete_ticket(tid)

    # several writers with no lock of the process between them, as two processes on one Postgres
    got, errors = [], []

    def writer():
        try:
            for _ in range(15):
                with sdb.begin() as conn:
                    got.append(s._next_ticket_number(conn, "uc_many"))
        except Exception as exc:         # reported by the check below
            errors.append(repr(exc))
    threads = [threading.Thread(target=writer) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    ck("four writers at once get sixty different numbers", not errors and sorted(got) == list(range(1, 61)),
       (errors[:2], len(got), len(set(got))))

    print("== reading, updating, adopting ==")
    ck("get_ticket by id, identifier in any case, external id, T<n> and <n>",
       {s.get_ticket("uc_t", r)["id"] for r in (b, "eng-9", "ext-9", "T2", "2", " t2 ")} == {b})
    byname = s.create_ticket("uc_t", "tares", "named 1", identifier="1")
    ck("a name is preferred to a number", s.get_ticket("uc_t", "1")["id"] == byname)
    s.delete_ticket(byname)
    ck("get_ticket of nothing is None", s.get_ticket("uc_t", "T99") is None and s.get_ticket("uc_t", None) is None
       and s.get_ticket(None, a) is None and s.get_ticket("uc_u", a) is None)
    row = s.get_ticket("uc_t", a)
    ck("a ticket reads with every column, depends_on a list and times aware",
       set(row) == set(_TICKET_COLS) and row["depends_on"] == [] and row["created_at"].tzinfo is not None, row)
    before = row["updated_at"]
    s.update_ticket(a, touch=False, title="A2", pr={"repo": "o/r", "number": 7, "at": _T})
    row = s.get_ticket("uc_t", a)
    ck("update_ticket without touch keeps updated_at; a pr's datetime is kept as text",
       row["title"] == "A2" and row["updated_at"] == before and row["pr"]["at"] == str(_T), row)
    s.update_ticket(a, stage="doing")
    s.update_ticket(a, status="done", why="marked there")
    ck("a done status clears an open stage and the history says why", s.get_ticket("uc_t", a)["stage"] is None
       and [(h["field"], h["value"], h["reason"]) for h in s.ticket_history(a)] == [("stage", None, "marked there")],
       (s.get_ticket("uc_t", a), s.ticket_history(a)))
    s.update_ticket(a, status="todo", stage="merged")
    s.update_ticket(a, status="done")
    ck("a merged stage stays", s.get_ticket("uc_t", a)["stage"] == "merged" and len(s.ticket_history(a)) == 1)
    s.update_ticket(b, depends_on=[])
    with sdb.begin() as conn:
        raw = conn.execute(text("SELECT depends_on FROM tickets WHERE id = :i"), {"i": b}).scalar()
    ck("an empty depends_on is stored as NULL", raw is None, raw)
    s.update_ticket(b, depends_on=[a])
    s.pending_writeback_fail(b, "linear", "done", "down", _T)
    ck("unless_pending leaves a ticket with a write-back waiting alone",
       s.update_ticket(b, unless_pending=True, title="changed") is False and s.get_ticket("uc_t", b)["title"] == "B")
    ck("and writes one with none", s.update_ticket(a, unless_pending=True, title="A3") is True)
    one = s.adopt_ticket("uc_u", "github", "i-1", "One", "todo", "#1", "https://x.test/1")
    two = s.adopt_ticket("uc_u", "github", "i-1", "One", "todo", "#1b", "https://x.test/1b", repo="o/u")
    ck("adopting one issue twice makes one row, which takes what is new", one == two
       and len(s.list_tickets("uc_u")) == 1 and s.get_ticket("uc_u", one)["identifier"] == "#1b"
       and s.get_ticket("uc_u", one)["repo"] == "o/u" and num(one, "uc_u") == 1, s.list_tickets("uc_u"))
    n1 = s.adopt_ticket("uc_u", "github", None, "x", "todo", "#x", "u")
    n2 = s.adopt_ticket("uc_u", "github", None, "x", "todo", "#x", "u")
    ck("no external id never matches a row", n1 != n2)
    s.update_ticket_owner(n1, "tares")
    ck("update_ticket_owner", s.get_ticket("uc_u", n1)["owner"] == "tares")

    print("== NULLs sort last, as on Postgres and DuckDB ==")
    with sdb.begin() as conn:
        conn.execute(tickets_table.update().where(tickets_table.c.id == n1).values(position=None))
        conn.execute(tickets_table.update().where(tickets_table.c.id == n2).values(repo="o/late"))
        conn.execute(tickets_table.update().where(tickets_table.c.id == n1).values(repo="o/null"))
    ck("list_tickets puts a ticket with no position last", [t["id"] for t in s.list_tickets("uc_u")] == [one, n2, n1],
       [(t["id"], t["position"]) for t in s.list_tickets("uc_u")])
    ck("tickets_by_project gives the same order, and an empty list for a project with none",
       [t["id"] for t in s.tickets_by_project(["uc_u", "uc_none"])["uc_u"]] == [one, n2, n1]
       and s.tickets_by_project(["uc_u", "uc_none"])["uc_none"] == [] and s.tickets_by_project([]) == {})
    facts = s.done_ticket_facts(["uc_u"])["uc_u"]
    ck("done_ticket_facts lists the repos in that order", facts["repos"] == ["o/u", "o/late", "o/null"]
       and facts["total"] == 3, facts)
    s.add_ticket_history(n2, "uc_u", "stage", "review", None, None, None, _T)
    s.add_ticket_history(n2, "uc_u", "holder", "x", None, None, None, _T)
    with sdb.begin() as conn:
        conn.execute(history_table.update().where(history_table.c.ticket == n2, history_table.c.field == "holder")
                     .values(changed_at=None))
    ck("ticket_history puts a line with no time last", [h["field"] for h in s.ticket_history(n2)]
       == ["stage", "holder"], s.ticket_history(n2))
    later = datetime(2026, 11, 21, tzinfo=timezone.utc)
    s.add_ticket_history(n2, "uc_u", "stage", "review", None, None, None, later)
    ck("entered_stage is the latest time, aware; a ticket that never did is absent",
       s.entered_stage([n2, n1], "review") == {n2: later} and s.entered_stage([], "review") == {},
       s.entered_stage([n2, n1], "review"))
    ck("history_between takes the lines in [since, until)", [h["at"] for h in s.history_between(_T, later)
       if h["ticket"] == n2] == [_T])

    print("== pull requests, reviews and milestones ==")
    s.update_ticket(a, pr={"repo": "O/R", "number": 7}, stage="merged")
    s.update_ticket(b, pr={"repo": "o/r", "number": 7}, stage="review")
    s.update_ticket(c_, pr={"repo": "o/r", "number": 70}, stage="doing")
    ck("ticket_for_pr matches repo in any case and the whole number, an open ticket first",
       s.ticket_for_pr("o/R", 7)["id"] == b and s.ticket_for_pr("o/r", "70")["id"] == c_
       and s.ticket_for_pr("o/r", 8) is None and s.ticket_for_pr("", 7) is None)
    ck("review_tickets gives the ticket in review with its project's name",
       [(name, t["id"]) for name, t in s.review_tickets() if t["project"] == "uc_t"] == [("T", b)])
    ms = s.create_milestone("uc_t", "M1")
    s.update_ticket(a, milestone=ms)
    ck("milestone_statuses", s.milestone_statuses([ms]) == [{"project": "uc_t", "milestone": ms, "id": a,
       "status": "done"}] and s.milestone_statuses([]) == [], s.milestone_statuses([ms]))
    # a pull request with no `repo` (and a null elsewhere in it) belongs to no repo
    nr = s.create_ticket("uc_nr", "tares", "no repo", identifier="ENG-9")
    s.update_ticket(nr, pr={"number": 1, "url": None}, stage="doing")
    ck("factory_ref_elsewhere: another project's open ticket of that name in that repo",
       s.factory_ref_elsewhere("uc_u", "o/r", "eng-9") is True
       and s.factory_ref_elsewhere("uc_t", "o/r", "ENG-9") is False
       and s.factory_ref_elsewhere("uc_u", "o/other", "ENG-9") is False
       and s.factory_ref_elsewhere("uc_u", None, "ENG-9") is False)
    finding = {"severity": "high", "file": "f.py", "line": 1, "blocking": True, "text": "a leak"}
    s.add_review("uc_t", b, None, "abc", "changes", "tests", "", "rev", [finding], {}, lambda t_: "bug", _T)
    found = s.repo_findings("o/r", datetime(2026, 1, 1, tzinfo=timezone.utc))
    ck("repo_findings joins a finding to its ticket in that repo", [(f["ticket"], f["ticket_project"],
       f["ticket_identifier"], f["ticket_number"]) for f in found] == [(b, "uc_t", "ENG-9", 2)]
       and s.repo_findings("o/other", datetime(2026, 1, 1, tzinfo=timezone.utc)) == []
       and s.repo_findings(None, datetime(2026, 1, 1, tzinfo=timezone.utc)) == [], found)
    ck("list_pending_writebacks of a project keeps its tickets' rows",
       [r["ticket"] for r in s.list_pending_writebacks(project="uc_t")] == [b]
       and s.list_pending_writebacks(project="uc_u") == []
       and [r["ticket"] for r in s.list_pending_writebacks()] == [b], s.list_pending_writebacks())

    print("== reads do not wait for the store's write lock ==")
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for name, call in (
            ("get_ticket", lambda: s.get_ticket("uc_t", b)),
            ("list_tickets", lambda: s.list_tickets("uc_t")),
            ("tickets_by_project", lambda: s.tickets_by_project(["uc_t", "uc_u"])),
            ("done_ticket_facts", lambda: s.done_ticket_facts(["uc_u"])),
            ("review_tickets", lambda: s.review_tickets()),
            ("factory_ref_elsewhere", lambda: s.factory_ref_elsewhere("uc_u", "o/r", "ENG-9")),
            ("repo_findings", lambda: s.repo_findings("o/r", since)),
            ("list_pending_writebacks", lambda: s.list_pending_writebacks(project="uc_t"))):
        ck(f"{name} does not wait for the write lock", _runs_under_the_write_lock(s, call))

    print("== writes wait for the store's write lock ==")
    for name, call in (
            ("create_ticket", lambda: s.create_ticket("uc_lock", "tares", "l")),
            ("adopt_ticket", lambda: s.adopt_ticket("uc_lock", "github", "i", "l", "todo", "#1", "u")),
            ("update_ticket", lambda: s.update_ticket(a, title="locked")),
            ("update_ticket_owner", lambda: s.update_ticket_owner(a, "tares")),
            ("add_ticket_history", lambda: s.add_ticket_history(a, "uc_t", "holder", "h", None, None, None, _T)),
            ("delete_ticket", lambda: s.delete_ticket("tk_none")),
            ("delete_milestone", lambda: s.delete_milestone("ms_none")),
            ("remove_doc", lambda: s.remove_doc("uc_t", "d_none"))):
        ck(f"{name} waits for the write lock", _waits_for_write_lock(s, call))

    print("== deletes reach both databases ==")
    s.remove_doc("uc_t", "d_x")
    ck("remove_doc clears the ticket's working doc", s.get_ticket("uc_t", b)["working_doc"] is None)
    s.delete_milestone(ms)
    ck("delete_milestone keeps the ticket, without a milestone", s.get_ticket("uc_t", a)["milestone"] is None
       and s.get_milestone("uc_t", ms) is None)
    s.delete_ticket(a)
    ck("delete_ticket takes it out of the others' dependencies", s.get_ticket("uc_t", a) is None
       and s.get_ticket("uc_t", b)["depends_on"] == [], s.get_ticket("uc_t", b))
    s.pending_writeback_fail(one, "github", "done", "down", _T)
    s.delete_project("uc_t")
    with sdb.begin() as conn:
        left = [conn.execute(text(f"SELECT count(*) FROM {t_} WHERE project = 'uc_t'")).scalar() for t_ in TICKET_TABLES]
        other = [conn.execute(text(f"SELECT count(*) FROM {t_} WHERE project = 'uc_u'")).scalar() for t_ in TICKET_TABLES]
    ck("delete_project drops its tickets, history and counter, and leaves another project's",
       left == [0, 0, 0] and other[0] == 3 and other[1] == 3 and other[2] == 1, (left, other))
    ck("and its tickets' write-backs, not another project's",
       [r["ticket"] for r in s.list_pending_writebacks()] == [one], s.list_pending_writebacks())
    s.close()


def tickets_takeover():
    print("== Postgres: the ticket tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        old_ddl_postgres(dsn, seed={**SEED, **TICKETS})
        added = ("pr_repo", "pr_number")          # by 0019

        def snapshot(leave_out=()):
            out = {}
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                for t in TICKET_TABLES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                     "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
                                     [sch, t]).fetchall()
                    cols = [col for col in cols if col[0] not in leave_out]
                    rows = c.execute(f"SELECT {', '.join(col[0] for col in cols)} FROM {t}").fetchall()
                    out[t] = (cols, sorted(tuple(map(str, r)) for r in rows))
                out["indexes"] = c.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND indexname IN "
                    "('tickets_project', 'ticket_history_ticket') ORDER BY indexname", [sch]).fetchall()
            return out

        before = snapshot()
        ck("the old DDL made the three tables, with the sample rows, and both indexes",
           {t: len(before[t][1]) for t in TICKET_TABLES} == TICKET_COUNTS and len(before["indexes"]) == 2,
           before["indexes"])
        ck("depends_on and pr are TEXT there", {c[0]: c[1] for c in before["tickets"][0]}["depends_on"] == "text"
           and {c[0]: c[1] for c in before["tickets"][0]}["pr"] == "text")
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its old columns, types, rows and indexes, is as the old code left it",
           snapshot(added) == before)
        new_cols = [col for col in snapshot()["tickets"][0] if col[0] in added]
        ck("tickets has the two new columns, nullable, and their index", new_cols
           == [("pr_number", "bigint", "YES"), ("pr_repo", "text", "YES")]
           and ts.index_columns(sdb.engine, "tickets", "tickets_pr") == list(added), new_cols)
        ck("they are filled from the pr the old code wrote", _pr_cols(sdb)
           == {"tk_a": ("o/r", 7), "tk_b": (None, None), "tk_null": (None, None)}, _pr_cols(sdb))
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        with ts.capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", ts.upgrade_lines(recs) == [])
        ts.step_down(sdb, "0017_project_tables")
        ck("a downgrade to 0017 keeps the tables, rows and indexes and drops what 0019 added", snapshot() == before
           and ts.versions(sdb) == ["0017_project_tables"]
           and ts.index_columns(sdb.engine, "tickets", "tickets_pr") is None, ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it takes them over again, rows untouched", snapshot(added) == before)
        with sdb.begin() as c:
            rows = {r.id: r for r in c.execute(tickets_table.select())}
        ck("Core reads a ticket the old code wrote", rows["tk_a"].pr == {"repo": "o/r", "number": 7, "state": "open"}
           and rows["tk_b"].depends_on == ["tk_a"] and rows["tk_null"].pr is None
           and rows["tk_null"].depends_on is None and rows["tk_a"].created_at == _T, rows)
        sdb.close()
    finally:
        _drop_schema(sch)


def _pr_cols(sdb):
    with sdb.begin() as c:
        return {r[0]: (r[1], r[2]) for r in c.execute(text("SELECT id, pr_repo, pr_number FROM tickets")).all()}


def _write_pr_as_before(sdb, tid, pr_text):
    """What a release without the two columns does: it writes `pr` and nothing else."""
    with sdb.begin() as c:
        c.execute(text("UPDATE tickets SET pr = :p WHERE id = :i"), {"p": pr_text, "i": tid})


# (name, the stored text of `pr`, what pr_repo and pr_number hold for it)
_ODD_PR = [
    ("sql_null", None, (None, None)),
    ("blank", "", (None, None)),
    ("json_null", "null", (None, None)),
    ("empty_object", "{}", (None, None)),
    ("array", '[{"repo": "o/odd", "number": 1}]', (None, None)),
    ("string", '"o/odd#1"', (None, None)),
    ("bare_number", "7", (None, None)),
    ("no_repo", '{"number": 1}', (None, 1)),
    ("empty_repo", '{"repo": "", "number": 2}', (None, 2)),
    ("null_repo", '{"repo": null, "number": 2}', (None, 2)),
    ("no_number", '{"repo": "O/Odd"}', ("o/odd", None)),
    ("null_number", '{"repo": "o/odd", "number": null}', ("o/odd", None)),
    ("number_as_text", '{"repo": "o/odd", "number": "3"}', ("o/odd", None)),
    ("true_number", '{"repo": "o/odd", "number": true}', ("o/odd", None)),
    ("fraction", '{"repo": "o/odd", "number": 4.5}', ("o/odd", None)),
    ("whole_float", '{"repo": "o/odd", "number": 5.0}', ("o/odd", 5)),
    ("zero", '{"repo": "o/odd", "number": 0}', ("o/odd", None)),
    ("negative", '{"repo": "o/odd", "number": -6}', ("o/odd", None)),
    ("past_int32", '{"repo": "o/odd", "number": 3000000000}', ("o/odd", 3000000000)),
    ("past_int64", '{"repo": "o/odd", "number": 9223372036854775808}', ("o/odd", None)),
    ("huge_float", '{"repo": "o/odd", "number": 1e300}', ("o/odd", None)),
    ("infinity", '{"repo": "o/odd", "number": Infinity}', ("o/odd", None)),
    ("nan", '{"repo": "o/odd", "number": NaN}', ("o/odd", None)),
    ("nul_in_repo", '{"repo": "o/\\u0000", "number": 8}', (None, 8)),
    ("surrogate_in_repo", '{"repo": "o/\\ud800", "number": 9}', (None, 9)),
    ("long_repo", '{"repo": "o/' + "".join(uuid.uuid4().hex for _ in range(300)) + '", "number": 10}', (None, 10)),
]
# texts nothing can read back as JSON: reading such a ticket fails (as it did before), a start must not
_NOT_JSON = [
    ("not_json", '{"repo": "o/odd", "number": 11'),
    ("too_many_digits", '{"repo": "o/odd", "number": ' + "9" * 5000 + "}"),
    ("too_deep", "[" * 100000),
]


def tickets_pr_lookup():
    print("== pr_repo and pr_number follow every write of pr ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    dsn = None if HARNESS else ""
    s = Store(path, state_dsn=dsn)
    s.create_project("uc_pr", "custom", "P", {})
    a = s.create_ticket("uc_pr", "tares", "A")
    ck("a new ticket has neither", _pr_cols(s.sessions_db)[a] == (None, None))
    s.update_ticket(a, pr={"repo": "Acme/API", "number": 12, "state": "open"})
    ck("update_ticket sets them, the repo in lower case", _pr_cols(s.sessions_db)[a] == ("acme/api", 12),
       _pr_cols(s.sessions_db)[a])
    s.update_ticket(a, title="A2", stage="review")
    ck("a write without pr leaves them", _pr_cols(s.sessions_db)[a] == ("acme/api", 12))
    found = s.ticket_for_pr(" ACME/api ", 12)
    ck("no ticket read carries them", all(set(t) == set(_TICKET_COLS) for t in (
       found, s.get_ticket("uc_pr", a), s.list_tickets("uc_pr")[0], s.tickets_by_project(["uc_pr"])["uc_pr"][0],
       s.review_tickets()[0][1])), sorted(found))
    for none in (None, {}):
        s.update_ticket(a, pr={"repo": "Acme/API", "number": 12})
        s.update_ticket(a, pr=none)
        ck(f"pr={none!r} clears them", _pr_cols(s.sessions_db)[a] == (None, None)
           and s.ticket_for_pr("acme/api", 12) is None, _pr_cols(s.sessions_db)[a])
    # a pr with only half of it: an unusable argument must not match the half that is NULL
    for half in ({"number": 12}, {"repo": "acme/api"}):
        s.update_ticket(s.create_ticket("uc_half", "tares", "half"), pr=half)
    for args in((None, 12), ("", 12), ("  ", 12), ("acme/api", None), ("acme/api", "x"), ("acme/api", 0),
                 ("acme/api", -1), ("acme/api", 10 ** 30), ("acme/api", float("inf")), ("a\x00b", 12),
                 ("a\ud800", 12), ("o" * 5000, 12)):
        try:
            got = s.ticket_for_pr(*args)
        except Exception as e:          # reported by the check
            got = repr(e)
        ck(f"ticket_for_pr{(args[0] if args[0] is None else args[0][:12], args[1])!r} is None", got is None, got)

    print("== 1,000 tickets an older release wrote are filled at the next start, and read by the index ==")
    ids = {}
    for n in range(1, 1001):
        ids[n] = s.create_ticket("uc_pr", "tares", f"t{n}")
    with s.sessions_db.begin() as c:
        c.execute(text("UPDATE tickets SET pr = :p WHERE id = :i"), [
            {"p": '{"url": null, "repo": "Acme/%s", "number": %d}' % ("web" if n % 2 else "api", n), "i": tid}
            for n, tid in ids.items()])
    ck("until then none is found", s.ticket_for_pr("acme/api", 42) is None)
    s.close()
    s = Store(path, state_dsn=dsn)

    def found_id(repo, number):
        return (s.ticket_for_pr(repo, number) or {}).get("id")
    ck("after the start each of a sample is found by its repo and number", all(
       found_id("acme/web" if n % 2 else "acme/api", n) == ids[n]
       for n in (1, 2, 4, 40, 42, 420, 421, 999, 1000)))
    ck("another repo, or a number no ticket has, finds nothing", s.ticket_for_pr("acme/web", 42) is None
       and s.ticket_for_pr("acme/api", 1002) is None)

    ran = []

    def saw(conn, cursor, statement, parameters, context, executemany):
        ran.append((statement, parameters))
    event.listen(s.sessions_db.engine, "before_cursor_execute", saw)
    s.ticket_for_pr("acme/api", 42)
    event.remove(s.sessions_db.engine, "before_cursor_execute", saw)
    query, params = next(r for r in ran if "FROM tickets" in r[0])
    with s.sessions_db.begin() as c:
        if s.sessions_db.dialect == "sqlite":
            plan = [r[-1] for r in c.exec_driver_sql("EXPLAIN QUERY PLAN " + query, params).all()]
        else:
            c.execute(text("ANALYZE tickets"))
            plan = [r[0] for r in c.exec_driver_sql("EXPLAIN " + query, params).all()]
    ck("the query ticket_for_pr runs searches the index tickets_pr", any("tickets_pr" in line for line in plan)
       and "LIKE" not in query, (plan, query))

    print("== stale columns are repaired at start ==")
    moved, gone, new = ids[2], ids[4], a
    _write_pr_as_before(s.sessions_db, moved, '{"repo": "acme/api", "number": 5000}')    # another pr
    _write_pr_as_before(s.sessions_db, gone, None)                                       # unlinked
    _write_pr_as_before(s.sessions_db, new, '{"repo": "acme/api", "number": 5001}')      # linked
    ck("before the start the columns still say the old thing", _pr_cols(s.sessions_db)[moved] == ("acme/api", 2)
       and _pr_cols(s.sessions_db)[gone] == ("acme/api", 4) and _pr_cols(s.sessions_db)[new] == (None, None))
    s.close()
    s = Store(path, state_dsn=dsn)
    cols = _pr_cols(s.sessions_db)
    ck("after it they say what pr says, a NULL pr included", (cols[moved], cols[gone], cols[new])
       == (("acme/api", 5000), (None, None), ("acme/api", 5001)), (cols[moved], cols[gone], cols[new]))
    ck("and the lookup follows", found_id("acme/api", 5000) == moved
       and found_id("acme/api", 2) is None and found_id("acme/api", 4) is None
       and found_id("acme/api", 5001) == new)
    with s.sessions_db.begin() as c:
        ck("a start with nothing stale writes no row", fill_pr_columns(c) == 0)

    print("== odd values of pr never stop a start ==")
    odd = {name: s.create_ticket("uc_odd", "tares", name) for name, _, _ in _ODD_PR}
    bad = {name: s.create_ticket("uc_bad", "tares", name) for name, _ in _NOT_JSON}
    for name, pr_text, _ in _ODD_PR:
        _write_pr_as_before(s.sessions_db, odd[name], pr_text)
    for name, pr_text in _NOT_JSON:
        s.update_ticket(bad[name], pr={"repo": "o/was", "number": 1})                     # columns now stale
        _write_pr_as_before(s.sessions_db, bad[name], pr_text)
    s.close()
    try:
        s = Store(path, state_dsn=dsn)
    except Exception as e:
        ck("the store starts", False, repr(e)[:500])
        return
    cols = _pr_cols(s.sessions_db)
    wrong = {name: cols[odd[name]] for name, _, want in _ODD_PR if cols[odd[name]] != want}
    ck("each reads into the columns as expected", not wrong, wrong)
    wrong = {name: cols[bad[name]] for name, _ in _NOT_JSON if cols[bad[name]] != (None, None)}
    ck("a text that is not JSON has NULL in both", not wrong, wrong)
    ck("the lookup finds the whole float and the number past int32, and no other odd one",
       found_id("o/odd", 5) == odd["whole_float"]
       and found_id("o/odd", 3000000000) == odd["past_int32"]
       and all(found_id("o/odd", n) is None for n in (1, 3, 4, 6, 7, 11)))
    # the writer reads a value the same way (what JSON cannot hold in a column stays in pr alone)
    for pr, want in (({"repo": "o/\x00", "number": 3}, (None, 3)), ({"repo": "o/w", "number": "3"}, ("o/w", None)),
                     ({"repo": "o/w", "number": 10 ** 30}, ("o/w", None)), (["o/w", 3], (None, None))):
        s.update_ticket(a, pr=pr)
        ck(f"update_ticket takes pr={pr!r}", _pr_cols(s.sessions_db)[a] == want
           and s.get_ticket("uc_pr", a)["pr"] == pr, _pr_cols(s.sessions_db)[a])
    s.close()


def tickets_pr_revision():
    from tares.session_store.tables import tickets as declared
    new = ["pr_repo", "pr_number"]
    for label, make in (("SQLite", None), ("Postgres", _pg_schema)):
        print(f"== 0019 up and down on {label} ==")
        if make and not PG:
            print("  skip  TARES_TEST_PG is not set")
            continue
        sch, dsn = make() if make else (None, None)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        try:
            sdb.upgrade()

            def columns():
                return [c["name"] for c in inspect(sdb.engine).get_columns("tickets")]
            ck("at head tickets has the columns tables.py declares, and the index", columns()
               == [c.name for c in declared.columns]
               and ts.index_columns(sdb.engine, "tickets", "tickets_pr") == new, columns())
            ts.step_down(sdb, "0018_ticket_tables")
            ck("a downgrade drops the two columns and the index and keeps the rest", columns() == list(_TICKET_COLS)
               and ts.index_columns(sdb.engine, "tickets", "tickets_pr") is None
               and ts.index_columns(sdb.engine, "tickets", "tickets_project") == ["project"], columns())
            with sdb.begin() as c:
                c.execute(text("INSERT INTO tickets (id, project, pr) VALUES (:i, 'p', :pr)"), [
                    {"i": "tk_pr", "pr": '{"repo": "O/R", "number": 7}'}, {"i": "tk_no", "pr": None},
                    {"i": "tk_bad", "pr": "{oops"}])
            sdb.upgrade()
            ck("the upgrade fills them from the rows there are", _pr_cols(sdb)
               == {"tk_pr": ("o/r", 7), "tk_no": (None, None), "tk_bad": (None, None)}
               and ts.versions(sdb) == [ts.HEAD], _pr_cols(sdb))
            with sdb.begin() as c:
                kept = dict(c.execute(text("SELECT id, pr FROM tickets")).all())
            ck("and leaves pr as it was", kept == {"tk_pr": '{"repo": "O/R", "number": 7}', "tk_no": None,
                                                    "tk_bad": "{oops"}, kept)
            # a column or the index left by a run that stopped half way does not stop the next one
            with sdb.begin() as c:
                c.execute(text("UPDATE session_store_version SET version_num = '0018_ticket_tables'"))
            sdb.upgrade()
            ck("a second run over columns that are there stamps head", ts.versions(sdb) == [ts.HEAD])
        finally:
            sdb.close()
            if sch:
                _drop_schema(sch)


REVIEW_TABLES = ["milestones", "milestone_check_results", "milestone_acceptances", "ticket_checks",
                 "challenges", "reviews", "review_findings", "rule_proposals"]
_LATER = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
REVIEWS = {
    "milestones": (("id", "project", "owner", "external_id", "name", "goal", "checks", "position",
                    "created_at", "updated_at"), [
        ("ms_b", "uc_demo", "tares", None, "Second", "", None, 2.0, _T, _T),
        ("ms_a", "uc_demo", "linear", "ext-ms", "Éclair", "ship it",
         '[{"text": "p95 under 1s", "kind": "command"}]', 1.0, _T, _T),
        ("ms_null", "uc_demo", "tares", None, None, None, "null", None, None, None),
    ]),
    "milestone_check_results": (("id", "project", "milestone", "idx", "result", "note", "by_kind",
                                 "by_name", "person_id", "checked_at"), [
        ("mc_1", "uc_demo", "ms_a", 0, "fail", "slow", "station", "builder", None, _T),
        ("mc_2", "uc_demo", "ms_a", 0, "pass", None, "person", None, "p_kim", _LATER),
    ]),
    "milestone_acceptances": (("milestone", "project", "by_name", "person_id", "accepted_at"),
                              [("ms_a", "uc_demo", None, "p_kim", _T)]),
    "ticket_checks": (("id", "project", "ticket", "command", "result", "commit_sha", "broke_test",
                       "by_station", "by_session", "checked_at", "person_id"), [
        ("ck_1", "uc_demo", "tk_a", "make test", "pass", None, "", "builder", None, _T, None),
    ]),
    "challenges": (("source", "key_value", "event_time", "event_type", "branch", "tares_project",
                    "payload"), [
        ("claude_code", "s1", _T, "challenge_commit", "factory/ENG-1", "uc_demo",
         '{"challenge": {"verdict": "PASS"}}'),
        ("claude_code", "", _T, "challenge_plan", "", None, None),
    ]),
    "reviews": (("id", "project", "ticket", "round", "pr", "head", "verdict", "verified",
                 "not_verified", "by_station", "reviewed_at", "person_id"), [
        ("rv_1", "uc_demo", "tk_a", 1, None, "abc", "changes", "tests", None, "reviewer", _T, None),
    ]),
    "review_findings": (("id", "project", "ticket", "review", "number", "severity", "file", "line",
                         "blocking", "text", "kind", "state", "fixed_in", "found_at"), [
        ("rf_1", "uc_demo", "tk_a", "rv_1", 1, "P1", None, None, None, "no test", "tests", "open",
         None, _T),
    ]),
    "rule_proposals": (("id", "repo", "kind", "project", "scope", "text", "tickets", "state",
                        "created_at", "decided_at"), [
        ("rp_1", "o/r", "tests", "uc_demo", "project", "Write the test", '["ENG-1"]', "open", _T,
         None),
        ("rp_2", "o/r", "docs", None, "repo", "", None, "dismissed", _T, _LATER),
    ]),
}
REVIEW_COUNTS = {t: len(rows) for t, (_, rows) in REVIEWS.items()}


def reviews_copy():
    print("== the milestone, check and review tables are copied from DuckDB at the first start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old_ddl_duckdb(con, seed={**SEED, **PROJECTS, **TICKETS, **REVIEWS})
    # a file from before TR-436: no person on a check or a review
    con.execute("ALTER TABLE reviews DROP COLUMN person_id")
    con.execute("CHECKPOINT")
    con.close()
    s = Store(path, state_dsn="")
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger counts each table as DuckDB held it", {t: ledger.get(t) for t in REVIEW_TABLES} == REVIEW_COUNTS,
       ledger)
    ck("the rows are there", {t: _n(s.sessions_db, t) for t in REVIEW_TABLES} == REVIEW_COUNTS)
    ms = s.list_milestones("uc_demo")
    ck("milestones come in position order, one with no position last, checks parsed, null as []",
       [(m["id"], m["goal"], m["checks"]) for m in ms]
       == [("ms_a", "ship it", [{"text": "p95 under 1s", "kind": "command"}]), ("ms_b", "", []),
           ("ms_null", "", [])], ms)
    ck("times arrive as UTC datetimes, a missing one as None", ms[0]["created_at"] == _T
       and ms[2]["created_at"] is None and ms[2]["name"] is None, ms)
    res = s.milestone_check_results(["ms_a", "ms_b"])
    ck("the latest result of a check stands", res == {"ms_a": {0: {"result": "pass", "note": None,
       "by_kind": "person", "by_name": None, "person_id": "p_kim", "at": _LATER}}}, res)
    ck("the acceptance arrives", s.milestone_acceptances(["ms_a"])
       == {"ms_a": {"by": None, "person_id": "p_kim", "at": _T}}, s.milestone_acceptances(["ms_a"]))
    chk = s.ticket_checks("tk_a")
    ck("the check arrives, null commit and empty broke_test kept", (chk[0]["commit"], chk[0]["broke_test"],
       chk[0]["at"]) == (None, "", _T), chk)
    rv = s.ticket_reviews("tk_a")
    ck("the review and its finding arrive; a column the old file lacks is None, a null flag is False",
       rv[0]["pr"] is None and rv[0]["person"] is None and rv[0]["findings"][0]["blocking"] is False
       and rv[0]["findings"][0]["label"] == "F1", rv)
    ev = s.challenge_events("claude_code", "uc_demo", "ENG-1")
    ck("the challenger's line arrives with its payload parsed",
       [(e["type"], e["session"], e["challenge"], e["at"]) for e in ev]
       == [("challenge_commit", "s1", {"verdict": "PASS"}, _T)], ev)
    props = {p["id"]: p for p in s.list_rule_proposals(None)}
    ck("rule proposals arrive, tickets parsed and null as []", props["rp_1"]["tickets"] == ["ENG-1"]
       and props["rp_2"]["tickets"] == [] and props["rp_2"]["decided_at"] == _LATER, props)
    with s._lock:
        n = [s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in REVIEW_TABLES]
    ck("DuckDB still has its rows: nothing is deleted there", n == [REVIEW_COUNTS[t] for t in REVIEW_TABLES], n)
    later = s.create_milestone("uc_demo", "after the copy")
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing: a milestone made after the copy is kept",
       s.get_milestone("uc_demo", later) is not None and _n(s.sessions_db, "milestones") == 4)
    s.close()


def _finding(text_, severity="P2", blocking=False, file=None, line=None):
    return {"severity": severity, "file": file, "line": line, "blocking": blocking, "text": text_}


def reviews_methods():
    print("== milestones ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    sdb = s.sessions_db
    s.create_project("uc_t", "custom", "Tee", {})
    s.create_project("uc_u", "custom", "U", {})
    base = {t: _n(sdb, t) for t in REVIEW_TABLES}
    m1 = s.create_milestone("uc_t", "First", "goal one", [{"text": "a"}])
    m2 = s.create_milestone("uc_t", "Éclair")
    m0 = s.create_milestone("uc_t", "Zero", position=0.5)
    ms = s.list_milestones("uc_t")
    ck("a milestone without a position goes after the last, one with a position where it says",
       [(m["id"], m["position"]) for m in ms] == [(m0, 0.5), (m1, 1.0), (m2, 2.0)], ms)
    ck("each row has every column, goal '' and checks [] when none were given",
       sorted(ms[0]) == sorted(REVIEWS["milestones"][0]) and ms[2]["goal"] == "" and ms[2]["checks"] == []
       and ms[2]["owner"] == "tares" and ms[2]["external_id"] is None, ms)
    ck("get_milestone by id and by name in any case, non-ASCII too",
       s.get_milestone("uc_t", m2)["id"] == m2 and s.get_milestone("uc_t", " éCLAIR ")["id"] == m2)
    ck("get_milestone: another project, blank and unknown give None", s.get_milestone("uc_u", m1) is None
       and s.get_milestone("uc_t", "  ") is None and s.get_milestone("uc_t", None) is None
       and s.get_milestone("uc_t", "nope") is None)
    got = s.adopt_milestone("uc_t", "linear", "L-1", "éclair", "from linear")
    row = s.get_milestone("uc_t", "L-1")
    ck("adopt takes the Tares milestone of the same name (any case), keeping its name and checks",
       got == m2 and (row["owner"], row["external_id"], row["name"], row["goal"], row["checks"], row["position"])
       == ("linear", "L-1", "Éclair", "from linear", [], 2.0), row)
    again = s.adopt_milestone("uc_t", "linear", "L-1", "renamed there", None, [{"text": "c"}], 3, "unused")
    row = s.get_milestone("uc_t", "L-1")
    ck("adopt finds it by owner and external id; None leaves the goal, checks and position given change",
       again == m2 and (row["goal"], row["checks"], row["position"]) == ("from linear", [{"text": "c"}], 3.0), row)
    new = s.adopt_milestone("uc_t", "github", "G-9", "New one", None, new_goal="from the tracker")
    ck("adopt makes a new one with new_goal", s.get_milestone("uc_t", new)["goal"] == "from the tracker"
       and s.get_milestone("uc_t", new)["position"] == 4.0, s.get_milestone("uc_t", new))
    none_a = s.adopt_milestone("uc_u", "linear", None, "N1", "g")
    none_b = s.adopt_milestone("uc_u", "linear", None, "N2", "g")
    ck("no external id matches no row by external id", none_a != none_b, (none_a, none_b))
    before = s.get_milestone("uc_t", m1)["updated_at"]
    s.update_milestone(m1, name="First!", checks=None, nonsense="x", position=9)
    row = s.get_milestone("uc_t", m1)
    ck("update_milestone sets the known columns, None checks as [], and moves updated_at",
       (row["name"], row["checks"], row["position"]) == ("First!", [], 9.0) and row["updated_at"] > before, row)
    by = s.milestones_by_project(["uc_t", "uc_u", "uc_none"])
    ck("milestones_by_project: every asked project, in order", [m["id"] for m in by["uc_t"]] == [m0, m2, new, m1]
       and [m["id"] for m in by["uc_u"]] == [none_a, none_b] and by["uc_none"] == [], by)
    ck("empty lists ask for nothing", s.milestones_by_project([]) == {} and s.milestone_check_results([]) == {}
       and s.milestone_acceptances([]) == {})
    s.record_milestone_check("uc_t", m1, 0, "fail", "", "station", "", "", _T)
    s.record_milestone_check("uc_t", m1, 0, "pass", "ok", "person", "Kim", "p_kim", _LATER)
    s.record_milestone_check("uc_t", m1, 1, "fail", None, "station", "builder", None, _T)
    res = s.milestone_check_results([m1, m2])
    ck("check results: the latest per index, empty strings stored as None",
       res == {m1: {0: {"result": "pass", "note": "ok", "by_kind": "person", "by_name": "Kim",
                        "person_id": "p_kim", "at": _LATER},
                    1: {"result": "fail", "note": None, "by_kind": "station", "by_name": "builder",
                        "person_id": None, "at": _T}}}, res)
    s.accept_milestone("uc_t", m1, "Kim", "", _T)
    s.accept_milestone("uc_t", m1, "", "p_kim", _LATER)
    ck("accepting again replaces the acceptance", s.milestone_acceptances([m1, m2])
       == {m1: {"by": None, "person_id": "p_kim", "at": _LATER}}, s.milestone_acceptances([m1]))
    tid = s.create_ticket("uc_t", "tares", "T1", milestone=m1)
    s.delete_milestone(m1)
    ck("delete_milestone removes it and leaves its ticket without one", s.get_milestone("uc_t", m1) is None
       and s.get_ticket("uc_t", tid)["milestone"] is None)

    print("== ticket checks ==")
    kim = s.create_person("Kim")
    ids = [s.add_check("uc_t", tid, f"cmd {i}", "pass", "" if i else "abc", "", "builder", "sess", at,
                       person_id=kim["id"] if i == 2 else None)
           for i, at in enumerate([_T, _LATER, datetime(2026, 11, 22, tzinfo=timezone.utc)])]
    chk = s.ticket_checks(tid)
    ck("checks come oldest first, with commit, by, at and the person", [(c["id"], c["command"]) for c in chk]
       == [(ids[0], "cmd 0"), (ids[1], "cmd 1"), (ids[2], "cmd 2")] and chk[0]["commit"] == "abc"
       and chk[1]["commit"] is None and chk[1]["broke_test"] is None and chk[1]["by"] == "builder"
       and chk[2]["person"]["name"] == "Kim" and chk[0]["person"] is None, chk)
    ck("a limit keeps the newest", [c["id"] for c in s.ticket_checks(tid, limit=2)] == ids[1:])

    print("== reviews and findings ==")
    s.update_ticket(tid, pr={"repo": "o/r", "number": 3})
    other = s.create_ticket("uc_t", "linear", "T2", identifier="ENG-2")
    s.update_ticket(other, pr={"repo": "o/other", "number": 4})
    kind = lambda t_: "k:" + t_
    one = s.add_review("uc_t", tid, "u", "h1", "changes", "tests", "", "reviewer",
                       [_finding("one", blocking=True, file="a.py", line=3), _finding("two", file="", line=0)],
                       {}, kind, _T)
    first = s.ticket_reviews(tid)[0]
    ck("round 1: findings numbered F1, F2, empty values as None",
       one["round"] == 1 and first["not_verified"] is None
       and [(f["label"], f["blocking"], f["file"], f["line"], f["state"], f["kind"]) for f in first["findings"]]
       == [("F1", True, "a.py", 3, "open", "k:one"), ("F2", False, None, None, "open", "k:two")], first)
    two_id = first["findings"][1]["id"]
    two = s.add_review("uc_t", tid, "u", "h2", "changes", "tests", "perf", "reviewer",
                       [_finding("three")], {"F1": "fixed", two_id: "open", "nonsense": "fixed"}, kind, _LATER,
                       person_id=kim["id"])
    revs = s.ticket_reviews(tid)
    ck("round 2 marks F1 fixed in it, F2 stays open, the new one is F3",
       two["round"] == 2 and revs[1]["fixed"] == ["F1"] and [f["label"] for f in revs[1]["findings"]] == ["F3"]
       and [(f["label"], f["state"], f["fixed_in"]) for f in revs[0]["findings"]]
       == [("F1", "fixed", two["id"]), ("F2", "open", None)] and revs[1]["person"]["name"] == "Kim", revs)
    ck("open_findings is the open ones of every round", [f["label"] for f in s.open_findings(tid)] == ["F2", "F3"])
    s.add_review("uc_t", tid, "u", "h2", "pass", "queue", "", "merge-queue", [], {}, kind, _LATER)
    last = s.last_reviews([tid, other, "tk_none"])
    ck("a merge queue pass over a reviewer's changes leaves the reviewer's round standing",
       list(last) == [tid] and last[tid]["id"] == two["id"] and last[tid]["fixed"] == ["F1"]
       and [f["label"] for f in last[tid]["findings"]] == ["F3"] and "ticket" not in last[tid], last)
    ck("no tickets, no read", s.last_reviews([]) == {})
    s.add_review("uc_t", other, None, "h", "changes", "t", "", "reviewer", [_finding("elsewhere")], {}, kind, _T)
    found = s.repo_findings("o/r", _T)
    ck("repo_findings: this repo's tickets only, oldest first, with the ticket's label parts",
       [(f["text"], f["ticket"], f["ticket_project"], f["ticket_number"], f["at"]) for f in found]
       == [("one", tid, "uc_t", 1, _T), ("two", tid, "uc_t", 1, _T), ("three", tid, "uc_t", 1, _LATER)], found)
    ck("repo_findings honours since, and no repo gives none", [f["text"] for f in s.repo_findings("o/r", _LATER)]
       == ["three"] and s.repo_findings(None, _T) == []
       and s.repo_findings("o/r", datetime(2027, 1, 1, tzinfo=timezone.utc)) == [])

    print("== which tickets are a repository's (TR-1705) ==")
    # found through the index of pr_repo, which is lower case: the pr's own text still decides
    made = []
    for i, pr in enumerate([{"repo": "pin/Repo", "number": 1}, {"repo": "pin/repo", "number": 2},
                            {"repo": "pin/repo2", "number": 3}, {"number": 4}, {"repo": "", "number": 5},
                            None, {"repo": "pin/Ünï", "number": 7}, None, None]):
        made.append(s.create_ticket("uc_pin", "tares", f"pin {i}"))
        if pr is not None:
            s.update_ticket(made[i], pr=pr)
        s.add_review("uc_pin", made[i], None, "h", "changes", "t", "", "reviewer", [_finding(f"pin {i}")],
                     {}, kind, _T)
    with sdb.begin() as c:
        # a pr that is no object, and one written without the columns, as an earlier release did
        c.execute(text("UPDATE tickets SET pr = '[1]' WHERE id = :i"), {"i": made[7]})
        c.execute(text("UPDATE tickets SET pr = :p WHERE id = :i"),
                  {"i": made[8], "p": '{"repo": "pin/before", "number": 9}'})
    got = lambda repo: sorted(f["text"] for f in s.repo_findings(repo, _T))
    said_ = {r: got(r) for r in ("pin/Repo", "pin/repo", "PIN/REPO", "pin/Ünï", "pin/ünï", "pin", "pin/%")}
    ck("a repository is the pr's text exactly, case and all", said_ == {
        "pin/Repo": ["pin 0"], "pin/repo": ["pin 1"], "PIN/REPO": [], "pin/Ünï": ["pin 6"], "pin/ünï": [],
        "pin": [], "pin/%": []}, said_)
    ck("an empty name is the pr that says one: not a pr with no repo, no pr or no object",
       got("") == ["pin 4"], got(""))
    s._ticket_pr_columns()                       # what every start does
    ck("a pr written without the columns is found after the next start's fill",
       got("pin/before") == ["pin 8"], got("pin/before"))
    if sdb.dialect == "sqlite":
        asked = []

        def keep(conn, cursor, statement, parameters, context, executemany):
            if "FROM tickets" in statement:
                asked.append((statement, parameters))

        event.listen(sdb.engine, "before_cursor_execute", keep)
        s.repo_findings("o/r", _T)
        event.remove(sdb.engine, "before_cursor_execute", keep)
        with sdb.engine.connect() as conn:
            plan = [r[3] for st, pr_ in asked for r in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + st, pr_)]
        ck("the repository's tickets are found through the index tickets_pr",
           len(plan) == 1 and "USING INDEX tickets_pr (pr_repo=?)" in plan[0], plan)
    n = (_n(sdb, "reviews"), _n(sdb, "review_findings"))
    try:
        s.add_review("uc_t", tid, "u", "h3", "changes", "t", "", "reviewer",
                     [_finding("fine"), {"text": "no severity"}], {"F2": "fixed"}, kind, _LATER)
        ck("a bad finding raises", False)
    except KeyError:
        ck("a round that fails half way stores nothing and marks nothing",
           (_n(sdb, "reviews"), _n(sdb, "review_findings")) == n
           and [f["label"] for f in s.open_findings(tid)] == ["F2", "F3"], s.open_findings(tid))
    ck("add_review and add_check wait for the process's write lock", _waits_for_write_lock(s, lambda: s.add_check(
       "uc_u", "tk_u", "c", "pass", "", "", "b", "", _T)) and _waits_for_write_lock(s, lambda: s.add_review(
       "uc_u", "tk_u", None, "h", "pass", "t", "", "reviewer", [_finding("u")], {}, kind, _T)))
    if sdb.dialect == "sqlite":
        ck("the reads do not queue behind a writer", all(_runs_under_the_write_lock(s, call) for call in (
           lambda: s.list_milestones("uc_t"), lambda: s.milestones_by_project(["uc_t"]),
           lambda: s.milestone_check_results([m2]), lambda: s.milestone_acceptances([m2]),
           lambda: s.ticket_checks(tid), lambda: s.ticket_reviews(tid), lambda: s.last_reviews([tid]),
           lambda: s.repo_findings("o/r", _T), lambda: s.challenge_events("claude_code", "uc_t", "X"),
           lambda: s.list_rule_proposals(), lambda: s.get_rule_proposal("rp_x"))))

    print("== challenger lines ==")
    line = ("claude_code", "sess-a", _T, "challenge_commit", "factory/ENG-2", "TEE",
            '{"challenge": {"verdict": "FAIL"}}')
    lines = [line,
             ("claude_code", "sess-b", _LATER, "challenge_waived", "factory/ENG-2-fix", None, '{"challenge": 3}'),
             ("claude_code", "sess-c", _T, "challenge_plan", "factory/ENG-20", "uc_t", None),
             ("claude_code", "sess-d", _T, "challenge_plan", "factory/ENG-2", "uc_u", "{}")]
    s._record_challenges(lines)
    s._record_challenges(lines[:2])
    ck("a line already kept is skipped", _n(sdb, "challenges") == base["challenges"] + 4)
    ev = s.challenge_events("claude_code", "uc_t", "ENG-2")
    ck("stamped with the project's name counts; the unstamped one of an untied session, the longer ref and "
       "another project's do not", [(e["session"], e["challenge"], e["at"]) for e in ev]
       == [("sess-a", {"verdict": "FAIL"}, _T)], ev)
    s.link_session("uc_t", "sess-b")
    ev = s.challenge_events("claude_code", "uc_t", "ENG-2")
    ck("once its session is tied, oldest first; a challenge that is no object reads as {}",
       [(e["session"], e["type"], e["challenge"]) for e in ev]
       == [("sess-a", "challenge_commit", {"verdict": "FAIL"}), ("sess-b", "challenge_waived", {})], ev)
    many = s.challenge_events_many("claude_code", [("uc_t", "ENG-2"), ("uc_u", "ENG-2"), ("uc_t", "NONE")])
    ck("challenge_events_many gives each pair its own", len(many[("uc_t", "ENG-2")]) == 2
       and [e["session"] for e in many[("uc_u", "ENG-2")]] == ["sess-d"] and many[("uc_t", "NONE")] == [], many)
    ck("another source has none", s.challenge_events("other", "uc_t", "ENG-2") == [])

    print("== rule proposals ==")
    a = s.upsert_rule_proposal("o/r", "tests", "uc_t", "project", "Write it", ["ENG-1"], _LATER)
    b = s.upsert_rule_proposal("o/r", "docs", None, "repo", "Say it", [], _T)
    ck("a new offer is open, with every column", sorted(a) == sorted(REVIEWS["rule_proposals"][0])
       and (a["state"], a["tickets"], a["created_at"], a["decided_at"]) == ("open", ["ENG-1"], _LATER, None)
       and b["tickets"] == [], (a, b))
    c_ = s.upsert_rule_proposal("o/r", "tests", "uc_u", "repo", "Write them", ["ENG-1", "ENG-2"], _LATER)
    ck("offering it again refreshes the open one and keeps when it was made", c_["id"] == a["id"]
       and (c_["tickets"], c_["scope"], c_["project"], c_["text"], c_["created_at"])
       == (["ENG-1", "ENG-2"], "repo", "uc_u", "Write them", _LATER), c_)
    ck("list_rule_proposals: open ones, oldest first", [p["id"] for p in s.list_rule_proposals()] == [b["id"], a["id"]])
    s.decide_rule_proposal(a["id"], "accepted", None, _LATER)
    s.decide_rule_proposal(b["id"], "dismissed", "Reworded", _LATER)
    ck("deciding without text keeps it, with text replaces it", s.get_rule_proposal(a["id"])["text"] == "Write them"
       and s.get_rule_proposal(b["id"])["text"] == "Reworded"
       and s.get_rule_proposal(a["id"])["decided_at"] == _LATER)
    ck("a decided rule is never offered again", s.upsert_rule_proposal(
       "o/r", "tests", "uc_t", "project", "again", [], _LATER) is None)
    ck("by state, all of them, and an unknown id", [p["id"] for p in s.list_rule_proposals("accepted")] == [a["id"]]
       and s.list_rule_proposals() == [] and len(s.list_rule_proposals(None)) == 2
       and s.get_rule_proposal("rp_none") is None)

    print("== deleting a project ==")
    s.record_milestone_check("uc_t", m2, 0, "pass", "", "person", "", "", _T)
    s.accept_milestone("uc_t", m2, "Kim", None, _T)
    s.delete_project("uc_t")
    mine = {}
    for t in ("milestones", "milestone_check_results", "milestone_acceptances", "ticket_checks", "reviews",
              "review_findings"):
        with sdb.begin() as conn:
            mine[t] = conn.execute(text(f"SELECT count(*) FROM {t} WHERE project = 'uc_t'")).scalar()
    ck("its milestones, results, acceptances, checks, reviews and findings go", set(mine.values()) == {0}, mine)
    ck("another project's stay", len(s.list_milestones("uc_u")) == 2 and len(s.ticket_checks("tk_u")) == 1
       and len(s.ticket_reviews("tk_u")) == 1 and len(s.open_findings("tk_u")) == 1)
    ck("challenger lines and rule proposals are kept, as before", _n(sdb, "challenges") == base["challenges"] + 4
       and len(s.list_rule_proposals(None)) == 2)
    with s._lock:
        have = set(s._tables_of(s.con))
    ck("DuckDB has none of the eight tables", not (have & set(REVIEW_TABLES)), sorted(have & set(REVIEW_TABLES)))
    s.close()


def reviews_takeover():
    print("== Postgres: the eight tables the old DDL made are taken over ==")
    import ast
    from tares import session_store
    rev = os.path.join(os.path.dirname(session_store.__file__), "migrations", "versions",
                       "0020_review_tables.py")
    imported = [(getattr(n, "module", None) or "") + "|" + ",".join(a.name for a in n.names)
                for n in ast.walk(ast.parse(open(rev).read())) if isinstance(n, (ast.Import, ast.ImportFrom))]
    ck("revision 0020 imports nothing from tares", not [i for i in imported if "tares" in i or i.startswith(".")]
       and len(imported) >= 3, imported)
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        old_ddl_postgres(dsn, seed={**SEED, **REVIEWS})

        def snapshot():
            out = {}
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                for t in REVIEW_TABLES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                     "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
                                     [sch, t]).fetchall()
                    rows = c.execute(f"SELECT {', '.join(col[0] for col in cols)} FROM {t}").fetchall()
                    out[t] = (cols, sorted(tuple(map(str, r)) for r in rows))
                out["indexes"] = c.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND "
                    "indexname = 'ix_challenges_branch'", [sch]).fetchall()
            return out

        before = snapshot()
        ck("the old DDL made the eight tables, with the sample rows, and the index",
           {t: len(before[t][1]) for t in REVIEW_TABLES} == REVIEW_COUNTS and len(before["indexes"]) == 1,
           before["indexes"])
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its columns, types, rows and index, is as the old code left it", snapshot() == before,
           [t for t in before if snapshot()[t] != before[t]])
        ck("each table's columns, types and primary key are what tables.py declares",
           ts.shape(sdb.engine, REVIEW_TABLES) == {t: ts.declared(sdb.engine)[t] for t in REVIEW_TABLES},
           ts.shape(sdb.engine, REVIEW_TABLES))
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        ts.step_down(sdb, "0019_ticket_pr_columns")
        ck("a downgrade keeps the tables, rows and index", snapshot() == before
           and ts.versions(sdb) == ["0019_ticket_pr_columns"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it leaves them as they are", snapshot() == before)
        with sdb.begin() as c:
            ms = {r.id: r.checks for r in c.execute(milestones_table.select())}
        ck("Core reads the JSON the old code wrote", ms["ms_a"][0]["kind"] == "command" and ms["ms_b"] is None
           and ms["ms_null"] is None, ms)

        # a database from before the person columns and the index
        ts.step_down(sdb, "0019_ticket_pr_columns")
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("ALTER TABLE reviews DROP COLUMN person_id")
            c.execute("ALTER TABLE ticket_checks DROP COLUMN person_id")
            c.execute("DROP INDEX ix_challenges_branch")
        sdb.upgrade()
        ck("a missing column and the missing index are added", ts.shape(sdb.engine, REVIEW_TABLES)
           == {t: ts.declared(sdb.engine)[t] for t in REVIEW_TABLES}
           and ts.index_columns(sdb.engine, "challenges", "ix_challenges_branch") == ["source", "branch"])
        ts.step_down(sdb, "0019_ticket_pr_columns")
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("ALTER TABLE rule_proposals ADD COLUMN surprise TEXT")
        try:
            sdb.upgrade()
            ck("a column the revision does not define stops the upgrade", False, ts.versions(sdb))
        except Exception as e:
            ck("a column the revision does not define stops the upgrade", "rule_proposals" in str(e)
               and "surprise" in str(e) and ts.versions(sdb) == ["0019_ticket_pr_columns"], e)
        sdb.close()
    finally:
        _drop_schema(sch)


def _fill_with_writes_between(sdb, fill, writes):
    """Run `fill`, and after it has read the rows and before it writes, write `pr` of the rows in
    `writes` as another writer would. On Postgres that is a transaction of its own that commits
    while the fill's is open. SQLite has one writer at a time, so there it is the fill's own
    connection."""
    done = []

    def between(conn, cursor, statement, parameters, context, executemany):
        if done or not statement.startswith("UPDATE tickets SET pr_repo"):
            return
        done.append(statement)
        for tid, pr_text in writes.items():
            if sdb.dialect == "postgresql":
                _write_pr_as_before(sdb, tid, pr_text)
            else:
                cursor.execute("UPDATE tickets SET pr = ? WHERE id = ?", (pr_text, tid))

    with sdb.begin() as c:
        event.listen(c, "before_cursor_execute", between)
        fill(c)
    return bool(done)


def tickets_pr_guard():
    fills = (("the fill at start", fill_pr_columns), ("revision 0019's fill", frozen_pr.fill_pr_columns))
    for label, make in (("SQLite", None), ("Postgres", _pg_schema)):
        print(f"== a pr written while the fill runs, on {label} ==")
        if make and not PG:
            print("  skip  TARES_TEST_PG is not set")
            continue
        sch, dsn = make() if make else (None, None)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        try:
            sdb.upgrade()
            for name, fill in fills:
                with sdb.begin() as c:
                    c.execute(text("DELETE FROM tickets"))
                    c.execute(text("INSERT INTO tickets (id, project, pr, pr_repo, pr_number) "
                                   "VALUES (:i, 'p', :pr, :r, :n)"), [
                        {"i": "tk_quiet", "pr": '{"repo": "o/r", "number": 1}', "r": None, "n": None},
                        {"i": "tk_moved", "pr": '{"repo": "o/r", "number": 2}', "r": None, "n": None},
                        # a pr that was NULL when read is guarded too
                        {"i": "tk_linked", "pr": None, "r": "o/old", "n": 3},
                        # and one that stays NULL is still NULL when written: its columns are
                        # cleared, which a guard of `pr = NULL` would never do (TR-1705)
                        {"i": "tk_unlinked", "pr": None, "r": "o/gone", "n": 4}])
                ran = _fill_with_writes_between(sdb, fill, {
                    "tk_moved": '{"repo": "o/r", "number": 20}', "tk_linked": '{"repo": "o/r", "number": 30}'})
                cols = _pr_cols(sdb)
                ck(f"{name}: the writes landed between its read and its write, and a row nobody wrote is filled",
                   ran and cols["tk_quiet"] == ("o/r", 1), (ran, cols))
                ck(f"{name}: a row whose pr changed in between is left as it is",
                   cols["tk_moved"] == (None, None), cols)
                ck(f"{name}: so is one whose pr was NULL when read", cols["tk_linked"] == ("o/old", 3), cols)
                ck(f"{name}: one whose pr is NULL and stays so loses the columns it should not have",
                   cols["tk_unlinked"] == (None, None), cols)
        finally:
            sdb.close()
            if sch:
                _drop_schema(sch)


# ── decisions, questions and crew tables (TR-1616) ──────────────────────────
_LATEST = datetime(2026, 11, 22, 9, 0, 0, tzinfo=timezone.utc)

CREW = {
    "assumptions": (("id", "project", "number", "question", "choice", "why", "affects", "tickets", "state",
                     "words", "batch", "made_by", "made_role", "made_at", "decided_at", "person_id"), [
        ("as_1", "uc_demo", 1, "q1", "c", "w", None, '["ENG-1"]', "open", None, None, "builder", "builder",
         _LATER, None, "p_kim"),
        ("as_2", "uc_demo", 2, "q2", "c", "w", "", None, "kept", "yes", "b1", None, None, _T, _LATER, None),
        ("as_3", "uc_demo", None, "q3", None, None, None, "[]", "open", None, None, None, None, None, None, None),
    ]),
    "desk_items": (("id", "number", "project", "question", "context", "options", "recommendation", "why",
                    "blocks", "tickets", "blocking", "asked_by", "assumptions", "state", "reason", "created_at",
                    "closed_at", "station", "command", "person_id", "to_person_id"), [
        ("dk_1", 1, "uc_demo", "ship?", None, '["yes", "no"]', "yes", "because", None, '["ENG-1"]', True, "orch",
         '["A1"]', "open", None, _T, None, None, None, "p_kim", None),
        ("dk_2", 2, None, "cell wide", None, None, None, None, None, None, None, None, None, "answered", "ok",
         _T, _LATER, "st", "ls", None, "p_kim"),
    ]),
    "decisions": (("id", "project", "desk_item", "question", "words", "choice", "standing", "scope", "matched",
                   "session", "granted", "tickets", "decided_at", "via", "person_id"), [
        ("dc_1", "uc_demo", "dk_1", "ship?", "yes", "yes", True, "project", True, "s1", False, '["ENG-1"]', _T,
         None, "p_kim"),
        ("dc_2", None, None, "q", "w", None, None, "factory", None, None, None, None, _LATER, "console", None),
        ("dc_3", "uc_demo", "dk_gone", "q", "w", None, False, "project", False, None, False, "[]", None, None, None),
    ]),
    "handovers": (("project", "repo", "handed_by", "handed_at", "repos", "autonomy", "person_id", "crew",
                   "override_reason", "override_at", "override_person_id"), [
        ("uc_demo", "o/r", "spec", _T, '[{"path": "o/r", "name": null}, {"path": "o/s", "name": "s"}]', "ask",
         "p_kim", "crew", "late", _T, None),
        ("uc_old", "o/old", None, None, None, None, None, None, None, None, None),
    ]),
    "grants": (("id", "kind", "project", "holder_person_id", "words", "given_by", "given_at", "areas",
                "consents", "state", "needs_lead"), [
        ("G2", "project", "uc_demo", None, "push", "Kim", _T, '["o/r"]', '[{"person_id": "p_kim", "at": "x"}]',
         "waiting", True),
        ("G10", "team", None, None, "read", "Kim", _LATER, "", None, "active", None),
    ]),
    "pending_memory": (("id", "scope", "line", "attempts", "last_error", "created_at", "tried_at"), [
        ("pm_1", "factory", "- line", 3, "down", _T, _LATER),
    ]),
    "crews": (("id", "name", "person_id", "machine", "first_seen", "last_seen"), [
        ("cr_1", "crew", "p_kim", "laptop", _T, _LATER),
        ("cr_2", "crew", "p_null", "", _T, None),
    ]),
    "spec_questions": (("project", "id", "to_person_id", "repo", "text", "asked_by", "asked_at", "answer",
                        "answered_by", "answered_at", "state", "via"), [
        ("uc_demo", "Q1", "p_kim", "o/r", "why?", None, _T, "because", "p_kim", _LATER, "answered", "console"),
        ("uc_demo", "Q2", "p_kim", "o/r", "how?", "p_kim", _LATER, None, None, None, "open", None),
    ]),
    "signoffs": (("project", "repo", "person_id", "at", "via"), [("uc_demo", "o/r", "p_kim", _T, None)]),
    "handover_overrides": (("project", "at", "reason", "person_id", "handed_by"), [
        ("uc_demo", _T, "late", "p_kim", "spec"),
        ("uc_demo", None, "older", None, None),
    ]),
    "answers": (("id", "item", "kind", "project", "ask", "choice", "said", "via", "person_id", "answered_at"), [
        ("an_1", "question:D1", "question", "uc_demo", "ship?", "yes", "yes", "console", "p_kim", _LATER),
        ("an_2", "grant:G2", "grant", None, "push", None, "ok", "feed", None, _T),
    ]),
    "waiting_told": (("person_id", "item", "told_at", "sent", "ask", "channel", "ts", "seq", "closed"), [
        ("p_kim", "*", _T, False, None, None, None, None, None),
        ("p_kim", "question:D1", _T, True, "ship?", "C1", "1.5", 0, None),
    ]),
}
CREW_COUNTS = {t: len(rows) for t, (_, rows) in CREW.items()}
CREW_NAMES = list(CREW)


def crew_copy():
    print("== the decision, question and crew tables are copied from DuckDB at the first start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old_ddl_duckdb(con, seed={**SEED, **ACCOUNTS, **CREW})
    # a file from before the later columns
    con.execute("ALTER TABLE grants DROP COLUMN needs_lead")
    con.execute("ALTER TABLE spec_questions DROP COLUMN via")
    con.execute("CHECKPOINT")
    con.close()
    s = Store(path, state_dsn="")
    with s.sessions_db.begin() as c:
        ledger = {r[0]: r[1] for r in c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger counts each table as DuckDB held it", {t: ledger.get(t) for t in CREW_NAMES} == CREW_COUNTS,
       ledger)
    ck("the rows are there", {t: _n(s.sessions_db, t) for t in CREW_NAMES} == CREW_COUNTS,
       {t: _n(s.sessions_db, t) for t in CREW_NAMES})
    a = s.list_assumptions("uc_demo")
    ck("assumptions: by number, one with none last; tickets parsed, null and [] as []; affects null as ''",
       [(x["id"], x["tickets"], x["affects"]) for x in a]
       == [("as_1", ["ENG-1"], ""), ("as_2", [], ""), ("as_3", [], "")], a)
    ck("their times are UTC datetimes and their person is named", a[0]["made_at"] == _LATER
       and a[0]["made_at"].utcoffset().total_seconds() == 0 and a[0]["person"]["name"] == "Kim", a[0])
    dk = {x["id"]: x for x in s.list_desk(state=None)}
    ck("desk items: lists parsed, null as [], null flag False, null text ''",
       (dk["dk_1"]["options"], dk["dk_1"]["assumptions"], dk["dk_1"]["blocking"], dk["dk_1"]["context"])
       == (["yes", "no"], ["A1"], True, "")
       and (dk["dk_2"]["options"], dk["dk_2"]["tickets"], dk["dk_2"]["assumptions"], dk["dk_2"]["blocking"],
            dk["dk_2"]["blocks"]) == ([], [], [], False, ""), dk)
    ck("a closed item keeps its closing time", dk["dk_2"]["closed_at"] == _LATER and dk["dk_1"]["closed_at"] is None)
    dc = s.list_decisions(None)
    ck("decisions: newest first, one with no time last; label from its desk item only",
       [(x["id"], x["label"], x["standing"], x["tickets"]) for x in dc]
       == [("dc_2", None, False, []), ("dc_1", "D1", True, ["ENG-1"]), ("dc_3", None, False, [])], dc)
    h = s.get_handover("uc_demo")
    ck("the hand-over arrives with its repos parsed and its override log, the entry with no time last",
       h["repos"] == [{"path": "o/r", "name": None}, {"path": "o/s", "name": "s"}] and h["at"] == _T
       and h["overridden"] and [o["reason"] for o in h["overrides"]] == ["late", "older"], h)
    ck("a hand-over from before `repos` reads its one repo", s.get_handover("uc_old")["repos"]
       == [{"path": "o/old", "name": None}])
    g = s.all_grant_rows()
    ck("grants: by number (G10 after G2), blank and null lists as [], a column the file lacks is False",
       [(x["id"], x["areas"], x["consents"], x["needs_lead"]) for x in g]
       == [("G2", ["o/r"], [{"person_id": "p_kim", "at": "x"}], False), ("G10", [], [], False)], g)
    ck("the waiting memory line arrives", [(x["id"], x["attempts"], x["created_at"], x["tried_at"])
       for x in s.list_pending_memory()] == [("pm_1", 3, _T, _LATER)])
    cr = s.list_crews()
    ck("crews: newest activity first, one never seen again last", [(x["id"], x["machine"]) for x in cr]
       == [("cr_1", "laptop"), ("cr_2", "")] and cr[0]["person"]["name"] == "Kim", cr)
    q = s.list_spec_questions("uc_demo")
    ck("questions arrive; a column the file lacks is None",
       [(x["id"], x["state"], x["via"], x["answered_at"]) for x in q]
       == [("Q1", "answered", None, _LATER), ("Q2", "open", None, None)], q)
    ck("the answers and what was told arrive", s.last_answer(["question:D1"])["choice"] == "yes"
       and s.told_items("p_kim") == {"*", "question:D1"}
       and s.open_told("p_kim") == [{"item": "question:D1", "channel": "C1", "ts": "1.5"}],
       (s.told_items("p_kim"), s.open_told("p_kim")))
    with s._lock:
        n = [s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CREW_NAMES]
    ck("DuckDB still has its rows: nothing is deleted there", n == [CREW_COUNTS[t] for t in CREW_NAMES], n)
    later = s.add_desk_item(None, "after the copy", "", [], "", "", "", [], False, "", [], _LATEST)
    ck("numbering goes on from the copied rows", later["number"] == 3
       and s.add_grant_row("team", "w")["id"] == "G11"
       and s.add_assumption("uc_demo", "q", "c", "w", "", [], "", "", _T)["number"] == 3
       and s.add_spec_question("uc_demo", "p_kim", "o/r", "t", None, _T)["id"] == "Q3", later)
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing: what was made after the copy is kept",
       s.get_desk_item("D3") is not None and _n(s.sessions_db, "desk_items") == 3
       and _n(s.sessions_db, "handover_overrides") == 2)
    s.close()


def _tz(name):
    if name is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()


def crew_methods():
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    sdb = s.sessions_db
    s.create_project("uc_t", "custom", "Tee", {})
    s.create_project("uc_u", "custom", "U", {})
    base = {t: _n(sdb, t) for t in CREW_NAMES}
    kim = s.create_person("Kim")["id"]
    ann = s.create_person("Ann")["id"]

    print("== assumptions ==")
    a1 = s.add_assumption("uc_t", "q1", "c1", "w1", "", [], "", "", _LATER, person_id="")
    a2 = s.add_assumption("uc_t", "q2", "c2", "w2", "api", ["ENG-1"], "builder-1", "builder", _T, person_id=kim)
    b1 = s.add_assumption("uc_u", "qu", "c", "w", "", ["ENG-1"], "b", "builder", _T)
    ck("numbered per project, with every field; empty values read as empty or None",
       [(a1["number"], a1["label"]), (a2["number"], a2["label"]), (b1["number"], b1["label"])]
       == [(1, "A1"), (2, "A2"), (1, "A1")] and (a1["affects"], a1["tickets"], a1["state"], a1["made_by"],
       a1["made_role"], a1["person"], a1["words"], a1["batch"], a1["decided_at"])
       == ("", [], "open", None, None, None, None, None, None)
       and a2["person"] == {"id": kim, "name": "Kim"} and a2["by_label"] == "Kim (builder-1)"
       and a2["made_at"] == _T, (a1, a2, b1))
    ck("list_assumptions by number, by state, by ticket (exact), of one project",
       [x["id"] for x in s.list_assumptions("uc_t")] == [a1["id"], a2["id"]]
       and [x["id"] for x in s.list_assumptions("uc_t", ticket="ENG-1")] == [a2["id"]]
       and s.list_assumptions("uc_t", ticket="eng-1") == [] and s.list_assumptions("uc_t", state="kept") == [])
    ck("open_assumptions: oldest first, ties by project then number, without the person looked up",
       [x["id"] for x in s.open_assumptions() if x["project"] in ("uc_t", "uc_u")]
       == [a2["id"], b1["id"], a1["id"]] and "person_id" in s.open_assumptions()[0])
    ck("get_assumption by A2, a2, 2 and id, in its project only",
       [s.get_assumption("uc_t", r)["id"] for r in ("A2", "a2", " 2 ", a2["id"])] == [a2["id"]] * 4
       and s.get_assumption("uc_u", a2["id"]) is None and s.get_assumption("uc_t", "A9") is None
       and s.get_assumption("uc_t", None) is None and s.get_assumption("uc_t", "") is None)
    s.decide_assumption(a1["id"], "kept", "fine", "b7", _LATEST)
    got = s.get_assumption("uc_t", "A1")
    ck("decide_assumption changes that one", (got["state"], got["words"], got["batch"], got["decided_at"])
       == ("kept", "fine", "b7", _LATEST) and [x["id"] for x in s.list_assumptions("uc_t", "open")] == [a2["id"]],
       got)

    print("== the desk ==")
    n0 = max([x["number"] for x in s.list_desk(state=None)], default=0)

    def desk(project, question, blocking, at, **more):
        return s.add_desk_item(project, question, more.pop("context", ""), more.pop("options", None),
                               "rec", "why", more.pop("blocks", ""), more.pop("tickets", None), blocking,
                               more.pop("asked_by", ""), more.pop("assumptions", None), at, **more)

    d1 = desk("uc_t", "one", False, _T)
    d2 = desk("uc_u", "two", True, _LATER, options=["a", "b"], tickets=["ENG-1"], assumptions=["A1"],
              person_id=kim, to_person_id=ann, station="st", command="ls", asked_by="orch", context="ctx",
              blocks="all")
    d3 = desk(None, "three", True, _T, station="", command="", person_id="", to_person_id="")
    d4 = desk("uc_t", "four", False, _T)
    ck("numbered once for the cell", [x["number"] for x in (d1, d2, d3, d4)] == [n0 + 1, n0 + 2, n0 + 3, n0 + 4]
       and d1["label"] == f"D{n0 + 1}")
    ck("an item with nothing given: empty lists, '' texts, None the rest",
       (d1["options"], d1["tickets"], d1["assumptions"], d1["context"], d1["blocks"], d1["asked_by"],
        d1["blocking"], d1["state"], d1["reason"], d1["closed_at"], d1["to_person"], d1["person"])
       == ([], [], [], "", "", None, False, "open", None, None, None, None)
       and (d3["station"], d3["command"], d3["to_person_id"]) == (None, None, None), d1)
    ck("an item with everything", (d2["options"], d2["tickets"], d2["assumptions"], d2["context"], d2["blocks"],
       d2["blocking"], d2["station"], d2["command"], d2["to_person"], d2["person"], d2["to_person_id"],
       d2["created_at"]) == (["a", "b"], ["ENG-1"], ["A1"], "ctx", "all", True, "st", "ls",
       {"id": ann, "name": "Ann"}, {"id": kim, "name": "Kim"}, ann, _LATER), d2)
    ck("get_desk_item by id, D3, d3 and 3; unknown, blank and None give None",
       [s.get_desk_item(r)["id"] for r in (d3["id"], f"D{n0 + 3}", f"d{n0 + 3}", str(n0 + 3))] == [d3["id"]] * 4
       and s.get_desk_item("D999") is None and s.get_desk_item("") is None and s.get_desk_item(None) is None)
    ck("list_desk: blocking first, then oldest, ties by number",
       [x["id"] for x in s.list_desk()] == [d3["id"], d2["id"], d1["id"], d4["id"]],
       [x["question"] for x in s.list_desk()])
    ck("by project", [x["id"] for x in s.list_desk(project="uc_t")] == [d1["id"], d4["id"]])
    try:
        _tz("Asia/Kolkata")
        s.close_desk_item(d1["id"], "withdrawn", "moot", _LATER)
        s.close_desk_item(d4["id"], "answered", None, _LATEST)
        got = s.get_desk_item(d1["id"])
        hours = got["closed_at"].utcoffset().total_seconds() / 3600
        if sdb.dialect == "sqlite":
            ck("closed_at reads in this machine's zone, as DuckDB gave it: the crew compares it as text",
               hours == 5.5 and str(got["closed_at"]).startswith("2026-11-21 17:30:00+05:30"),
               str(got["closed_at"]))
        ck("and it is the same instant", got["closed_at"] == _LATER and got["created_at"] == _T,
           (got["closed_at"], got["created_at"]))
    finally:
        _tz(None)
    ck("close_desk_item sets state, reason and time", (got["state"], got["reason"]) == ("withdrawn", "moot"), got)
    ck("by state; state None is every item", [x["id"] for x in s.list_desk("withdrawn")] == [d1["id"]]
       and len(s.list_desk(state=None, project="uc_t")) == 2 and len(s.list_desk()) == 2)
    ck("closed_since keeps what closed at or after it",
       [x["id"] for x in s.list_desk(None, closed_since=_LATER)] == [d1["id"], d4["id"]]
       and [x["id"] for x in s.list_desk(None, closed_since=_LATEST)] == [d4["id"]])
    unsaid, said = desk("uc_n", "unsaid", None, _T), desk("uc_n", "said", False, _LATER)
    ck("an item that does not say whether it blocks comes last and reads as not blocking",
       [x["id"] for x in s.list_desk(project="uc_n")] == [said["id"], unsaid["id"]]
       and unsaid["blocking"] is False, [x["question"] for x in s.list_desk(project="uc_n")])

    print("== decisions ==")
    def decide(project, item, at, choice="yes", **more):
        return s.add_decision(project, item, "q", "w", choice, True, "project", False, None, True,
                              more.pop("tickets", None), at, **more)
    n_all = len(s.list_decisions(None))
    c1 = decide("uc_t", d1["id"], _T, choice="", tickets=["ENG-1"], via="console", person_id=kim)
    c2 = decide("uc_t", "dk_none", _LATEST)
    c3 = decide(None, d2["id"], _LATER)
    c4 = decide("uc_u", d2["id"], _LATER)
    ck("add_decision returns it with its desk label, person and values",
       (c1["label"], c1["choice"], c1["tickets"], c1["via"], c1["at"], c1["standing"], c1["matched"],
        c1["granted"], c1["person"], c2["label"], c2["person"], c3["label"])
       == (f"D{n0 + 1}", None, ["ENG-1"], "console", _T, True, False, True, {"id": kim, "name": "Kim"}, None, None,
           f"D{n0 + 2}") and "person_id" not in c1 and c3["project"] is None, (c1, c2, c3))
    ck("a project's decisions, newest first", [x["id"] for x in s.list_decisions("uc_t")] == [c2["id"], c1["id"]])
    ck("all_ adds the cell-wide ones", [x["id"] for x in s.list_decisions("uc_t", all_=True)]
       == [c2["id"], c3["id"], c1["id"]])
    ck("no project: every one", len(s.list_decisions(None)) == n_all + 4
       and c4["id"] in [x["id"] for x in s.list_decisions(None)])
    untimed, timed = decide("uc_nd", "dk_none", None), decide("uc_nd", "dk_none", _T)
    ck("a decision with no time comes last", [x["id"] for x in s.list_decisions("uc_nd")]
       == [timed["id"], untimed["id"]], s.list_decisions("uc_nd"))      # TR-1705
    with sdb.begin() as c:
        c.execute(text("DELETE FROM decisions WHERE project = 'uc_nd'"))

    print("== answers ==")
    def answer(item, kind, project, person, at, via="console", choice="yes"):
        return s.add_answer(item, kind, project, "ask", choice, "said", via, person, at)
    for bad in (("maybe", "console"), ("yes", "email")):
        try:
            s.add_answer("i", "k", None, "a", bad[0], "s", bad[1], None, _T)
            ck(f"{bad} is refused", False)
        except ValueError:
            ck(f"{bad} is refused and not stored", _n(sdb, "answers") == base["answers"])
    x1 = answer("question:D1", "question", "uc_t", kim, _T)
    x2 = answer("question:D1", "question", "uc_t", ann, _LATER, via="feed", choice=None)
    x3 = answer("grant:G1", "grant", "uc_u", kim, _LATER)
    x4 = answer("question:D2", "question", "uc_u", kim, _LATEST)
    x5 = answer("message:1", "message", None, "", _T)
    ck("add_answer returns it with the person", x1["person"] == {"id": kim, "name": "Kim"} and x5["person"] is None
       and x1["at"] == _T and x2["choice"] is None, x1)
    last = s.last_answer(["question:D1", "question:nope"])
    ck("last_answer: the newest of the items", last == {"item": "question:D1", "choice": None, "said": "said",
       "via": "feed", "at": _LATER, "person": {"id": ann, "name": "Ann"}}, last)
    ck("no answer and no items give None", s.last_answer(["nope"]) is None and s.last_answer([]) is None)
    got = s.list_answers(_T)
    ck("list_answers: newest first, since included, with the person",
       got[0]["id"] == x4["id"] and {x["id"] for x in got[1:3]} == {x2["id"], x3["id"]}
       and {x["id"] for x in got[3:]} == {x1["id"], x5["id"]} and got[0]["person"]["name"] == "Kim"
       and "person_id" not in got[0] and got[0]["at"] == _LATEST, got)
    ck("since cuts the older ones", {x["id"] for x in s.list_answers(_LATER)} == {x2["id"], x3["id"], x4["id"]})
    ck("hidden projects are left out, an answer with no project stays",
       {x["id"] for x in s.list_answers(_T, hidden=["uc_u"])} == {x1["id"], x2["id"], x5["id"]})
    ck("own: the caller's grant answer in a hidden project stays, no other kind",
       {x["id"] for x in s.list_answers(_T, hidden=["uc_u"], own=kim)} == {x1["id"], x2["id"], x3["id"], x5["id"]}
       and {x["id"] for x in s.list_answers(_T, hidden=["uc_u"], own=ann)} == {x1["id"], x2["id"], x5["id"]})
    ck("by person and by items", {x["id"] for x in s.list_answers(_T, person=kim)} == {x1["id"], x3["id"], x4["id"]}
       and [x["id"] for x in s.list_answers(_T, items=["grant:G1"])] == [x3["id"]])
    with sdb.begin() as c:
        c.execute(answers_table.insert(), [{"id": f"an_{i:04d}", "item": "bulk", "kind": "k", "answered_at": _LATEST,
                                            "person_id": ann} for i in range(505)])
    ck("500 at most, filtered before the cut", len(s.list_answers(_T)) == 500
       and {x["id"] for x in s.list_answers(_T, person=kim)} == {x1["id"], x3["id"], x4["id"]})

    print("== what a person was told ==")
    s.mark_waiting_told(kim, [], True, _T)
    ck("nothing told: empty", s.told_items(kim) == set() and _n(sdb, "waiting_told") == base["waiting_told"])
    s.mark_waiting_told(kim, ["*", "a", "b", "a"], False, _T)
    s.mark_waiting_told(kim, ["a", "c"], True, _LATER)
    s.mark_waiting_told(ann, ["a"], True, _T)
    with sdb.begin() as c:
        rows = {r.item: (r.sent, r.told_at) for r in
                c.execute(told_table.select().where(told_table.c.person_id == kim))}
    ck("each item once, the first row kept", s.told_items(kim) == {"*", "a", "b", "c"}
       and rows["a"] == (False, _T) and rows["c"] == (True, _LATER), rows)
    s.set_told_message(kim, [("c", "ask c"), ("a", "ask a")], "C1", "9.1")
    s.set_told_message(ann, [("a", "ask a")], "C1", "9.1")
    ck("open_told: the person's items with a message", sorted((x["item"], x["channel"], x["ts"])
       for x in s.open_told(kim)) == [("a", "C1", "9.1"), ("c", "C1", "9.1")] and
       sorted(s.open_told(kim)[0]) == ["channel", "item", "ts"], s.open_told(kim))
    ck("told_message: in the message's order, with the ask", s.told_message(kim, "C1", "9.1")
       == [{"item": "c", "ask": "ask c", "closed": None}, {"item": "a", "ask": "ask a", "closed": None}],
       s.told_message(kim, "C1", "9.1"))
    ck("no channel or no ts names no message", s.told_message(kim, None, None) == []
       and s.told_message(kim, "C1", None) == [] and s.told_message(kim, "C1", "0") == [])
    s.forget_waiting_told(kim, ["a", "b", "zzz"])
    s.forget_waiting_told(kim, [])
    ck("forget_waiting_told drops an item with no message, keeps one with", s.told_items(kim) == {"*", "a", "c"})
    s.close_told(kim, {"c": "answered by Ann", "zzz": "x"})
    ck("close_told: no longer open, the line kept", [x["item"] for x in s.open_told(kim)] == ["a"]
       and s.told_message(kim, "C1", "9.1")[0]["closed"] == "answered by Ann"
       and [x["item"] for x in s.open_told(ann)] == ["a"])

    print("== hand-overs ==")
    repos = [{"path": "", "name": "n"}, {"path": "o/r", "name": None}]
    ck("none yet", s.get_handover("uc_t") is None and s.handovers_by_project([]) == {}
       and s.handovers_by_project([None, ""]) == {})
    s.set_handover("uc_t", repos, "", "", _T, person_id="", crew="")
    h = s.get_handover("uc_t")
    ck("a first hand-over: the first named repo, empty values None, nothing else",
       h == {"repo": "o/r", "repos": repos, "autonomy": None, "by": None, "at": _T, "person": None,
             "by_label": None, "handed_by_person": None, "crew": None, "override": None, "overridden": False,
             "overrides": [], "finished": None, "takeover": None}, h)
    s.set_handover("uc_u", [], None, "spec", _T)
    ck("no repos is an empty list", s.get_handover("uc_u")["repos"] == [] and s.get_handover("uc_u")["repo"] is None)
    s._set_handover_fields("uc_u", repo="o/x")
    ck("an empty list stays a list beside a `repo`", s.get_handover("uc_u")["repos"] == [])
    s._set_handover_fields("uc_u", repos=None)
    ck("a hand-over with no `repos` falls back to `repo`",
       s.get_handover("uc_u")["repos"] == [{"path": "o/x", "name": None}])
    s._set_handover_fields("uc_u", repos=[], repo=None)
    s.set_handover("uc_t", repos, "ask", "spec", _LATER, person_id=kim, crew="c1", override_reason="deadline")
    h = s.get_handover("uc_t")
    ck("one with an override", h["overridden"] and h["override"] == {"reason": "deadline", "at": _LATER,
       "person": {"id": kim, "name": "Kim"}, "by_label": "Kim"} and h["crew"] == "c1" and h["autonomy"] == "ask"
       and h["handed_by_person"] == {"id": kim, "name": "Kim"}, h)
    s.finish_handover("uc_t", "done", "dc_x", "orch", _LATER, person_id=kim)
    s.take_over("uc_t", "mine", "session", _LATER, person_id=ann)
    s.release_takeover("uc_t", _LATEST)
    h = s.get_handover("uc_t")
    ck("finished and taken over",
       h["finished"] == {"at": _LATER, "words": "done", "decision": "dc_x", "by": "orch",
                         "person": {"id": kim, "name": "Kim"}, "by_label": "Kim (orch)"}
       and h["takeover"] == {"at": _LATER, "words": "mine", "by": "session", "released_at": _LATEST,
                             "person": {"id": ann, "name": "Ann"}, "by_label": "Ann (session)"}, h)
    ck("another project's is untouched", s.get_handover("uc_u")["finished"] is None
       and s.get_handover("uc_u")["takeover"] is None)
    both = {"uc_t", "uc_u"}
    pick = lambda **kw: both & set(s.handed_over_projects(**kw))
    ck("handed_over_projects by finished and taken", pick() == both and pick(finished=True) == {"uc_t"}
       and pick(finished=False) == {"uc_u"} and pick(taken=True) == {"uc_t"} and pick(taken=False) == {"uc_u"}
       and pick(finished=False, taken=True) == set() and pick(finished=True, taken=True) == {"uc_t"})
    s.take_over("uc_t", "again", None, _LATEST)
    ck("taking over again clears the release", s.get_handover("uc_t")["takeover"]["released_at"] is None)
    s.set_handover("uc_t", [{"path": "o/s", "name": None}], None, "spec2", _LATEST, person_id=ann)
    h = s.get_handover("uc_t")
    ck("a later clean hand-over replaces it, clears finished and taken, keeps the override, not overridden",
       (h["repo"], h["by"], h["at"], h["autonomy"], h["crew"], h["finished"], h["takeover"], h["overridden"])
       == ("o/s", "spec2", _LATEST, None, None, None, None, False)
       and h["override"]["reason"] == "deadline" and h["override"]["at"] == _LATER
       and h["override"]["person"]["name"] == "Kim", h)
    s.set_handover("uc_t", repos, None, "zed", _T, person_id=ann, override_reason="again")
    s.set_handover("uc_t", repos, None, "amy", _T, override_reason="again")
    h = s.get_handover("uc_t")
    ck("every override stays in the log, oldest first, ties by who handed it",
       [(o["reason"], o["at"], o["by_label"]) for o in s.list_handover_overrides("uc_t")]
       == [("again", _T, "amy"), ("again", _T, "Ann (zed)"), ("deadline", _LATER, "Kim (spec)")]
       and h["overrides"] == s.list_handover_overrides("uc_t") and h["override"]["at"] == _T
       and h["override"]["person"] == {"id": ann, "name": "Ann"}, s.list_handover_overrides("uc_t"))
    ck("handovers_by_project: the asked ones that have one", set(s.handovers_by_project(["uc_t", "uc_u", "uc_no"]))
       == both and set(s.handovers_by_project()) >= both)
    # an override from before the log, beside a log row with no project: NOT IN a list that holds
    # a NULL matches nothing, so the row must be left out of it (TR-1705)
    with sdb.begin() as c:
        c.execute(text("INSERT INTO handover_overrides (reason) VALUES ('no project')"))
        c.execute(text("INSERT INTO handovers (project, override_reason) VALUES ('uc_old', 'before the log')"))
    s._first_override_entries()
    ck("an override from before the log gets its first entry, whatever else the log holds",
       [o["reason"] for o in s.list_handover_overrides("uc_old")] == ["before the log"],
       s.list_handover_overrides("uc_old"))
    with sdb.begin() as c:
        c.execute(text("DELETE FROM handover_overrides WHERE project IS NULL OR project = 'uc_old'"))
        c.execute(text("DELETE FROM handovers WHERE project = 'uc_old'"))

    def refuse(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO handover_overrides"):
            raise RuntimeError("boom")

    before = (s.get_handover("uc_u"), _n(sdb, "handover_overrides"))
    event.listen(sdb.engine, "before_cursor_execute", refuse)
    try:
        s.set_handover("uc_u", repos, None, "x", _LATEST, override_reason="r")
        ck("a refused log entry raises", False)
    except Exception:
        ck("the hand-over and its log entry are saved together or not at all",
           (s.get_handover("uc_u"), _n(sdb, "handover_overrides")) == before, s.get_handover("uc_u"))
    finally:
        event.remove(sdb.engine, "before_cursor_execute", refuse)

    print("== questions to an area owner ==")
    q1 = s.add_spec_question("uc_t", ann, "o/r", "one", "", _LATER)
    for i in range(2, 11):
        s.add_spec_question("uc_t", ann, "o/r", str(i), kim, _T)
    qu = s.add_spec_question("uc_u", kim, "o/r", "u", kim, _T)
    ck("numbered per project, with the people", q1["id"] == "Q1" and qu["id"] == "Q1"
       and (q1["to_person"], q1["asked_by"], q1["asked_by_person"], q1["answered_by_person"], q1["state"],
            q1["answer"], q1["via"], q1["asked_at"]) == ({"id": ann, "name": "Ann"}, None, None, None, "open", None,
                                                      None, _LATER), q1)
    ids = [x["id"] for x in s.list_spec_questions("uc_t")]
    ck("by when asked, then by number (Q10 after Q9), past Q9 the next is Q11",
       ids == [f"Q{i}" for i in range(2, 11)] + ["Q1"]
       and s.add_spec_question("uc_t", ann, "o/r", "next", kim, _LATEST)["id"] == "Q11", ids)
    mine = [(x["project"], x["id"]) for x in s.list_spec_questions(to_person_id=kim)]
    ck("by person and by state", mine == [("uc_u", "Q1")]
       and len(s.list_spec_questions("uc_t", state="open")) == 11
       and s.list_spec_questions("uc_t", state="answered") == [])
    ck("get_spec_question by q2, 2 and Q2; an unreadable ref and another project's give None",
       [s.get_spec_question("uc_t", r)["id"] for r in ("q2", "2", " Q2 ")] == ["Q2"] * 3
       and s.get_spec_question("uc_u", "Q2") is None and s.get_spec_question("uc_t", "two") is None
       and s.get_spec_question("uc_t", None) is None)
    ck("answering an open question once", s.answer_spec_question("uc_t", "Q2", "yes", ann, _LATEST, via="console")
       is True and s.answer_spec_question("uc_t", "Q2", "no", kim, _LATEST) is False
       and s.answer_spec_question("uc_t", "Q99", "no", kim, _LATEST) is False)
    q = s.get_spec_question("uc_t", "Q2")
    ck("the first answer stands", (q["answer"], q["answered_by"], q["answered_at"], q["state"], q["via"],
       q["answered_by_person"]["name"]) == ("yes", ann, _LATEST, "answered", "console", "Ann")
       and s.get_spec_question("uc_u", "Q1")["state"] == "open", q)

    print("== grants ==")
    top = max([int(x["id"][1:]) for x in s.all_grant_rows()], default=0)
    g = [s.add_grant_row("project", f"w{i}", project="uc_t", state="waiting", given_at=_T) for i in range(11)]
    gp = s.add_grant_row("personal", "p", holder=kim, given_by="Kim", areas=["o/r"],
                         consents=[{"person_id": kim}], needs_lead=True)
    ck("numbered for the cell, with defaults", [x["id"] for x in g] == [f"G{top + i + 1}" for i in range(11)]
       and (g[0]["areas"], g[0]["consents"], g[0]["needs_lead"], g[0]["holder_person_id"], g[0]["given_at"])
       == ([], [], False, None, _T) and (gp["areas"], gp["consents"], gp["needs_lead"], gp["holder_person_id"],
       gp["state"]) == (["o/r"], [{"person_id": kim}], True, kim, "active") and gp["given_at"] is not None, gp)
    ck("all_grant_rows by number", [int(x["id"][1:]) for x in s.all_grant_rows()]
       == sorted(int(x["id"][1:]) for x in s.all_grant_rows()))
    ck("get_grant_row in any case, spaces off; unknown and None give None",
       s.get_grant_row(f" g{top + 1} ")["words"] == "w0" and s.get_grant_row("G999") is None
       and s.get_grant_row(None) is None)
    a, b, c_ = g[0]["id"], g[1]["id"], g[2]["id"]
    ck("consent keeps it waiting", s.consent_waiting_grant(a, [{"person_id": ann}]) is True
       and (s.get_grant_row(a)["state"], s.get_grant_row(a)["consents"]) == ("waiting", [{"person_id": ann}]))
    ck("activate with areas and needs_lead", s.activate_waiting_grant(a, areas=["o/x"], needs_lead=True) is True
       and (s.get_grant_row(a)["state"], s.get_grant_row(a)["areas"], s.get_grant_row(a)["needs_lead"])
       == ("active", ["o/x"], True))
    ck("activate without them keeps what it had", s.consent_waiting_grant(b, []) is True
       and s.activate_waiting_grant(b) is True and s.get_grant_row(b)["areas"] == []
       and s.get_grant_row(b)["needs_lead"] is False and s.get_grant_row(b)["state"] == "active")
    ck("decline ends it", s.decline_waiting_grant(c_, [{"no": kim}]) is True
       and (s.get_grant_row(c_)["state"], s.get_grant_row(c_)["consents"]) == ("revoked", [{"no": kim}]))
    before = s.get_grant_row(a)
    ck("none of the three touches a grant that no longer waits, or an unknown one",
       s.consent_waiting_grant(a, []) is False and s.activate_waiting_grant(a, areas=[]) is False
       and s.decline_waiting_grant(a, []) is False and s.decline_waiting_grant("G999", []) is False
       and s.get_grant_row(a) == before)
    d_ = g[3]["id"]
    s.update_grant_row(d_)
    s.update_grant_row(d_, areas=[], needs_lead=False)
    ck("update_grant_row with nothing or empty values", s.get_grant_row(d_)["state"] == "waiting"
       and s.get_grant_row(d_)["areas"] == [])
    s.update_grant_row(d_, state="revoked", consents=[{"a": 1}], areas=["z"], needs_lead=True)
    got = s.get_grant_row(d_)
    ck("update_grant_row sets the given ones", (got["state"], got["consents"], got["areas"], got["needs_lead"])
       == ("revoked", [{"a": 1}], ["z"], True) and s.get_grant_row(g[4]["id"])["state"] == "waiting", got)

    print("== memory lines waiting ==")
    p1 = s.add_pending_memory("uc_t", "- a", "x" * 600)
    p2 = s.add_pending_memory("factory", "- b")
    rows = s.list_pending_memory()
    rows = [r for r in rows if r["id"] in (p1, p2)]
    ck("oldest first, one attempt, the error cut at 500", [r["id"] for r in rows] == [p1, p2]
       and sorted(rows[0]) == ["attempts", "created_at", "id", "last_error", "line", "scope", "tried_at"]
       and rows[0]["attempts"] == 1 and len(rows[0]["last_error"]) == 500 and rows[1]["last_error"] == ""
       and rows[0]["created_at"] == rows[0]["tried_at"], rows)
    ck("by scope; an empty scope matches none", [r["id"] for r in s.list_pending_memory("factory")] == [p2]
       and s.list_pending_memory("") == [])
    time.sleep(0.01)
    s.retried_pending_memory(p1, "y" * 600)
    r = s.list_pending_memory("uc_t")[0]
    ck("a retry counts and moves tried_at", r["attempts"] == 2 and r["last_error"] == "y" * 500
       and r["tried_at"] > r["created_at"], r)
    ck("delete says whether it removed one", s.delete_pending_memory(p2) is True
       and s.delete_pending_memory(p2) is False and s.list_pending_memory("factory") == [])

    print("== crews ==")
    n = _n(sdb, "crews")
    c1 = s.resolve_crew(" ", None, None, _LATER)
    same = s.resolve_crew("crew", "local", "", _T)
    ck("no name, person or machine is the local person's `crew`; an older line does not move last_seen",
       c1 == same and _n(sdb, "crews") == n + 1
       and [(x["name"], x["person_id"], x["machine"], x["first_seen"], x["last_seen"])
            for x in s.list_crews() if x["id"] == c1] == [("crew", "local", "", _LATER, _LATER)], s.list_crews())
    ck("a machine joins the bare crew", s.resolve_crew("crew", "local", "box", _LATEST) == c1
       and [(x["machine"], x["last_seen"]) for x in s.list_crews() if x["id"] == c1] == [("box", _LATEST)])
    ck("a line without a machine joins the person's one named crew",
       s.resolve_crew("crew", "local", "", _T) == c1 and _n(sdb, "crews") == n + 1)
    c2 = s.resolve_crew("crew", "local", "other", _T)
    c3 = s.resolve_crew("crew", "local", "", None)
    ck("a second machine is a second crew; with two, a bare line makes its own", len({c1, c2, c3}) == 3)
    c4 = s.resolve_crew("Crew", kim, "box", _LATEST)
    got = s.list_crews()
    ck("newest activity first, ties by id, never seen last; the name's case matters",
       [x["id"] for x in got] == [x["id"] for x in sorted(got, key=lambda x: (
           x["last_seen"] is None, -(x["last_seen"] or _T).timestamp(), x["id"]))]
       and got[-1]["id"] in (c3,) and c4 not in (c1, c2, c3)
       and [x["person"] for x in got if x["id"] == c4] == [{"id": kim, "name": "Kim"}]
       and [x["person"] for x in got if x["id"] == c1] == [{"id": "local", "name": "You"}], got)
    try:
        with sdb.begin() as conn:
            conn.execute(crews_table.insert().values(id="c_dup", name="Crew", person_id=kim, machine="box"))
        ck("one crew per person, name and machine", False)
    except IntegrityError:
        ck("one crew per person, name and machine", True)

    # one named crew and a bare one of the same person: merged, its sessions move (TR-1223)
    with sdb.begin() as conn:
        conn.execute(crews_table.insert(), [
            {"id": cid, "name": name, "person_id": ann, "machine": machine, "first_seen": first, "last_seen": last}
            for cid, name, machine, first, last in (
                ("c_named", "m", "box", _LATER, _LATER), ("c_bare", "m", "", _T, None),
                ("c_two1", "n", "a", None, None), ("c_two2", "n", "b", None, None),
                ("c_two0", "n", "", None, None))])
    s.set_session_station("s-bare", "crew-builder", "builder", "", "uc_t", _T, "c_bare")
    s._merge_split_crews()
    left = {x["id"]: x for x in s.list_crews()}
    with sdb.begin() as conn:
        moved = conn.execute(text("SELECT crew_id FROM session_stations WHERE session = 's-bare'")).scalar()
    ck("the bare one is gone, the named one spans both, the station moved", "c_bare" not in left
       and (left["c_named"]["first_seen"], left["c_named"]["last_seen"]) == (_T, _LATER) and moved == "c_named",
       (left.get("c_named"), moved))
    ck("with two named crews nothing is merged", {"c_two0", "c_two1", "c_two2"} <= set(left))

    print("== the reads do not queue behind a writer ==")
    ck("the writes wait for the process's write lock", all(_waits_for_write_lock(s, call) for call in (
       lambda: s.add_assumption("uc_t", "w", "c", "w", "", [], "", "", _T),
       lambda: s.decide_assumption("as_none", "kept", None, None, _T),
       lambda: s.add_desk_item(None, "w", "", [], "", "", "", [], False, "", [], _T),
       lambda: s.close_desk_item("dk_none", "answered", None, _T),
       lambda: s.set_handover("uc_w", [], None, "x", _T), lambda: s.release_takeover("uc_w", _T),
       lambda: s.add_spec_question("uc_w", kim, "o/r", "t", None, _T),
       lambda: s.add_grant_row("team", "w"), lambda: s.update_grant_row("G1", state="active"),
       lambda: s.consent_waiting_grant("G999", []), lambda: s.add_pending_memory("w", "l"),
       lambda: s.mark_waiting_told(kim, ["w"], True, _T), lambda: s.close_told(kim, {"w": "x"}),
       lambda: s.resolve_crew("w", kim, "w", _T))))
    if sdb.dialect == "sqlite":
        ck("the reads run while a transaction holds the write lock", all(_runs_under_the_write_lock(s, call)
           for call in (
           lambda: s.list_assumptions("uc_t"), lambda: s.open_assumptions(),
           lambda: s.get_assumption("uc_t", "A1"), lambda: s.get_desk_item("D1"), lambda: s.list_desk(),
           lambda: s.list_decisions(None), lambda: s.last_answer(["a"]), lambda: s.list_answers(_T),
           lambda: s.told_items(kim), lambda: s.open_told(kim), lambda: s.told_message(kim, "C", "1"),
           lambda: s.list_handover_overrides("uc_t"), lambda: s.handed_over_projects(),
           lambda: s.handovers_by_project(["uc_t"]), lambda: s.get_spec_question("uc_t", "Q1"),
           lambda: s.list_spec_questions(), lambda: s.get_grant_row("G1"), lambda: s.all_grant_rows(),
           lambda: s.list_pending_memory(), lambda: s.list_crews())))

    print("== two processes number one after the other ==")
    other = []
    for _ in range(4):                     # each as another process: its own lock, the same database
        o = copy.copy(s)
        o._swrite = threading.RLock()
        other.append(o)

    def slow(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(("SELECT max(", "SELECT crews.id, crews.last_seen")):
            time.sleep(0.15)               # the read of one writer is done before the next one writes

    def at_once(call):
        got, errors = [], []

        def run(o):
            try:
                got.append(call(o))
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}"[:200])
        threads = [threading.Thread(target=run, args=(o,)) for o in other]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return got, errors

    event.listen(sdb.engine, "after_cursor_execute", slow)
    try:
        for label, call in (
                ("assumption numbers", lambda o: o.add_assumption("uc_p", "q", "c", "w", "", [], "", "", _T)["number"]),
                ("desk numbers", lambda o: o.add_desk_item(None, "p", "", [], "", "", "", [], False, "", [], _T)["number"]),
                ("grant numbers", lambda o: o.add_grant_row("team", "p")["id"]),
                ("question numbers", lambda o: o.add_spec_question("uc_p", kim, "o/r", "p", None, _T)["id"])):
            got, errors = at_once(call)
            ck(f"four writers at once get four different {label}", not errors and len(set(got)) == 4, (got, errors))
        got, errors = at_once(lambda o: o.resolve_crew("par", kim, "box", _T))
        ck("four writers at once make one crew", not errors and len(got) == 4 and len(set(got)) == 1, (got, errors))
    finally:
        event.remove(sdb.engine, "after_cursor_execute", slow)

    print("== deleting a project ==")
    s.add_pending_memory("uc_t", "- t")
    s.add_pending_memory("uc_u", "- u")
    with sdb.begin() as conn:
        conn.execute(text("INSERT INTO signoffs (project, repo) VALUES ('uc_t', 'o/r'), ('uc_u', 'o/r')"))
    kept = {t: _n(sdb, t) for t in ("answers", "waiting_told", "crews")}
    by_project = ("assumptions", "desk_items", "decisions", "grants", "handovers", "handover_overrides",
                  "spec_questions", "signoffs")

    def of(uid):
        with sdb.begin() as conn:
            out = {t: conn.execute(text(f"SELECT count(*) FROM {t} WHERE project = :p"), {"p": uid}).scalar()
                   for t in by_project}
            out["pending_memory"] = conn.execute(text("SELECT count(*) FROM pending_memory WHERE scope = :p"),
                                                 {"p": uid}).scalar()
        return out

    others = of("uc_u")
    ck("both projects have rows in each before", all(of("uc_t").values()) and all(
       others[t] for t in ("assumptions", "desk_items", "decisions", "handovers", "spec_questions", "signoffs",
                           "pending_memory")), (of("uc_t"), others))
    s.delete_project("uc_t")
    ck("its rows go from each table", set(of("uc_t").values()) == {0}, of("uc_t"))
    ck("another project's stay", of("uc_u") == others, (of("uc_u"), others))
    ck("answers, what was told and crews are kept, as before",
       {t: _n(sdb, t) for t in kept} == kept)
    with s._lock:
        have = set(s._tables_of(s.con))
    ck("DuckDB has none of the twelve tables", not (have & set(CREW_NAMES)), sorted(have & set(CREW_NAMES)))
    s.close()


def crew_takeover():
    print("== Postgres: the twelve tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        old_ddl_postgres(dsn, seed=CREW)

        def pg():
            return psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}")

        def snapshot():
            out = {}
            with pg() as c:
                for t in CREW_NAMES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable, column_default FROM "
                                     "information_schema.columns WHERE table_schema = %s AND table_name = %s "
                                     "ORDER BY column_name", [sch, t]).fetchall()
                    rows = c.execute(f"SELECT {', '.join(chr(34) + col[0] + chr(34) for col in cols)} "
                                     f"FROM {t}").fetchall()
                    idx = c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND "
                                    "tablename = %s ORDER BY indexname", [sch, t]).fetchall()
                    out[t] = (cols, sorted(tuple(map(str, r)) for r in rows), idx)
            return out

        before = snapshot()
        ck("the old DDL made the twelve tables, with the sample rows",
           {t: len(before[t][1]) for t in CREW_NAMES} == CREW_COUNTS)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its columns, types, defaults, rows and indexes, is as the old code left it",
           snapshot() == before, [t for t in before if snapshot()[t] != before[t]])
        ck("each table's columns, types and primary key are what tables.py declares",
           ts.shape(sdb.engine, CREW_NAMES) == {t: ts.declared(sdb.engine)[t] for t in CREW_NAMES},
           ts.shape(sdb.engine, CREW_NAMES))
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        ts.step_down(sdb, "0020_review_tables")
        ck("a downgrade keeps the tables, rows and indexes", snapshot() == before
           and ts.versions(sdb) == ["0020_review_tables"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it leaves them as they are", snapshot() == before)
        with sdb.begin() as c:
            dk = {r.id: r.options for r in c.execute(desk_table.select())}
        ck("Core reads the JSON the old code wrote", dk == {"dk_1": ["yes", "no"], "dk_2": None}, dk)

        # a database from before the later columns, the defaults and the index
        ts.step_down(sdb, "0020_review_tables")
        with pg() as c:
            c.execute("ALTER TABLE grants DROP COLUMN needs_lead")
            c.execute("ALTER TABLE waiting_told DROP COLUMN closed")
            c.execute("ALTER TABLE handovers DROP COLUMN override_person_id")
            c.execute("DROP INDEX idx_answers_at")
        sdb.upgrade()
        ck("missing columns and the missing index are added", ts.shape(sdb.engine, CREW_NAMES)
           == {t: ts.declared(sdb.engine)[t] for t in CREW_NAMES}
           and ts.index_columns(sdb.engine, "answers", "idx_answers_at") == ["answered_at"])
        with pg() as c:
            lead = c.execute("SELECT id, needs_lead FROM grants ORDER BY id").fetchall()
        ck("rows already there read needs_lead false", lead == [("G10", False), ("G2", False)], lead)
        sdb.close()
    finally:
        _drop_schema(sch)

    # a database the revision made itself: the code before it (rc.1) can still write to it
    sch, dsn = _pg_schema()
    try:
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("INSERT INTO grants (id) VALUES ('G1')")
            c.execute("INSERT INTO pending_memory (id) VALUES ('pm')")
            got = (c.execute("SELECT needs_lead FROM grants").fetchone()[0],
                   c.execute("SELECT attempts FROM pending_memory").fetchone()[0])
            c.execute("INSERT INTO crews (id, name, person_id, machine) VALUES ('a', 'n', 'p', '')")
            try:
                c.execute("INSERT INTO crews (id, name, person_id, machine) VALUES ('b', 'n', 'p', '')")
                twice = True
            except psycopg.errors.UniqueViolation:
                twice = False
            idx = c.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND indexname IN "
                            "('idx_answers_at', 'crews_person_id_name_machine_key')", [sch]).fetchone()[0]
        ck("a new database has the old defaults, the crew's unique constraint and the index",
           got == (False, 0) and not twice and idx == 2, (got, twice, idx))
        sdb.close()
    finally:
        _drop_schema(sch)


# ── merge queue, integration, secret and guardrail tables (TR-1617) ─────────
INTEGR = {
    "guardrail_rules": (("id", "scope", "project", "kind", "text", "created_by", "created_at"), [
        ("R10", "project", "uc_demo", "must", "", None, None),
        ("R2", "factory", None, "never", "force push", "Kim", _T),
    ]),
    "guardrail_pairs": (("id", "scope", "project_uid", "hook", "integration", "settings", "on_fail",
                         "created_at", "created_by"), [
        ("GP2", "factory", None, "pre_build", "rail_a", '{"token": "g"}', None, _T, "Kim"),
        ("GP10", "project", "uc_demo", "pre_merge", "rail_a", "", "warn", _LATER, None),
        # the same pair as GP2 once neither holds settings: the upgrade keeps the oldest
        ("GP11", "factory", None, "pre_build", "rail_a", None, "block", _LATER, None),
    ]),
    "integration_choices": (("slot", "name", "settings", "updated_at", "updated_by"), [
        ("tickets", "tixa", '{"project": "FAC"}', _T, "Kim"),
        ("review", "reva", None, _T, None),
    ]),
    "integration_connections": (("slot", "name", "settings", "updated_at", "updated_by"), [
        ("tickets", "tixa", '{"url": "https://a", "token": "t1"}', _T, "Kim"),
        ("ci", "cia", None, None, None),
    ]),
    "project_integrations": (("project_uid", "slot", "name", "settings", "updated_at", "updated_by"), [
        ("uc_demo", "tickets", "tixa", "{}", _T, "Kim"),
        ("uc_demo", "ci", "gone_ci", '{"url": "u"}', _LATER, None),
        ("uc_old", "review", "reva", None, None, None),
    ]),
    "pending_writeback": (("ticket", "integration", "status", "attempts", "last_error", "updated_at"), [
        ("tk_1", "linear", "done", 2, "down", _LATER),
        ("tk_2", "linear", None, None, None, None),
    ]),
    "project_secrets": (("project", "name", "value", "set_by", "set_at", "used_by", "used_at"), [
        ("uc_demo", "API_KEY", None, "Kim", _T, None, None),       # the value is sealed in the case
        ("uc_demo", "EMPTY", "", None, None, "Kim", _LATER),
    ]),
    "project_required_checks": (("project", "commands", "updated_at", "updated_by"), [
        ("uc_demo", '["make test"]', _T, "Kim"),
        ("uc_empty", "[]", None, None),
        ("uc_null", None, None, None),
    ]),
    "merge_queue": (("id", "repo", "base", "pr", "head", "ticket", "project_uid", "state", "reason",
                     "desk_item", "approved_head", "entered_at", "updated_at", "before_files",
                     "unknown_ticks"), [
        ("mq_1", "o/r", "main", 1, "abc", "tk_1", "uc_demo", "queued", None, None, None, _T, _T,
         '{"head": "abc", "files": ["a"]}', 3),
        ("mq_2", "o/r", "main", 2, "def", None, None, "merged", "", None, None, None, None, None, None),
    ]),
    "project_merge_method": (("project", "method", "updated_at", "updated_by"), [
        ("uc_demo", "rebase", _T, "Kim"),
        ("uc_null", None, None, None),
    ]),
}
INTEGR_COUNTS = {t: len(rows) for t, (_, rows) in INTEGR.items()}
INTEGR_NAMES = list(INTEGR)
_PLAIN = "sk-live-0123456789"


def integr_copy():
    print("== the merge queue, integration, secret and guardrail tables are copied at the first start ==")
    key = os.environ.get(project_secrets.KEY_ENV)
    os.environ[project_secrets.KEY_ENV] = "k" * 32
    try:
        seed = copy.deepcopy(INTEGR)
        cols, rows = seed["project_secrets"]
        sealed = project_secrets.seal(_PLAIN)
        rows[0] = rows[0][:2] + (sealed,) + rows[0][3:]
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.duckdb")
        con = duckdb.connect(path)
        old_ddl_duckdb(con, seed={**SEED, **seed})
        con.execute("DROP INDEX IF EXISTS idx_merge_queue_repo")   # it holds back the ALTER
        con.execute("ALTER TABLE merge_queue DROP COLUMN unknown_ticks")   # a file from before it
        con.execute("CHECKPOINT")
        con.close()
        s = Store(path, state_dsn="")
        sdb = s.sessions_db
        with sdb.begin() as c:
            ledger = {r[0]: r[1] for r in
                      c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
        ck("the ledger counts each table as DuckDB held it",
           {t: ledger.get(t) for t in INTEGR_NAMES} == INTEGR_COUNTS, ledger)

        got = s.secret_rows("uc_demo")
        ck("a secret decrypts after the local copy, byte for byte as sealed",
           [r["name"] for r in got] == ["API_KEY", "EMPTY"] and got[0]["value"] == sealed
           and project_secrets.unseal(got[0]["value"]) == _PLAIN, got)
        ck("its times arrive; an empty value stays empty and reads as unreadable",
           got[0]["set_at"] == _T and got[0]["used_at"] is None and got[1]["value"] == ""
           and got[1]["used_at"] == _LATER and project_secrets.unseal(got[1]["value"]) is None, got)
        project_secrets._cache.update(version=None, pairs=[])
        ck("session scrubbing finds the copied secret",
           project_secrets.scrub(f"key={_PLAIN}", project_secrets.scrub_pairs(s)) == "key=[secret API_KEY]")
        project_secrets._cache.update(version=None, pairs=[])

        ck("rules arrive by number, empty text kept",
           [(r["id"], r["text"], r["created_at"]) for r in s.list_guardrail_rules()]
           == [("R2", "force push", _T), ("R10", "", None)], s.list_guardrail_rules())
        pairs = s.list_guardrail_pairs()
        ck("the data upgrade runs on the copied pairs: settings gone, the twin removed, null on_fail is block",
           [(p["id"], p["on_fail"], p["settings"]) for p in pairs]
           == [("GP2", "block", {}), ("GP10", "warn", {})], pairs)
        ck("and a pair's settings became its integration's connection",
           s.stored_settings("guardrails", "rail_a") == {"token": "g"}, s.stored_settings("guardrails", "rail_a"))
        ck("a connection the cell had is kept as it was",
           s.stored_settings("tickets", "tixa") == {"url": "https://a", "token": "t1"})
        ck("a project's secret settings moved to a new connection, and its row keeps none",
           s.stored_settings("ci", "gone_ci") == {"url": "u"}
           and s.get_project_choice("uc_demo", "ci")["settings"] == {}, s.stored_settings("ci", "gone_ci"))
        ck("a choice with no settings has a connection, empty",
           s.get_connection("review", "reva") is not None and s.stored_settings("review", "reva") == {})
        ck("the picks arrive", s.project_choice_names("tickets", ["uc_demo", "uc_old"]) == {"uc_demo": "tixa"}
           and s.get_project_choice("uc_old", "review")["name"] == "reva")
        ck("required checks arrive; [] and null read as none",
           [s.get_required_checks(u) for u in ("uc_demo", "uc_empty", "uc_null", "uc_none")]
           == [["make test"], [], [], []])
        q = s.merge_queue_list("o/r", "main")
        ck("the merge queue arrives, oldest first with no time last; a column the file lacks reads 0",
           [(r["id"], r["unknown_ticks"], r["entered_at"]) for r in q] == [("mq_1", 0, _T), ("mq_2", 0, None)]
           and q[0]["before_files"] == '{"head": "abc", "files": ["a"]}' and q[0]["pr"] == 1
           and q[1]["reason"] == "" and q[1]["before_files"] is None, q)
        ck("the merge method arrives; a null one reads squash",
           [s.get_merge_method(u) for u in ("uc_demo", "uc_null")] == ["rebase", "squash"])
        w = s.list_pending_writebacks()
        ck("the waiting write-backs arrive, the one with no time last",
           [(x["ticket"], x["attempts"], x["status"]) for x in w] == [("tk_1", 2, "done"), ("tk_2", None, None)]
           and w[0]["updated_at"] == _LATER, w)
        with s._lock:
            n = {t: s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in INTEGR_NAMES}
        ck("DuckDB still has its rows: nothing is deleted there", n == INTEGR_COUNTS, n)
        s.set_merge_method("uc_demo", "merge", "ann")
        s.close()
        s = Store(path, state_dsn="")
        ck("a second start copies nothing and the upgrade changes nothing more",
           s.get_merge_method("uc_demo") == "merge" and len(s.list_guardrail_pairs()) == 2
           and _n(s.sessions_db, "integration_connections") == 5, _n(s.sessions_db, "integration_connections"))
        s.close()
    finally:
        if key is None:
            os.environ.pop(project_secrets.KEY_ENV, None)
        else:
            os.environ[project_secrets.KEY_ENV] = key


def _another_process(s, d):
    """The Store as a second process on the same database has it: its own write lock and its own
    engine."""
    o = copy.copy(s)
    o._swrite = threading.RLock()
    o.sessions_db = SessionStore(d, s.state_dsn or None)
    return o


def integr_methods():
    print("== rules, pairs, integrations, secrets, checks, merge queue and write-backs through Core ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    sdb = s.sessions_db
    s.create_project("uc_t", "custom", "Tee", {})
    s.create_project("uc_u", "custom", "U", {})

    def stored(sql, **args):
        with sdb.begin() as conn:
            return conn.execute(text(sql), args).scalar()

    print("== guardrail rules ==")
    r1 = s.add_guardrail_rule("factory", None, "must", "a", "Kim")
    r2 = s.add_guardrail_rule("project", "uc_t", "never", "", None)
    ck("numbered for the cell, with every field",
       (r1["id"], r2["id"], r1["project"], r2["project"], r2["created_by"], r2["text"])
       == ("R1", "R2", None, "uc_t", None, "") and r1["created_at"].utcoffset().total_seconds() == 0, (r1, r2))
    ck("an id reads in any case, with spaces; null and unknown ids read as None",
       s.get_guardrail_rule(" r1 ")["text"] == "a" and s.get_guardrail_rule(None) is None
       and s.get_guardrail_rule("") is None and s.get_guardrail_rule("R9") is None)
    ck("a text changes; an unknown rule gives None",
       s.set_guardrail_rule_text("r1", "b")["text"] == "b" and s.set_guardrail_rule_text("R9", "x") is None)
    ck("a delete says whether a rule went",
       s.delete_guardrail_rule("r1") is True and s.delete_guardrail_rule("R1") is False
       and s.delete_guardrail_rule(None) is False)
    ck("the next number follows the highest left",
       s.add_guardrail_rule("factory", None, "must", "c")["id"] == "R3")

    print("== guardrail pairs ==")
    p1, how1 = s.save_guardrail_pair("factory", None, "pre_build", "rail_a", "block", "Kim")
    twin, how2 = s.save_guardrail_pair("factory", None, "pre_build", "rail_a", "block", "Ann")
    p2, _ = s.save_guardrail_pair("project", "uc_t", "pre_build", "rail_b", "warn", None)
    ck("a pair is added once; an exact twin adds nothing",
       (p1["id"], how1, how2, twin["id"], twin["created_by"], p2["id"]) == ("GP1", "added", "twin", "GP1", "Kim",
                                                                            "GP2"), (p1, twin, p2))
    changed, how3 = s.save_guardrail_pair("project", "uc_t", "pre_merge", "rail_b", "block", None,
                                          pair_id=" gp2 ")
    ck("a pair id changes that pair only", how3 == "updated" and changed["id"] == "GP2"
       and s.get_guardrail_pair("gp2")["hook"] == "pre_merge" and s.get_guardrail_pair("GP1")["hook"] == "pre_build",
       changed)
    try:
        s.save_guardrail_pair("factory", None, "pre_build", "rail_b", "block", pair_id="GP2")
        refused = False
    except LookupError:
        refused = True
    ck("a pair id of another scope is refused", refused and s.get_guardrail_pair("GP2")["scope"] == "project")
    s.save_guardrail_pair("project", "uc_u", "pre_merge", "rail_c", "block")
    ck("the pairs of a run: the factory's, then the project's own",
       [p["id"] for p in s.guardrail_pairs_for_run("pre_merge", "uc_t")] == ["GP2"]
       and [p["id"] for p in s.guardrail_pairs_for_run("pre_build", "uc_t")] == ["GP1"]
       and [p["id"] for p in s.guardrail_pairs_for_run("pre_build", None)] == ["GP1"])
    ck("the lists by scope", [p["id"] for p in s.list_guardrail_pairs("project", "uc_t")] == ["GP2"]
       and [p["id"] for p in s.list_guardrail_pairs("project", None)] == []
       and len(s.list_guardrail_pairs()) == 3)
    ck("get of an unknown, blank or null pair is None",
       [s.get_guardrail_pair(x) for x in ("GP9", "", None)] == [None, None, None])
    ck("the pair's settings are stored as an empty object",
       stored("SELECT settings FROM guardrail_pairs WHERE id = 'GP1'") == "{}")

    print("== integrations ==")
    ck("nothing chosen: no choice, no connection",
       s.get_choice("tickets") is None and s.get_connection("tickets", "tixa") is None
       and s.list_connections("tickets") == [] and s.stored_settings("tickets", "tixa") == {})
    s.set_choice("tickets", "tixa", {"token": "t1"}, "Kim")
    s.set_choice("tickets", "tixa", {"token": "t2"}, "Ann")
    ck("connecting again replaces the one row", _n(sdb, "integration_connections") == 1
       and s.stored_settings("tickets", "tixa") == {"token": "t2"}
       and s.get_choice("tickets")["updated_by"] == "Ann")
    raw = stored("SELECT settings FROM integration_connections WHERE name = 'tixa'")
    ck("settings are stored as one JSON object, not text inside text", raw == '{"token": "t2"}', raw)
    s.set_project_choice("uc_t", "tickets", "tixa", None, "Kim")
    s.set_project_choice("uc_t", "tickets", "tixa", {}, "Ann")
    ck("a project's pick is one row per slot", _n(sdb, "project_integrations") == 1
       and s.get_project_choice("uc_t", "tickets")["updated_by"] == "Ann"
       and s.get_project_choice("uc_u", "tickets") is None
       and s.projects_with_choice("tickets", ["uc_t", "uc_u", "", None]) == {"uc_t"}
       and s.project_choice_names("tickets", []) == {})
    many = [f"uc_{i}" for i in range(1201)] + ["uc_t"]
    ck("a long list of projects is read in parts", s.project_choice_names("tickets", many) == {"uc_t": "tixa"})
    ck("clearing a pick says whether there was one",
       s.clear_project_choice("uc_t", "tickets") is True and s.clear_project_choice("uc_t", "tickets") is False)

    print("== secrets ==")
    v0 = s.secrets_version()
    s.set_secret("uc_t", "B_KEY", "sealed-1", "Kim")
    s.set_secret("uc_t", "A_KEY", "sealed-a", "Kim")
    s.secrets_used("uc_t", ["B_KEY", "NOPE"], "builder")
    used = {r["name"]: r for r in s.secret_rows("uc_t")}
    s.set_secret("uc_t", "B_KEY", "sealed-2", "Ann")
    now = {r["name"]: r for r in s.secret_rows("uc_t")}
    ck("secrets by name, with every field", list(now) == ["A_KEY", "B_KEY"]
       and set(now["A_KEY"]) == {"project", "name", "value", "set_by", "set_at", "used_by", "used_at"}
       and now["A_KEY"]["used_at"] is None, now)
    ck("a new value keeps who used it and when", (now["B_KEY"]["value"], now["B_KEY"]["set_by"],
       now["B_KEY"]["used_by"], now["B_KEY"]["used_at"]) == ("sealed-2", "Ann", "builder", used["B_KEY"]["used_at"])
       and used["B_KEY"]["used_at"] is not None, now["B_KEY"])
    s.secrets_used("uc_t", [], "x")
    ck("a removal says whether there was one", s.delete_secret("uc_t", "A_KEY") is True
       and s.delete_secret("uc_t", "A_KEY") is False and [r["name"] for r in s.secret_rows("uc_t")] == ["B_KEY"]
       and len(s.all_secret_rows()) == 1 and s.secret_rows("uc_none") == [])
    ck("the version moves with a change", s.secrets_version() > v0)

    print("== required checks and the merge method ==")
    ck("none until set", s.get_required_checks("uc_t") == [] and s.get_merge_method("uc_t") == "squash")
    s.set_required_checks("uc_t", ["make test", "  ", "make lint"], "Kim")
    ck("saved cleaned and in order", s.get_required_checks("uc_t") == ["make test", "make lint"])
    s.set_required_checks("uc_t", [], "Ann")
    ck("an empty list is no required checks, one row", s.get_required_checks("uc_t") == []
       and stored("SELECT commands FROM project_required_checks WHERE project = 'uc_t'") == "[]"
       and _n(sdb, "project_required_checks") == 1)
    s.set_merge_method("uc_t", " Rebase ", "Kim")
    s.set_merge_method("uc_t", "merge", "Kim")
    refused = []
    for bad in ("", None, "ff"):
        try:
            s.set_merge_method("uc_t", bad, "Kim")
        except ValueError:
            refused.append(bad)
    ck("one method per project; a blank, null or unknown one is refused",
       s.get_merge_method("uc_t") == "merge" and refused == ["", None, "ff"]
       and _n(sdb, "project_merge_method") == 1, (refused, s.get_merge_method("uc_t")))

    print("== merge queue ==")
    a = s.merge_queue_add("o/r", "main", 1, "h1", None, "uc_t")
    again = s.merge_queue_add("o/r", "main", 1, "h9", None, "uc_t")
    b = s.merge_queue_add("o/r", "main", 2, "h2", "tk_x", "uc_u")
    ck("one open row per pull request, every field present", again["id"] == a["id"] and again["head"] == "h1"
       and b["id"] != a["id"] and a["unknown_ticks"] == 0 and a["before_files"] is None
       and a["entered_at"] == a["updated_at"] and len(a) == 15, a)
    ck("get of an unknown row is None", s.merge_queue_get("mq_none") is None and s.merge_queue_get(None) is None)
    ck("not claimed without the approval it needs", s.merge_queue_claim(a["id"], approved_only=True) is False)
    s.merge_queue_update(a["id"], approved_head="other")
    ck("an approval of another commit does not count", s.merge_queue_claim(a["id"], approved_only=True) is False)
    s.merge_queue_update(a["id"], approved_head="h1")
    ck("claimed once; the other waits while it is merging",
       s.merge_queue_claim(a["id"], approved_only=True) is True and s.merge_queue_claim(a["id"]) is False
       and s.merge_queue_claim(b["id"]) is False and s.merge_queue_claim("mq_none") is False
       and s.merge_queue_get(a["id"])["state"] == "merging")
    before = s.merge_queue_get(a["id"])["updated_at"]
    s.merge_queue_update(a["id"])
    try:
        s.merge_queue_update(a["id"], entered_at=_T)
        bad = False
    except ValueError:
        bad = True
    ck("no field is no write; a field that is not the queue's is refused",
       bad and s.merge_queue_get(a["id"])["updated_at"] == before)
    s.merge_queue_update(a["id"], state="merged", reason=None, before_files='{"head": "h1"}', unknown_ticks=2)
    got = s.merge_queue_get(a["id"])
    ck("an update writes its fields and moves updated_at", (got["state"], got["before_files"], got["unknown_ticks"])
       == ("merged", '{"head": "h1"}', 2) and got["updated_at"] > before, got)
    s.merge_queue_update(b["id"], state="merged")
    c3 = s.merge_queue_add("o/r", "main", 3, "h3", None, None)
    s.merge_queue_update(c3["id"], state="sent_back")
    ck("the last merged, leaving one out", s.merge_queue_last_merged("o/r", "main")["pr"] == 2
       and s.merge_queue_last_merged("o/r", "main", not_pr=2)["pr"] == 1
       and s.merge_queue_last_merged("o/r", "other") is None)
    d4 = s.merge_queue_add("o/r", "main", 1, "h4", None, None)
    ck("a pull request whose row is finished gets a new one; the open, sent back and listed rows",
       d4["id"] != a["id"] and [r["id"] for r in s.merge_queue_open()] == [d4["id"]]
       and [r["id"] for r in s.merge_queue_sent_back()] == [c3["id"]]
       and [r["pr"] for r in s.merge_queue_list("o/r", "main")] == [1, 2, 3, 1])
    s.merge_queue_delete(d4["id"])
    ck("a deleted row is gone", s.merge_queue_get(d4["id"]) is None)

    print("== write-backs ==")
    tk = s.create_ticket("uc_t", "tares", "w")
    other_tk = s.create_ticket("uc_u", "tares", "w")
    n = [s.pending_writeback_fail(tk, "linear", "done", "down", _T),
         s.pending_writeback_fail(tk, "linear", "done", "still", _LATER),
         s.pending_writeback_fail(tk, "linear", "doing", "", _LATER),
         s.pending_writeback_fail(tk, "github_issues", "done", None, _T),
         s.pending_writeback_fail(other_tk, "linear", "done", "down", _T)]
    ck("attempts count while the status wanted is the same", n == [1, 2, 1, 1, 1], n)
    row = s.get_pending_writeback(tk, "linear")
    ck("the row holds the newest status and error", (row["status"], row["attempts"], row["last_error"],
       row["updated_at"]) == ("doing", 1, "", _LATER) and s.get_pending_writeback("tk_none") is None, row)
    ck("listed oldest first, by integration and by project",
       [(x["ticket"], x["integration"]) for x in s.list_pending_writebacks(project="uc_t")]
       == [(tk, "github_issues"), (tk, "linear")]
       and [x["ticket"] for x in s.list_pending_writebacks("linear", "uc_u")] == [other_tk]
       and len(s.list_pending_writebacks()) == 3 and s.list_pending_writebacks(project="uc_none") == [])
    ck("a ticket with one waiting is left alone", s.update_ticket(tk, unless_pending=True, title="x") is False
       and s.get_ticket("uc_t", tk)["title"] == "w")
    ck("clear says whether there was one", s.pending_writeback_clear(tk, "linear") is True
       and s.pending_writeback_clear(tk, "linear") is False and s.pending_writeback_clear(tk) is True
       and s.update_ticket(tk, unless_pending=True, title="x") is True
       and s.get_ticket("uc_t", tk)["title"] == "x")
    gone = s.create_ticket("uc_t", "tares", "gone")
    s.pending_writeback_fail(gone, "linear", "done", "down", _T)
    s.delete_ticket(gone)
    ck("a deleted ticket takes its write-back", s.get_pending_writeback(gone) is None
       and s.get_pending_writeback(other_tk) is not None)

    print("== two processes write one after the other ==")
    first, second = _another_process(s, d), _another_process(s, d)     # an engine each

    def in_turn(prefix, call):
        """`call` on both. The first is held after the statement that starts with `prefix` (it has
        read, it has not written) while the second starts: what the first got, what the second
        got, whether the second could read meanwhile, and the errors."""
        held, second_read, release = threading.Event(), threading.Event(), threading.Event()
        got, errors = {}, []

        def hold(conn, cursor, statement, *a):
            if statement.startswith(prefix) and not held.is_set():
                held.set()
                release.wait(10)

        def saw(conn, cursor, statement, *a):
            if statement.startswith(prefix):
                second_read.set()

        def run(who, o):
            try:
                got[who] = call(o)
            except Exception as e:
                errors.append(f"{who}: {type(e).__name__}: {e}"[:200])

        threads = [threading.Thread(target=run, args=a) for a in (("first", first), ("second", second))]
        event.listen(first.sessions_db.engine, "after_cursor_execute", hold)
        event.listen(second.sessions_db.engine, "after_cursor_execute", saw)
        try:
            threads[0].start()
            if not held.wait(10):
                errors.append("the first never read")
            threads[1].start()
            # under SQLite's busy timeout (5 s): the second must still be waiting, not refused
            read_meanwhile = second_read.wait(1)
            release.set()
            for t in threads:
                t.join(60)
        finally:
            release.set()
            event.remove(first.sessions_db.engine, "after_cursor_execute", hold)
            event.remove(second.sessions_db.engine, "after_cursor_execute", saw)
        return got.get("first"), got.get("second"), read_meanwhile, errors

    one, two, early, errors = in_turn("SELECT max(", lambda o: o.add_guardrail_rule("factory", None, "must", "p")["id"])
    ck("two writers at once get two different rule numbers",
       not errors and not early and one and two and one != two, (one, two, early, errors))
    one, two, early, errors = in_turn(
        "SELECT guardrail_pairs", lambda o: o.save_guardrail_pair("factory", None, "pre_merge", "rail_p", "block")[1])
    ck("two writers at once add one pair",
       not errors and not early and (one, two) == ("added", "twin"), (one, two, early, errors))
    one, two, early, errors = in_turn(
        "SELECT merge_queue", lambda o: o.merge_queue_add("o/p", "main", 7, "h", None, None)["id"])
    ck("two writers at once make one queue row",
       not errors and not early and one and one == two, (one, two, early, errors))
    one, two, early, errors = in_turn(
        "SELECT pending_writeback.attempts",
        lambda o: o.pending_writeback_fail("tk_par", "linear", "done", "e", _T))
    ck("two failures at once count two attempts",
       not errors and not early and (one, two) == (1, 2), (one, two, early, errors))
    first.sessions_db.close()
    second.sessions_db.close()

    print("== deleting a project ==")
    for uid in ("uc_t", "uc_u"):
        s.set_required_checks(uid, ["make test"], "Kim")
        s.set_merge_method(uid, "merge", "Kim")
        s.set_secret(uid, "KEY", "sealed", "Kim")
        s.add_guardrail_rule("project", uid, "must", "x")
        s.save_guardrail_pair("project", uid, "pre_build", "rail_d", "block")
        s.set_project_choice(uid, "tickets", "tixa", {}, "Kim")
        s.merge_queue_add("o/d", "main", 1 if uid == "uc_t" else 2, "h", None, uid)
        s.pending_writeback_fail(s.create_ticket(uid, "tares", "d"), "linear", "done", "down", _T)
    by_uid = {"project_required_checks": "project", "project_merge_method": "project",
              "project_secrets": "project", "guardrail_rules": "project", "guardrail_pairs": "project_uid",
              "project_integrations": "project_uid", "merge_queue": "project_uid"}

    def of(uid):
        out = {t: stored(f"SELECT count(*) FROM {t} WHERE {col} = :p", p=uid) for t, col in by_uid.items()}
        out["pending_writeback"] = len(s.list_pending_writebacks(project=uid))
        return out

    mine, theirs, kept = of("uc_t"), of("uc_u"), _n(sdb, "integration_connections")
    ck("both projects have rows in each before", all(mine.values()) and all(theirs.values()), (mine, theirs))
    v0 = s.secrets_version()
    s.delete_project("uc_t")
    ck("the required checks of a deleted project go", stored(
        "SELECT count(*) FROM project_required_checks WHERE project = 'uc_t'") == 0
       and s.get_required_checks("uc_t") == [])
    ck("its rows go from each table", set(of("uc_t").values()) == {0}
       and stored("SELECT count(*) FROM pending_writeback WHERE ticket = :t", t=tk) == 0, of("uc_t"))
    ck("another project's stay, and the factory's connections", of("uc_u") == theirs
       and _n(sdb, "integration_connections") == kept, (of("uc_u"), theirs))
    ck("the secrets cache is told", s.secrets_version() > v0)
    with s._lock:
        have = set(s._tables_of(s.con))
    ck("DuckDB has none of the ten tables", not (have & set(INTEGR_NAMES)), sorted(have & set(INTEGR_NAMES)))
    s.close()


def integr_takeover():
    print("== Postgres: the ten tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        old_ddl_postgres(dsn, seed=INTEGR)

        def pg():
            return psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}")

        def snapshot():
            out = {}
            with pg() as c:
                for t in INTEGR_NAMES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable, column_default FROM "
                                     "information_schema.columns WHERE table_schema = %s AND table_name = %s "
                                     "ORDER BY column_name", [sch, t]).fetchall()
                    rows = c.execute(f"SELECT {', '.join(chr(34) + col[0] + chr(34) for col in cols)} "
                                     f"FROM {t}").fetchall()
                    idx = c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND "
                                    "tablename = %s ORDER BY indexname", [sch, t]).fetchall()
                    out[t] = (cols, sorted(tuple(map(str, r)) for r in rows), idx)
            return out

        before = snapshot()
        ck("the old DDL made the ten tables, with the sample rows",
           {t: len(before[t][1]) for t in INTEGR_NAMES} == INTEGR_COUNTS)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its columns, types, defaults, rows and indexes, is as the old code left it",
           snapshot() == before, [t for t in before if snapshot()[t] != before[t]])
        ck("each table's columns, types and primary key are what tables.py declares",
           ts.shape(sdb.engine, INTEGR_NAMES) == {t: ts.declared(sdb.engine)[t] for t in INTEGR_NAMES},
           ts.shape(sdb.engine, INTEGR_NAMES))
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        ts.step_down(sdb, "0021_crew_tables")
        ck("a downgrade keeps the tables, rows and indexes", snapshot() == before
           and ts.versions(sdb) == ["0021_crew_tables"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it leaves them as they are", snapshot() == before)
        with sdb.begin() as c:
            conns = {r.name: r.settings for r in c.execute(connections_table.select())}
        ck("Core reads the JSON the old code wrote",
           conns == {"tixa": {"url": "https://a", "token": "t1"}, "cia": None}, conns)

        # a database from before the later columns and the index
        ts.step_down(sdb, "0021_crew_tables")
        with pg() as c:
            c.execute("ALTER TABLE merge_queue DROP COLUMN before_files")
            c.execute("ALTER TABLE merge_queue DROP COLUMN unknown_ticks")
            c.execute("DROP INDEX idx_merge_queue_repo")
        sdb.upgrade()
        ck("missing columns and the missing index are added", ts.shape(sdb.engine, INTEGR_NAMES)
           == {t: ts.declared(sdb.engine)[t] for t in INTEGR_NAMES}
           and ts.index_columns(sdb.engine, "merge_queue", "idx_merge_queue_repo") == ["repo", "base"])
        sdb.close()
    finally:
        _drop_schema(sch)

    # a database the revision made itself: the code before it can still write to it
    sch, dsn = _pg_schema()
    try:
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("INSERT INTO guardrail_pairs (id) VALUES ('GP1')")
            got = c.execute("SELECT on_fail FROM guardrail_pairs").fetchone()[0]
            idx = c.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND indexname = "
                            "'idx_merge_queue_repo'", [sch]).fetchone()[0]
        ck("a new database has the old default and index", got == "block" and idx == 1, (got, idx))
        sdb.close()
    finally:
        _drop_schema(sch)


# ── repo maps and brain tables (TR-1618) ────────────────────────────────────
BRAIN = {
    "repo_maps": (("repo", "body", "updated_at", "updated_by"), [
        ("o/web", "# map", _T, "st1"),
        ("o/api", "", None, None),
    ]),
    "repo_map_history": (("repo", "changed_at", "by_station", "project", "note"), [
        ("o/web", _T, "st1", None, "first"),
        ("o/web", _LATER, "st2", "uc_demo", "second"),
        ("o/web", None, None, None, None),
    ]),
    "brain_repos": (("repo", "repo_source_id", "state", "files_done", "files_total", "indexed_commit",
                     "last_synced", "error", "added_by", "added_at", "place", "job_started_at",
                     "sync_failures", "sync_error"), [
        ("o/web", "src-1", "ready", 5, 9, "abc", _LATER, None, "pe_gone", _T, None, _T, 2, "boom"),
        ("o/api", None, "queued", None, None, None, None, "", None, None, 3, None, None, None),
    ]),
    "brain_repo_folders": (("repo", "path", "chosen_by", "chosen_at"), [
        ("o/web", "docs/adr", None, _T),
        ("o/web", "b", "pe_gone", None),
    ]),
    "brain_repo_updates": (("id", "repo", "at", "kind", "ok", "indexed_commit", "files_changed",
                            "files_deleted", "files_total", "error"), [
        ("bu_1", "o/web", _T, "index", 1, "abc", 9, 0, 9, None),
        ("bu_2", "o/web", _LATER, "update", 0, None, None, None, None, ""),
    ]),
    "brain_calls": (("id", "at", "tool", "deep", "question", "repos", "duration_ms", "input_tokens",
                     "output_tokens", "ok", "error", "station", "person_id", "via"), [
        ("c1", _T, "ask_brain", 0, "why?", '["o/web"]', 12, 3, 4, 1, None, "st", "pe_gone", "mcp"),
        ("c2", _LATER, "search_brain", None, "", "not json", None, None, None, None, None, None, None, None),
        ("c3", None, "ask_brain", 1, None, None, None, None, None, 0, "e", None, None, "console"),
    ]),
}
BRAIN_COUNTS = {t: len(rows) for t, (_, rows) in BRAIN.items()}
BRAIN_NAMES = list(BRAIN)


def brain_copy():
    print("== the repo maps and the brain tables are copied at the first start ==")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.duckdb")
    con = duckdb.connect(path)
    old_ddl_duckdb(con, seed={**SEED, **BRAIN})
    con.execute("ALTER TABLE brain_repos DROP COLUMN sync_error")      # a file from before it
    con.execute("CHECKPOINT")
    con.close()
    s = Store(path, state_dsn="")
    sdb = s.sessions_db
    with sdb.begin() as c:
        ledger = {r[0]: r[1] for r in
                  c.execute(text("SELECT table_name, rows FROM session_store_copies")).all()}
    ck("the ledger counts each table as DuckDB held it",
       {t: ledger.get(t) for t in BRAIN_NAMES} == BRAIN_COUNTS, ledger)
    got = s.get_repo_map("O/Web")
    ck("a repo map arrives, its time in UTC", got == {"repo": "o/web", "body": "# map",
       "updated_at": _T, "updated_by": "st1"} and got["updated_at"].utcoffset().total_seconds() == 0, got)
    ck("an empty map and its nulls arrive", s.get_repo_map("o/api")
       == {"repo": "o/api", "body": "", "updated_at": None, "updated_by": None}, s.get_repo_map("o/api"))
    ck("the map history arrives, newest first with no time last",
       [(h["note"], h["at"], h["project"]) for h in s.repo_map_history("o/web")]
       == [("second", _LATER, "uc_demo"), ("first", _T, None), (None, None, None)],
       s.repo_map_history("o/web"))
    repos = s.list_brain_repos()
    ck("the repos arrive, the one with no date last; null counts and a column the file lacks read 0 or None",
       [(r["repo"], r["files_done"], r["files_total"], r["sync_failures"], r["sync_error"], r["place"],
         r["error"], r["added_by_name"]) for r in repos]
       == [("o/web", 5, 9, 2, None, None, None, None), ("o/api", 0, 0, 0, None, 3, "", None)], repos)
    ck("their times arrive", repos[0]["added_at"] == _T and repos[0]["last_synced"] == _LATER
       and repos[0]["job_started_at"] == _T and repos[1]["added_at"] is None, repos)
    ck("the chosen folders arrive, by path",
       [(f["path"], f["chosen_by"], f["chosen_at"]) for f in s.list_brain_folders("o/web")]
       == [("b", "pe_gone", None), ("docs/adr", None, _T)], s.list_brain_folders("o/web"))
    ups = s.list_brain_repo_updates("o/web")
    ck("the update history arrives, newest first; null counts read 0, empty error kept",
       [(u["kind"], u["ok"], u["indexed_commit"], u["files_changed"], u["files_total"], u["error"])
        for u in ups] == [("update", False, None, 0, 0, ""), ("index", True, "abc", 9, 9, None)], ups)
    calls = s.list_brain_calls()
    ck("the calls arrive, newest first with no time last; bad or absent repos read as none",
       [(c["id"], c["repos"], c["deep"], c["ok"]) for c in calls]
       == [("c2", [], False, None), ("c1", ["o/web"], False, True), ("c3", [], True, False)], calls)
    with s._lock:
        n = {t: s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in BRAIN_NAMES}
    ck("DuckDB still has its rows: nothing is deleted there", n == BRAIN_COUNTS, n)
    s.update_brain_repo("o/web", {"state": "failed"})
    s.close()
    s = Store(path, state_dsn="")
    ck("a second start copies nothing",
       s.get_brain_repo("o/web")["state"] == "failed"
       and {t: _n(s.sessions_db, t) for t in BRAIN_NAMES} == BRAIN_COUNTS)
    s.close()


def brain_methods():
    print("== repo maps and brain tables through Core ==")
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.duckdb"), state_dsn="" if not HARNESS else None)
    sdb = s.sessions_db

    def stored(sql, **args):
        with sdb.begin() as conn:
            return conn.execute(text(sql), args).scalar()

    print("== repo maps ==")
    ck("a repo with no map reads as None and has no history",
       s.get_repo_map("o/none") is None and s.repo_map_history("o/none") == [])
    try:
        s.set_repo_map("o/web", "one", "n", "st", None, _T, "0123456789ab")
        got = "written"
    except MapChanged as e:
        got = e.current
    ck("a writer that read a map there is not gets MapChanged(None), nothing written",
       got is None and s.get_repo_map("o/web") is None, got)
    ck("a first map is written, the name in lowercase, NUL characters dropped",
       s.set_repo_map("O/Web", "on\x00e", "fi\x00rst", "s\x00t", "uc_\x00t", _T) is True
       and s.get_repo_map("o/WEB") == {"repo": "o/web", "body": "one", "updated_at": _T, "updated_by": "st"}
       and s.repo_map_history("o/web") == [{"at": _T, "by": "st", "project": "uc_t", "note": "first"}],
       (s.get_repo_map("o/web"), s.repo_map_history("o/web")))
    ck("the map already there changes nothing, whatever the writer read",
       s.set_repo_map("o/web", "one", "again", "other", None, _LATER, "stale") is False
       and len(s.repo_map_history("o/web")) == 1 and s.get_repo_map("o/web")["updated_by"] == "st")
    try:
        s.set_repo_map("o/web", "two", "n", "st", None, _LATER, "")
        got = "written"
    except MapChanged as e:
        got = e.current
    ck("a stale base is refused with the map that is there",
       got == {"repo": "o/web", "body": "one", "updated_at": _T, "updated_by": "st"}, got)
    ck("the version read lets the write through; history newest first, limited",
       s.set_repo_map("o/web", "two", "second", "st2", None, _LATER, map_version("one")) is True
       and [h["note"] for h in s.repo_map_history("o/web")] == ["second", "first"]
       and [h["note"] for h in s.repo_map_history("O/Web", limit=1)] == ["second"])

    print("== the connected repos ==")
    kim = s.create_person("Kim")["id"]
    ck("no repos yet", s.list_brain_repos() == [] and s.get_brain_repo("o/web") is None)
    ck("add, then refused the second time with the row kept",
       s.add_brain_repo("o/web", "src\x00-1", kim, _LATER) is True
       and s.add_brain_repo("o/web", "src-2", None, _T) is False
       and s.get_brain_repo("o/web")["repo_source_id"] == "src-1")
    s.add_brain_repo("o/api", "", "", _T, state="ready")
    s.add_brain_repo("o/b", None, None, _T)
    s.add_brain_repo("o/a", None, None, None)
    rows = s.list_brain_repos()
    ck("listed oldest first, then by name, no date last", [r["repo"] for r in rows]
       == ["o/api", "o/b", "o/web", "o/a"], [r["repo"] for r in rows])
    ck("empty ids are kept as null; the adder is named",
       (rows[0]["repo_source_id"], rows[0]["added_by"], rows[0]["state"], rows[2]["added_by_name"])
       == (None, None, "ready", "Kim"), rows)
    ck("a new row has its counts at 0",
       (rows[2]["files_done"], rows[2]["files_total"], rows[2]["sync_failures"], rows[2]["state"])
       == (0, 0, 0, "queued"), rows[2])
    ck("update sets what it is given, NUL characters dropped",
       s.update_brain_repo("o/web", {"state": "indexing", "error": "a\x00b", "sync_error": "\x00",
                                     "files_done": 3, "place": 2, "job_started_at": _T}) is True
       and (lambda r: (r["state"], r["error"], r["sync_error"], r["files_done"], r["place"],
                       r["job_started_at"]))(s.get_brain_repo("o/web"))
       == ("indexing", "ab", "", 3, 2, _T), s.get_brain_repo("o/web"))
    ck("None clears a column; only_in holds back and lets through; a missing repo is False",
       s.update_brain_repo("o/web", {"place": None}) is True and s.get_brain_repo("o/web")["place"] is None
       and s.update_brain_repo("o/web", {"state": "ready"}, only_in=("queued",)) is False
       and s.get_brain_repo("o/web")["state"] == "indexing"
       and s.update_brain_repo("o/web", {"state": "failed"}, only_in=("indexing",)) is True
       and s.update_brain_repo("o/none", {"state": "failed"}) is False)
    refused = []
    for bad in ({"repo": "x"}, {}):
        try:
            s.update_brain_repo("o/web", bad)
        except ValueError:
            refused.append(True)
    ck("an unknown column or nothing to set is a ValueError", refused == [True, True], refused)
    ck("requeue only from the state named, and it clears the job",
       s.requeue_brain_repo("o/web", "ready") is False and s.requeue_brain_repo("o/web") is True
       and s.requeue_brain_repo("o/web") is False and s.requeue_brain_repo("o/none") is False
       and (lambda r: (r["state"], r["error"], r["files_done"], r["job_started_at"], r["sync_error"]))(
           s.get_brain_repo("o/web")) == ("queued", None, 0, None, None), s.get_brain_repo("o/web"))
    s.update_brain_repo("o/web", {"files_done": None, "files_total": None, "sync_failures": None})
    ck("a null counter reads as 0",
       (lambda r: (r["files_done"], r["files_total"], r["sync_failures"]))(s.get_brain_repo("o/web"))
       == (0, 0, 0))

    print("== chosen folders ==")
    ck("a first choice adds all, sorted", s.set_brain_folders("o/web", ["b", "a"], kim, _T) == (["a", "b"], []))
    ck("a folder kept keeps its chooser and date; added and removed are said",
       s.set_brain_folders("o/web", ["b", "c"], "", _LATER) == (["c"], ["a"])
       and [(f["path"], f["chosen_by"], f["chosen_by_name"], f["chosen_at"])
            for f in s.list_brain_folders("o/web")]
       == [("b", kim, "Kim", _T), ("c", None, None, _LATER)], s.list_brain_folders("o/web"))
    ck("the same choice changes nothing; an empty one removes all",
       s.set_brain_folders("o/web", ["c", "b"], kim, _LATER) == ([], [])
       and s.set_brain_folders("o/web", [], kim, _LATER) == ([], ["b", "c"])
       and s.list_brain_folders("o/web") == [])

    print("== update history ==")
    ck("no updates yet", s.list_brain_repo_updates("o/web") == [] and s.last_brain_repo_updates() == {})
    for i in range(52):
        s.add_brain_repo_update("o/web", "update", True, _T + timedelta(seconds=i), files_changed=i)
    s.add_brain_repo_update("o/api", "index", False, _T, commit="", files_changed=None, error="it\x00 broke")
    ups = s.list_brain_repo_updates("o/web", limit=100)
    ck("only the newest 50 of a repo are kept, newest first",
       len(ups) == 50 and ups[0]["files_changed"] == 51 and ups[-1]["files_changed"] == 2
       and stored("SELECT count(*) FROM brain_repo_updates WHERE repo = 'o/web'") == 50,
       (len(ups), ups[:1], ups[-1:]))
    ck("20 are listed when no limit is given", len(s.list_brain_repo_updates("o/web")) == 20)
    ck("the last of each repo; an empty commit is null, a null count 0",
       {k: (v["files_changed"], v["ok"], v["indexed_commit"], v["error"])
        for k, v in s.last_brain_repo_updates().items()}
       == {"o/web": (51, True, None, None), "o/api": (0, False, None, "it broke")},
       s.last_brain_repo_updates())

    s.set_brain_folders("o/web", ["a"], None, _T)
    s.set_brain_folders("o/api", ["a"], None, _T)
    ck("deleting a repo takes its folders and history, and no one else's",
       s.delete_brain_repo("o/web") is True and s.delete_brain_repo("o/web") is False
       and s.get_brain_repo("o/web") is None and s.list_brain_folders("o/web") == []
       and s.list_brain_repo_updates("o/web") == [] and len(s.list_brain_folders("o/api")) == 1
       and len(s.list_brain_repo_updates("o/api")) == 1)

    print("== calls to the brain ==")

    def call(cid, tool="ask_brain", at=_T, **more):
        return {"id": cid, "at": at, "tool": tool, **more}

    ck("a call with no cap is kept", s.reserve_brain_call(call(
        "c1", question="w\x00hy", repos=["o/web"], station="s\x00t", person_id=kim, via="mcp")) is True)
    ck("it reads back as running", s.list_brain_calls() == [{
        "id": "c1", "at": _T, "tool": "ask_brain", "deep": False, "question": "why", "repos": ["o/web"],
        "duration_ms": None, "input_tokens": None, "output_tokens": None, "ok": None, "error": None,
        "station": "st", "person_id": kim, "via": "mcp"}], s.list_brain_calls())
    s.finish_brain_call("c1", 12, False, 3, None, "bo\x00om")
    ck("finishing sets how it went",
       (lambda c: (c["duration_ms"], c["ok"], c["input_tokens"], c["output_tokens"], c["error"]))(
           s.list_brain_calls()[0]) == (12, False, 3, None, "boom"), s.list_brain_calls())
    since = _T - timedelta(hours=1)
    ck("the cap counts the calls since then, and a refused call is not kept",
       s.reserve_brain_call(call("c2"), 2, since) is True
       and s.reserve_brain_call(call("c3"), 2, since) is False
       and s.reserve_brain_call(call("c4"), 0, since) is False
       and sorted(c["id"] for c in s.list_brain_calls()) == ["c1", "c2"])
    ck("another depth or tool has its own count, and an older call is not counted",
       s.reserve_brain_call(call("c5", deep=True, station="", person_id="", via=""), 1, since) is True
       and s.reserve_brain_call(call("c6", tool="search_brain", at=_LATER), 1, since) is True
       and s.reserve_brain_call(call("c7"), 1, _LATER) is True)
    ck("with a cap and no since nothing is counted", s.reserve_brain_call(call("c8"), 1, None) is True
       and s.reserve_brain_call(call("c9"), 0, None) is False)
    by = {c["id"]: c for c in s.list_brain_calls()}
    ck("empty station, person and via are null; no question or repos read as such",
       (by["c5"]["station"], by["c5"]["person_id"], by["c5"]["via"], by["c5"]["question"], by["c5"]["repos"],
        by["c5"]["deep"]) == (None, None, None, None, [], True), by["c5"])
    ck("newest first, then by id; since, tool, station and limit narrow the list",
       [c["id"] for c in s.list_brain_calls()] == ["c6", "c1", "c2", "c5", "c7", "c8"]
       and [c["id"] for c in s.list_brain_calls(since=_LATER)] == ["c6"]
       and [c["id"] for c in s.list_brain_calls(tool="search_brain")] == ["c6"]
       and [c["id"] for c in s.list_brain_calls(station="st")] == ["c1"]
       and [c["id"] for c in s.list_brain_calls(limit=2)] == ["c6", "c1"],
       [c["id"] for c in s.list_brain_calls()])

    print("== two processes, each with its own engine ==")
    others, engines = [], []
    for _ in range(4):                  # each as another process: its own lock and its own engine
        o = copy.copy(s)
        o._swrite = threading.RLock()
        o.sessions_db = SessionStore(d, sdb.engine.url.render_as_string(hide_password=False)
                                     if sdb.dialect == "postgresql" else None)
        others.append(o)
        engines.append(o.sessions_db.engine)

    def slow(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(("SELECT repo_maps.body", "SELECT count(*)", "SELECT brain_repo_folders.path",
                                 "INSERT INTO brain_repo_updates")):
            time.sleep(0.15)            # the read of one writer is done before the next one writes

    def at_once(call):
        got, errors = [], []

        def run(i, o):
            try:
                got.append(call(i, o))
            except Exception as e:
                errors.append(type(e).__name__)
        threads = [threading.Thread(target=run, args=(i, o)) for i, o in enumerate(others)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return got, errors

    for e in engines:
        event.listen(e, "after_cursor_execute", slow)
    try:
        s.set_repo_map("o/race", "start", "n", "st", None, _T)
        got, errors = at_once(lambda i, o: o.set_repo_map("o/race", f"body {i}", f"n{i}", "st", None, _LATER,
                                                         map_version("start")))
        ck("four writers of one map version: one wins, the others are told it changed",
           got == [True] and errors == ["MapChanged"] * 3 and len(s.repo_map_history("o/race")) == 2,
           (got, errors, s.repo_map_history("o/race")))
        got, errors = at_once(lambda i, o: o.reserve_brain_call(
            call(f"r{i}", tool="read_doc"), 2, _T - timedelta(hours=1)))
        ck("four calls at once under a cap of 2: two are kept",
           not errors and sorted(got) == [False, False, True, True]
           and len(s.list_brain_calls(tool="read_doc")) == 2, (got, errors))
        got, errors = at_once(lambda i, o: o.add_brain_repo("o/race", f"src-{i}", None, _T))
        ck("one repo connected four times at once is added once",
           not errors and sorted(got) == [False, False, False, True], (got, errors))
        got, errors = at_once(lambda i, o: o.set_brain_folders("o/race", ["a", "b"], None, _T))
        ck("the same folders chosen at once are added by one writer only",
           not errors and sorted(got) == sorted([(["a", "b"], [])] + [([], [])] * 3), (got, errors))
        got, errors = at_once(lambda i, o: o.set_brain_folders("o/race", [f"only{i}"], None, _T))
        have = [f["path"] for f in s.list_brain_folders("o/race")]
        ck("different choices made at once leave one writer's folders, not all of them",
           not errors and len(have) == 1, (have, errors))
        for i in range(50):
            s.add_brain_repo_update("o/race", "update", True, _T + timedelta(seconds=i))
        got, errors = at_once(lambda i, o: o.add_brain_repo_update("o/race", "update", True,
                                                                  _LATER + timedelta(seconds=i)))
        ck("updates kept at once still leave exactly 50",
           not errors and stored("SELECT count(*) FROM brain_repo_updates WHERE repo = 'o/race'") == 50,
           (stored("SELECT count(*) FROM brain_repo_updates WHERE repo = 'o/race'"), errors))
        s.update_brain_repo("o/race", {"state": "failed"})
        got, errors = at_once(lambda i, o: o.requeue_brain_repo("o/race"))
        ck("a failed repo is requeued once", not errors and sorted(got) == [False, False, False, True],
           (got, errors))
    finally:
        for o in others:
            o.sessions_db.close()

    with s._lock:
        have = set(s._tables_of(s.con))
    ck("DuckDB has none of the six tables", not (have & set(BRAIN_NAMES)), sorted(have & set(BRAIN_NAMES)))
    s.close()


def brain_takeover():
    print("== Postgres: the six tables the old DDL made are taken over ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    sch, dsn = _pg_schema()
    try:
        old_ddl_postgres(dsn, seed=BRAIN)

        def pg():
            return psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}")

        def snapshot():
            out = {}
            with pg() as c:
                for t in BRAIN_NAMES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable, column_default FROM "
                                     "information_schema.columns WHERE table_schema = %s AND table_name = %s "
                                     "ORDER BY column_name", [sch, t]).fetchall()
                    rows = c.execute(f"SELECT {', '.join(chr(34) + col[0] + chr(34) for col in cols)} "
                                     f"FROM {t}").fetchall()
                    idx = c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND "
                                    "tablename = %s ORDER BY indexname", [sch, t]).fetchall()
                    out[t] = (cols, sorted(tuple(map(str, r)) for r in rows), idx)
            return out

        before = snapshot()
        ck("the old DDL made the six tables, with the sample rows",
           {t: len(before[t][1]) for t in BRAIN_NAMES} == BRAIN_COUNTS)
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        ck(f"the upgrade stamps {ts.HEAD}", ts.versions(sdb) == [ts.HEAD], ts.versions(sdb))
        ck("every table, its columns, types, defaults, rows and indexes, is as the old code left it",
           snapshot() == before, [t for t in before if snapshot()[t] != before[t]])
        ck("each table's columns, types and primary key are what tables.py declares",
           ts.shape(sdb.engine, BRAIN_NAMES) == {t: ts.declared(sdb.engine)[t] for t in BRAIN_NAMES},
           ts.shape(sdb.engine, BRAIN_NAMES))
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        ts.step_down(sdb, "0022_integration_tables")
        ck("a downgrade keeps the tables, rows and indexes", snapshot() == before
           and ts.versions(sdb) == ["0022_integration_tables"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it leaves them as they are", snapshot() == before)

        # a database from before the later columns and the index
        ts.step_down(sdb, "0022_integration_tables")
        with pg() as c:
            for col in ("place", "job_started_at", "sync_failures", "sync_error"):
                c.execute(f"ALTER TABLE brain_repos DROP COLUMN {col}")
            c.execute("DROP INDEX idx_brain_repo_updates_repo")
        sdb.upgrade()
        ck("missing columns and the missing index are added", ts.shape(sdb.engine, BRAIN_NAMES)
           == {t: ts.declared(sdb.engine)[t] for t in BRAIN_NAMES}
           and ts.index_columns(sdb.engine, "brain_repo_updates", "idx_brain_repo_updates_repo")
           == ["repo", "at"])
        sdb.close()
    finally:
        _drop_schema(sch)

    # a database the revision made itself: the code before it can still write to it
    sch, dsn = _pg_schema()
    try:
        sdb = SessionStore(tempfile.mkdtemp(), dsn)
        sdb.upgrade()
        with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            c.execute("INSERT INTO brain_repos (repo) VALUES ('o/r')")
            c.execute("INSERT INTO brain_calls (id) VALUES ('c')")
            got = (c.execute("SELECT files_done, files_total, sync_failures FROM brain_repos").fetchone(),
                   c.execute("SELECT deep FROM brain_calls").fetchone()[0])
            idx = c.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND indexname = "
                            "'idx_brain_repo_updates_repo'", [sch]).fetchone()[0]
        ck("a new database has the old defaults and index", got == ((0, 0, 0), 0) and idx == 1, (got, idx))
        sdb.close()
    finally:
        _drop_schema(sch)


if __name__ == "__main__":
    ck("settings, the accounts, the project and ticket tables are in MOVED_TABLES, with the five session tables",
       [t.name for t in MOVED_TABLES]
       == ["session_stations", "session_states", "project_sessions", "session_usage", "session_meters",
           "settings", "people", "api_keys", "project_members", "usecases", "usecase_objects", "usecase_log",
           "docs", "skills", "shared_skills", "catalog_sources", "github_credentials", "model_usage",
           "tickets", "ticket_history", "ticket_numbers", "milestones", "milestone_check_results",
           "milestone_acceptances", "ticket_checks", "challenges", "reviews", "review_findings",
           "rule_proposals", "assumptions", "desk_items", "decisions", "handovers", "grants", "pending_memory",
           "crews", "spec_questions", "signoffs", "handover_overrides", "answers", "waiting_told",
           "guardrail_rules", "guardrail_pairs", "integration_choices", "integration_connections",
           "project_integrations", "pending_writeback", "project_secrets", "project_required_checks",
           "merge_queue", "project_merge_method", "repo_maps", "repo_map_history", "brain_repos",
           "brain_repo_folders", "brain_repo_updates", "brain_calls"])
    if not HARNESS:
        settings_copy()
    legacy_file()
    if not HARNESS:
        cli_edition()
    copy_units()
    sqlite_and_pg_units()
    ddl_skips()
    methods()
    accounts_copy()
    projects_copy()
    tickets_copy()
    reviews_copy()
    crew_copy()
    accounts_methods()
    projects_methods()
    tickets_methods()
    postgres_takeover()
    accounts_takeover()
    projects_takeover()
    tickets_takeover()
    tickets_pr_lookup()
    tickets_pr_revision()
    reviews_methods()
    reviews_takeover()
    tickets_pr_guard()
    crew_methods()
    crew_takeover()
    integr_copy()
    integr_methods()
    integr_one_snapshot()
    integr_takeover()
    brain_copy()
    brain_methods()
    brain_takeover()
    print("FAIL " + ", ".join(ts.FAILS) if ts.FAILS else "OK")
    sys.exit(1 if ts.FAILS else 0)
