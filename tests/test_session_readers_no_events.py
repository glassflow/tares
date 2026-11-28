"""The factory edition reads no transcript line from DuckDB once the one-time move is complete (TR-1613).

A factory cell is seeded the way one looks before the move (lines in DuckDB's events only), the move
runs, the pages are read, the claude_code rows are deleted from DuckDB's events (in this test only:
nothing in the product deletes them) and the pages are read again: Home, Crew, a station and one of
its tool calls, the project, its sessions and tickets, a ticket brief, two conversations and a
source's field profile answer the same. Then, store by store: a damaged session born in the table is
not read from DuckDB on a factory cell and still is on a platform one; the passes that caught the
tables up from DuckDB stop; the usage and challenger fills read the events once; and
`recent_payloads` reads `session_lines`, leaving out what a hidden project owns.

The lines are written to both stores here, as they were before the write gate opened (TR-1647);
tests/test_session_readers_held.py covers lines that were never in DuckDB.

Plain:                python tests/test_session_readers_no_events.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_readers_no_events.py
"""
import asyncio
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-no-events-")
ARCHIVE = tempfile.mkdtemp(prefix="tares-no-events-archive-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-no-events"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_ARCHIVE_URL"] = "file://" + ARCHIVE

import httpx  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

import tares.mcp_factory as mf  # noqa: E402
import tares.mcp_server as m  # noqa: E402
from tares import (claude_usage, claude_usage_store, factory_home, session_ingest,  # noqa: E402
                   session_move, session_read)
from tares.envelope import Envelope, now_utc  # noqa: E402
from tares.factory_store import FactoryStore  # noqa: E402
from tares.session_store.tables import challenges as challenges_table  # noqa: E402
from tares.session_store.tables import (session_archive_parts, session_ingest_marks,  # noqa: E402
                                        session_lines, session_moves, session_usage, sessions)
from tares.store import Store  # noqa: E402

SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {str(detail)[:600]}"))
    if not ok:
        FAILS.append(name)


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
def spying(store):
    spy = Spy(store.con)
    store.con = spy
    try:
        yield spy.seen
    finally:
        store.con = spy.con


@contextlib.contextmanager
def patched(obj, name, value):
    real = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, real)


def forget_lines(s):
    """Leave the store as it was before the session store held lines: DuckDB has them all, no row
    in `session_lines`, no session born in the table."""
    with s.sessions_db.begin() as c:
        c.execute(delete(session_lines))
        c.execute(update(sessions).values(lines_origin=None, lines_complete=False))


def drop_events(s):
    with s._lock:
        s.con.execute("DELETE FROM events WHERE source = ?", [SRC])


def events_left(s):
    with s._lock:
        return s.con.execute("SELECT count(*) FROM events WHERE source = ?", [SRC]).fetchone()[0]


def move(s):
    session_move.run(s, threading.Event(), pause=0, old_pause=0)


# ── 1. every factory page, through the app ───────────────────────────────────────────────────
def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def wire(sid, etype, ago, content, project="", **msg):
    o = {"type": etype, "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "uuid": uuid.uuid4().hex, "message": {"role": etype, "content": content, **msg}}
    if project:
        o["tares_project"] = project
    return o


def station_line(sid, station, role, ago, parent="", project=""):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def project_line(sid, project, ago):
    return {"type": "session_project", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "tares_project": project}


def state_line(sid, state, ago):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "state": state, "reason": ""}


