"""The one-time move of recent sessions' lines into the session store (TR-1609).

On the first start of RC 1 a background job rebuilds every claude_code session whose last line is
inside the 7 day window: every DuckDB line, then the lines only the table or the archive holds,
numbered from 1, each once, and verified per session. This test seeds 30 sessions straight into
DuckDB (10 of them recent), one recent session whose later half is also in `session_lines` plus 3
lines only in the table, and one whose early rows the archive moved into a part. It stops the move
after 4 sessions and runs it again, and checks: every recent session verified, no duplicate line,
the conversation in time order and equal to DuckDB's lines followed by the extra ones; a session
older than the window stays pending; a forced count mismatch, and a part that cannot be read, mark
the session `failed` and leave its rows as they were; `plan` twice and `move_recent` twice add and
move nothing; the daemon's thread, its switch and the revision.

Plain:                python tests/test_session_move.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_move.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alembic import command  # noqa: E402
from sqlalchemy import inspect, select, text  # noqa: E402

from tares import metrics, session_archive, session_ingest, session_move, session_read  # noqa: E402
from tares.envelope import Envelope, now_utc  # noqa: E402
from tares.session_archive import FileBackend  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import (session_archive_parts, session_commands,  # noqa: E402
                                        session_lines, session_moves, session_turns, sessions)
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
# the harness (CI's postgres suite) puts every Store on Postgres; the duckdb and mit suites have
# TARES_TEST_PG and no harness, so their Stores are on SQLite
HARNESS = Store.__init__.__module__ != "tares.store"
FAILS = []
SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
_N = [0]
ARCHIVE = tempfile.mkdtemp()
os.environ["TARES_ARCHIVE_URL"] = "file://" + ARCHIVE


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, at, etype="assistant", text_="t", block=None):
    """One envelope; `at` is how long ago, in seconds."""
    _N[0] += 1
    payload = {"uuid": "u-" + uuid.uuid4().hex[:10], "n": _N[0]}
    if block:
        payload["message"] = {"content": [block]}
    return Envelope(source=SRC, source_type="agent_session", key_value=session, event_type=etype,
                    text=text_, event_time=NOW - timedelta(seconds=at), payload=payload,
                    labels={"tares_person": "ann"})


def open_store():
    return Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))


def rows_of(s, session):
    t = session_lines.c
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(select(
            t.seq, t.line_key, t.at, t.type, t.labels, t.render, t.payload).where(
            t.session == session).order_by(t.seq)).all()]


def parts_of(s, session):
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(select(session_archive_parts).where(
            session_archive_parts.c.session == session).order_by(session_archive_parts.c.part)).all()]


def row_of(s, session):
    with s.sessions_db.begin() as c:
        r = c.execute(select(sessions).where(sessions.c.session == session)).first()
    return None if r is None else tuple(r)


def move_of(s, session):
    with s.sessions_db.begin() as c:
        r = c.execute(select(session_moves).where(session_moves.c.session == session)).first()
    return None if r is None else r._mapping


def states(s):
    return session_move.counts(s)


def duck_payloads(s, session):
    with s._lock:
        return [r[0] for r in s.con.execute(
            "SELECT payload FROM events WHERE key_value = ? ORDER BY event_time, rowid",
            [session]).fetchall()]


def seed(s, session, n, first_age, step=60, types=("user", "assistant", "tool_use", "tool_result")):
    """`n` lines of `session` straight into DuckDB, `first_age` seconds ago for the oldest."""
    envs = [line(session, first_age - i * step, types[i % len(types)]) for i in range(n)]
    s._append_events(envs)
    return envs


def check_rows(s, sid, extra_payloads, label):
    """The session's rows are DuckDB's lines then the extra ones, numbered 1..N, each once."""
    got = rows_of(s, sid)
    want = [json.dumps(json.loads(p)) for p in duck_payloads(s, sid)] + extra_payloads
    ck(f"{label}: the rows are DuckDB's lines then the extra ones", [r[6] for r in got] == want,
       (len(got), len(want)))
    ck(f"{label}: numbered 1..N", [r[0] for r in got] == list(range(1, len(want) + 1)))
    ck(f"{label}: no line twice", len({r[6] for r in got}) == len(got))
    head = row_of(s, sid)
    c = sessions.c.keys()
    h = dict(zip(c, head))
    ck(f"{label}: the row says table, complete, N lines",
       h["lines_origin"] == "table" and h["lines_complete"] and h["line_count"] == len(want), h)
    ck(f"{label}: last_line_at is the newest line", h["last_line_at"] == max(r[2] for r in got), h)
    m = move_of(s, sid)
    ck(f"{label}: verified, with both counts", m["state"] == "verified" and m["lines_after"] == len(want)
       and m["lines_events"] == len(duck_payloads(s, sid)) and m["verified_at"] is not None, dict(m))
    ck(f"{label}: no part is left", parts_of(s, sid) == [])
    # the conversation as the reader gives it: time order, the same lines
    got_read = session_read.lines(s, SRC, sid, 100000, 0)
    ck(f"{label}: read from the table", got_read is not None and got_read[1] == "table")
    if got_read:
        ats = [x["at"] for x in got_read[0]]
        ck(f"{label}: the conversation is in time order", ats == sorted(ats))
        ck(f"{label}: the conversation holds the same lines in order",
           [x["payload"] for x in got_read[0]] == want)


def main_run():
    print("== 30 sessions, 10 recent, a dual-written one and an archived one ==")
    s = open_store()
    ck("the session store is on the expected database", s.sessions_db.dialect == (
        "postgresql" if PG and HARNESS else "sqlite"), s.sessions_db.dialect)
    backend = FileBackend(ARCHIVE)
    day = 86400
    # 20 old sessions (20 days), 8 recent ones from 6 hours to 1 hour ago, oldest first
    for i in range(20):
        seed(s, f"old{i:02d}", 6, 20 * day)
    for i in range(8):
        seed(s, f"rec{i}", 6, 6 * 3600 - i * 3000)
    seed(s, "rec0", 0, 0)
    # a Bash call and a person's turn in rec1, for the facts and turns the move fills first
    s._append_events([
        line("rec1", 100, "user", "please run it"),
        line("rec1", 90, "tool_use", "→ Bash({\"command\": \"ls\"})", block={
            "type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}}),
        line("rec1", 80, "tool_result", "ok", block={
            "type": "tool_result", "tool_use_id": "b1", "content": "files"})])
    # dual: the first half in DuckDB only, reconciled; the second half through append (both
    # stores), then 3 lines only in the table (written after the stop)
    seed(s, "dual", 5, 5000)
    session_ingest.reconcile_sessions(s)
    s.append([line("dual", 4000 - i * 60, "assistant") for i in range(5)])
    extra_dual = [line("dual", 600 - i * 60, "assistant") for i in range(3)]
    ck("the extra lines go only into the table", session_ingest.record(s, extra_dual))
    # arch: 8 lines in DuckDB, 4 more in both, 2 only in the table, all archived into part 1, then
    # one more only in the table
    seed(s, "arch", 8, 7000)
    session_ingest.reconcile_sessions(s)
    s.append([line("arch", 5000 - i * 60, "assistant") for i in range(4)])
    extra_arch = [line("arch", 900 - i * 60, "assistant") for i in range(2)]
    session_ingest.record(s, extra_arch)
    n = session_archive._archive_session(s.sessions_db, backend, "arch", NOW)
    ck("the archive wrote part 1 with the dual lines and the extras", n is not None
       and len(parts_of(s, "arch")) == 1 and rows_of(s, "arch") == [])
    extra_arch2 = [line("arch", 300, "assistant")]
    session_ingest.record(s, extra_arch2)
    session_ingest.reconcile_sessions(s)
    extra_dual_p = [json.dumps(e.payload) for e in extra_dual]
    extra_arch_p = [json.dumps(e.payload) for e in extra_arch + extra_arch2]
    ck("30 sessions in DuckDB", s.con.execute(
        "SELECT count(DISTINCT key_value) FROM events").fetchone()[0] == 30)

    n = session_move.plan(s)
    ck("plan adds a row for each of the 30 sessions", n == 30 and states(s) == {"pending": 30}, (n, states(s)))
    ck("each row has DuckDB's line count", move_of(s, "old00")["lines_events"] == 6
       and move_of(s, "dual")["lines_events"] == 10 and move_of(s, "arch")["lines_events"] == 12)
    ck("plan again adds nothing", session_move.plan(s) == 0 and states(s) == {"pending": 30})
    ck("the plan is marked done", session_move.planned(s))
    before_old = {sid: (rows_of(s, sid), row_of(s, sid)) for sid in ("old00", "old19")}

    order = session_move.due(s)
    with s.sessions_db.begin() as c:
        last = dict(c.execute(select(sessions.c.session, sessions.c.last_line_at)).all())
    ck("due: the 10 recent sessions, the oldest last line first", len(order) == 10 and set(order) == {
        "arch", "dual", *[f"rec{i}" for i in range(8)]}
       and [last[x] for x in order] == sorted(last[x] for x in order), order)

    # stop after 4 sessions, then run it again
    ck("the first call moves 4", session_move.move_recent(s, limit=4) == 4)
    st = states(s)
    ck("4 verified, 26 pending", st == {"verified": 4, "pending": 26}, st)
    done = [x for x in order if move_of(s, x)["state"] == "verified"]
    ck("the 4 oldest were moved", done == order[:4], (done, order))
    ck("a pending session is still pending", move_of(s, order[9])["state"] == "pending")
    stopper = threading.Event()
    stopper.set()
    ck("a stop set before the call takes none", session_move.move_recent(s, stop=stopper) == 0
       and states(s)["verified"] == 4)
    lines_before = metrics.SESSION_MOVE_LINES._value.get() if metrics.AVAILABLE else 0
    n = session_move.move_recent(s)
    ck("the second call moves the other 6", n == 6, n)
    st = states(s)
    ck("10 verified, 20 old ones pending", st == {"verified": 10, "pending": 20}, st)
    if metrics.AVAILABLE:
        ck("the gauge says so", metrics.SESSION_MOVE_SESSIONS.labels(state="verified")._value.get() == 10
           and metrics.SESSION_MOVE_SESSIONS.labels(state="pending")._value.get() == 20
           and metrics.SESSION_MOVE_SESSIONS.labels(state="failed")._value.get() == 0)
        ck("the counter counted the lines of the second call",
           metrics.SESSION_MOVE_LINES._value.get() > lines_before)

    for i in range(8):
        check_rows(s, f"rec{i}", [], f"rec{i}")
    check_rows(s, "dual", extra_dual_p, "dual")
    check_rows(s, "arch", extra_arch_p, "arch")
    ck("the dual session's line_count is DuckDB's 10 plus the 3", row_of(s, "dual") is not None
       and dict(zip(sessions.c.keys(), row_of(s, "dual")))["line_count"] == 13)
    ck("the archive's part files are left in place", len(os.listdir(os.path.join(ARCHIVE, "arch"))) == 1)
    with s.sessions_db.begin() as c:
        cmds = c.execute(select(session_commands.c.command, session_commands.c.exit_code).where(
            session_commands.c.session == "rec1")).all()
        turns = c.execute(select(session_turns.c.text).where(session_turns.c.session == "rec1")).all()
    ck("the facts and turns were filled before the move", len(cmds) == 1 and "ls" in cmds[0][0]
       and cmds[0][1] == 0 and "please run it" in [t[0] for t in turns], (cmds, turns))

    ck("an old session stays pending and untouched", all(
        move_of(s, sid)["state"] == "pending" and (rows_of(s, sid), row_of(s, sid)) == before_old[sid]
        for sid in before_old))
    ck("move_recent again moves nothing", session_move.move_recent(s) == 0 and states(s) == {
        "verified": 10, "pending": 20})
    # nothing was deleted from DuckDB
    ck("DuckDB keeps every line", s.con.execute("SELECT count(*) FROM events").fetchone()[0]
       == 20 * 6 + 8 * 6 + 3 + 10 + 12, s.con.execute("SELECT count(*) FROM events").fetchone()[0])
    s.close()


def repeats():
    print("== a line key that repeats: matched as a multiset ==")
    s = open_store()
    # DuckDB holds the same line twice (a resumed session copies old lines; DuckDB does not
    # deduplicate), the table holds it three times: the third is the extra one
    e = line("rep", 600, "assistant")
    other = line("rep", 500, "assistant")
    s._append_events([e, e, other])
    ck("the table takes all four lines", session_ingest.record(s, [e, e, e, other]))
    ck("the table holds 4 rows", len(rows_of(s, "rep")) == 4, len(rows_of(s, "rep")))
    # a session written out of time order: the move keeps time order (`event_time`, then rowid)
    s._append_events([line("shuf", 100 + i * 60, "assistant") for i in range(6)])
    session_ingest.reconcile_sessions(s)
    session_move.plan(s)
    ck("the move takes both", session_move.move_recent(s) == 2)
    check_rows(s, "shuf", [], "shuf")
    got = rows_of(s, "rep")
    ck("verified", move_of(s, "rep")["state"] == "verified", dict(move_of(s, "rep")))
    ck("DuckDB's 3 lines and the 1 extra, none lost", len(got) == 4 and [r[0] for r in got] == [1, 2, 3, 4],
       len(got))
    ck("the repeated line is there 3 times", [r[6] for r in got].count(json.dumps(e.payload)) == 3,
       [r[6] for r in got])
    s.close()


def failures():
    print("== a failed check or an unreadable part leaves the session as it was ==")
    s = open_store()
    backend = FileBackend(ARCHIVE)
    seed(s, "bad", 6, 5000)
    seed(s, "gone", 4, 4000)
    session_ingest.reconcile_sessions(s)
    s.append([line("gone", 3000 - i * 60) for i in range(3)])
    session_ingest.record(s, [line("gone", 200)])
    session_ingest.reconcile_sessions(s)
    session_archive._archive_session(s.sessions_db, backend, "gone", NOW)
    session_ingest.record(s, [line("bad", 100)])         # a table-only line for "bad"
    snap = {sid: (rows_of(s, sid), parts_of(s, sid), row_of(s, sid)) for sid in ("bad", "gone")}
    session_move.plan(s)

    real = session_move._count_rows
    session_move._count_rows = lambda conn, sid: real(conn, sid) + 1     # the check sees one row too many
    try:
        ok, err = session_move.move_session(s, "bad")
    finally:
        session_move._count_rows = real
    m = move_of(s, "bad")
    ck("a count mismatch fails the session", ok is False and m["state"] == "failed", (ok, err))
    ck("the error holds the counts", "6 of 6 DuckDB lines read, 1 extra, 8 rows written" in m["error"],
       m["error"])
    ck("its rows, parts and row are as they were", (rows_of(s, "bad"), parts_of(s, "bad"), row_of(s, "bad"))
       == snap["bad"], "")

    os.remove(os.path.join(ARCHIVE, "gone", "1.jsonl.gz"))
    ok, err = session_move.move_session(s, "gone")
    m = move_of(s, "gone")
    ck("a part that cannot be read leaves the session pending, with the error and one attempt",
       ok is False and m["state"] == "pending" and m["error"] and m["attempts"] == 1, (ok, err, m))
    for _ in range(session_move.MAX_ATTEMPTS - 1):
        ok, err = session_move.move_session(s, "gone")
    m = move_of(s, "gone")
    ck("after the cap of tries it is failed", ok is False and m["state"] == "failed"
       and m["attempts"] == session_move.MAX_ATTEMPTS and "gave up after" in m["error"], m)
    ck("its rows, parts and row are as they were", (rows_of(s, "gone"), parts_of(s, "gone"),
                                                    row_of(s, "gone")) == snap["gone"], "")
    ck("a failed session is not taken again", session_move.move_recent(s) == 0)
    ck("the gauge counts the failures", states(s) == {"failed": 2}, states(s))
    # a move that works after the cause is gone
    with s.sessions_db.begin() as c:
        c.execute(session_moves.update().where(session_moves.c.session == "bad").values(state="pending"))
    ok, _ = session_move.move_session(s, "bad")
    ck("the same session moves once the cause is gone", ok and move_of(s, "bad")["state"] == "verified"
       and len(rows_of(s, "bad")) == 7)
    s.close()


def transient():
    print("== a transient error is retried, a mismatch is not ==")
    from sqlalchemy.exc import OperationalError
    s = open_store()
    seed(s, "blip", 5, 3000)
    seed(s, "blip2", 4, 2500)
    session_ingest.reconcile_sessions(s)
    session_move.plan(s)
    real = session_move._rebuild
    boom = {"n": 1}

    def flaky(store, conn, read, sid):
        if sid == "blip" and boom["n"] > 0:
            boom["n"] -= 1
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        return real(store, conn, read, sid)

    old = session_move.RETRY_AFTER
    session_move._rebuild = flaky
    try:
        session_move.RETRY_AFTER = 3600.0
        n = session_move.move_recent(s)
        m = move_of(s, "blip")
        ck("a database error leaves the session pending with an attempt", n == 2 and m["state"] == "pending"
           and m["attempts"] == 1 and "OperationalError" in (m["error"] or ""), (n, m))
        ck("the other session of the batch moved", move_of(s, "blip2")["state"] == "verified")
        ck("it is left alone until RETRY_AFTER has passed", session_move.move_recent(s) == 0
           and session_move.retrying(s) == 1)
        session_move.RETRY_AFTER = 0.0
        ck("then it is taken again and verifies", session_move.move_recent(s) == 1
           and move_of(s, "blip")["state"] == "verified" and states(s) == {"verified": 2}, states(s))
        ck("its error is cleared and its rows are the lines", move_of(s, "blip")["error"] is None
           and len(rows_of(s, "blip")) == 5)
    finally:
        session_move._rebuild = real
        session_move.RETRY_AFTER = old
    # the thread keeps going while a session waits to be tried again
    seed(s, "blip3", 3, 2000)
    session_ingest.reconcile_sessions(s)
    with s.sessions_db.begin() as c:
        c.execute(session_moves.insert().values(session="blip3", state="pending", target="table",
                                                lines_events=3))
    boom["n"] = 0

    def flaky3(store, conn, read, sid):
        if sid == "blip3" and boom["n"] < 2:
            boom["n"] += 1
            raise OperationalError("SELECT 1", {}, Exception("busy"))
        return real(store, conn, read, sid)

    session_move._rebuild = flaky3
    session_move.RETRY_AFTER = 0.05      # a session that failed is out of the next batch, so the thread waits
    try:
        session_move.run(s, threading.Event(), pause=0)
    finally:
        session_move._rebuild = real
        session_move.RETRY_AFTER = old
    m = move_of(s, "blip3")
    ck("the thread tried again until it verified", m["state"] == "verified" and m["attempts"] == 2, m)
    # a failed write of the failure is printed and leaves the row as it was
    seed(s, "blip4", 3, 1500)
    session_ingest.reconcile_sessions(s)
    with s.sessions_db.begin() as c:
        c.execute(session_moves.insert().values(session="blip4", state="pending", target="table",
                                                lines_events=3))
    real_rec = session_move._record_failure

    def bad_rec(*a, **k):
        raise OperationalError("UPDATE", {}, Exception("down"))

    def bad_rebuild(store, conn, read, sid):
        raise OperationalError("SELECT 1", {}, Exception("down"))

    session_move._rebuild, session_move._record_failure = bad_rebuild, bad_rec
    try:
        ok, err = session_move.move_session(s, "blip4")
    finally:
        session_move._rebuild, session_move._record_failure = real, real_rec
    ck("a failed record of the failure does not raise and leaves the row pending",
       ok is False and move_of(s, "blip4")["state"] == "pending" and not move_of(s, "blip4")["attempts"])
    # a stop during the fill takes no session
    stop = threading.Event()
    real_fill = session_ingest._fill_chunk
    session_ingest._fill_chunk = lambda *a, **k: stop.set()
    try:
        n = session_move.move_recent(s, stop=stop)
    finally:
        session_ingest._fill_chunk = real_fill
    ck("a stop set during the fill takes no session", n == 0 and move_of(s, "blip4")["state"] == "pending")
    # a mismatch is final even with tries left
    realc = session_move._count_rows
    session_move._count_rows = lambda conn, sid: realc(conn, sid) + 1
    try:
        ok, _ = session_move.move_session(s, "blip4")
    finally:
        session_move._count_rows = realc
    m = move_of(s, "blip4")
    ck("a count mismatch fails at once", ok is False and m["state"] == "failed" and m["attempts"] == 1, m)
    s.close()


def locks():
    print("== the rebuild and a store call that writes the session store do not shut each other out ==")
    # a store call holds the state lock, which on SQLite is DuckDB's, while it writes the session
    # store; a DuckDB read inside the rebuild's write then waits on the call, and the call on the write
    s = open_store()
    s.create_project("gone-project", "custom", "Gone", {})
    s.create_project("uc_a", "software_factory", "A", {})
    s.upsert_catalog_source("owned", "logs", "webhook", "1m", {})
    s.set_owned_by("source", "owned", "uc_a")
    doc = s.create_doc("uc_a", "spec", "Spec", "# hi")
    s.upsert_catalog_source("loose", "logs", "webhook", "1m", {})
    # the last three write the session store once the project tables are there (TR-1610)
    calls = [("create_person", lambda: s.create_person("Rosa")),
             ("mark_customized", lambda: s.mark_customized("source", "owned")),
             ("remove_doc", lambda: s.remove_doc("uc_a", doc)),
             ("put_in_project", lambda: s.put_in_project("source", "loose", "uc_a")),
             ("update_person", lambda: s.update_person("local", name="Sam")),
             ("delete_project", lambda: s.delete_project("gone-project"))]
    for i, (name, _) in enumerate(calls):
        seed(s, f"busy-{i}", 5, 3000)
    for sid in ("late", "bare", "quiet"):
        seed(s, sid, 5, 3000)
    session_ingest.reconcile_sessions(s)
    session_move.plan(s)
    db = s.sessions_db
    begin = db.begin
    mover = threading.current_thread()
    for i, (name, call) in enumerate(calls):
        out: dict = {}

        def run():
            try:
                call()
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {e}"

        t = threading.Thread(target=run)

        @contextmanager
        def pause():
            with begin() as conn:
                if threading.current_thread() is mover and t.ident is None:
                    t.start()       # the rebuild's write has just opened: the call comes now
                    end = time.time() + 5
                    while t.is_alive() and not s._lock.locked() and time.time() < end:
                        time.sleep(0.005)
                yield conn

        db.begin = pause
        try:
            started = time.time()
            ok, err = session_move.move_session(s, f"busy-{i}")
        finally:
            db.begin = begin
        t.join(30)
        ck(f"{name} during a rebuild raises nothing", not t.is_alive() and "error" not in out, out)
        ck(f"the rebuild open during {name} verifies without waiting out a lock",
           ok and err is None and time.time() - started < 4, (ok, err, time.time() - started))

    # the count is checked again after the read: a line that came or went meanwhile is not a failure
    chunks = session_move._duck_chunks

    def one_more(store, sources, sid):
        yield from chunks(store, sources, sid)
        seed(s, sid, 1, 100)

    before = (rows_of(s, "late"), parts_of(s, "late"), row_of(s, "late"))
    session_move._duck_chunks = one_more
    try:
        ok, err = session_move.move_session(s, "late")
    finally:
        session_move._duck_chunks = chunks
    m = move_of(s, "late")
    ck("a line added during the read leaves the session pending, to be tried again",
       ok is False and m["state"] == "pending" and m["attempts"] == 1
       and "5 lines before the read and 6 after" in (m["error"] or ""), (ok, err, dict(m)))
    ck("its rows, parts and row are as they were",
       (rows_of(s, "late"), parts_of(s, "late"), row_of(s, "late")) == before, "")
    ok, err = session_move.move_session(s, "late")
    ck("the next try moves all of them", ok and move_of(s, "late")["state"] == "verified"
       and len(rows_of(s, "late")) == 6, (ok, err, len(rows_of(s, "late"))))

    # a session whose DuckDB lines are gone: nothing to read ahead, its table lines stay
    session_ingest.record(s, [line("bare", 200), line("bare", 100)])
    with s._lock:
        s.con.execute("DELETE FROM events WHERE key_value = 'bare'")
    ok, err = session_move.move_session(s, "bare")
    m = move_of(s, "bare")
    ck("a session with no DuckDB line moves and keeps its table lines",
       ok and m["state"] == "verified" and m["lines_events"] == 0 and m["lines_after"] == 2
       and len(rows_of(s, "bare")) == 2, (ok, err, dict(m)))

    # no DuckDB statement at all between the opening of the rebuild's write and its end
    inside = {"on": False}
    ran: list = []

    class Watch:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, *a, **kw):
            if inside["on"]:
                ran.append(sql.split("\n")[0][:80])
            return self.con.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self.con, name)

    con, sc = s.con, s.sc
    s.con = Watch(con)
    if sc is con:
        s.sc = s.con
    @contextmanager
    def watched():
        with begin() as conn:
            inside["on"] = True
            try:
                yield conn
            finally:
                inside["on"] = False

    db.begin = watched
    try:
        ok, err = session_move.move_session(s, "quiet")
    finally:
        db.begin = begin
        s.con, s.sc = con, sc
    ck("the session moved", ok, err)
    ck("DuckDB runs no statement inside the rebuild's write", ran == [], ran)
    s.close()


def thread():
    print("== the daemon's thread, its switch and the edition ==")
    s = open_store()
    seed(s, "t1", 5, 3000)
    seed(s, "t2", 5, 2000)
    session_ingest.reconcile_sessions(s)
    ck("a cell that is not a factory starts no move", session_move.start(s) is None)
    s.set_setting("edition", "factory")
    session_move.MOVE_ENABLED = False
    try:
        ck("parked, it starts none", session_move.start(s) is None)
    finally:
        session_move.MOVE_ENABLED = True
    ck("nothing was planned", states(s) == {})
    th = session_move.start(s)
    ck("a factory cell starts the thread", th is not None)
    th.join(30)
    ck("it ends when the sessions are done", not th.is_alive())
    ck("both sessions verified", states(s) == {"verified": 2}, states(s))
    # a restart resumes from the ledger: nothing left, nothing planned again
    stop = threading.Event()
    session_move.run(s, stop, pause=0)
    ck("a second run changes nothing", states(s) == {"verified": 2})
    s.close()
    # a stop between batches leaves the rest pending, and a later run finishes them
    s = open_store()
    for i in range(3):
        seed(s, f"u{i}", 4, 3000 - i * 100)
    session_ingest.reconcile_sessions(s)
    stop = threading.Event()
    real = session_move.move_recent

    def one_batch(store, limit=session_move.BATCH, stop=None, now=None):
        n = real(store, limit=1, stop=stop, now=now)
        if stop is not None:
            stop.set()
        return n

    session_move.move_recent = one_batch
    try:
        session_move.run(s, stop, pause=0)
    finally:
        session_move.move_recent = real
    ck("a stopped run leaves the rest pending", states(s) == {"verified": 1, "pending": 2}, states(s))
    session_move.run(s, threading.Event(), pause=0)
    ck("the next run finishes them", states(s) == {"verified": 3}, states(s))
    s.close()


def revision():
    print("== revision 0011_session_moves ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "sm_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")

    def version(sdb):
        with sdb.begin() as c:
            return [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]

    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        insp = inspect(sdb.engine)
        ck("the table exists with its columns", sorted(c["name"] for c in insp.get_columns("session_moves"))
           == sorted(["session", "state", "target", "lines_events", "lines_after", "started_at",
                      "verified_at", "error", "attempts"]))
        ck("its key is session", insp.get_pk_constraint("session_moves")["constrained_columns"] == ["session"])
        ck("it is not a moved table", "session_moves" not in __import__(
            "tares.session_store.tables", fromlist=["x"]).MOVED_TABLE_NAMES)
        with sdb.begin() as c:
            c.execute(text("INSERT INTO sessions (session, lines_complete) VALUES ('x', true)"))
            c.execute(text("INSERT INTO session_moves (session, state) VALUES ('x', 'pending')"))
            c.execute(text("INSERT INTO session_ingest_marks (name) VALUES ('session_move_planned')"))
            c.execute(text("INSERT INTO session_ingest_marks (name) VALUES ('other_mark')"))
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0009_session_origin")
        names = inspect(sdb.engine).get_table_names()
        with sdb.begin() as c:
            kept = c.execute(text("SELECT lines_complete FROM sessions WHERE session = 'x'")).scalar()
        ck("a downgrade drops only the table", "session_moves" not in names and "sessions" in names
           and "session_lines" in names and kept, names)
        with sdb.begin() as c:
            marks = sorted(r[0] for r in c.execute(text("SELECT name FROM session_ingest_marks")).all())
        ck("a downgrade drops the plan's mark, so an upgrade plans again, and no other mark",
           marks == ["other_mark"], marks)
        sdb.upgrade()
        ck("an upgrade builds it again", "session_moves" in inspect(sdb.engine).get_table_names()
           and version(sdb) == ["0015_ticket_pr_columns"])
        sdb.upgrade()
        ck("an upgrade at the head changes nothing", version(sdb) == ["0015_ticket_pr_columns"])
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


if __name__ == "__main__":
    revision()
    main_run()
    repeats()
    failures()
    transient()
    locks()
    thread()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED:\n  " + "\n  ".join(FAILS))
        sys.exit(1)
    print("\nall ok")
