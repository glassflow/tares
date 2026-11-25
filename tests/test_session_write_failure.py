"""A session store write that fails is never lost in the factory edition (TR-1665).

The factory's pages read only the session store, so it is written first. While it takes no write,
`/ingest` answers 503 and nothing is stored; the same lines sent again give what a delivery with no
failure gives. A line the store will not take is acknowledged, its session is marked incomplete and
the M1 drain (`session_drain`) copies it from DuckDB later; one that can never be stored is given up
once. The platform editions behave as before.

Plain:                python tests/test_session_write_failure.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_write_failure.py
"""
import asyncio
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ  # noqa: E402
scrub_os_environ()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-write-failure-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-write-failure"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp(prefix="tares-write-failure-archive-")

import httpx  # noqa: E402
from sqlalchemy import delete, func, select, update  # noqa: E402

from tares import session_drain, session_ingest, session_move  # noqa: E402
from tares import store as store_mod  # noqa: E402
from tares.envelope import Envelope, now_utc  # noqa: E402
from tares.session_store.tables import (session_commands, session_files,  # noqa: E402
                                        session_ingest_marks, session_lines, session_messages,
                                        session_said, session_turns, sessions)
from tares.store import Store  # noqa: E402

SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {str(detail)[:900]}"))
    if not ok:
        FAILS.append(name)


@contextlib.contextmanager
def patched(obj, name, value):
    real = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield real
    finally:
        setattr(obj, name, real)


