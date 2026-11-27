"""The one-time move of older sessions from DuckDB straight to the archive (TR-1611), with a folder
archive: every old session ends verified with its full line list in parts and reads from the
archive; the move resumes after a stop; a session that changes while it moves (a new line, the
hourly archive job, another process's ingest) is left as it is and moved again after a wait; on
Postgres a lock another holds is not waited for longer than LOCK_WAIT (TR-1664); the sweep removes
only old archive files that no part row names; a failed write or check loses nothing; a cell with no
archive keeps the lines in DuckDB. Runs on SQLite and on the Postgres harness. Plain script, exits
non-zero on failure."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

PG = os.environ.get("TARES_TEST_PG", "").strip()

from sqlalchemy import func, insert, select, text, update

from tares import metrics, session_archive, session_ingest, session_move, session_read
from tares.envelope import Envelope, now_utc
from tares.session_archive import FileBackend, S3Backend
from tares.session_store.tables import (session_archive_parts, session_lines, session_moves,
                                        session_turns, sessions)
from tares.store import Store

# the harness (CI's postgres suite) puts every Store on Postgres; the duckdb and mit suites have
# TARES_TEST_PG and no harness, so their Stores are on SQLite
HARNESS = Store.__init__.__module__ != "tares.store"
POSTGRES = bool(PG and HARNESS)
FAILS = []
SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
DAY = 86400
_N = [0]
ARCHIVE = tempfile.mkdtemp()
os.environ["TARES_ARCHIVE_URL"] = "file://" + ARCHIVE


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, at, etype="assistant", text_="t"):
    """One envelope; `at` is how long ago, in seconds."""
    _N[0] += 1
    payload = {"uuid": "u-" + uuid.uuid4().hex[:10], "n": _N[0]}
    return Envelope(source=SRC, source_type="agent_session", key_value=session, event_type=etype,
                    text=text_, event_time=NOW - timedelta(seconds=at), payload=payload,
                    labels={"tares_person": "ann"})


def seed(s, session, n, first_age=20 * DAY, step=60):
    """`n` lines of `session` straight into DuckDB, `first_age` seconds ago for the oldest."""
    types = ("user", "assistant", "tool_use", "tool_result")
    envs = [line(session, first_age - i * step, types[i % 4]) for i in range(n)]
    s._append_events(envs)
    return envs


def open_store(path=None):
    return Store(path or os.path.join(tempfile.mkdtemp(), "t.duckdb"))


def ready(s):
    """What the daemon does before the move: the sessions table counts DuckDB's lines, then the plan."""
    session_ingest.reconcile_sessions(s)
    session_move.plan(s)


def rows_of(s, session):
    t = session_lines.c
    with s.sessions_db.begin() as c:
        return [tuple(r) for r in c.execute(select(t.seq, t.at, t.payload).where(
            t.session == session).order_by(t.seq)).all()]


def parts_of(s, session):
    with s.sessions_db.begin() as c:
        return c.execute(select(session_archive_parts).where(
            session_archive_parts.c.session == session).order_by(session_archive_parts.c.part)).all()


def head_of(s, session):
    with s.sessions_db.begin() as c:
        r = c.execute(select(sessions).where(sessions.c.session == session)).first()
    return None if r is None else dict(r._mapping)


def held(s, session):
    """Everything a move may change of a session."""
    return rows_of(s, session), [tuple(p) for p in parts_of(s, session)], head_of(s, session)


def move_of(s, session):
    with s.sessions_db.begin() as c:
        r = c.execute(select(session_moves).where(session_moves.c.session == session)).first()
    return None if r is None else r._mapping


def duck_payloads(s, session):
    with s._lock:
        return [json.dumps(json.loads(r[0])) for r in s.con.execute(
            "SELECT payload FROM events WHERE key_value = ? ORDER BY event_time, rowid",
            [session]).fetchall()]


def duck_total(s):
    with s._lock:
        return s.con.execute("SELECT count(*) FROM events").fetchone()[0]


def pj(envs):
    return [json.dumps(e.payload) for e in envs]


def archived_payloads(s, session):
    """The payloads in the session's parts, each part checked against its row."""
    backend = FileBackend(ARCHIVE)
    out = []
    for p in parts_of(s, session):
        out += [r["payload"] for r in session_archive.read_part(backend, p)]
    return out


def parts_check_out(s, session):
    try:
        archived_payloads(s, session)
        return True
    except Exception as e:
        print("       part:", e)
        return False


def check_moved(s, sid, want, label):
    """The session is verified in the archive and holds exactly the payloads `want`, in order."""
    m, h, parts = move_of(s, sid), head_of(s, sid), parts_of(s, sid)
    ck(f"{label}: verified, target archive, with the count",
       m["state"] == "verified" and m["target"] == "archive" and m["lines_after"] == len(want)
       and m["verified_at"] is not None and m["error"] is None, dict(m))
    try:
        got = archived_payloads(s, sid)
    except Exception as e:
        got = str(e)
    ck(f"{label}: the archive holds the full list", got == want, (got if isinstance(got, str)
                                                                 else (len(got), len(want))))
    ck(f"{label}: no row left in session_lines", rows_of(s, sid) == [], rows_of(s, sid)[:2])
    seqs = [(p.first_seq, p.last_seq, p.lines) for p in parts]
    ck(f"{label}: the parts number the lines 1..N",
       sum(x[2] for x in seqs) == len(want) and seqs[0][0] == 1 and seqs[-1][1] == len(want), seqs)
    ck(f"{label}: the row says table, complete, N lines, the newest line",
       h["lines_origin"] == "table" and h["lines_complete"] and h["line_count"] == len(want)
       and h["last_line_at"] is not None and h["last_line_at"] < NOW - timedelta(days=7), h)
    read = session_read.lines(s, SRC, sid, 100000, 0)
    ck(f"{label}: the conversation reads from the archive", read is not None and read[1] == "archive",
       None if read is None else read[1])
    ck(f"{label}: the conversation holds the same lines",
       read is not None and sorted(x["payload"] for x in read[0]) == sorted(want))


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield old
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def meanwhile(action, session):
    """Run `action` once, after `session`'s list was written to the archive and before the store is
    changed to point at it."""
    real = session_move._write_list
    ran = []

    def write(store, backend, sources, sid, have, part_no):
        out = real(store, backend, sources, sid, have, part_no)
        if sid == session and not ran:
            ran.append(1)
            action()
        return out

    with patched(session_move, "_write_list", write):
        yield ran


