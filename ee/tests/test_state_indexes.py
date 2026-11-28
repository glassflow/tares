# Tares enterprise licence, see ee/LICENSE
"""The ticket indexes (TR-1503, TR-1612): a Store has an index on tickets(project) and on
ticket_history(ticket), in the session store on both engines.

Run: .venv/bin/python ee/tests/test_state_indexes.py
Postgres half: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_state_indexes.py
Without TARES_TEST_PG the Postgres half is skipped.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from sqlalchemy import text  # noqa: E402

from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def indexes(store, sql: str) -> dict:
    with store.sessions_db.read() as c:
        return {r[0]: r[1] for r in c.execute(text(sql)).all()}


WANT = {"tickets_project": "tickets", "ticket_history_ticket": "ticket_history"}
tmp = tempfile.mkdtemp()

lite = Store(os.path.join(tmp, "lite"), state_dsn="")   # "" keeps the store on SQLite, even under the harness
got = indexes(lite, "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'")
ck("the SQLite store has tickets_project and ticket_history_ticket",
   all(got.get(k) == v for k, v in WANT.items()), got)

if not PG:
    print("  skip the Postgres half: TARES_TEST_PG is not set")
else:
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    path = os.path.join(tmp, "pg")
    dsn = f"{PG}?options=-csearch_path%3D{schema}"
    sql = "SELECT indexname, tablename FROM pg_indexes WHERE schemaname = current_schema()"
    s1 = Store(path, state_dsn=dsn)
    got = indexes(s1, sql)
    ck("a Postgres store has tickets_project on tickets", got.get("tickets_project") == "tickets", got)
    ck("a Postgres store has ticket_history_ticket on ticket_history",
       got.get("ticket_history_ticket") == "ticket_history", got)
    s2 = Store(path, state_dsn=dsn)    # a second start finds them and changes nothing
    got2 = indexes(s2, sql)
    ck("a second start keeps both indexes", set(WANT) <= set(got2), got2)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