def said(fn):
    """(what `fn` returned, what it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        got = fn()
    return got, out.getvalue()


def raised(fn):
    """(the exception `fn` raised or None, what it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            fn()
        except Exception as e:
            return e, out.getvalue()
    return None, out.getvalue()


class Down:
    """A session store write that cannot open its transaction."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        raise RuntimeError("the sessions database is down")

    def __exit__(self, *a):
        return False


def down(s):
    return patched(s.sessions_db, "begin", Down)


class Spy:
    """The DuckDB connection, keeping every statement that reads `events`."""

    def __init__(self, con):
        self.con, self.seen = con, []

    def execute(self, sql, *args, **kw):
        if re.search(r"\bFROM\s+events\b", sql) and not sql.lstrip().upper().startswith("DELETE"):
            self.seen.append(sql)
        return self.con.execute(sql, *args, **kw)

    def __getattr__(self, name):
        return getattr(self.con, name)


@contextlib.contextmanager
def spying(s):
    spy = Spy(s.con)
    s.con = spy
    try:
        yield spy.seen
    finally:
        s.con = spy.con


def refuse(s):
    """Make the session store refuse every line whose payload holds REFUSE, and nothing else."""
    with s.sessions_db.begin() as c:
        if s.sessions_db.dialect == "postgresql":
            c.exec_driver_sql("ALTER TABLE session_lines ADD CONSTRAINT refuse_lines "
                              "CHECK (strpos(payload, 'REFUSE') = 0) NOT VALID")
        else:
            c.exec_driver_sql("CREATE TRIGGER refuse_lines BEFORE INSERT ON session_lines "
                              "WHEN instr(NEW.payload, 'REFUSE') > 0 "
                              "BEGIN SELECT RAISE(ABORT, 'refused by the test'); END")


def accept(s):
    with s.sessions_db.begin() as c:
        if s.sessions_db.dialect == "postgresql":
            c.exec_driver_sql("ALTER TABLE session_lines DROP CONSTRAINT refuse_lines")
        else:
            c.exec_driver_sql("DROP TRIGGER refuse_lines")


def open_store(edition="factory", moved=True):
    """A new store. `moved`: the one-time move is complete (planned with nothing to move)."""
    os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp()
    s = Store(os.path.join(tempfile.mkdtemp(dir=_TMP), "t.duckdb"))
    s.set_setting("edition", edition)
    if moved:
        session_move.plan(s)
    return s


def env(sid, tag, i, etype, text, content, payload=None, source=SRC):
    p = {"uuid": f"{tag}-u{i}", "type": "assistant" if etype != "user" else "user",
         "message": {"content": content}, **(payload or {})}
    return Envelope(source=source, source_type="agent_session", key_value=sid, event_type=etype,
                    text=text, event_time=NOW - timedelta(seconds=100 - i), payload=p,
                    labels={"tares_person": "ann"})


def work(sid, tag, mark=""):
    """A turn, a reply, a command and its result, a file edit and a protocol message. `mark` is
    put in the reply's payload (REFUSE makes `refuse` turn it down)."""
    use = lambda i, name, inp: [{"type": "tool_use", "id": f"{tag}-{i}", "name": name, "input": inp}]
    return [
        env(sid, tag, 1, "user", f"{tag}: build the footer", f"{tag}: build the footer"),
        env(sid, tag, 2, "assistant", f"{tag}: running the tests",
            [{"type": "text", "text": f"{tag}: running the tests"}], {"note": mark} if mark else None),
        env(sid, tag, 3, "tool_use", "→ Bash(python t.py)",
            use("bash", "Bash", {"command": "python t.py"})),
        env(sid, tag, 4, "tool_result", f"{tag}: 12 passed",
            [{"type": "tool_result", "tool_use_id": f"{tag}-bash", "content": f"{tag}: 12 passed"}]),
        env(sid, tag, 5, "tool_use", "→ Edit(footer.css)",
            use("edit", "Edit", {"file_path": f"/w/{tag}/footer.css"})),
        env(sid, tag, 6, "tool_use", "→ SendMessage(crew-reviewer)",
            use("send", "SendMessage", {"to": "crew-reviewer",
                                        "message": "[TF:REVIEW_REQUEST] review T3"})),
    ]


def row(s, sid):
    c = sessions.c
    with s.sessions_db.begin() as conn:
        got = conn.execute(select(c.line_count, c.tool_calls, c.coordination_calls,
                                  c.lines_complete).where(c.session == sid)).first()
    return None if got is None else tuple(got)


def set_row(s, sid, **values):
    with s.sessions_db.begin() as c:
        c.execute(update(sessions).where(sessions.c.session == sid).values(**values))


def rows_of(s, sid):
    with s.sessions_db.begin() as c:
        return c.execute(select(func.count()).where(session_lines.c.session == sid)).scalar()


def marks(s):
    with s.sessions_db.begin() as c:
        return sorted(n for (n,) in c.execute(select(session_ingest_marks.c.name)).all()
                      if n.startswith("session_drain"))


def duck(s, sid):
    with s._lock:
        return s.con.execute("SELECT count(*) FROM events WHERE key_value = ?", [sid]).fetchone()[0]


def drop_duck(s, sid=None):
    with s._lock:
        s.con.execute("DELETE FROM events WHERE source = ?" + (" AND key_value = ?" if sid else ""),
                      [SRC] + ([sid] if sid else []))


def challenges(s):
    return s.sc.execute("SELECT count(*) FROM challenges").fetchone()[0]


def facts(s, sid):
    """The facts and turns of a session, without the columns that say when they were written."""
    out = {}
    with s.sessions_db.begin() as c:
        for t in (session_commands, session_messages, session_turns, session_said, session_files):
            cols = [col for col in t.c if col.name not in ("updated_at", "written_at")]
            out[t.name] = sorted(str(tuple(r)) for r in
                                 c.execute(select(*cols).where(t.c.session == sid)).all())
    return out


def view(s, sid, tag):
    """What the factory holds of a session, with its id and tag written as X: the page's lines,
    the counts, the facts and the turns."""
    c = sessions.c
    with s.sessions_db.begin() as conn:
        head = conn.execute(select(c.source, c.line_count, c.tool_calls, c.coordination_calls,
                                   c.started_at, c.last_line_at, c.lines_origin,
                                   c.lines_complete).where(c.session == sid)).first()
    page, where = s.key_events_from(SRC, sid, limit=1000)
    got = {"row": None if head is None else str(tuple(head)), "where": where,
           "page": [str(sorted(r.items())) for r in page], **facts(s, sid)}
    word = rf"(?<![A-Za-z_])({re.escape(sid)}|{re.escape(tag)})(?![A-Za-z_])"
    return json.loads(re.sub(word, "X", json.dumps(got)))


def differs(a, b):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}


