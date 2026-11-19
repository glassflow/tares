# Tares enterprise licence, see ee/LICENSE
"""The state indexes (TR-1503): a Store on Postgres has an index on tickets(project) and on
ticket_history(ticket), and a DuckDB Store has neither (its ART indexes have broken after upgrades).

Run: .venv/bin/python ee/tests/test_state_indexes.py
Postgres half: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_state_indexes.py
Without TARES_TEST_PG the Postgres half is skipped.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


WANT = {"tickets_project", "ticket_history_ticket"}
tmp = tempfile.mkdtemp()

duck = Store(os.path.join(tmp, "duck.duckdb"), state_dsn="")   # "" keeps the state in DuckDB, even under the harness
have = {r[0] for r in duck.con.execute("SELECT index_name FROM duckdb_indexes()").fetchall()}
ck("a DuckDB store has no tickets_project or ticket_history_ticket index", not (have & WANT), have)

if not PG:
    print("  skip the Postgres half: TARES_TEST_PG is not set")
else:
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    path = os.path.join(tmp, "pg.duckdb")
    dsn = f"{PG}?options=-csearch_path%3D{schema}"
    sql = ("SELECT indexname, tablename FROM pg_indexes WHERE schemaname = current_schema()")
    s1 = Store(path, state_dsn=dsn)
    got = {r[0]: r[1] for r in s1.sc.execute(sql).fetchall()}
    ck("a Postgres store has tickets_project on tickets", got.get("tickets_project") == "tickets", got)
    ck("a Postgres store has ticket_history_ticket on ticket_history",
       got.get("ticket_history_ticket") == "ticket_history", got)
    s2 = Store(path, state_dsn=dsn)    # a second start finds them and changes nothing
    got2 = {r[0] for r in s2.sc.execute(sql).fetchall()}
    ck("a second start keeps both indexes", WANT <= got2, got2)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
