"""In the factory edition, with an archive, claude_code lines go only to the session store (TR-1479).

`Store.append` leaves the lines of a claude_code source out of the DuckDB events table when the cell's
edition is factory and an archive backend exists (the folder archive always does on SQLite; a Postgres
cell needs TARES_ARCHIVE_URL). The lines are still counted in `source_stats`, a challenger line is
still kept in `challenges` (entity counts went in TR-1810; a new database has no table for them): the readers
of `events` answer from the session store for them (TR-1647). Every other case writes to DuckDB as
before, and a factory cell on Postgres with no archive says so once and keeps writing there.

Plain:                python tests/test_session_no_duckdb_write.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_no_duckdb_write.py
"""
import contextlib
import io
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import func, select  # noqa: E402

from tares import metrics  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store.tables import challenges as challenges_table  # noqa: E402
from tares.session_store.tables import session_lines, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

FAILS = []
T0 = datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc)
SRC = "claude_code"
_N = [0]


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, etype="assistant", source=SRC, labels=None):
    _N[0] += 1
    return Envelope(source=source, source_type="agent_session", key_value=session, event_type=etype,
                    text="t", event_time=T0 + timedelta(seconds=_N[0]), payload={"uuid": uuid.uuid4().hex[:10]},
                    labels=labels or {})


def open_store(edition=None, archive=True):
    """A store of the given edition. `archive` points TARES_ARCHIVE_URL at a folder (or clears it)."""
    if archive:
        os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp()
    else:
        os.environ.pop("TARES_ARCHIVE_URL", None)
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    if edition:
        s.set_setting("edition", edition)
    return s


def duck_count(s, source=SRC):
    with s._lock:
        return s.con.execute("SELECT count(*) FROM events WHERE source = ?", [source]).fetchone()[0]


def table_count(s):
    with s.sessions_db.begin() as c:
        return c.execute(select(func.count()).select_from(session_lines)).scalar()


def stats(s, source=SRC):
    with s._lock:
        r = s.con.execute("SELECT events, last_ingest FROM source_stats WHERE source = ?", [source]).fetchone()
    return r