def baseline():
    s = open_store()
    s.append(work("base", "tb"))
    got = view(s, "base", "tb")
    s.close()
    return got


def unavailable(want):
    print("== the store takes no write: refused, nothing stored, sent again ==")
    ck("the baseline holds 6 lines, a command, a message, a turn, said and a file",
       len(want["page"]) == 6 and len(want["session_commands"]) == 1
       and len(want["session_messages"]) == 1 and len(want["session_turns"]) == 1
       and len(want["session_said"]) == 1 and len(want["session_files"]) == 1, want)
    s = open_store()
    commit = env("s", "ts", 7, "challenge_commit", "challenger: PASS", [])
    commit.labels["branch"] = "b1"
    batch = work("s", "ts") + [commit]
    with down(s):
        e, _ = raised(lambda: s.append(batch))
    ck("append raises LinesNotStored", isinstance(e, session_ingest.LinesNotStored), repr(e))
    ck("no line, count or fact is stored, in either database",
       row(s, "s") is None and rows_of(s, "s") == 0 and duck(s, "s") == 0
       and not any(facts(s, "s").values()), (row(s, "s"), rows_of(s, "s"), duck(s, "s")))
    ck("the challenger's verdict is kept all the same", challenges(s) == 1, challenges(s))
    s.append(work("s", "ts"))
    ck("sent again, the factory holds what a delivery with no failure holds",
       not differs(view(s, "s", "ts"), want), differs(view(s, "s", "ts"), want))
    ck("DuckDB has each line once", duck(s, "s") == 6, duck(s, "s"))
    s.append([commit])
    ck("the challenger's line sent again is not counted twice", challenges(s) == 1, challenges(s))
    ck("nothing is left to drain", session_drain.drain(s) == 0 and marks(s) == [], marks(s))
    with patched(session_ingest, "_write_whole", lambda *a: (_ for _ in ()).throw(AssertionError())):
        e, _ = raised(lambda: s.append([]))
    ck("an empty batch writes nothing", e is None, repr(e))

    print("== the store goes down part way through a batch's second try ==")
    s = open_store()
    s.append(work("a", "ta")[:2])
    before = (row(s, "a"), rows_of(s, "a"))
    real_whole, real_probe = session_ingest._write_whole, session_ingest._probe
    probes = []

    def whole(store, envelopes, written):
        if any(x.key_value == "b" for x in envelopes):
            raise RuntimeError("passing error")
        real_whole(store, envelopes, written)

    def probe(store):
        probes.append(1)
        if len(probes) > 1:
            raise session_ingest.LinesNotStored("gone")
        real_probe(store)

    with patched(session_ingest, "_write_whole", whole), patched(session_ingest, "_probe", probe):
        e, _ = raised(lambda: s.append(work("a", "ta")[2:] + work("b", "tb2")))
    ck("the batch is refused", isinstance(e, session_ingest.LinesNotStored), repr(e))
    ck("what the second try had written is taken back",
       (row(s, "a"), rows_of(s, "a")) == before and row(s, "b") is None and duck(s, "a") == 2,
       ((row(s, "a"), rows_of(s, "a")), before, row(s, "b")))

    print("== DuckDB fails after the store was written ==")
    s = open_store()
    s.append(work("s", "ts")[:2])
    before = (row(s, "s"), rows_of(s, "s"))

    def broken(*a, **k):
        raise RuntimeError("DuckDB would not write")

    with patched(s, "_append", broken):
        e, _ = raised(lambda: s.append(work("s", "ts")[2:]))
    ck("the error reaches the caller", "DuckDB" in str(e), repr(e))
    ck("the store's write is taken back", (row(s, "s"), rows_of(s, "s")) == before,
       (row(s, "s"), rows_of(s, "s"), before))
    with patched(s, "_append", broken):
        e, _ = raised(lambda: s.append(work("new", "tn")))
    ck("a session the batch made is removed again", row(s, "new") is None and rows_of(s, "new") == 0,
       row(s, "new"))
    s.append(work("s", "ts")[2:])
    ck("sent again, it equals a delivery with no failure", not differs(view(s, "s", "ts"), want),
       differs(view(s, "s", "ts"), want))
    s.close()


