"""A read of the session store takes no write lock, so ingest never waits behind one (TR-1702).

On SQLite `begin()` and `snapshot()` start with BEGIN IMMEDIATE, so a long read held every /ingest.
Each read here is held open half way, with its transaction still open, while lines are ingested
through `Store.append`: the ingest finishes, and a read of several queries still answers from one
state. Then every read function is called and the transactions it opened are looked at: SQLite's
deferred BEGIN only; on Postgres the isolation it had before.

Plain:                python tests/test_session_read_no_write_lock.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_read_no_write_lock.py
"""
import contextlib
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import event, func, insert, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from tares import claude_usage_hourly as hourly  # noqa: E402
from tares import report, session_archive, session_ingest, session_move, session_read  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore, copy as store_copy  # noqa: E402
from tares.session_store.tables import (session_archive_parts, session_lines,  # noqa: E402
                                        session_stations, session_store_copies)
from tares.store import Store, now_utc  # noqa: E402

FAILS = []
PASSES = []
SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
OLD = NOW - timedelta(days=session_archive.ARCHIVE_AFTER_DAYS + 2)
_N = [0]
os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp()


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    (PASSES if ok else FAILS).append(name)


def line(session, at=None):
    _N[0] += 1
    return Envelope(source=SRC, source_type="agent_session", key_value=session,
                    event_type="assistant", text="t",
                    event_time=(at or NOW) + timedelta(milliseconds=_N[0]),
                    payload={"uuid": "u-" + uuid.uuid4().hex[:10]},
                    labels={"tares_person": "ann"})


def reply(s, session, message, tokens=10):
    s.record_reply_usage(session, message, NOW, "claude-opus-5-5", None, None,
                         {"input_tokens": tokens, "output_tokens": tokens})


def open_store(path=None):
    s = Store(path or os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    s.set_setting("edition", "factory")
    return s


def seed(s):
    """Two sessions at one builder station, with lines, replies and a link, and an old session."""
    for sid in ("s1", "s2"):
        s.set_session_station(sid, "builder-1", "builder", "", "p1", NOW, "crew")
        s.append([line(sid) for _ in range(3)])
        reply(s, sid, f"m-{sid}")
    s.link_session("p1", "s1")
    s.append([line("old", OLD) for _ in range(4)])
    hourly.fill(s)


def line_count(s, session):
    with s.sessions_db.read() as c:
        return c.execute(select(func.count()).select_from(session_lines)
                         .where(session_lines.c.session == session)).scalar_one()


@contextlib.contextmanager
def held(s, read, marker):
    """Run `read` in a thread and hold it right after its first statement that has `marker`, with
    that transaction still open. Yields a dict: `paused`, and after the block `value` or `error`."""
    here, go, out = threading.Event(), threading.Event(), {}

    def run():
        try:
            out["value"] = read()
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=run, daemon=True)

    def after(conn, cursor, statement, *a):
        if threading.current_thread() is t and not here.is_set() and marker in statement:
            here.set()
            go.wait(30)

    engine = s.sessions_db.engine
    event.listen(engine, "after_cursor_execute", after)
    try:
        t.start()
        out["paused"] = here.wait(10)
        yield out
    finally:
        go.set()
        t.join(30)
        event.remove(engine, "after_cursor_execute", after)
        if t.is_alive():
            out["error"] = "the read never ended"


def beside(write, wait=3.0):
    """Start `write` in another thread: (it ended without an error within `wait` seconds, what
    happened, the thread, its errors). `wait` is under SQLite's 5 s busy timeout."""
    errors = []

    def run():
        try:
            write()
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=run, daemon=True)
    started = time.time()
    t.start()
    t.join(wait)
    ended = not t.is_alive()
    return ended and not errors, (ended, errors, round(time.time() - started, 2)), t, errors


def check_held(s, name, read, marker, write):
    """Hold `read`, run `write` beside it. Returns what the read answered and what `write` did."""
    with held(s, read, marker) as out:
        ok, detail, t, errors = beside(write)
        ck(f"{name}: lines are ingested while the read is open", out["paused"] and ok,
           (out["paused"], detail))
    t.join(30)
    ck(f"{name}: the read and the ingest both end without an error",
       "error" not in out and not errors and not t.is_alive(), (out.get("error"), errors))
    return out.get("value")


