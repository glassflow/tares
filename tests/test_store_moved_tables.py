"""The groundwork for moving tables out of DuckDB (TR-1480): `settings` is the first table moved.

Plain (SQLite):       python tests/test_store_moved_tables.py
With the Postgres harness (every Store keeps its state in Postgres, the takeover cases run):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness \\
        python tests/test_store_moved_tables.py

Cases:
  settings_copy      a cell from before the move: DuckDB's settings rows arrive in SQLite, counts
                     verified, one ledger row per moved table, DuckDB untouched
  legacy_file        sessions.sqlite becomes tares.sqlite on start, with its -wal and -shm files
  takeover_unit      takeover(): create, add a column, error on an extra column, types ignored,
                     twice is the same as once
  json_text          JSONText round trip, None is NULL
  ddl_skips          Store._ddl() and state_db.move leave the moved tables alone
  methods            get_setting / set_setting answer as before, on whatever the Store runs on
  postgres_takeover  a table made by the old DDL is taken over with its rows untouched; an upgrade,
                     a downgrade and an upgrade again keep the rows
"""
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import duckdb  # noqa: E402
from sqlalchemy import Column, Index, Integer, MetaData, Table, Text, inspect, text  # noqa: E402

import test_session_store as ts  # noqa: E402  (its helpers; its checks share its FAILS list)
from old_ddl import OLD_DDL, SEED, old_ddl_duckdb, old_ddl_postgres  # noqa: E402
from tares import state_db  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.engine import LEGACY_SQLITE_FILE, SQLITE_FILE, adopt_legacy_file  # noqa: E402
from tares.session_store.tables import MOVED_TABLE_NAMES, MOVED_TABLES  # noqa: E402
from tares.session_store.tables import settings as settings_table  # noqa: E402
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


if __name__ == "__main__":
    ck("settings is in MOVED_TABLES, with the five session tables", [t.name for t in MOVED_TABLES]
       == ["session_stations", "session_states", "project_sessions", "session_usage", "session_meters", "settings"])
    if not HARNESS:
        settings_copy()
    legacy_file()
    sqlite_and_pg_units()
    ddl_skips()
    methods()
    postgres_takeover()
    print("FAIL " + ", ".join(ts.FAILS) if ts.FAILS else "OK")
    sys.exit(1 if ts.FAILS else 0)