def refused(want):
    print("== the store refuses a line: acknowledged, marked, copied by the drain ==")
    s = open_store()
    refuse(s)
    e, out = raised(lambda: s.append(work("s", "ts", mark="REFUSE") + work("ok", "tk")))
    ck("the batch is acknowledged", e is None, repr(e))
    ck("the daemon says which session was refused",
       "refused 6 claude_code line(s) of session s" in out, out)
    ok = view(s, "ok", "tk")
    ck("the other session of the batch is whole", not differs(ok, want), differs(ok, want))
    ck("the refused session is counted and marked incomplete",
       row(s, "s") == (6, 3, 1, False) and rows_of(s, "s") == 0, (row(s, "s"), rows_of(s, "s")))
    ck("DuckDB keeps its lines", duck(s, "s") == 6, duck(s, "s"))
    e, _ = raised(lambda: s.append([env("s", "ts", 7, "assistant", "one more", [])]))
    ck("a later line is not held up", e is None and row(s, "s")[0] == 7 and rows_of(s, "s") == 1,
       (repr(e), row(s, "s"), rows_of(s, "s")))
    accept(s)
    page = s.key_events_from(SRC, "s", limit=100)[0]
    ck("until the drain, the page shows only what the table holds", len(page) == 1, len(page))
    n, out = said(lambda: session_drain.drain(s))
    ck("the drain takes the one marked session and says so",
       n == 1 and "M1 drain: session s rebuilt" in out and "7 line(s) stored" in out, (n, out))
    ck("counts and the page are whole again",
       row(s, "s") == (7, 3, 1, True) and rows_of(s, "s") == 7, (row(s, "s"), rows_of(s, "s")))
    # the same lines with no refusal, in a store of their own
    other = open_store()
    other.append(work("s", "ts", mark="REFUSE"))
    other.append([env("s", "ts", 7, "assistant", "one more", [])])
    a, b = view(s, "s", "ts"), view(other, "s", "ts")
    ck("lines, counts, facts and turns equal a run with no refusal", not differs(a, b),
       differs(a, b))
    other.close()
    with spying(s) as read:
        n, out = said(lambda: session_drain.drain(s))
    ck("a second drain takes nothing, reads nothing and changes nothing",
       n == 0 and not read and not out and view(s, "s", "ts") == a, (n, read, out))

    print("== a line that can never be stored ==")
    s = open_store()
    refuse(s)
    raised(lambda: s.append(work("s", "ts", mark="REFUSE")))
    n, out = said(lambda: session_drain.drain(s))
    ck("the drain stores the lines it can and gives the other up",
       n == 1 and rows_of(s, "s") == 5 and "5 line(s) stored, 1 cannot be stored" in out,
       (n, rows_of(s, "s"), out))
    ck("the session stays incomplete, its counts kept", row(s, "s") == (6, 3, 1, False), row(s, "s"))
    ck("with the gave-up mark", marks(s) == [session_ingest.DRAIN_GAVE_UP + "s"], marks(s))
    got = facts(s, "s")
    ck("its facts and turns are there", all(len(v) == 1 for v in got.values()), got)
    with spying(s) as read:
        again, out = said(lambda: session_drain.drain(s))
    ck("it is not tried again: no read, no log", again == 0 and not read and not out,
       (again, read, out))
    s.append([env("s", "ts", 7, "assistant", "one more", [])])
    ck("a good line after it is stored, with no new try",
       rows_of(s, "s") == 6 and session_drain.drain(s) == 0, rows_of(s, "s"))
    raised(lambda: s.append([env("s", "ts", 8, "assistant", "REFUSE again", [],
                                 {"note": "REFUSE"})]))
    n, out = said(lambda: session_drain.drain(s))
    ck("a new refusal is tried once more", n == 1 and rows_of(s, "s") == 6 and row(s, "s")[0] == 8,
       (n, rows_of(s, "s"), row(s, "s"), out))
    ck("and given up again", session_drain.drain(s) == 0)

    print("== a counted line neither database holds ==")
    s = open_store()
    s.append(work("s", "ts"))
    drop_duck(s, "s")
    with s.sessions_db.begin() as c:
        c.execute(delete(session_lines).where(session_lines.c.session == "s",
                                              session_lines.c.seq == 3))
    set_row(s, "s", lines_complete=False)
    n, out = said(lambda: session_drain.drain(s))
    ck("it is not called complete, and the count is not lowered",
       n == 1 and row(s, "s") == (6, 3, 1, False) and rows_of(s, "s") == 5, (n, row(s, "s"), out))
    ck("and is not tried again", session_drain.drain(s) == 0)


