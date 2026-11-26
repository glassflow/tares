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
"""
import os
import re
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import duckdb  # noqa: E402
from sqlalchemy import Column, Index, Integer, MetaData, Table, Text, inspect, text  # noqa: E402

import test_session_store as ts  # noqa: E402  (its helpers; its checks share its FAILS list)
from old_ddl import _T, OLD_DDL, SEED, old_ddl_duckdb, old_ddl_postgres  # noqa: E402
from tares import state_db  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store import copy as copy_mod  # noqa: E402
from tares.session_store.engine import LEGACY_SQLITE_FILE, SQLITE_FILE, adopt_legacy_file  # noqa: E402
from tares.session_store.tables import MOVED_TABLE_NAMES, MOVED_TABLES  # noqa: E402
from tares.session_store.tables import api_keys as api_keys_table  # noqa: E402
from tares.session_store.tables import people as people_table  # noqa: E402
from tares.session_store.tables import project_members as members_table  # noqa: E402
from tares.session_store.tables import settings as settings_table  # noqa: E402
from tares.session_store.tables import ticket_history as history_table  # noqa: E402
from tares.session_store.tables import ticket_numbers as numbers_table  # noqa: E402
from tares.session_store.tables import tickets as tickets_table  # noqa: E402
from tares.session_store.tables import usecases as projects_table  # noqa: E402
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
        ts.step_down(sdb, "0009_session_origin")
        ck("a downgrade to the revision before keeps the tables and their rows", snapshot() == before
           and ts.versions(sdb) == ["0009_session_origin"], ts.versions(sdb))
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
        ts.step_down(sdb, "0012_session_usage_hourly")
        ck("a downgrade to the revision before keeps the tables and their rows", snapshot() == before
           and ts.versions(sdb) == ["0012_session_usage_hourly"], ts.versions(sdb))
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
    s = Store(path, state_dsn="")
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

        def snapshot():
            out = {}
            with psycopg.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                for t in TICKET_TABLES:
                    cols = c.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                     "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
                                     [sch, t]).fetchall()
                    rows = c.execute(f"SELECT * FROM {t}").fetchall()
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
        ck("every table, its columns, types, rows and indexes, is as the old code left it", snapshot() == before)
        ck("a take-over made no copy", ts._count(sdb, "session_store_copies") == 0)
        with ts.capture_logs("alembic.runtime.migration") as recs:
            sdb.upgrade()
        ck("a second upgrade runs nothing", ts.upgrade_lines(recs) == [])
        ts.step_down(sdb, "0013_project_tables")
        ck("a downgrade to the revision before keeps the tables, rows and indexes", snapshot() == before
           and ts.versions(sdb) == ["0013_project_tables"], ts.versions(sdb))
        sdb.upgrade()
        ck("an upgrade after it takes them over again, rows untouched", snapshot() == before)
        with sdb.begin() as c:
            rows = {r.id: r for r in c.execute(tickets_table.select())}
        ck("Core reads a ticket the old code wrote", rows["tk_a"].pr == {"repo": "o/r", "number": 7, "state": "open"}
           and rows["tk_b"].depends_on == ["tk_a"] and rows["tk_null"].pr is None
           and rows["tk_null"].depends_on is None and rows["tk_a"].created_at == _T, rows)
        sdb.close()
    finally:
        _drop_schema(sch)


if __name__ == "__main__":
    ck("settings, the accounts, the project and ticket tables are in MOVED_TABLES, with the five session tables",
       [t.name for t in MOVED_TABLES]
       == ["session_stations", "session_states", "project_sessions", "session_usage", "session_meters",
           "settings", "people", "api_keys", "project_members", "usecases", "usecase_objects", "usecase_log",
           "docs", "skills", "shared_skills", "catalog_sources", "github_credentials", "model_usage",
           "tickets", "ticket_history", "ticket_numbers"])
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
    accounts_methods()
    projects_methods()
    tickets_methods()
    postgres_takeover()
    accounts_takeover()
    projects_takeover()
    tickets_takeover()
    print("FAIL " + ", ".join(ts.FAILS) if ts.FAILS else "OK")
    sys.exit(1 if ts.FAILS else 0)
