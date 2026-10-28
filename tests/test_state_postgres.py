"""A cell's state in Postgres (P-TR-268, state_db.py): the SQL translation always; the move, the
guard after it and parallel reads when a Postgres is at hand (TARES_TEST_PG, a DSN whose user may
create schemas).

    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres python tests/test_state_postgres.py
"""
import os
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tares import state_db  # noqa: E402

FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def translation():
    print("== DuckDB SQL as Postgres runs it ==")
    t = state_db.to_pg
    ck("placeholders, and a literal % doubled",
       t("SELECT * FROM x WHERE a = ? AND b LIKE '%z%'") == "SELECT * FROM x WHERE a = %s AND b LIKE '%%z%%'",
       t("SELECT * FROM x WHERE a = ? AND b LIKE '%z%'"))
    ck("a ? inside a string stays", t("SELECT '?' FROM x WHERE a = ?") == "SELECT '?' FROM x WHERE a = %s")
    ck("json_extract_string, quoted key",
       t("SELECT json_extract_string(labels, '$.\"a b\"')") == "SELECT (CAST(labels AS jsonb) #>> '{\"a b\"}')",
       t("SELECT json_extract_string(labels, '$.\"a b\"')"))
    ck("json_extract, nested path",
       t("SELECT json_extract(p, '$.message.usage')") == "SELECT (CAST(p AS jsonb) #> '{\"message\",\"usage\"}')",
       t("SELECT json_extract(p, '$.message.usage')"))
    ck("arg_max skips NULL orderings",
       "FILTER (WHERE (d.delivered_at) IS NOT NULL))[1]" in t("SELECT arg_max(d.error, d.delivered_at) FROM d"))
    ck("arg_max with a CASE argument",
       "ORDER BY CASE WHEN ok IS NULL THEN NULL ELSE at END DESC"
       in t("SELECT arg_max(ok, CASE WHEN ok IS NULL THEN NULL ELSE at END) FROM d"))
    ck("date_diff in milliseconds",
       t("SELECT date_diff('millisecond', started_at, ?)")
       == "SELECT (EXTRACT(EPOCH FROM (CAST(%s AS timestamptz) - CAST(started_at AS timestamptz))) * 1000)")
    ck("INTERVAL n unit", t("SELECT now() - INTERVAL 1 DAY") == "SELECT now() - INTERVAL '1 day'")
    ck("an empty IN list", t("SELECT 1 WHERE a IN () AND b NOT IN ()")
       == "SELECT 1 WHERE a = ANY('{}') AND b <> ALL('{}')")
    ck("DDL: DOUBLE and JSON", t("CREATE TABLE t (a DOUBLE, b JSON)") == "CREATE TABLE t (a DOUBLE PRECISION, b TEXT)")
    ck("not DDL: a JSON word in a query stays", "JSON" in t("SELECT CAST(x AS JSON) FROM t"))
    ck("events tables are not state",
       state_db.state_ddl(["CREATE TABLE IF NOT EXISTS events (a TEXT)",
                           "CREATE INDEX IF NOT EXISTS ix ON events(a)",
                           "CREATE TABLE IF NOT EXISTS tickets (a TEXT)"])
       == ["CREATE TABLE IF NOT EXISTS tickets (a TEXT)"])


def waits_for_its_login():
    print("== at startup, the state database is waited for, then given up on ==")
    t0 = time.perf_counter()
    try:
        state_db.PgState("postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1", wait=6)
        ck("an unreachable state database fails the start", False, "it connected")
    except Exception as e:                     # noqa: BLE001
        took = time.perf_counter() - t0
        ck("an unreachable state database fails the start, after the wait",
           6 <= took < 20 and type(e).__name__ == "OperationalError", f"{took:.1f}s {e!r}")


def _schema_dsn(base: str) -> str:
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    return f"{base}?options=-csearch_path%3D{schema}"