class Waits(threading.Event):
    """A stop event that records each wait and does not sleep."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


# ── the move ────────────────────────────────────────────────────────────────────
def main_run():
    print("== 20 old sessions move to the archive and read from it ==")
    s = open_store()
    ck("the session store is on the expected database",
       s.sessions_db.dialect == ("postgresql" if POSTGRES else "sqlite"), s.sessions_db.dialect)
    # 17 plain ones, a dual-written one with lines only the table holds, one the hourly job has
    # archived once, and one of 5001 lines
    plain = [f"old{i:02d}" for i in range(17)]
    for i, sid in enumerate(plain):
        seed(s, sid, 4, 30 * DAY - i * 3600)
    seed(s, "big", 5001, 15 * DAY, step=1)
    seed(s, "dual", 5, 12 * DAY)
    s._append_events([line("dual", 11 * DAY, "user", "please do it")])
    session_ingest.reconcile_sessions(s)
    both = [line("dual", 11 * DAY - 100 - i) for i in range(3)]
    s.append(both)
    only_table = [line("dual", 10 * DAY - i) for i in range(2)]
    session_ingest.record(s, only_table)
    seed(s, "hourly", 6, 12 * DAY)
    session_ingest.reconcile_sessions(s)
    h_both = [line("hourly", 11 * DAY - i) for i in range(2)]
    s.append(h_both)
    h_only = [line("hourly", 10 * DAY)]
    session_ingest.record(s, h_only)
    session_ingest.reconcile_sessions(s)
    backend = FileBackend(ARCHIVE)
    session_archive._archive_session(s.sessions_db, backend, "hourly", NOW)
    h_late = [line("hourly", 9 * DAY)]
    session_ingest.record(s, h_late)
    session_ingest.reconcile_sessions(s)
    old_part = parts_of(s, "hourly")[0]
    old_bytes = backend.get(old_part.key)
    for i in range(2):
        seed(s, f"rec{i}", 4, 3600)
    ready(s)
    events = duck_total(s)
    with s.sessions_db.begin() as c:       # a row that is behind: the move writes it from the list
        c.execute(update(sessions).where(sessions.c.session == "hourly")
                  .values(line_count=3, last_line_at=NOW - timedelta(days=20)))

    want = {sid: duck_payloads(s, sid) for sid in plain + ["big"]}
    want["dual"] = duck_payloads(s, "dual") + pj(only_table)
    want["hourly"] = duck_payloads(s, "hourly") + pj(h_only + h_late)
    order = session_move.due_old(s, limit=100)
    ck("due_old: the 20 old sessions, the oldest last line first, no recent one",
       len(order) == 20 and set(order) == set(want) and order[:17] == plain, order)
    ck("a batch is 20", session_move.BATCH == 20)
    ck("a part holds at most 5000 lines", session_move.PART_LINES == 5000)
    counted = metrics.SESSION_MOVE_LINES._value.get() if metrics.AVAILABLE else 0
    n = session_move.move_old(s)
    ck("one call takes the 20", n == 20, n)
    ck("20 verified, the 2 recent ones pending",
       session_move.counts(s) == {"verified": 20, "pending": 2}, session_move.counts(s))
    for sid in ("old00", "old16", "dual", "hourly", "big"):
        check_moved(s, sid, want[sid], sid)
    ck("every other old session too", all(archived_payloads(s, sid) == want[sid]
                                          and rows_of(s, sid) == [] for sid in plain))
    ck("last_line_at is the newest line of the list", head_of(s, "hourly")["last_line_at"]
       == NOW - timedelta(seconds=9 * DAY), head_of(s, "hourly"))
    ck("5001 lines are 2 parts", [(p.part, p.first_seq, p.last_seq) for p in parts_of(s, "big")]
       == [(1, 1, 5000), (2, 5001, 5001)], [tuple(p) for p in parts_of(s, "big")])
    ck("the parts of an archived session are numbered after its last part",
       [p.part for p in parts_of(s, "hourly")] == [2], [p.part for p in parts_of(s, "hourly")])
    ck("a part's file is named by its content", all(
        p.key == f"hourly/{p.part}-{p.md5}.jsonl.gz" for p in parts_of(s, "hourly")),
       [p.key for p in parts_of(s, "hourly")])
    ck("the replaced part's file is left as it was", backend.get(old_part.key) == old_bytes)
    with s.sessions_db.begin() as c:
        turns = [r[0] for r in c.execute(select(session_turns.c.text).where(
            session_turns.c.session == "dual")).all()]
    ck("the turns were filled", "please do it" in turns, turns)
    if metrics.AVAILABLE:
        ck("the gauge says so", metrics.SESSION_MOVE_SESSIONS.labels(state="verified")._value.get() == 20
           and metrics.SESSION_MOVE_SESSIONS.labels(state="pending")._value.get() == 2)
        ck("the counter counted every line", metrics.SESSION_MOVE_LINES._value.get() - counted
           == sum(len(v) for v in want.values()))
    ck("nothing is left to take", session_move.move_old(s) == 0)
    ck("move_recent takes the 2 recent ones into the table", session_move.move_recent(s) == 2
       and move_of(s, "rec0")["target"] == "table" and len(rows_of(s, "rec0")) == 4)
    ck("DuckDB keeps every line", duck_total(s) == events, (duck_total(s), events))
    ck("the hourly job finds nothing of them to do", session_archive.archive_pass(s) == 0)

    # a line that arrives after the move, and the hourly job after it
    late = line("old00", 8 * DAY)
    s.append([late])
    got = session_read.lines(s, SRC, "old00", 100, 0)
    ck("a late line is read from both", got is not None and got[1] == "both" and len(got[0]) == 5, got
       and got[1])
    ck("the hourly job archives it as the next part", session_archive.archive_pass(s) == 1
       and [(p.part, p.first_seq) for p in parts_of(s, "old00")] == [(1, 1), (2, 5)]
       and archived_payloads(s, "old00") == want["old00"] + pj([late]),
       [tuple(p) for p in parts_of(s, "old00")])
    s.close()


def thread():
    print("== the thread: recent sessions first, then older ones, 1 s between batches ==")
    s = open_store()
    for i in range(5):
        seed(s, f"o{i}", 3)
    for i in range(3):
        seed(s, f"r{i}", 3, 3600)
    session_ingest.reconcile_sessions(s)
    order = []
    real_new, real_old = session_move.move_session, session_move.move_old_session

    def new(store, sid, *a):
        order.append("recent")
        return real_new(store, sid, *a)

    def old(store, sid, *a):
        order.append("old")
        return real_old(store, sid, *a)

    real_due, real_fill = session_move.due_old, session_move._fill
    filled = []

    def fill(store, sources, sids):
        filled.append(list(sids))
        return real_fill(store, sources, sids)

    stop = Waits()
    with patched(session_move, "move_session", new), patched(session_move, "move_old_session", old), \
            patched(session_move, "_fill", fill), \
            patched(session_move, "due_old", lambda store, limit=20, now=None: real_due(store, 2, now)):
        session_move.run(s, stop)
    ck("both movers fill one session at a time, so one session's lines are held at once",
       [len(x) for x in filled] == [1] * 8, filled)
    ck("all 8 are verified", session_move.counts(s) == {"verified": 8}, session_move.counts(s))
    ck("the recent ones went first", order == ["recent"] * 3 + ["old"] * 5, order)
    ck("OLD_PAUSE is 1 s", session_move.OLD_PAUSE == 1.0)
    ck("it slept 1 s after each of the 3 batches of older sessions", stop.waits == [0.5, 1.0, 1.0, 1.0],
       stop.waits)
    s.close()


def resume():
    print("== the move resumes after a stop ==")
    path = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    s = open_store(path)
    for i in range(7):
        seed(s, f"s{i}", 3, 30 * DAY - i * 3600)
    ready(s)
    stop = threading.Event()
    real = session_move.move_old_session
    done = []

    def three(store, sid, *a):
        out = real(store, sid, *a)
        done.append(sid)
        if len(done) == 3:
            stop.set()
        return out

    with patched(session_move, "move_old_session", three):
        n = session_move.move_old(s, stop=stop)
    ck("a stop ends the batch after the third", n == 3 and session_move.counts(s)
       == {"verified": 3, "pending": 4}, (n, session_move.counts(s)))
    ck("a session not reached is untouched", parts_of(s, "s5") == []
       and move_of(s, "s5")["state"] == "pending")
    first = {sid: held(s, sid) for sid in done}
    s.close()

    s = open_store(path)
    session_move.run(s, threading.Event(), pause=0, old_pause=0)
    ck("the next start finishes them", session_move.counts(s) == {"verified": 7}, session_move.counts(s))
    ck("the 3 verified ones were not written again", {sid: held(s, sid) for sid in done} == first)
    check_moved(s, "s5", duck_payloads(s, "s5"), "s5")
    s.close()


# ── a session that changes while it moves ───────────────────────────────────────
def new_line():
    print("== a new line during the move: pending, and moved again ==")
    s = open_store()
    seed(s, "live", 4)
    ready(s)
    fresh = line("live", 5)
    out = io.StringIO()
    with meanwhile(lambda: s.append([fresh]), "live") as ran, contextlib.redirect_stdout(out):
        n = session_move.move_old(s)
    m = move_of(s, "live")
    ck("the line came during the move", ran == [1] and n == 1)
    ck("it is back to pending, with no error and no attempt", m["state"] == "pending"
       and m["error"] is None and not m["attempts"], dict(m))
    ck("nothing was replaced", parts_of(s, "live") == [] and [r[2] for r in rows_of(s, "live")]
       == pj([fresh]), held(s, "live"))
    ck("the log says why", "live: it changed while it moved" in out.getvalue(), out.getvalue())
    ck("move_recent leaves it until the wait has passed", session_move.move_recent(s) == 0)
    with patched(session_move, "RETRY_AFTER", 0.0):
        again = session_move.move_recent(s)
    ck("then move_recent redoes it", again == 1 and move_of(s, "live")["state"]
       == "verified" and move_of(s, "live")["target"] == "table")
    ck("with every line", [r[2] for r in rows_of(s, "live")] == duck_payloads(s, "live")
       and len(rows_of(s, "live")) == 5, len(rows_of(s, "live")))

    # lines that leave last_line_at as it is: one with an old time (both stores), one only in
    # DuckDB (the count failed after the DuckDB commit), one only in the session store
    for name, add in (("a late line", lambda sid, e: s.append([e])),
                      ("a line only DuckDB has", lambda sid, e: s._append_events([e])),
                      ("a line only the store has", lambda sid, e: session_ingest.record(s, [e]))):
        sid = "late-" + name.replace(" ", "-")
        seed(s, sid, 4)
        ready(s)
        with s.sessions_db.begin() as c:
            c.execute(insert(session_moves).values(session=sid, state="pending", target="table"))
        extra = line(sid, 25 * DAY)
        with meanwhile(lambda: add(sid, extra), sid):
            ok, err = session_move.move_old_session(s, sid, FileBackend(ARCHIVE))
        ck(f"{name}: not replaced, still pending", (ok, err) == (False, None)
           and move_of(s, sid)["state"] == "pending" and parts_of(s, sid) == [], (ok, err))
        ok, err = session_move.move_old_session(s, sid, FileBackend(ARCHIVE))
        got = archived_payloads(s, sid)
        ck(f"{name}: the next try archives it with the rest", ok and len(got) == 5
           and pj([extra])[0] in got, (ok, err, len(got)))

    # a line that comes after the last count, while the replace is committing, waits for the commit
    seed(s, "commit", 4)
    ready(s)
    with s.sessions_db.begin() as c:
        c.execute(insert(session_moves).values(session="commit", state="pending", target="table"))
    events, seen, writers = duck_total(s), [], []
    real_count = session_move._count_archived

    def count_with_a_line(conn, sid):
        t = threading.Thread(target=s.append, args=([line("commit", 5)],))
        t.start()
        writers.append(t)
        t.join(1.0)
        seen.append(duck_total(s))
        return real_count(conn, sid)

    with patched(session_move, "_count_archived", count_with_a_line):
        ok, err = session_move.move_old_session(s, "commit", FileBackend(ARCHIVE))
    writers[0].join(30)
    ck("a line during the replace is not written until it committed", ok and seen == [events],
       (ok, err, seen, events))
    ck("then it is stored after the moved ones", duck_total(s) == events + 1
       and [r[0] for r in rows_of(s, "commit")] == [5], [r[0] for r in rows_of(s, "commit")])

    # a store call that holds DuckDB's lock while it writes the session store (on SQLite one writer
    # at a time) is not shut out by the replace, which counts DuckDB before it starts its write
    seed(s, "locks", 4)
    ready(s)
    with s.sessions_db.begin() as c:
        c.execute(insert(session_moves).values(session="locks", state="pending", target="table"))
    wrote, others, holding = [], [], threading.Event()
    real_duck = session_move._duck_count

    def other_call():
        with s._lock:
            holding.set()
            try:
                with s.sessions_db.begin() as c:
                    c.execute(update(session_moves).where(session_moves.c.session == "nobody")
                              .values(error=None))
                wrote.append("ok")
            except Exception as e:
                wrote.append(type(e).__name__)

    def count_while_held(store, sources, sid):
        if store._sess_lock.locked() and not others:
            others.append(threading.Thread(target=other_call))
            others[0].start()
            holding.wait(10)
        return real_duck(store, sources, sid)

    with patched(session_move, "_duck_count", count_while_held):
        ok, err = session_move.move_old_session(s, "locks", FileBackend(ARCHIVE))
    others[0].join(30)
    ck("a call under DuckDB's lock still writes the session store during the replace",
       ok and wrote == ["ok"], (ok, err, wrote))

    # through the thread: it ends verified without another start
    seed(s, "live2", 4)
    ready(s)
    with s.sessions_db.begin() as c:
        c.execute(insert(session_moves).values(session="live2", state="pending", target="table"))
    with meanwhile(lambda: s.append([line("live2", 5)]), "live2"), \
            patched(session_move, "RETRY_AFTER", 0.2):
        session_move.run(s, threading.Event(), pause=0, old_pause=0)
    ck("the thread redoes it", move_of(s, "live2")["state"] == "verified"
       and len(rows_of(s, "live2")) == 5, dict(move_of(s, "live2")))
    s.close()


def hourly_between():
    print("== the hourly job archives the session between the list and the replace ==")
    s = open_store()
    seed(s, "hb", 5)
    session_ingest.reconcile_sessions(s)
    only = [line("hb", 15 * DAY)]
    session_ingest.record(s, only)
    ready(s)
    backend = FileBackend(ARCHIVE)
    with meanwhile(lambda: session_archive._archive_session(s.sessions_db, backend, "hb", NOW), "hb"):
        ok, err = session_move.move_old_session(s, "hb", backend)
    ck("the move leaves it", (ok, err) == (False, None) and move_of(s, "hb")["state"] == "pending",
       (ok, err))
    ck("the job's part is kept and matches its file", [p.key for p in parts_of(s, "hb")]
       == ["hb/1.jsonl.gz"] and archived_payloads(s, "hb") == pj(only), held(s, "hb"))
    want = duck_payloads(s, "hb") + pj(only)
    ok, err = session_move.move_old_session(s, "hb", backend)
    ck("the next try verifies", ok, err)
    check_moved(s, "hb", want, "hb")
    s.close()


def hourly_around():
    s = open_store()
    seed(s, "ha", 5)
    session_ingest.reconcile_sessions(s)
    only = [line("ha", 15 * DAY)]
    session_ingest.record(s, only)
    ready(s)
    want = duck_payloads(s, "ha") + pj(only)
    if not POSTGRES:
        print("== the hourly job reads before the move commits and finishes after (SQLite) ==")

        class During(FileBackend):
            def put(self, key, data):
                self.moved = session_move.move_old_session(s, "ha", FileBackend(ARCHIVE))
                return super().put(key, data)

        job = During(ARCHIVE)
        try:
            session_archive._archive_session(s.sessions_db, job, "ha", NOW)
            failed = None
        except Exception as e:
            failed = e
        ck("the move verified meanwhile", job.moved == (True, None), job.moved)
        ck("the job rolls back", failed is not None, failed)
        check_moved(s, "ha", want, "ha")
    else:
        print("== the move leaves a session the hourly job holds, without waiting (Postgres) ==")
        inside, release = threading.Event(), threading.Event()

        class Slow(FileBackend):
            def put(self, key, data):
                inside.set()
                release.wait(30)
                return super().put(key, data)

        job = threading.Thread(target=session_archive._archive_session,
                               args=(s.sessions_db, Slow(ARCHIVE), "ha", NOW))
        job.start()
        ck("the job holds the session", inside.wait(30))
        out = []
        mover = threading.Thread(target=lambda: out.append(
            session_move.move_old_session(s, "ha", FileBackend(ARCHIVE))))
        mover.start()
        mover.join(10)
        left = list(out)            # what it answered while the job still held the session
        free = s._sess_lock.acquire(timeout=1)
        if free:
            s._sess_lock.release()
        release.set()
        job.join(30)
        mover.join(30)
        ck("the move left the session while the job held it", left == [(False, None)], (left, out))
        ck("and let go of the lock ingest waits on", free)
        ck("nothing recorded as an error", move_of(s, "ha")["state"] == "pending"
           and not move_of(s, "ha")["attempts"], dict(move_of(s, "ha")))
        ck("the job's part is whole", archived_payloads(s, "ha") == pj(only))
        ok, err = session_move.move_old_session(s, "ha", FileBackend(ARCHIVE))
        ck("the next try verifies", ok, err)
        check_moved(s, "ha", want, "ha")
    s.close()


def other_ingest():
    print("== another process's ingest holds the session's row while the move replaces it ==")
    s = open_store()
    seed(s, "oi", 5)
    ready(s)
    db = s.sessions_db
    holding, release = threading.Event(), threading.Event()

    def ingest():
        # what session_ingest._add does, held open: no lock of this process is taken
        with db.begin() as c:
            c.execute(update(sessions).where(sessions.c.session == "oi")
                      .values(line_count=sessions.c.line_count + 1))
            c.execute(insert(session_lines).values(session="oi", seq=6, line_key="other",
                                                   at=NOW - timedelta(days=15), type="assistant",
                                                   labels="{}", render="{}", payload='{"x": 1}'))
            holding.set()
            release.wait(30)

    out = []
    threads = []

    def start_ingest():
        t = threading.Thread(target=ingest)
        t.start()
        threads.append(t)
        holding.wait(30)
        threading.Timer(1.0, release.set).start()

    with meanwhile(start_ingest, "oi"):
        out = session_move.move_old_session(s, "oi", FileBackend(ARCHIVE))
    threads[0].join(30)
    ck("the move sees the line and leaves the session", out == (False, None), out)
    ck("the line is still there", [r[2] for r in rows_of(s, "oi")] == ['{"x": 1}']
       and head_of(s, "oi")["line_count"] == 6, held(s, "oi"))
    ok, err = session_move.move_old_session(s, "oi", FileBackend(ARCHIVE))
    ck("the next try archives all 6", ok and len(archived_payloads(s, "oi")) == 6, (ok, err))
    s.close()


# ── held or changed: later, not at once (TR-1664) ───────────────────────────────
def backoff():
    print("== a session left alone waits RETRY_AFTER, with no attempt counted ==")
    s = open_store()
    seed(s, "bo", 4)
    ready(s)
    tries = []
    real = session_move._replace

    def held_once(*a):
        tries.append(time.monotonic())
        return False if len(tries) == 1 else real(*a)

    with patched(session_move, "_replace", held_once):
        with patched(session_move, "RETRY_AFTER", 3600.0):
            with contextlib.redirect_stdout(io.StringIO()):
                took = [session_move.move_old(s) for _ in range(3)]
            m = move_of(s, "bo")
            ck("taken once, not handed back on the next passes", took == [1, 0, 0] and len(tries) == 1,
               (took, len(tries)))
            ck("pending, no attempt, no error, with the time", m["state"] == "pending"
               and not m["attempts"] and m["error"] is None and m["started_at"] is not None, dict(m))
            ck("it counts as waiting, so the thread does not end",
               session_move.retrying(s, old=True) == 1 and session_move.due_old(s) == [])
        with patched(session_move, "RETRY_AFTER", 0.5), contextlib.redirect_stdout(io.StringIO()):
            session_move.run(s, threading.Event(), pause=0, old_pause=0)
    ck("the thread tries it again after the wait", len(tries) == 2 and tries[1] - tries[0] >= 0.5,
       [t - tries[0] for t in tries])
    ck("and it is verified", move_of(s, "bo")["state"] == "verified", dict(move_of(s, "bo")))
    s.close()


@contextlib.contextmanager
def holding(s, statement, params=None):
    """Another connection runs `statement` and keeps its transaction open until the block ends."""
    got, release = threading.Event(), threading.Event()

    def go():
        with s.sessions_db.begin() as c:
            c.execute(statement, params or {})
            got.set()
            release.wait(60)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    if not got.wait(30):
        raise RuntimeError("the lock was not taken")
    try:
        yield
    finally:
        release.set()
        t.join(30)


def lock_timeouts():
    if not POSTGRES:
        return
    print("== a lock another holds is not waited for past LOCK_WAIT (Postgres) ==")
    ck("LOCK_WAIT is under a second", 0 < session_move.LOCK_WAIT < 1)

    def row(sid):
        return (update(sessions).where(sessions.c.session == sid)
                .values(line_count=sessions.c.line_count), None)

    def advisory(sid):
        return (text("SELECT pg_advisory_xact_lock(hashtext('tares_session_move:' "
                     "|| CAST(:s AS text)))"), {"s": sid})

    for name, age, lock in (("old-row", 20 * DAY, row), ("old-advisory", 20 * DAY, advisory),
                            ("new-row", 3600, row), ("new-advisory", 3600, advisory)):
        recent = age < DAY
        s = open_store()
        seed(s, name, 4, age)
        ready(s)
        before = held(s, name)
        statement, params = lock(name)
        out, took, log = [], [], io.StringIO()

        def move():
            t0 = time.monotonic()
            with contextlib.redirect_stdout(log):
                out.append((session_move.move_recent if recent else session_move.move_old)(s))
            took.append(time.monotonic() - t0)

        with holding(s, statement, params):
            t = threading.Thread(target=move, daemon=True)
            t.start()
            t.join(15)
            answered = list(out)
            free = s._sess_lock.acquire(timeout=1)
            if free:
                s._sess_lock.release()
        t.join(30)
        ck(f"{name}: the move returns while the lock is held", answered == [1] and took[0] < 10,
           (answered, took))
        ck(f"{name}: and lets go of the lock ingest waits on", free)
        m = move_of(s, name)
        ck(f"{name}: pending, no attempt, no error, waiting", m["state"] == "pending"
           and not m["attempts"] and m["error"] is None and m["started_at"] is not None
           and session_move.retrying(s, old=not recent) == 1, dict(m))
        ck(f"{name}: the log says so", f"{name}: " in log.getvalue() and "held it" in log.getvalue()
           and "failed" not in log.getvalue(), log.getvalue())
        ck(f"{name}: nothing changed", held(s, name) == before, held(s, name))
        with patched(session_move, "RETRY_AFTER", 0.0), contextlib.redirect_stdout(io.StringIO()):
            n = (session_move.move_recent if recent else session_move.move_old)(s)
        ck(f"{name}: a later pass verifies", n == 1 and move_of(s, name)["state"] == "verified",
           dict(move_of(s, name)))
        lines = [r[2] for r in rows_of(s, name)] if recent else archived_payloads(s, name)
        ck(f"{name}: with every line", lines == duck_payloads(s, name), len(lines))
        s.close()


def one_snapshot():
    print("== the first read is one snapshot of the row, the parts and the table rows ==")
    s = open_store()
    seed(s, "snap", 4)
    ready(s)
    session_ingest.record(s, [line("snap", 15 * DAY)])
    seen, writers, waited = [], [], []
    real_state, real_rows = session_move._state, session_move._table_rows

    def state(conn, sid, lock=False):
        out = real_state(conn, sid, lock)
        if not lock:
            seen.append(out[2])
            # another writer, between the first query and the next ones
            t = threading.Thread(target=session_ingest.record, args=(s, [line("snap", 14 * DAY)]),
                                 daemon=True)
            t.start()
            writers.append(t)
            t.join(3)
            waited.append(t.is_alive())
        return out

    def rows(conn, sid):
        out = real_rows(conn, sid)
        seen.append(len(out))
        return out

    with patched(session_move, "_state", state), patched(session_move, "_table_rows", rows):
        ok, err = session_move.move_old_session(s, "snap", FileBackend(ARCHIVE))
    writers[0].join(30)
    ck("the rows read are the rows counted", seen[:2] == [1, 1], seen)
    ck("the read held no other writer up", waited == [False], waited)
    ck("the replace then sees the new line and leaves the session", (ok, err) == (False, None)
       and len(rows_of(s, "snap")) == 2 and parts_of(s, "snap") == [], (ok, err))
    s.close()


# ── the sweep of unused files (TR-1664) ─────────────────────────────────────────
OLD = 8 * DAY


def aged(root, key, age=OLD, data=b"x"):
    """A file `key` under `root`, last written `age` seconds ago; kept as it is when it exists."""
    path = os.path.join(root, *key.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                  for d, _, files in os.walk(root) for f in files)


@contextlib.contextmanager
def own_archive():
    root = tempfile.mkdtemp()
    url = os.environ["TARES_ARCHIVE_URL"]
    os.environ["TARES_ARCHIVE_URL"] = "file://" + root
    try:
        yield root
    finally:
        os.environ["TARES_ARCHIVE_URL"] = url


def part_row(s, session, part, key):
    with s.sessions_db.begin() as c:
        c.execute(insert(session_archive_parts).values(
            session=session, part=part, first_seq=1, last_seq=1, lines=1, bytes=1, md5="m", key=key,
            written_at=NOW))


def sweep_files():
    print("== the sweep removes old files no part row names, and nothing else ==")
    with own_archive() as root:
        s = open_store()
        seed(s, "sw", 4)
        session_ingest.reconcile_sessions(s)
        session_ingest.record(s, [line("sw", 15 * DAY)])
        session_archive._archive_session(s.sessions_db, FileBackend(root), "sw", NOW)
        ready(s)
        session_move.move_old(s)
        used = parts_of(s, "sw")[0]
        ck("the move left the replaced file", tree(root) == sorted(["sw/1.jsonl.gz", used.key])
           and used.part == 2, tree(root))
        s.set_setting("edition", "factory")
        ck("a young unused file stays", session_archive.sweep(s) == 0 and len(tree(root)) == 2)
        orphan_try = "sw/2-" + "a" * 32 + ".jsonl.gz"        # a try of part 2 that did not finish
        keep = ["sw/3.jsonl.gz",                              # after the last recorded part
                "sw/3-" + "b" * 32 + ".jsonl.gz",
                "nobody/1.jsonl.gz",                          # a session with no part here
                "sw/1.jsonl.gz.tmp-123", "sw/notes.txt", "sw/01.jsonl.gz", "sw/1-abc.jsonl.gz",
                "sw/deep/1.jsonl.gz", "1.jsonl.gz", "other.txt",
                "%73w/1.jsonl.gz"]                            # not a name part_key writes
        young = "sw/2-" + "c" * 32 + ".jsonl.gz"
        for key in keep + [orphan_try, used.key, "sw/1.jsonl.gz"]:
            aged(root, key)
        aged(root, young, 6 * DAY)
        everything = tree(root)

        for edition in ("platform", "sre"):
            s.set_setting("edition", edition)
            ck(f"a {edition} cell removes nothing", session_archive.sweep(s) == 0
               and tree(root) == everything)
        s.set_setting("edition", "factory")

        class NoList(FileBackend):
            def files(self):
                raise OSError("listing failed")

        try:
            session_archive.sweep(s, NoList(root))
            raised = False
        except OSError:
            raised = True
        ck("a listing that fails removes nothing", raised and tree(root) == everything)

        def broken():
            raise RuntimeError("no rows")

        out = io.StringIO()
        with patched(s.sessions_db, "read", broken), contextlib.redirect_stdout(out):
            n = session_archive.sweep_pass(s)
        ck("rows that cannot be read remove nothing, and one line says so", n == 0
           and tree(root) == everything and out.getvalue().count("sweep failed") == 1, out.getvalue())

        class Rewritten(FileBackend):          # listed as old, written again since
            def files(self):
                return [(k, m - timedelta(days=30)) for k, m in super().files()]

        os.utime(os.path.join(root, "sw", "1.jsonl.gz"))
        os.utime(os.path.join(root, *orphan_try.split("/")))
        os.utime(os.path.join(root, *young.split("/")))
        ck("a file written again after the listing stays",
           session_archive.sweep(s, Rewritten(root)) == 0 and tree(root) == everything)
        aged(root, "sw/1.jsonl.gz")
        aged(root, orphan_try)
        aged(root, young, 6 * DAY)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = session_archive.sweep_pass(s)
        ck("the replaced file and the old try are removed", n == 2 and tree(root)
           == sorted(set(everything) - {"sw/1.jsonl.gz", orphan_try}), (n, tree(root)))
        ck("one line says how many", "removed 2 unused file(s)" in out.getvalue(), out.getvalue())
        ck("the session still reads", archived_payloads_in(root, s, "sw") is not None)

        # the hourly loop runs it
        aged(root, "sw/1.jsonl.gz")

        async def loop():
            stop = asyncio.Event()
            task = asyncio.create_task(session_archive.run(s, stop, every=0.05, first=0.05))
            for _ in range(100):
                if "sw/1.jsonl.gz" not in tree(root):
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, 5)

        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(loop())
        ck("the hourly loop removes it", "sw/1.jsonl.gz" not in tree(root), tree(root))

        # two ids that differ in case only share a folder where the file system ignores case
        part_row(s, "case", 1, "case/1.jsonl.gz")
        part_row(s, "Case", 2, "Case/2.jsonl.gz")

        class OtherCase(FileBackend):
            removed = []

            def files(self):
                return [("Case/1.jsonl.gz", NOW - timedelta(days=30))]

            def modified(self, key):
                return NOW - timedelta(days=30)

            def delete(self, key):
                self.removed.append(key)

        ck("a file another case of the id names stays",
           session_archive.sweep(s, OtherCase(root)) == 0 and OtherCase.removed == [])
        s.close()

    print("== no archive folder yet, and no archive ==")
    with own_archive() as root:
        s = open_store()
        s.set_setting("edition", "factory")
        os.environ["TARES_ARCHIVE_URL"] = "file://" + os.path.join(root, "never")
        ck("a folder that is not there is empty", session_archive.sweep(s) == 0)
        with patched(session_archive, "backend_of", lambda store: None):
            ck("with no archive there is nothing to do", session_archive.sweep(s) == 0)
        s.close()


def archived_payloads_in(root, s, session):
    backend = FileBackend(root)
    return [r["payload"] for p in parts_of(s, session) for r in session_archive.read_part(backend, p)]


def sweep_and_writers():
    print("== the sweep never runs against a session being written ==")
    with own_archive() as root:
        s = open_store()
        s.set_setting("edition", "factory")
        seed(s, "wr", 4)
        session_ingest.reconcile_sessions(s)
        session_ingest.record(s, [line("wr", 15 * DAY)])
        session_archive._archive_session(s.sessions_db, FileBackend(root), "wr", NOW)
        session_ingest.record(s, [line("wr", 14 * DAY)])
        ready(s)
        orphan = "wr/1-" + "d" * 32 + ".jsonl.gz"             # unused, numbered within the parts
        for who in ("hourly", "move"):
            aged(root, orphan)
            during = []

            class SweepDuring(FileBackend):
                def put(self, key, data):
                    out = super().put(key, data)
                    t = threading.Thread(target=lambda: during.append(session_archive.sweep(s)),
                                         daemon=True)
                    t.start()
                    t.join(15)
                    during.append(orphan in tree(root))
                    return out

            if who == "hourly":
                session_archive._archive_session(s.sessions_db, SweepDuring(root), "wr", NOW)
            else:
                session_move.move_old_session(s, "wr", SweepDuring(root))
            ck(f"while the {who} writer is at work, the session's files stay", during[:2] == [0, True],
               during)
            ck(f"after the {who} writer, the sweep removes the unused one",
               session_archive.sweep(s) >= 1 and orphan not in tree(root), tree(root))
        ck("what the rows name is whole", len(archived_payloads_in(root, s, "wr")) == 6)

        # a writer that starts while the sweep is between its check and its delete waits
        aged(root, orphan)
        entered, states, threads = threading.Event(), [], []

        def write():
            with session_archive.writing("another"):
                entered.set()

        class DuringDelete(FileBackend):
            def delete(self, key):
                t = threading.Thread(target=write, daemon=True)
                t.start()
                threads.append(t)
                states.append(entered.wait(0.5))
                super().delete(key)

        n = session_archive.sweep(s, DuringDelete(root))
        for t in threads:
            t.join(10)
        ck("a writer cannot start between the sweep's check and its delete", n == 1
           and states == [False] and entered.is_set(), (n, states))
        ck("nothing is left registered", not session_archive._writing, session_archive._writing)
        s.close()


def sweep_s3():
    print("== the sweep on S3, against botocore's Stubber ==")
    import boto3
    from botocore.stub import Stubber
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test-id",
                          aws_secret_access_key="test-secret")
    s = open_store()
    s.set_setting("edition", "factory")
    part_row(s, "c1", 2, "c1/2.jsonl.gz")
    b = S3Backend("bkt", "pre/fix/", client=client)
    then = datetime.now(timezone.utc) - timedelta(days=30)
    obj = lambda k: {"Key": k, "LastModified": then}
    listing = {"Bucket": "bkt", "Prefix": "pre/fix/"}
    with Stubber(client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "next",
                                            "Contents": [obj("pre/fix/c1/2.jsonl.gz"),
                                                         obj("pre/fix/c1/readme")]}, listing)
        st.add_response("list_objects_v2", {"IsTruncated": False,
                                            "Contents": [obj("pre/fix/c1/1.jsonl.gz")]},
                        {**listing, "ContinuationToken": "next"})
        st.add_response("head_object", {"LastModified": then},
                        {"Bucket": "bkt", "Key": "pre/fix/c1/1.jsonl.gz"})
        st.add_response("delete_object", {}, {"Bucket": "bkt", "Key": "pre/fix/c1/1.jsonl.gz"})
        n = session_archive.sweep(s, b)
        st.assert_no_pending_responses()
    ck("both pages are read, and only the unused part is deleted, under the prefix", n == 1, n)
    with Stubber(client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "next",
                                            "Contents": [obj("pre/fix/c1/1.jsonl.gz")]}, listing)
        st.add_client_error("list_objects_v2", service_error_code="AccessDenied",
                            http_status_code=403)
        out = io.StringIO()
        with patched(session_archive, "backend_of", lambda store: b), contextlib.redirect_stdout(out):
            n = session_archive.sweep_pass(s)
        st.assert_no_pending_responses()          # no delete was asked for
    ck("a listing that fails on a later page deletes nothing", n == 0
       and "AccessDenied" in out.getvalue(), out.getvalue())
    with Stubber(client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": False}, {"Bucket": "bkt", "Prefix": ""})
        ck("an empty bucket with no prefix", S3Backend("bkt", "", client=client).files() == [])
    s.close()


# ── a crash, a failure ──────────────────────────────────────────────────────────
class Crash(BaseException):
    pass


def crash():
    print("== a stop between writing the parts and the commit changes no part a row points to ==")
    s = open_store()
    seed(s, "cr", 6)
    session_ingest.reconcile_sessions(s)
    only = [line("cr", 15 * DAY)]
    session_ingest.record(s, only)
    backend = FileBackend(ARCHIVE)
    session_archive._archive_session(s.sessions_db, backend, "cr", NOW)
    ready(s)
    before = held(s, "cr")
    want = duck_payloads(s, "cr") + pj(only)

    def die(*a):
        raise Crash()

    with patched(session_move, "_replace", die):
        try:
            session_move.move_old_session(s, "cr", backend)
            died = False
        except Crash:
            died = True
    ck("it stopped after the files were written", died
       and len(os.listdir(os.path.join(ARCHIVE, "cr"))) == 2, os.listdir(os.path.join(ARCHIVE, "cr")))
    ck("the rows are as they were", held(s, "cr") == before)
    ck("the old part still matches its file", parts_check_out(s, "cr")
       and archived_payloads(s, "cr") == pj(only))
    # and a stop inside the transaction rolls it back
    with patched(session_move, "_count_archived", die):
        try:
            session_move.move_old_session(s, "cr", backend)
        except Crash:
            pass
    ck("a stop inside the transaction leaves the rows as they were", held(s, "cr") == before)
    ck("the ledger still says pending", move_of(s, "cr")["state"] == "pending")
    ck("the next run moves it", session_move.move_old(s) == 1)
    check_moved(s, "cr", want, "cr")
    ck("with the same file as the first try wrote", len(os.listdir(os.path.join(ARCHIVE, "cr"))) == 2,
       os.listdir(os.path.join(ARCHIVE, "cr")))
    s.close()


def failures():
    print("== a part that cannot be written or checked loses nothing ==")
    s = open_store()
    for i, sid in enumerate(("f-put", "f-ok", "f-etag", "f-ok2")):
        seed(s, sid, 4, 30 * DAY - i * 3600)
    session_ingest.reconcile_sessions(s)
    for sid in ("f-put", "f-etag"):
        session_ingest.record(s, [line(sid, 15 * DAY)])
    ready(s)
    before = {sid: held(s, sid) for sid in ("f-put", "f-etag")}
    real = FileBackend.put

    def bad(self, key, data):
        if key.startswith("f-put/"):
            raise OSError("disk full")
        if key.startswith("f-etag/"):
            real(self, key, data)
            return hashlib.md5(b"other").hexdigest()
        return real(self, key, data)

    with patched(FileBackend, "put", bad), patched(session_move, "RETRY_AFTER", 3600.0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = session_move.move_old(s)
        ck("the others of the batch moved", n == 4 and move_of(s, "f-ok")["state"] == "verified"
           and move_of(s, "f-ok2")["state"] == "verified", session_move.counts(s))
        for sid, why in (("f-put", "disk full"), ("f-etag", "did not check out")):
            m = move_of(s, sid)
            ck(f"{sid}: pending with the error and one attempt", m["state"] == "pending"
               and m["attempts"] == 1 and why in (m["error"] or ""), dict(m))
            ck(f"{sid}: its rows and row are as they were", held(s, sid) == before[sid])
            ck(f"{sid}: one log line", out.getvalue().count(f"session move of {sid} to the archive") == 1)
        ck("they wait for RETRY_AFTER", session_move.move_old(s) == 0
           and session_move.retrying(s, old=True) == 2 and session_move.retrying(s) == 0)
        with patched(session_move, "RETRY_AFTER", 0.0):
            for _ in range(session_move.MAX_ATTEMPTS - 1):
                with contextlib.redirect_stdout(io.StringIO()):
                    session_move.move_old(s)
        m = move_of(s, "f-put")
        ck("after the cap of tries it is failed", m["state"] == "failed"
           and m["attempts"] == session_move.MAX_ATTEMPTS and "gave up after" in m["error"], dict(m))
        ck("and still holds its lines", held(s, "f-put") == before["f-put"])

    # the thread waits and tries again
    seed(s, "f-once", 3)
    ready(s)
    with s.sessions_db.begin() as c:
        c.execute(insert(session_moves).values(session="f-once", state="pending", target="table"))
    boom = [1]

    def once(self, key, data):
        if boom and key.startswith("f-once/"):
            boom.pop()
            raise OSError("blip")
        return real(self, key, data)

    with patched(FileBackend, "put", once), patched(session_move, "RETRY_AFTER", 0.2), \
            contextlib.redirect_stdout(io.StringIO()):
        session_move.run(s, threading.Event(), pause=0, old_pause=0)
    m = move_of(s, "f-once")
    ck("the thread tried again until it verified", m["state"] == "verified" and m["attempts"] == 1,
       dict(m))

    print("== a count that does not add up fails the session for good ==")
    for sid in ("m-archived", "m-read"):
        seed(s, sid, 4)
        session_ingest.reconcile_sessions(s)
        session_ingest.record(s, [line(sid, 15 * DAY)])
        with s.sessions_db.begin() as c:
            c.execute(insert(session_moves).values(session=sid, state="pending", target="table"))
    before = {sid: held(s, sid) for sid in ("m-archived", "m-read")}
    real_count, real_chunks = session_move._count_archived, session_move._duck_chunks
    with patched(session_move, "_count_archived", lambda conn, sid: real_count(conn, sid) - 1):
        ok, err = session_move.move_old_session(s, "m-archived", FileBackend(ARCHIVE))
    with patched(session_move, "_duck_chunks", lambda *a: (envs[1:] for envs in real_chunks(*a))):
        ok2, err2 = session_move.move_old_session(s, "m-read", FileBackend(ARCHIVE))
    for sid, good, why in (("m-archived", ok, "4 of 4 DuckDB lines read, 1 extra, 4 lines archived of 5"),
                           ("m-read", ok2, "3 of 4 DuckDB lines read")):
        m = move_of(s, sid)
        ck(f"{sid}: failed with the counts", good is False and m["state"] == "failed"
           and why in (m["error"] or ""), dict(m))
        ck(f"{sid}: its rows, parts and row are as they were", held(s, sid) == before[sid])

    print("== an archived part that cannot be read ==")
    seed(s, "unread", 4)
    session_ingest.reconcile_sessions(s)
    session_ingest.record(s, [line("unread", 15 * DAY)])
    session_archive._archive_session(s.sessions_db, FileBackend(ARCHIVE), "unread", NOW)
    os.remove(os.path.join(ARCHIVE, "unread", "1.jsonl.gz"))
    ready(s)
    with s.sessions_db.begin() as c:
        c.execute(insert(session_moves).values(session="unread", state="pending", target="table"))
    before = held(s, "unread")
    ok, err = session_move.move_old_session(s, "unread", FileBackend(ARCHIVE))
    ck("pending with the error, its part row kept", ok is False and move_of(s, "unread")["state"]
       == "pending" and move_of(s, "unread")["attempts"] == 1 and held(s, "unread") == before, (ok, err))
    s.close()


# ── odd data ────────────────────────────────────────────────────────────────────
def odd():
    print("== odd session ids, sessions with no lines, a line with no time ==")
    s = open_store()
    ids = ["a/b", "..", ".hidden", "sp ace%2F?#", "ünï-日本", "..\\up"]
    for sid in ids:
        seed(s, sid, 3)
    seed(s, "notime", 3)
    seed(s, "norow", 3)
    ready(s)
    with s._lock:
        s.con.execute("UPDATE events SET event_time = NULL WHERE key_value = 'notime' AND rowid = "
                      "(SELECT max(rowid) FROM events WHERE key_value = 'notime')")
    with s.sessions_db.begin() as c:
        c.execute(sessions.delete().where(sessions.c.session == "norow"))
        # in the ledger with no line anywhere: one with no row, one with a row and no time
        c.execute(insert(session_moves), [{"session": x, "state": "pending", "target": "table"}
                                          for x in ("gone", "empty")])
        c.execute(insert(sessions).values(session="empty", source=SRC, line_count=0, tool_calls=0,
                                          coordination_calls=0, lines_complete=False,
                                          updated_at=NOW))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = session_move.move_old(s)
    ck("all are taken", n == 10, n)
    for sid in ids:
        check_moved(s, sid, duck_payloads(s, sid), repr(sid))
    inside = all(os.path.commonpath([ARCHIVE, os.path.abspath(os.path.join(ARCHIVE, p.key))]) == ARCHIVE
                 and p.key.count("/") == 1 and not p.key.startswith(".")
                 for sid in ids for p in parts_of(s, sid))
    ck("each id's files are in one folder of its own", inside
       and len({parts_of(s, sid)[0].key.split("/")[0] for sid in ids}) == len(ids))
    for sid in ("gone", "empty"):
        m = move_of(s, sid)
        ck(f"{sid}: no line, verified with 0 and no part", m["state"] == "verified"
           and m["lines_after"] == 0 and parts_of(s, sid) == [], dict(m))
    ck("the empty row says 0 lines, complete", head_of(s, "empty")["line_count"] == 0
       and head_of(s, "empty")["lines_complete"])
    # a session with no row: a line another process records after the check stays
    with s.sessions_db.begin() as c:
        c.execute(insert(session_moves).values(session="appears", state="pending", target="table"))
    real_state = session_move._state
    calls = []

    def state(conn, sid, lock=False):
        out = real_state(conn, sid, lock)
        calls.append(lock)
        if lock and sid == "appears":
            conn.execute(insert(session_lines).values(session=sid, seq=1, line_key="k", at=NOW,
                                                      type="user", labels="{}", render="{}",
                                                      payload="{}"))
        return out

    with patched(session_move, "_state", state):
        ok, err = session_move.move_old_session(s, "appears", FileBackend(ARCHIVE))
    ck("a session with no row: nothing is deleted", ok and calls == [False, True]
       and len(rows_of(s, "appears")) == 1, (ok, err, rows_of(s, "appears")))
    m = move_of(s, "norow")
    ck("lines but no row in sessions: not verified, with the error", m["state"] == "pending"
       and "no row in sessions" in (m["error"] or "") and parts_of(s, "norow") == [], dict(m))
    m = move_of(s, "notime")
    ck("a line with no time: not verified, with the error, the others moved", m["state"] == "pending"
       and m["error"] and parts_of(s, "notime") == [], dict(m))
    s.close()


def no_archive():
    print("== a cell with no archive keeps the older lines in DuckDB, and says so once ==")
    s = open_store()
    for i in range(3):
        seed(s, f"n{i}", 3)
    seed(s, "nr", 3, 3600)
    ready(s)
    session_ingest.record(s, [line("n0", 15 * DAY)])
    with s.sessions_db.begin() as c:       # one has failed a try: the thread must not wait for it
        c.execute(update(session_moves).where(session_moves.c.session == "n1")
                  .values(attempts=1, started_at=NOW - timedelta(days=1)))
    events = duck_total(s)
    before = {f"n{i}": held(s, f"n{i}") for i in range(3)}
    url = os.environ.pop("TARES_ARCHIVE_URL")
    real = session_archive.backend_of
    # a Postgres cell has no backend without the URL; a SQLite one defaults to its data directory,
    # so there the answer is given by hand
    none = real if POSTGRES else (lambda store: None)
    try:
        with patched(session_archive, "backend_of", none):
            out = io.StringIO()
            done = threading.Event()

            def go():
                with contextlib.redirect_stdout(out):
                    session_move.move_old(s)
                    session_move.run(s, stop, pause=0, old_pause=0)
                    session_move.move_old(s)
                done.set()

            stop = threading.Event()
            threading.Thread(target=go, daemon=True).start()
            finished = done.wait(15)
            stop.set()
            done.wait(15)
    finally:
        os.environ["TARES_ARCHIVE_URL"] = url
    ck("the thread ends", finished)
    ck("the recent session moved", move_of(s, "nr")["state"] == "verified")
    ck("the older ones stay pending", all(move_of(s, f"n{i}")["state"] == "pending" for i in range(3)))
    ck("and were not tried", all(move_of(s, x)["error"] is None and not move_of(s, x)["attempts"]
                                 for x in ("n0", "n2")) and move_of(s, "n1")["attempts"] == 1)
    ck("their lines stay where they were", {f"n{i}": held(s, f"n{i}") for i in range(3)} == before
       and duck_total(s) == events)
    ck("it is said once", out.getvalue().count("older sessions stay in DuckDB") == 1, out.getvalue())
    s._move_old_skip_said = False
    os.environ["TARES_ARCHIVE_URL"] = "ftp://nowhere"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            a, b = session_move.move_old(s), session_move.move_old(s)
    finally:
        os.environ["TARES_ARCHIVE_URL"] = url
    ck("an unusable URL is skipped and said once", (a, b) == (0, 0)
       and out.getvalue().count("the archive cannot be used") == 1, out.getvalue())
    ck("with an archive again they move", session_move.move_old(s) == 3
       and session_move.counts(s) == {"verified": 4}, session_move.counts(s))
    s.close()


def main():
    t0 = time.perf_counter()
    main_run()
    thread()
    resume()
    new_line()
    hourly_between()
    hourly_around()
    other_ingest()
    backoff()
    lock_timeouts()
    one_snapshot()
    sweep_files()
    sweep_and_writers()
    sweep_s3()
    crash()
    failures()
    odd()
    no_archive()
    print(f"({time.perf_counter() - t0:.1f}s)")
    if FAILS:
        print(f"\n{len(FAILS)} failed: {FAILS}")
        sys.exit(1)
    print("\nall ok")


if __name__ == "__main__":
    main()