def held_reads():
    print("== a held read does not hold ingest, and answers from one state ==")
    s = open_store()
    seed(s)
    ck("the setup: lines are in the session store, the totals are built",
       line_count(s, "s1") == 3 and hourly.ready(s) and session_ingest.caught_up(s),
       (line_count(s, "s1"), hourly.ready(s)))

    def more(sid, station_session=None):
        def write():
            if station_session:
                s.set_session_station(station_session, "builder-1", "builder", "", "p1", NOW, "crew")
                s.append([line(station_session)])
                reply(s, station_session, f"m-{station_session}")
            s.append([line(sid), line(sid)])
            reply(s, sid, f"m-{sid}-{_N[0]}")
        return write

    def rollup():
        return s.station_rollup(SRC)

    before = rollup()
    got = check_held(s, "station_rollup", rollup, "row_number", more("s1", "s3"))
    ck("station_rollup: every query answered from the state before the ingest", got == before,
       (before, got))
    after = rollup()
    ck("station_rollup: the next read has the new session",
       after[0]["earlier_count"] == before[0]["earlier_count"] + 1,
       (before[0]["earlier_count"], after[0]["earlier_count"]))

    def earlier():
        return s.station_earlier(SRC, "crew", "builder-1")

    before = earlier()
    got = check_held(s, "station_earlier", earlier, "count(", more("s1", "s4"))
    if s.sessions_db.dialect == "sqlite":   # on Postgres it stays at READ COMMITTED, as before
        ck("station_earlier: every query answered from the state before the ingest", got == before,
           (before, got))
    ck("station_earlier: the next read has the new session",
       earlier()["earlier_count"] == before["earlier_count"] + 1, (before, earlier()))

    def page():
        return session_read.lines(s, SRC, "s1", 100, 0)

    before = page()
    ck("the setup: the session's lines read from the table", before and before[1] == "table", before)
    got = check_held(s, "session lines", page, "lines_complete", more("s1"))
    ck("session lines: the count and the rows are of one state", got == before,
       (len(before[0]), got and len(got[0])))
    ck("session lines: the next read has the new lines", len(page()[0]) == len(before[0]) + 2,
       (len(before[0]), len(page()[0])))

    got = check_held(s, "hourly check", lambda: hourly.check(s), "sum(", more("s2"))
    ck("hourly check: the rows and the totals are of one state", got == (True, ""), got)

    before = s.usage_by_project()
    got = check_held(s, "usage_by_project", s.usage_by_project, "sum(", more("s2"))
    ck("usage_by_project: the state before the ingest", got == before, (before, got))
    ck("usage_by_project: the next read has the new replies", s.usage_by_project() != before, before)

    had = line_count(s, "old")
    got = check_held(s, "archive", lambda: session_archive.archive_pass(s), "render",
                     lambda: s.append([line("old", OLD), line("old", OLD)]))
    with s.sessions_db.read() as c:
        parts = c.execute(select(session_archive_parts.c.lines).where(
            session_archive_parts.c.session == "old")).scalars().all()
    ck("archive: the lines read are archived, the ones that came meanwhile stay",
       got == 1 and parts == [had] and line_count(s, "old") == 2,
       (got, parts, had, line_count(s, "old")))
    all_lines = session_read.lines(s, SRC, "old", 100, 0)
    ck("archive: no line is lost", all_lines and len(all_lines[0]) == had + 2,
       all_lines and len(all_lines[0]))

    # holdable is on the ingest path itself: it must not wait for another writer either
    one = line("s1")
    with s.sessions_db.begin():
        out = []
        ok, detail, t, errors = beside(lambda: out.append(session_ingest.holdable(s, [one])))
    t.join(30)
    if s.sessions_db.dialect == "sqlite":
        ck("holdable answers while another transaction holds the write lock",
           ok and out == [[one]], (detail, out))
    s.close()


