"""A station that appears while station_rollup is reading does not break it (TR-1523, from the TR-1500 review).

`FactoryStore.station_rollup` runs four statements. On Postgres at read committed a station created
between the first and the second showed in the totals but not in the list of stations, and
`out[(crew, station)]` raised KeyError, so GET /api/crew answered 500. The four statements now run in
one snapshot: the new station is left out of this answer and is in the next. On SQLite a writer waits for
the read to finish, so the answer is the same.

Plain:                python tests/test_station_rollup_snapshot.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_station_rollup_snapshot.py
"""
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import event  # noqa: E402

from tares.store import Store  # noqa: E402

FAILS = []
SEEN = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def names(rows):
    return sorted((r["crew_id"], r["station"]) for r in rows)


def rollup_while_a_station_appears(s, new):
    """Run station_rollup and, right after its first statement, create the station `new` from another
    thread. Returns (rows, error, writer_thread)."""
    state = {"armed": True, "writer": None}

    def add():
        s.set_session_station(new[0], new[1], "builder", "", None, SEEN, crew_id=new[2])

    def after(conn, cursor, statement, parameters, context, executemany):
        if state["armed"] and "row_number" in statement:
            state["armed"] = False
            state["writer"] = threading.Thread(target=add)
            state["writer"].start()
            state["writer"].join(1.0)       # Postgres: done at once. SQLite: waits for the read to end

    engine = s.sessions_db.engine
    event.listen(engine, "after_cursor_execute", after)
    rows = err = None
    try:
        rows = s.station_rollup("claude_code")
    except Exception as e:      # noqa: BLE001
        err = e
    finally:
        event.remove(engine, "after_cursor_execute", after)
    if state["writer"] is not None:
        state["writer"].join(30)
    return rows, err, state["writer"]


def station_appears_mid_rollup():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    s.set_session_station("s1", "builder-1", "builder", "", None, SEEN, crew_id="crew")
    s.set_session_station("s2", "builder-2", "builder", "", None, SEEN, crew_id="crew")
    from tares import session_ingest
    print("  on " + s.sessions_db.dialect)
    ck("the rollup reads the sessions table", session_ingest.caught_up(s))
    base = names(s.station_rollup("claude_code"))
    ck("two stations before", base == [("crew", "builder-1"), ("crew", "builder-2")], base)

    rows, err, writer = rollup_while_a_station_appears(s, ("s3", "builder-3", "crew"))
    ck("a station created between the statements raises nothing", err is None, repr(err))
    ck("the writer finished", writer is not None and not writer.is_alive())
    ck("the answer is the stations that were there when it started",
       rows is not None and names(rows) == base, rows and names(rows))
    ck("every station in the answer has its totals",
       rows is not None and all("earlier_count" in r and "coordination_calls" in r and r["cur"] for r in rows))
    after = names(s.station_rollup("claude_code"))
    ck("the next answer has the new station",
       after == base + [("crew", "builder-3")], after)
    s.close()


def empty_cell():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    ck("a cell with no station answers an empty list", s.station_rollup("claude_code") == [])
    s.close()


def main():
    for fn in (station_appears_mid_rollup, empty_cell):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nOK")


if __name__ == "__main__":
    main()