def entity_tables(s):
    with s._lock:
        return [r[0] for r in s.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('entity_counts', 'entity_label_state')").fetchall()]


def challenges(s):
    with s.sessions_db.read() as c:
        return c.execute(select(func.count()).select_from(challenges_table)).scalar()


def quiet(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def counter():
    return metrics.SESSION_LINES_TO_DUCKDB._value.get() if metrics.AVAILABLE else 0


def main():
    print("== the factory edition, with an archive ==")
    s = open_store("factory")
    batch = [line("s1") for _ in range(49)] + [line("s1", "challenge_commit", labels={"branch": "b1"})]
    before = counter()
    s.append(batch)
    ck("DuckDB events holds no claude_code row", duck_count(s) == 0, duck_count(s))
    ck("session_lines holds the 50 lines", table_count(s) == 50, table_count(s))
    with s.sessions_db.begin() as c:
        got = c.execute(select(sessions.c.line_count, sessions.c.lines_complete).where(
            sessions.c.session == "s1")).first()
    ck("the session counts 50 lines, complete", got is not None and tuple(got) == (50, True), got)
    ck("challenges holds the challenge line", challenges(s) == 1, challenges(s))
    st = stats(s)
    ck("source_stats counts 50 for claude_code", st is not None and st[0] == 50, st)
    ck("source_stats has the newest ingest time", st is not None and st[1] is not None, st)
    ck("the DuckDB counter did not move", counter() == before, (before, counter()))
    plat = open_store("platform")
    plat.append(batch)
    ck("a new database has no entity count tables (TR-1810), and the platform cell writes DuckDB",
       entity_tables(s) == [] and entity_tables(plat) == [] and duck_count(plat) == 50,
       (entity_tables(s), entity_tables(plat)))
    s.append([line("s1") for _ in range(5)])
    st = stats(s)
    ck("a second batch adds to source_stats", st[0] == 55, st)
    ck("and to session_lines", table_count(s) == 55, table_count(s))
    other = line("s2", source="github")
    other.source_type = "event"
    s.append([other])
    ck("a line of another source still goes to DuckDB", duck_count(s, "github") == 1)
    nokey = Envelope(source=SRC, source_type="agent_session", key_value=None, event_type="assistant",
                     text="t", event_time=T0, payload={})
    s.append([nokey])
    ck("a claude_code line with no session stays in DuckDB", duck_count(s) == 1, duck_count(s))
    s.append([line("s3", "challenge_plan", labels={"branch": "b2"})])
    ck("a challenge line alone is recorded", challenges(s) == 2, challenges(s))
    ck("and held back from DuckDB", duck_count(s) == 1, duck_count(s))

    print("== before the sessions table has caught up, the lines go to both ==")
    from tares import session_ingest
    s = open_store("factory")
    real_cu = session_ingest.caught_up
    session_ingest.caught_up = lambda st: False
    try:
        s.append([line("c1") for _ in range(4)])
    finally:
        session_ingest.caught_up = real_cu
    ck("the lines are in DuckDB", duck_count(s) == 4, duck_count(s))
    ck("and in session_lines", table_count(s) == 4, table_count(s))
    ck("source_stats counts them once", (stats(s) or (0,))[0] == 4, stats(s))
    s.append([line("c1")])
    ck("once caught up, a line is held back again", duck_count(s) == 4, duck_count(s))

    print("== a session from before lines_origin keeps its lines in DuckDB (F1) ==")
    from tares import session_ingest as si
    s = open_store("factory")
    s._archive_on = False                    # the old lines were written to both stores
    s.append([line("old") for _ in range(5)])
    s.append([line("old2") for _ in range(3)])
    s._archive_on = None
    with s.sessions_db.begin() as c:        # what 0013 left on every existing row
        c.execute(sessions.update().values(lines_origin=None))
    s.append([line("old") for _ in range(3)] + [line("new") for _ in range(2)])
    ck("the old session's new lines are in DuckDB too", duck_count(s) == 8 + 3, duck_count(s))
    ck("the new session's lines are held back", duck_count(s) == 11 and table_count(s) == 13, (duck_count(s), table_count(s)))
    si.reconcile_sessions(s)
    with s.sessions_db.begin() as c:
        rows = {r[0]: tuple(r[1:]) for r in c.execute(select(
            sessions.c.session, sessions.c.line_count, sessions.c.lines_complete))}
    ck("the old session is still complete after the reconcile", rows["old"] == (8, True), rows["old"])
    ck("so is the one the gate never touched", rows["old2"] == (3, True), rows["old2"])
    ck("and the new one", rows["new"] == (2, True), rows["new"])

    print("== a line of another claude_code source than its session's keeps going to DuckDB ==")
    s = open_store("factory")
    s.upsert_catalog_source(SRC, "agent_session", SRC, "10s", {"push": True})
    s.upsert_catalog_source("laptop", "agent_session", SRC, "10s", {"push": True})
    s.append([line("two")])
    s.append([line("two", source="laptop"), line("two")])
    ck("the other source's line is in DuckDB", duck_count(s, "laptop") == 1, duck_count(s, "laptop"))
    ck("the session's own source stays held", duck_count(s) == 0, duck_count(s))
    s.append([line("mixed", source="laptop"), line("mixed")])
    ck("a new session takes its first line's source", (duck_count(s, "laptop"), duck_count(s)) == (1, 1),
       (duck_count(s, "laptop"), duck_count(s)))

    print("== a session id with a NUL byte does not stop the batch it is in ==")
    s = open_store("factory")
    odd = "n\x00ul"
    s.append([line("plain"), line(odd)])          # raised on every retry on Postgres
    with s.sessions_db.begin() as c:
        held = sorted(r[0] for r in c.execute(select(session_lines.c.session)))
    if s.sessions_db.dialect == "postgresql":
        ck("the other session is held", held == ["plain"], held)
        ck("DuckDB keeps the line the session store cannot take", duck_count(s) == 1, duck_count(s))
    else:
        ck("SQLite takes both", held == [odd, "plain"] and duck_count(s) == 0, (held, duck_count(s)))

    print("== a session a failed write left incomplete keeps its lines in DuckDB ==")
    s = open_store("factory")
    s.append([line("f1") for _ in range(2)])
    with s.sessions_db.begin() as c:
        c.execute(sessions.update().where(sessions.c.session == "f1").values(lines_complete=False))
    s.append([line("f1") for _ in range(2)])
    ck("its new lines are written to DuckDB", duck_count(s) == 2, duck_count(s))

    print("== when the session store takes no write, nothing is stored and the lines are sent again (TR-1665) ==")
    s = open_store("factory")
    real = s.sessions_db.begin

    class Down:
        def __enter__(self):
            raise RuntimeError("session store is down")

        def __exit__(self, *a):
            return False

    s.sessions_db.begin = lambda *a, **k: Down()
    batch, refused = [line("s4") for _ in range(3)], None
    try:
        quiet(lambda: s.append(batch))
    except session_ingest.LinesNotStored as e:
        refused = e
    s.sessions_db.begin = real
    ck("the append is refused", refused is not None)
    ck("nothing is in DuckDB or session_lines", (duck_count(s), table_count(s)) == (0, 0),
       (duck_count(s), table_count(s)))
    ck("and nothing is counted", (stats(s) or (0,))[0] == 0, stats(s))
    s.append(batch)
    ck("sent again, the lines are held back as usual", (duck_count(s), table_count(s)) == (0, 3),
       (duck_count(s), table_count(s)))
    ck("source_stats counts them once", (stats(s) or (0,))[0] == 3, stats(s))

    print("== the platform edition ==")
    s = open_store("platform")
    before = counter()
    s.append([line("p1") for _ in range(10)])
    ck("lines are written to DuckDB", duck_count(s) == 10, duck_count(s))
    ck("and to session_lines", table_count(s) == 10, table_count(s))
    ck("source_stats counts them", stats(s)[0] == 10, stats(s))
    ck("the DuckDB counter moved by 10", counter() == before + 10, (before, counter()))
    s = open_store(None)
    s.append([line("p2")])
    ck("a cell with no edition set writes to DuckDB", duck_count(s) == 1, duck_count(s))

    # the store's own backend decides, not the env: CI's duckdb shard sets TARES_TEST_PG without the
    # harness on the path, so its session store is SQLite
    if s.sessions_db.dialect == "postgresql":
        print("== the factory edition on Postgres with no archive ==")
        s = open_store("factory", archive=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s.start_session_reconcile()
            s.append([line("n1") for _ in range(4)])
            s.append([line("n1") for _ in range(4)])
        s._reconcile_stop.set()
        ck("lines are still written to DuckDB", duck_count(s) == 8, duck_count(s))
        ck("and to session_lines", table_count(s) == 8, table_count(s))
        said = out.getvalue().count("transcript lines are still written to DuckDB: no archive configured")
        ck("the cell says so once", said == 1, said)
        s = open_store("factory", archive=True)
        s.append([line("n2")])
        ck("with TARES_ARCHIVE_URL set it leaves DuckDB", duck_count(s) == 0, duck_count(s))
    else:
        print("== SQLite: the folder archive is always there ==")
        s = open_store("factory", archive=False)
        s.append([line("n1")])
        ck("a factory cell with no URL still leaves DuckDB", duck_count(s) == 0, duck_count(s))

    os.environ.pop("TARES_ARCHIVE_URL", None)
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