def gates(want):
    print("== when the drain does not run ==")
    s = open_store()
    ck("an empty store has nothing to drain", session_drain.drain(s) == 0)
    s.append(work("s", "ts"))
    drop_duck(s)
    with spying(s) as read:
        n = session_drain.drain(s)
    ck("with the DuckDB lines gone and nothing marked, it reads nothing", n == 0 and not read,
       (n, read))

    s = open_store(moved=False)
    s.append(work("s", "ts"))
    set_row(s, "s", lines_complete=False)
    with spying(s) as read:
        n = session_drain.drain(s)
    ck("before the move is complete it takes nothing", n == 0 and not read, (n, read))

    s = open_store()
    s.append(work("s", "ts"))
    set_row(s, "s", lines_complete=False, lines_origin=None)
    ck("a session not born in the table is left to DuckDB", session_drain.drain(s) == 0)

    print("== a try that fails as a whole ==")
    s = open_store()
    refuse(s)
    raised(lambda: s.append(work("s", "ts", mark="REFUSE")))
    accept(s)

    def gone(*a):
        raise RuntimeError("the sessions database went away")

    with patched(session_move, "_existing", gone):
        n, out = said(lambda: session_drain.drain(s))
        n2, out2 = said(lambda: session_drain.drain(s))
    ck("the session stays marked", row(s, "s")[3] is False and n == 1 and n2 == 1, (row(s, "s"), n, n2))
    ck("and it is said once", out.count("M1 drain of session s failed") == 1 and not out2,
       (out, out2))
    said(lambda: session_drain.drain(s))
    ck("the next pass repairs it", row(s, "s")[3] is True and rows_of(s, "s") == 6, row(s, "s"))

    print("== a pass takes at most its limit ==")
    s = open_store()
    refuse(s)
    for i in range(3):
        raised(lambda: s.append(work(f"s{i}", f"t{i}", mark="REFUSE")))
    accept(s)
    n, _ = said(lambda: session_drain.drain(s, limit=2))
    m, _ = said(lambda: session_drain.drain(s, limit=2))
    ck("two, then the third", (n, m) == (2, 1), (n, m))

    print("== the background job runs it ==")
    s = open_store()
    refuse(s)
    raised(lambda: s.append(work("s", "ts", mark="REFUSE")))
    accept(s)
    said(lambda: session_ingest._drain_pass(s))
    ck("one pass of the job repairs the session", row(s, "s") == (6, 3, 1, True), row(s, "s"))

    print("== the move could not fill a session's facts and turns ==")
    s = open_store()
    s.append(work("s", "ts"))
    with s.sessions_db.begin() as c:
        for t in (session_commands, session_messages, session_turns, session_said, session_files):
            c.execute(delete(t))

    def no_fill(*a):
        raise RuntimeError("the fill broke")

    with patched(session_ingest, "_fill_chunk", no_fill):
        _, out = said(lambda: session_move._fill(s, [SRC], ["s"]))
    ck("the move says they are marked for the M1 drain",
       "not filled (RuntimeError: the fill broke); marked, so the M1 drain fills them" in out, out)
    ck("the session has the todo mark", marks(s) == [session_ingest.DRAIN_TODO + "s"], marks(s))
    said(lambda: session_drain.drain(s))
    got = view(s, "s", "ts")
    ck("the drain fills them", not differs(got, want) and marks(s) == [], (differs(got, want), marks(s)))

    print("== a purge forgets the marks ==")
    s = open_store()
    refuse(s)
    raised(lambda: s.append(work("s", "ts", mark="REFUSE")))
    said(lambda: session_drain.drain(s))
    session_ingest.mark_drain_todo(s, ["s"])
    assert len(marks(s)) == 2, marks(s)
    s.purge_events(SRC)
    ck("no drain mark is left", marks(s) == [], marks(s))

    print("== held-back lines (TR-1647): a refused session's lines go to DuckDB ==")
    with patched(store_mod, "HOLD_LINES_BACK", True), \
            patched(session_ingest, "caught_up", lambda st: True):
        s = open_store()
        s.append(work("h", "th"))
        ck("an accepted line is held back", duck(s, "h") == 0 and rows_of(s, "h") == 6,
           (duck(s, "h"), rows_of(s, "h")))
        refuse(s)
        raised(lambda: s.append([env("h", "th", 7, "assistant", "no", [], {"note": "REFUSE"}),
                                 env("g", "tg", 1, "assistant", "yes", [])]))
        ck("the refused session's line is written to DuckDB", duck(s, "h") == 1, duck(s, "h"))
        ck("the accepted one of the same batch is held back", duck(s, "g") == 0, duck(s, "g"))
        s.append([env("h", "th", 8, "assistant", "after", [])])
        ck("and its next lines too, while it is marked", duck(s, "h") == 2, duck(s, "h"))
        accept(s)
        said(lambda: session_drain.drain(s))
        ck("the drain keeps the held lines and adds the DuckDB ones",
           row(s, "h") == (8, 3, 1, True) and rows_of(s, "h") == 8, (row(s, "h"), rows_of(s, "h")))
        with down(s):
            e, _ = raised(lambda: s.append([env("h", "th", 9, "assistant", "down", [])]))
        ck("with the store down nothing is written anywhere",
           isinstance(e, session_ingest.LinesNotStored) and duck(s, "h") == 2, (repr(e), duck(s, "h")))