def work(sid, project, ago, tag):
    """What a builder's session holds: a typed turn, a reply, a command and its output, a file
    edit and a protocol message."""
    use = lambda i, name, inp: {"type": "tool_use", "id": f"{tag}-{i}", "name": name, "input": inp}
    usage = {"input_tokens": 2, "output_tokens": 10, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 50000}
    return [
        wire(sid, "user", ago, f"{tag}: build the footer", project),
        wire(sid, "assistant", ago - 1, [{"type": "text", "text": f"{tag}: running the tests"}],
             project, id=f"{tag}-msg", model="claude-opus-5-5", usage=usage),
        wire(sid, "assistant", ago - 2, [use("bash", "Bash", {"command": "python tests/test_x.py"})],
             project),
        wire(sid, "user", ago - 3, [{"type": "tool_result", "tool_use_id": f"{tag}-bash",
                                     "content": f"{tag}: 12 passed"}], project),
        wire(sid, "assistant", ago - 4, [use("edit", "Edit", {"file_path": "/w/crew/footer.css"})],
             project),
        wire(sid, "assistant", ago - 5, [use("send", "SendMessage",
                                             {"to": "crew-reviewer", "message": "review T3"})],
             project),
        wire(sid, "assistant", ago - 6, [{"type": "text", "text": f"{tag}: the footer is done"}],
             project),
    ]


def without_now(x):
    if isinstance(x, dict):
        return {k: without_now(v) for k, v in x.items() if k != "now"}
    if isinstance(x, list):
        return [without_now(v) for v in x]
    return x