def reads(s):
    """{name: call} of every read function; the second set is those whose queries must agree."""
    def forget():
        s.__dict__.pop("_sessions_caught_up", None)
        s.__dict__.pop("_usage_hourly_ready", None)

    def fresh(call):
        return lambda: (forget(), call())[1]

    db = s.sessions_db
    since = NOW - timedelta(days=60)
    plain = {
        "context_asks": lambda: s.context_asks(since),
        "session_meters": lambda: s.session_meters(["s1"]),
        "latest_rate_limits": s.latest_rate_limits,
        "usage_by_session": lambda: s.usage_by_session(["s1"]),
        "usage_by_project": s.usage_by_project,
        "session_station": lambda: s.session_station("s1"),
        "station_sessions": lambda: s.station_sessions(SRC),
        "projects_by_session": lambda: s.projects_by_session(["s1"]),
        "station_earlier": lambda: s.station_earlier(SRC, "crew", "builder-1"),
        "_station_of": lambda: s._station_of({"s1"}),
        "protocol_messages": lambda: s.protocol_messages(SRC),
        "coordination_calls": lambda: s.coordination_calls(SRC, ["s1"]),
        "recorded_commands": lambda: s.recorded_commands(SRC, ["s1"]),
        "station_session_ids": lambda: s.station_session_ids("builder-1"),
        "person_turns": lambda: s.person_turns(SRC, None, since),
        "role_sessions": lambda: s.role_sessions("builder"),
        "session_archive.due": lambda: session_archive.due(db, now_utc()),
        "holdable": lambda: session_ingest.holdable(s, [line("s1")]),
        "_mark": lambda: session_ingest._mark(s),
        "caught_up": fresh(lambda: session_ingest.caught_up(s)),
        "lines_size": lambda: session_ingest.lines_size(s),
        "_facts_mark": lambda: session_ingest._facts_mark(s),
        "_turns_mark": lambda: session_ingest._turns_mark(s),
        "session_move.planned": lambda: session_move.planned(s),
        "session_move.due": lambda: session_move.due(s),
        "session_move.due_old": lambda: session_move.due_old(s),
        "session_move.retrying": lambda: session_move.retrying(s),
        "session_move.counts": lambda: session_move.counts(s),
        "copy._in_ledger": lambda: store_copy._in_ledger(db, session_stations),
        "sessions_of_projects": lambda: s.sessions_of_projects(["p1"]),
        "projects_of_sessions": lambda: s.projects_of_sessions(["s1"]),
        "project_sessions": lambda: s.project_sessions("p1"),
        "session_activity": lambda: s.session_activity(SRC, ["s1"]),
        "session_state": lambda: s.session_state("s1"),
        "said_lately": lambda: s.said_lately(SRC, "s1"),
        "files_touched": lambda: s.files_touched(SRC, "s1"),
        "session_projects": lambda: s.session_projects("s1"),
        "list_connections": lambda: s.list_connections("tickets"),
        "usage_by_week": s.usage_by_week,
        "sessions_in": lambda: s.sessions_in(["s1"], {"p1"}),
        "sessions_counting_for": lambda: s.sessions_counting_for(["s1"], {"p1"}),
        "sessions_tied_to": lambda: s.sessions_tied_to(["s1"], {"p1"}),
        "unread_among": lambda: s.unread_among(["s1"], {"p1"}, set()),
        "all_stage_changes": s.all_stage_changes,
        "all_reviews": s.all_reviews,
        "all_review_findings": s.all_review_findings,
        "report.read_weeks": lambda: report.read_weeks(s),
        "get_report_text": lambda: s.get_report_text("project", "p1"),
        "private_and_records": s.private_and_records,
    }
    agreeing = {
        "station_rollup": fresh(lambda: s.station_rollup(SRC)),
        "hourly.check": lambda: hourly.check(s),
        "session_read.lines": lambda: session_read.lines(s, SRC, "s1", 100, 0),
        "get_choice": lambda: s.get_choice("tickets"),
        "get_connection": lambda: s.get_connection("tickets", "tixa"),
        "stored_settings": lambda: s.stored_settings("tickets", "tixa", "p1"),
    }
    return plain, agreeing


def opened(s, call):
    """What `call` opened on the session store: on SQLite each BEGIN as sent, on Postgres the
    isolation level each statement asked for ("default" for none)."""
    seen, me = [], threading.current_thread()
    sqlite = s.sessions_db.dialect == "sqlite"

    def after(conn, cursor, statement, *a):
        if threading.current_thread() is not me:
            return
        if not sqlite:
            seen.append(conn.get_execution_options().get("isolation_level") or "default")
        elif statement.startswith("BEGIN"):
            seen.append(statement)

    engine = s.sessions_db.engine
    event.listen(engine, "after_cursor_execute", after)
    try:
        call()
    finally:
        event.remove(engine, "after_cursor_execute", after)
    return seen