def with_postgres(base: str):
    from tares.store import Store, StoreUnavailable
    print("== a cell's state moves to Postgres, once ==")
    path = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    st = Store(path, state_dsn="")               # a cell from before, state in DuckDB
    st.set_setting("greeting", "hello")
    pid = st.default_project_id()
    st.con.close()
    dsn = _schema_dsn(base)
    st = Store(path, state_dsn=dsn)
    ck("the state is read from Postgres", isinstance(st.sc, state_db.PgState), type(st.sc).__name__)
    ck("a setting came along", st.get_setting("greeting") == "hello", st.get_setting("greeting"))
    ck("the projects came along", any(p["id"] == pid for p in st.list_projects()))
    st.set_setting("greeting", "after the move")
    st.con.close()
    st = Store(path, state_dsn=dsn)
    ck("the second start reads what was written since, not the DuckDB copy",
       st.get_setting("greeting") == "after the move", st.get_setting("greeting"))
    st.con.close()

    print("== started without its state database, the cell says so ==")
    try:
        Store(path, state_dsn="")
        ck("refuses to serve the stale DuckDB copy", False, "it opened")
    except StoreUnavailable as e:
        ck("refuses to serve the stale DuckDB copy", "TARES_STATE_DSN" in e.reason, e.reason)

    print("== pointed at a database without its state, the cell says so ==")
    try:
        Store(path, state_dsn=_schema_dsn(base))
        ck("does not move the old DuckDB copy a second time", False, "it opened")
    except StoreUnavailable as e:
        ck("does not move the old DuckDB copy a second time", "does not hold it" in e.reason, e.reason)

    print("== a move that stopped before marking DuckDB is marked on the next start ==")
    st = Store(path, state_dsn=dsn)
    st.con.execute("DELETE FROM settings WHERE key = 'state_moved_to_postgres'")
    st.con.close()
    st = Store(path, state_dsn=dsn)
    st.con.close()
    try:
        Store(path, state_dsn="")
        ck("then a start without the DSN still refuses", False, "it opened")
    except StoreUnavailable:
        ck("then a start without the DSN still refuses", True)

    print("== a dropped connection is opened again ==")
    import psycopg
    st = Store(path, state_dsn=dsn)
    backend = st.sc.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(%s)", [backend])
    try:
        ck("the next read after the drop works", st.get_setting("greeting") == "after the move")
    except Exception as e:                     # noqa: BLE001
        ck("the next read after the drop works", False, repr(e))
    backend = st.sc.execute("SELECT pg_backend_pid()").fetchone()[0]
    st.sc.execute("BEGIN")
    with psycopg.connect(base, autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(%s)", [backend])
    try:
        st.sc.execute("SELECT 1")
        ck("a drop inside a transaction is not retried", False, "it was retried")
    except psycopg.OperationalError:
        ck("a drop inside a transaction is not retried", True)
    st.con.close()

    print("== reads run in parallel, writes stay one at a time ==")
    st = Store(path, state_dsn=dsn)
    done, errs = [], []

    def read():
        try:
            for _ in range(20):
                st.list_projects()
                st.get_setting("greeting")
            done.append(1)
        except Exception as e:                 # noqa: BLE001
            errs.append(repr(e))

    threads = [threading.Thread(target=read) for _ in range(8)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ck("8 threads reading at once all finish", len(done) == 8 and not errs, errs[:2])
    ids, errs = [], []

    def make():
        try:
            ids.append(st.create_ticket(pid, "tares", f"t{uuid.uuid4().hex[:6]}"))
        except Exception as e:                 # noqa: BLE001
            errs.append(repr(e))

    threads = [threading.Thread(target=make) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    nums = [st.get_ticket(pid, i)["number"] for i in ids]
    ck("6 tickets made at once all land", len(ids) == 6 and not errs, errs[:2])
    ck("and get distinct numbers", len(set(nums)) == 6, str(nums))
    print(f"  ({(time.perf_counter() - t0) * 1000:.0f} ms)")


if __name__ == "__main__":
    translation()
    try:
        import psycopg  # noqa: F401
        waits_for_its_login()
    except ImportError:
        print("== (no psycopg: the startup wait is skipped) ==")
    base = os.environ.get("TARES_TEST_PG")
    if base:
        with_postgres(base)
    else:
        print("== (no TARES_TEST_PG: the Postgres half is skipped) ==")
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all ok'}")
    sys.exit(1 if FAILS else 0)