async def pages():
    from tares.daemon import make_app
    print("== every factory page, before and after the claude_code rows leave DuckDB ==")
    with patched(session_move, "MOVE_ENABLED", False):      # the test runs the move itself
        app = make_app()
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t",
                                              headers={"Authorization": f"Bearer {TOKEN}"})
    m._cx = mf._cx = mk
    store = app.state.store

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            async def get(path):
                factory_home.clear_cache()
                claude_usage.clear_week_by_project()
                r = await cx.get(path)
                assert r.status_code == 200, (path, r.status_code, r.text)
                return without_now(r.json())

            async def ingest(lines):
                r = await cx.post("/ingest/claude_code",
                                  content="\n".join(json.dumps(x) for x in lines) + "\n",
                                  headers={"Content-Type": "application/x-ndjson"})
                assert r.status_code < 300, r.text

            px = json.loads(await m.create_project("project-x", "Ship the docs site"))["id"]
            await mf.write_milestone("M1: first pages", project="project-x")
            for t in ("Home page", "Footer"):
                await m.write_ticket(t, milestone="M1: first pages", project="project-x",
                                     repo="example-org/docs-site")
            await cx.post(f"/api/projects/{px}/tickets/T2/assign",
                          json={"holder": "project-x-build-m1"})
            r = await cx.post(f"/api/projects/{px}/tickets/T2/stage", json={"stage": "doing"},
                              headers={"X-Tares-Station": "project-x-build-m1",
                                       "X-Tares-Role": "builder"})
            assert r.status_code == 200, r.text
            r = await cx.post(f"/api/projects/{px}/handover",
                              json={"repos": [{"path": "/w/docs-site",
                                               "name": "example-org/docs-site"}]})
            assert r.status_code == 200, r.text

            old = 12 * 24 * 60       # twelve days ago: outside the archive window
            await ingest([station_line("old1", "project-x-build-m1", "builder", old,
                                       "crew-orchestrator", px),
                          project_line("old1", px, old - 0.5)] + work("old1", px, old - 1, "old"))
            await ingest([station_line("o1", "crew-orchestrator", "orchestrator", 40.5),
                          state_line("o1", "idle", 1.5),
                          station_line("r1", "crew-reviewer", "reviewer", 40.5),
                          state_line("r1", "idle", 3.5),
                          station_line("b1", "project-x-build-m1", "builder", 40.5,
                                       "crew-orchestrator", px),
                          state_line("b1", "working", 2.5),
                          project_line("b1", px, 31)]
                         + work("b1", px, 30.5, "now")
                         + [wire("o1", "user", 20.5, "start the build", px),
                            wire("r1", "assistant", 19.5, [{"type": "text", "text": "reviewing"}])])

            forget_lines(store)
            before_move = await get(f"/api/projects/{px}/sessions/b1")
            ck("before the move a conversation comes from DuckDB",
               before_move["lines_from"] == "events" and len(before_move["lines"]) >= 7, before_move)
            ck("the move is not complete before it ran", not session_move.complete(store))
            move(store)
            with store.sessions_db.begin() as c:
                states = {r.session: (r.state, r.target) for r in c.execute(select(session_moves))}
            ck("the move verified every session, the old one into the archive",
               set(states) >= {"old1", "b1", "o1", "r1"}
               and {v[0] for v in states.values()} == {"verified"}
               and states["old1"][1] == "archive" and states["b1"][1] == "table", states)
            ck("then the move is complete and the sessions table has caught up",
               session_move.complete(store) and session_ingest.caught_up(store))

            urls = {
                "Home": "/api/home",
                "Crew": "/api/crew",
                "a station": "/api/crew/project-x-build-m1?recent=50&conversation=100",
                "a tool call": "/api/crew/project-x-build-m1/tool/now-bash",
                "the projects": "/api/factory/projects",
                "a project": f"/api/factory/projects/{px}",
                "its sessions": f"/api/projects/{px}/sessions",
                "its tickets": f"/api/projects/{px}/tickets",
                "a ticket": f"/api/factory/projects/{px}/tickets/T2",
                "a ticket brief": f"/api/projects/{px}/tickets/T2/brief",
                "a builder brief": f"/api/projects/{px}/tickets/T2/builder-brief",
                "a conversation": f"/api/projects/{px}/sessions/b1",
                "an archived conversation": f"/api/projects/{px}/sessions/old1",
            }
            scans = []

            def spy_on(cls, name):
                real = getattr(cls, name)

                def wrapped(*a, **kw):
                    scans.append(name)
                    return real(*a, **kw)
                return patched(cls, name, wrapped)

            with spy_on(Store, "_session_activity_scan"), spy_on(FactoryStore, "_rollup_from_rows"), \
                    spy_on(FactoryStore, "_coordination_calls_scan"):
                before = {k: await get(v) for k, v in urls.items()}
                kept = events_left(store)
                drop_events(store)
                ck("the claude_code rows left DuckDB", kept > 15 and events_left(store) == 0, kept)
                with spying(store) as read:
                    after = {k: await get(v) for k, v in urls.items()}
                    n = (session_ingest.reconcile_sessions(store), session_ingest.fill_facts(store),
                         session_ingest.fill_turns(store))
            ck("no activity scan of the events ran", not scans, scans)
            ck("the three passes looked at nothing", n == (0, 0, 0), n)
            ck("no statement of those pages read claude_code lines from events",
               not [q for q in read if "key_value" in q], read)

            st = before["a station"]
            ck("the station has its lines, a conversation and a source of them other than events",
               st["session"] == "b1" and len(st["recent"]) >= 7 and len(st["conversation"]) >= 4
               and st["lines_from"] == "table" and "the footer is done" in json.dumps(st), st)
            ck("the tool call has its output", "12 passed" in before["a tool call"]["output"],
               before["a tool call"])
            ck("the conversations have lines, one from the table, one from the archive",
               before["a conversation"]["lines_from"] == "table"
               and before["an archived conversation"]["lines_from"] == "archive"
               and "old: the footer is done" in json.dumps(before["an archived conversation"])
               and len(before["a conversation"]["lines"]) >= 7, before["an archived conversation"])
            ck("Home and Crew show the three stations",
               len(before["Home"]["crew"]["members"]) == 3
               and "project-x-build-m1" in json.dumps(before["Crew"]), before["Home"]["crew"])
            ck("the project, its sessions and the ticket are not empty",
               "project-x" in json.dumps(before["a project"]) and "b1" in json.dumps(
                   before["its sessions"]) and "old1" in json.dumps(before["its sessions"])
               and "Footer" in json.dumps(before["a ticket brief"])
               and "Footer" in json.dumps(before["a builder brief"]), before["its sessions"])
            for k in urls:
                ck(f"{k} answers the same without the rows", after[k] == before[k],
                   (before[k], after[k]))


# ── the stores below ─────────────────────────────────────────────────────────────────────────
def open_store(edition="factory"):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    s.set_setting("edition", edition)
    return s