def transactions(s):
    plain, agreeing = reads(s)
    sqlite = s.sessions_db.dialect == "sqlite"

    def write():
        s.set_session_state("s1", "idle", None, now_utc())

    wrote = opened(s, write)
    ck("the setup: a write is seen as one",
       ("BEGIN IMMEDIATE" in wrote) if sqlite else bool(wrote), wrote)
    for name, call in {**plain, **agreeing}.items():
        try:
            seen = opened(s, call)
        except Exception as e:
            ck(f"{name} runs", False, f"{type(e).__name__}: {e}")
            continue
        if sqlite:
            ck(f"{name} takes no write lock", bool(seen) and set(seen) == {"BEGIN"}, seen)
        elif name in agreeing:
            ck(f"{name} reads at REPEATABLE READ", "REPEATABLE READ" in seen, seen)
        else:
            ck(f"{name} reads at the default isolation", set(seen) == {"default"}, seen)


def first_reads(s):
    """{name: call} of the functions that read and then write: their first transaction is the read."""
    return {
        "_factory_upgrade": s._factory_upgrade,
        "_claude_usage_fill": s._claude_usage_fill,
        "_archive_session": lambda: session_archive._archive_session(
            s.sessions_db, session_archive.backend_of(s), "old", now_utc()),
    }


def first_transactions(s):
    sqlite = s.sessions_db.dialect == "sqlite"
    for name, call in first_reads(s).items():
        try:
            seen = opened(s, call)
        except Exception as e:
            ck(f"{name} runs", False, f"{type(e).__name__}: {e}")
            continue
        if sqlite:
            ck(f"{name} reads with no write lock", seen[:1] == ["BEGIN"], seen)
        else:
            ck(f"{name} reads at the default isolation", set(seen) == {"default"}, seen)


def local_copy_source():
    print("== the first start with a state database reads the local file with no write lock ==")
    s = open_store()
    seed(s)
    local_dir = tempfile.mkdtemp()
    local = SessionStore(local_dir)
    local.upgrade()
    with local.begin() as c:    # its ledger says this table was copied, so the file is its source
        c.execute(insert(session_store_copies).values(table_name=session_stations.name, rows=0,
                                                      copied_at=now_utc()))
    local.close()
    began, read_in = {}, []

    def after(conn, cursor, statement, *a):
        if not str(conn.engine.url.database or "").startswith(local_dir):
            return
        key = id(conn.connection.dbapi_connection)
        if statement.startswith("BEGIN"):
            began[key] = statement
        elif f"FROM {session_stations.name}" in statement:
            read_in.append(began.get(key))

    event.listen(Engine, "after_cursor_execute", after)
    try:
        store_copy.copy_from_duckdb(s, s.sessions_db, state_move=True, local_dir=local_dir)
    finally:
        event.remove(Engine, "after_cursor_execute", after)
    ck("copy_from_duckdb reads the local file's table with no write lock", read_in == ["BEGIN"],
       read_in)
    s.close()


def every_read_function():
    print("== what each read function opens ==")
    s = open_store()
    seed(s)
    print("  on " + s.sessions_db.dialect)
    transactions(s)
    first_transactions(s)
    s.close()


def in_memory():
    print("== a store in memory: one connection, so its transactions still take turns ==")
    s = open_store(":memory:")
    seed(s)
    transactions(s)
    first_transactions(s)
    if s.sessions_db.dialect == "sqlite":
        with held(s, lambda: s.station_rollup(SRC), "row_number") as out:
            ok, detail, t, errors = beside(lambda: s.append([line("s1")]), wait=0.5)
            ck("an ingest waits for its turn while a read is open",
               out["paused"] and not detail[0], (out["paused"], detail))
        t.join(30)
        ck("and then ends without an error, as the read does",
           not t.is_alive() and not errors and "error" not in out and line_count(s, "s1") == 4,
           (errors, out.get("error"), line_count(s, "s1")))
    s.close()


if __name__ == "__main__":
    held_reads()
    every_read_function()
    local_copy_source()
    in_memory()
    print(f"\n{len(PASSES)} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