def platform():
    print("== the platform edition is as before ==")
    s = open_store("platform")
    with down(s):
        e, out = raised(lambda: s.append(work("s", "ts")))
    ck("a store that is down does not refuse the batch", e is None and duck(s, "s") == 6,
       (repr(e), duck(s, "s")))
    s = open_store("platform")
    refuse(s)
    e, out = raised(lambda: s.append(work("s", "ts", mark="REFUSE")))
    ck("a refused line is counted and marked, in one go", e is None and row(s, "s") == (6, 3, 1, False)
       and "caught up at the next start" in out, (repr(e), row(s, "s"), out))
    ck("and the drain leaves it alone", session_drain.drain(s) == 0 and marks(s) == [])


def tail():
    print("== the newest readable lines (F2) ==")
    for what, days in (("table", 0), ("archive", 20)):
        s = open_store(moved=False)
        lines = work("s", "ts")
        for x in lines:
            x.event_time -= timedelta(days=days)
        s.append(lines)
        session_move.plan(s)
        if days:
            said(lambda: session_move.move_old(s))
        set_row(s, "s", line_count=12)      # six lines counted that cannot be read
        got, _ = said(lambda: s.key_events_from(SRC, "s", limit=3, offset=9, tail={}))
        start = {}
        said(lambda: s.key_payload_rows_from(SRC, "s", limit=3, offset=9, tail=start))
        ck(f"{what}: the three newest lines that can be read",
           got[1] == what and [r["event_type"] for r in got[0]] == ["tool_result", "tool_use", "tool_use"],
           got)
        ck(f"{what}: and says where the page starts", start == {"start": 3}, start)
        plain, _ = said(lambda: s.key_events_from(SRC, "s", limit=3, offset=9))
        ck(f"{what}: a page that is not the tail keeps its offset", plain[0] == [], plain)
        s.close()