def env(session, i, etype="assistant", days=0, block=None, labels=None, payload=None, source=SRC):
    p = {"uuid": f"u-{session}-{i}", "type": etype,
         "message": {"content": [block or {"type": "text", "text": f"{session} line {i}"}]},
         **(payload or {})}
    return Envelope(source=source, source_type="agent_session", key_value=session, event_type=etype,
                    text=f"{session} line {i}", event_time=NOW - timedelta(days=days, seconds=100 - i),
                    payload=p, labels={"tares_person": "ann", **(labels or {})})


def result_block(call):
    return {"type": "tool_result", "tool_use_id": call, "content": f"output of {call}"}


def set_row(s, session, **values):
    with s.sessions_db.begin() as c:
        c.execute(update(sessions).where(sessions.c.session == session).values(**values))


def said(fn):
    """(what `fn` returned, what it printed), with the once-a-minute limiter forgotten first."""
    session_read._WARNED.clear()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        got = fn()
    return got, out.getvalue()


def damaged(s, session, source=SRC):
    """(lines, where, tool block, its where, what was printed, statements that read events)."""
    with spying(s) as read:
        got, out = said(lambda: (s.key_events_from(source, session, limit=100),
                                 s.key_tool_result_from(source, session, "call-1")))
    (rows, where), (block, tool_where) = got
    return rows, where, block, tool_where, out, read


def reading():
    print("== a damaged session born in the table (step 1) ==")
    for edition in ("factory", "platform"):
        s = open_store(edition)
        factory = edition == "factory"

        def seed(session, days=0):
            s.append([env(session, 1, "tool_result", days, result_block("call-1")),
                      env(session, 2, days=days), env(session, 3, days=days)])

        # not every line was written: one row is missing
        seed("short")
        with s.sessions_db.begin() as c:
            c.execute(delete(session_lines).where(session_lines.c.session == "short",
                                                  session_lines.c.seq == 3))
        set_row(s, "short", lines_complete=False)
        rows, where, block, tool_where, out, read = damaged(s, "short")
        if factory:
            ck("factory: an incomplete session answers with the rows it has, not from DuckDB",
               where == "table" and len(rows) == 2 and tool_where == "table" and block
               and not read, (where, len(rows), read))
            ck("factory: and says which lines it shows, once", "may not be all of them" in out
               and "read from DuckDB" not in out and out.count("\n") == 1, out)
        else:
            ck("platform: it is read from DuckDB as before",
               where == "events" and len(rows) == 3 and tool_where == "events" and block and read,
               (where, len(rows)))

        # marked complete, a row gone: the counts do not add up
        seed("count")
        with s.sessions_db.begin() as c:
            c.execute(delete(session_lines).where(session_lines.c.session == "count",
                                                  session_lines.c.seq == 2))
        rows, where, _, _, out, read = damaged(s, "count")
        ck(f"{edition}: a count that does not add up answers from "
           + ("the table" if factory else "DuckDB"),
           (where, len(rows), bool(read)) == (("table", 2, False) if factory else ("events", 3, True)),
           (where, len(rows), out))
        ck(f"{edition}: with the warning that is true for it",
           ("read from DuckDB" in out) is not factory and out != "", out)

        # a row that does not parse
        seed("bad")
        with s.sessions_db.begin() as c:
            c.execute(update(session_lines).where(session_lines.c.session == "bad",
                                                  session_lines.c.seq == 2).values(payload="nope"))
        rows, where, _, _, out, read = damaged(s, "bad")
        ck(f"{edition}: a row that does not parse is " + ("left out" if factory else "read from DuckDB"),
           (where, len(rows), bool(read)) == (("table", 2, False) if factory else ("events", 3, True))
           and ("do not parse" in out or not factory), (where, len(rows), out))

        # another source than the session's own (a name no session line is stored under, TR-1811)
        seed("mine")
        rows, where, block, tool_where, out, read = damaged(s, "mine", source="other_cc")
        ck(f"{edition}: another source than the session's " + ("has no lines" if factory
                                                             else "is asked of DuckDB"),
           (rows, where, block, bool(read)) == ([], "table", None, False) if factory
           else (where, tool_where, bool(read)) == ("events", "events", True), (rows, where, read))

        # a session from before `lines_origin` still reads DuckDB in both editions
        seed("older")
        set_row(s, "older", lines_origin=None, lines_complete=False)
        rows, where, _, _, out, read = damaged(s, "older")
        ck(f"{edition}: a session not born in the table is read from DuckDB",
           where == "events" and len(rows) == 3 and read, (where, len(rows)))

        # a part that cannot be read
        seed("parted", days=20)
        session_move.plan(s)
        assert session_move.move_old(s) == 1
        with s.sessions_db.begin() as c:
            part = c.execute(select(session_archive_parts).where(
                session_archive_parts.c.session == "parted")).one()
        ck(f"{edition}: the old session is in the archive",
           s.key_events_from(SRC, "parted")[1] == "archive")
        found = [os.path.join(d, f) for d, _, fs in os.walk(ARCHIVE) for f in fs
                 if os.path.join(d, f).endswith(part.key)]
        assert len(found) == 1, (part.key, found)
        os.remove(found[0])
        rows, where, block, _, out, read = damaged(s, "parted")
        ck(f"{edition}: a part that cannot be read " + ("leaves its lines out" if factory
                                                       else "sends the read to DuckDB"),
           (where, rows, block, bool(read)) == ("archive", [], None, False) if factory
           else (where, len(rows), bool(read)) == ("events", 3, True), (where, len(rows), out))

        # a whole session costs no look at the edition
        seed("whole")
        with patched(session_read, "_factory", lambda st: (_ for _ in ()).throw(AssertionError())):
            rows, where = s.key_events_from(SRC, "whole")
        ck(f"{edition}: a session read in full does not look at the edition",
           where == "table" and len(rows) == 3, (where, len(rows)))

        # the session store fails after the row said the session was born in the table
        def broken(r):
            raise RuntimeError("the session store is gone")
        with patched(session_read, "_row", broken):
            try:
                (_, where), _out = said(lambda: s.key_events_from(SRC, "whole"))
                got = where
            except RuntimeError:
                got = "raised"
        ck(f"{edition}: a session store that fails part way " + ("raises" if factory
                                                               else "falls back to DuckDB"),
           got == ("raised" if factory else "events"), got)
        s.close()


def passes():
    print("== the passes that read DuckDB stop (steps 2 and 3) ==")
    for edition in ("factory", "platform"):
        s = open_store(edition)
        s.append([env("p1", 1, "user"), env("p1", 2), env("p1", 3, "tool_use")])
        forget_lines(s)

        def run():
            with spying(s) as read:
                session_ingest.reconcile_sessions(s)
                a = len(read)
                session_ingest.fill_facts(s)
                b = len(read) - a
                session_ingest.fill_turns(s)
                return a, b, len(read) - a - b

        with s.sessions_db.begin() as c:
            c.execute(delete(session_ingest_marks))
        s._sessions_caught_up = False
        ck(f"{edition}: before the plan the three passes read DuckDB", all(run()))
        session_move.plan(s)
        ck(f"{edition}: with a pending session the move is not complete", not session_move.complete(s))
        ck(f"{edition}: and they still read DuckDB", all(run()))
        assert session_move.move_recent(s) == 1
        ck(f"{edition}: no pending session: complete", session_move.complete(s))
        with s.sessions_db.begin() as c:
            c.execute(delete(session_ingest_marks).where(session_ingest_marks.c.name.not_in(
                [session_move.PLANNED, session_ingest.FACTS_FILLED, session_ingest.TURNS_FILLED])))
        s._sessions_caught_up = False
        got = run()
        ck(f"{edition}: after the move they " + ("read nothing from DuckDB" if edition == "factory"
                                                 else "read DuckDB as before"),
           got == (0, 0, 0) if edition == "factory" else all(got), got)
        ck(f"{edition}: and the sessions table counts as caught up", session_ingest.caught_up(s))
        s.close()

    # the move can finish before the start-up fills ran, and its own fill of a session can fail
    s = open_store()
    s.append([env("g1", 1, "user"), env("g1", 2), env("g1", 3, "tool_use")])
    forget_lines(s)
    with s.sessions_db.begin() as c:
        c.execute(delete(session_ingest_marks))
    s._sessions_caught_up = False
    session_ingest.reconcile_sessions(s)
    session_move.plan(s)
    assert session_move.move_recent(s) == 1 and session_move.complete(s)

    def fill_one(fn):
        with spying(s) as read:
            fn(s)
        return len(read)

    for what, fn, chunk in (("facts", session_ingest.fill_facts, "_fill_chunk"),
                            ("turns", session_ingest.fill_turns, "_fill_turns_chunk")):
        def broken(*a, **kw):
            raise RuntimeError("no")
        with patched(session_ingest, chunk, broken):
            try:
                fn(s)
                raised = False
            except RuntimeError:
                raised = True
        ck(f"factory, complete: the {what} fill that never finished still reads", raised)
        got = fill_one(fn), fill_one(fn)
        ck(f"factory, complete: the {what} fill reads again after a failure, then no more",
           got[0] > 0 and got[1] == 0, got)
    s.close()

    s = open_store()
    s.append([env("f1", 1)])
    session_move.plan(s)
    with s.sessions_db.begin() as c:
        c.execute(update(session_moves).values(state=session_move.FAILED))
    ck("a failed session does not keep the move open", session_move.complete(s))
    s.close()