def wire(sid, etype, ago, content, **more):
    t = datetime.now(timezone.utc) - timedelta(minutes=ago)
    return {"type": etype, "sessionId": sid, "cwd": "/w/crew",
            "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "uuid": f"{sid}-{ago}", "message": {"role": etype, "content": content}, **more}


async def route():
    print("== through /ingest and the station page ==")
    from tares.daemon import make_app
    with patched(session_move, "MOVE_ENABLED", False):      # the test plans the move itself
        app = make_app()
    mk = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                   headers={"Authorization": f"Bearer {TOKEN}"})
    store = app.state.store
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": SRC, "connector": SRC, "poll": "10s",
                                                    "config": {"push": True}})
            assert r.status_code < 300, r.text
            for _ in range(200):
                if session_ingest.caught_up(store):
                    break
                await asyncio.sleep(0.05)
            session_move.plan(store)
            body = [wire("w", "user", 9, "build the footer"),
                    wire("w", "assistant", 8, [{"type": "text", "text": "running the tests"}]),
                    wire("w", "assistant", 7, [{"type": "text", "text": "done"}])]
            station = {"type": "session_station", "sessionId": "w", "cwd": "/w/crew",
                       "timestamp": body[0]["timestamp"], "station": "shop-build-m1",
                       "role": "builder", "parent": "crew-orchestrator",
                       "factory_station": "shop-build-m1"}
            r = await cx.post(f"/ingest/{SRC}", json=[station])
            assert r.status_code == 202, r.text
            held = (row(store, "w"), duck(store, "w"))
            with down(store):
                r = await cx.post(f"/ingest/{SRC}", json=body)
            ck("while the store takes no write, /ingest answers 503 and says to send again",
               r.status_code == 503 and "send the lines again" in r.text, (r.status_code, r.text))
            ck("and keeps nothing of the batch", (row(store, "w"), duck(store, "w")) == held,
               (row(store, "w"), duck(store, "w"), held))
            r = await cx.post(f"/ingest/{SRC}", json=body)
            ck("sent again, it is taken", r.status_code == 202, r.text)
            n = held[0][0] + 3
            ck("each line once", row(store, "w")[0] == n and rows_of(store, "w") == n
               and duck(store, "w") == n, (row(store, "w"), rows_of(store, "w"), duck(store, "w")))
            set_row(store, "w", line_count=n + 5)
            d = (await cx.get("/api/crew/shop-build-m1",
                              params={"recent": 2, "conversation": 2})).json()
            ck("the station page shows its newest readable lines, not an empty window",
               [x["text"] for x in d.get("recent") or []][-1:] == ["done"]
               and len(d["recent"]) == 2, d.get("recent"))
            ck("and its conversation, from where that page starts",
               any(i.get("text") == "done" for i in d.get("conversation") or [])
               and d.get("conversation_from") == n - 2,
               (d.get("conversation"), d.get("conversation_from"), n))


def main():
    want = baseline()
    unavailable(want)
    refused(want)
    gates(want)
    platform()
    tail()
    os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp()
    asyncio.run(route())
    print(f"\n{len(FAILS)} failed: {FAILS}" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