def fills():
    print("== the usage and challenger fills read the events once (step 4) ==")
    usage = {"input_tokens": 5, "output_tokens": 7}
    for edition in ("factory", "platform"):
        s = open_store(edition)
        with s.sessions_db.begin() as c:
            c.execute(delete(session_ingest_marks).where(session_ingest_marks.c.name.in_(
                [session_ingest.USAGE_FILLED, session_ingest.CHALLENGES_FILLED])))
        s.append([env("u1", 1, payload={"message": {"id": "msg-1", "model": "claude-opus-5-5",
                                                    "usage": usage, "content": []}}),
                  env("u1", 2, "challenge_commit", labels={"branch": "b1"},
                      payload={"challenge": {"verdict": "pass"}})])

        def marks():
            with s.sessions_db.begin() as c:
                return {r[0] for r in c.execute(select(session_ingest_marks.c.name))}

        def counts():
            with s.sessions_db.begin() as c:
                replies = len(c.execute(select(session_usage.c.message)).all())
            with s.sessions_db.read() as c:
                kept = len(c.execute(select(challenges_table.c.branch)).all())
            return replies, kept

        def empty():
            with s.sessions_db.begin() as c:
                c.execute(delete(session_usage))
                c.execute(delete(challenges_table))

        def fill():
            with spying(s) as read:
                s._claude_usage_fill()
                s._challenges_fill()
            return len(read)

        # a fill that fails leaves no mark
        def broken(*a, **kw):
            raise RuntimeError("no")
        empty()
        raised = []
        for obj, name, run in ((Store, "_record_challenges", s._challenges_fill),
                               (claude_usage_store.hourly, "after_write", s._claude_usage_fill)):
            with patched(obj, name, broken):
                try:
                    run()
                except RuntimeError:
                    raised.append(name)
        ck(f"{edition}: a fill that failed leaves no mark",
           len(raised) == 2 and not marks() & {session_ingest.CHALLENGES_FILLED,
                                     session_ingest.USAGE_FILLED}, (raised, marks()))
        empty()
        reads = fill()
        ck(f"{edition}: the first fill reads the events and writes both rows",
           reads == 2 and counts() == (1, 1), (reads, counts()))
        want = {session_ingest.USAGE_FILLED, session_ingest.CHALLENGES_FILLED}
        ck(f"{edition}: the factory records the two fills, a platform cell does not",
           marks() & want == (want if edition == "factory" else set()), marks())
        empty()
        reads = fill()
        ck(f"{edition}: the next fill " + ("reads no event" if edition == "factory"
                                           else "reads them again"),
           (reads, counts()) == ((0, (0, 0)) if edition == "factory" else (2, (1, 1))),
           (reads, counts()))
        # what the usage fill does besides reading the events runs every time
        with s.sessions_db.begin() as c:
            c.execute(delete(session_usage))
            c.execute(session_usage.insert(), [
                {"session": "u1", "message": "synthetic", "replied_at": NOW, "model": "<synthetic>",
                 "input_tokens": 1, "output_tokens": 1},
                {"session": "u1", "message": "unpriced", "replied_at": NOW,
                 "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 1000}])
        s._claude_usage_fill()
        with s.sessions_db.begin() as c:
            left = {r.message: r.cost_usd for r in c.execute(select(session_usage))}
        ck(f"{edition}: synthetic rows still go and unpriced replies are still priced",
           "synthetic" not in left and left.get("unpriced"), left)
        if edition == "factory":
            s.set_setting("edition", "platform")
            empty()
            reads = fill()
            ck("switched to platform: the fills read the events again despite the marks",
               (reads, counts()) == (2, (1, 1)), (reads, counts()))
        s.close()


def payloads():
    print("== recent_payloads reads session_lines (step 5) ==")
    hidden = {"ids": ["uc_hidden"], "names": ["Secret"], "shared": []}

    def build(edition):
        s = open_store(edition)
        s.link_session("uc_hidden", "linked")
        lines = [env("a", 1), env("a", 2, labels={"tares_project": "secret"}),
                 env("a", 3, labels={"tares_project": "uc_open"}),
                 env("a", 4, payload={"project": "uc_hidden"}), env("linked", 5), env("b", 6)]
        lines += [env(f"many{i:02d}", 10 + i) for i in range(25)]      # more than one page
        s.append(lines)
        return s

    def names(s, limit=100, exclude=None, source=SRC):
        with spying(s) as read:
            got = s.recent_payloads(source, limit, exclude=exclude)
        return [p["uuid"] for p in got], len(read)

    f, p = build("factory"), build("platform")
    for label, kw in (("all of them", {}), ("the newest three", {"limit": 3}),
                      ("without a hidden project's", {"exclude": hidden}),
                      ("with the payload test of a shared source",
                       {"exclude": {**hidden, "shared": [SRC]}}),
                      ("with no list of shared sources",
                       {"exclude": {"ids": ["uc_hidden"], "names": ["Secret"]}})):
        got, read = names(f, **kw)
        want, _ = names(p, **kw)
        ck(f"factory, {label}: what DuckDB gives, read from the table",
           got == want and got and read == 0, (got, want, read))
    kept = names(f, exclude=hidden)[0]
    ck("the hidden label and the linked session are left out, the payload's project is not",
       "u-a-2" not in kept and "u-linked-5" not in kept and "u-a-4" in kept and "u-a-3" in kept, kept)
    ck("a platform cell reads DuckDB", names(p)[1] == 1)
    f.append([env("w1", 1, source="web")])
    ck("another connector's source reads DuckDB", names(f, source="web") == (["u-w1-1"], 1),
       names(f, source="web"))
    with f.sessions_db.begin() as c:
        c.execute(delete(session_lines))
    got, read = names(f)
    ck("an empty table before the move is complete: DuckDB", len(got) == 31 and read == 1,
       (len(got), read))
    with f.sessions_db.begin() as c:
        c.execute(session_ingest_marks.insert().values(name=session_move.PLANNED, at=NOW))
    got, read = names(f)
    ck("an empty table once the move is complete: nothing, and no read of DuckDB",
       got == [] and read == 0, (got, read))
    f.close()
    p.close()


async def main():
    with patched(Store, "_lines_skip_duckdb", lambda self: False):     # DuckDB gets every line
        await pages()
        reading()
        passes()
        fills()
        payloads()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
