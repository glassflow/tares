"""The session readers use the events scan until the sessions table has caught up once (TR-1520).

`Store.session_activity`, `Store.project_sessions`, `FactoryStore.station_sessions` and
`FactoryStore.coordination_calls` read the `sessions` table (TR-1499). On a cell that was upgraded, the
table is empty or short until the first `reconcile_sessions` finishes, so until a `sessions_caught_up` row
is in `session_ingest_marks` they scan events, as they did before. The row is set by a complete reconcile
(not by one that raised) and at once by Store init on a cell whose events hold no claude_code line.

Plain:                python tests/test_sessions_caught_up.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_sessions_caught_up.py
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import delete, select, update  # noqa: E402

from tares import session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store.tables import session_ingest_marks, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
SEEN = T0 - timedelta(hours=1)
IDS = ["sA", "sB", "sC", "sD", "ghost"]


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, sec, kind="assistant", tool=None, source="claude_code"):
    """One line of a Claude Code session; a tool_use line renders `→ name(...)` and carries the tool in
    payload.message.content, as the connector writes it."""
    payload, text = {}, f"line at {sec}"
    if tool:
        kind, text = "tool_use", f"→ {tool}(x=1)"
        payload = {"message": {"content": [{"type": "tool_use", "name": tool}]}}
    return Envelope(source=source, source_type="agent_session", key_value=session, event_type=kind,
                    text=text, event_time=T0 + timedelta(seconds=sec), payload=payload)


# Batch 1 and batch 2 of the sessions' lines. By hand:
#   sA: 7 lines, first T0, last T0+6s, 4 tool calls (Bash, SendMessage, Skill, mcp__other__thing),
#       2 coordination calls (SendMessage and Skill)
#   sB: 2 lines, T0+10s and T0+11s, 2 tool calls, 1 coordination call (a Tares mcp tool)
BATCH1 = [line("sA", 0), line("sA", 1), line("sA", 2), line("sA", 3, tool="Bash")]
BATCH2 = [line("sA", 4, tool="SendMessage"), line("sA", 5, tool="Skill"),
          line("sA", 6, tool="mcp__other__thing"),
          line("sB", 10, tool="Read"), line("sB", 11, tool="mcp__plugin_tares_tares__get_ticket")]
ACT = {"sA": (T0, T0 + timedelta(seconds=6), 7), "sB": (T0 + timedelta(seconds=10),
                                                        T0 + timedelta(seconds=11), 2)}
COORD = {"sA": 2, "sB": 1}
# project p1: sB (newest), sA, and sD that never wrote a line (last)
PS = [("sB", ACT["sB"][0], ACT["sB"][1], 2), ("sA", ACT["sA"][0], ACT["sA"][1], 7), ("sD", None, None, 0)]
# stations: sA, sB, and sC that was never seen in events (its lines are 0 and its times are seen_at)
SS = {"sA": ACT["sA"], "sB": ACT["sB"], "sC": (SEEN, SEEN, 0)}


def new_dir():
    return tempfile.mkdtemp()


def open_store(d):
    return Store(os.path.join(d, "t.duckdb"))


def link(s):
    s.set_session_station("sA", "builder-1", "builder", "", "p1", SEEN)
    s.set_session_station("sB", "builder-2", "builder", "", "p1", SEEN)
    s.set_session_station("sC", "builder-3", "builder", "", None, SEEN)
    for sid in ("sA", "sB", "sD"):
        s.link_session("p1", sid)


def views(s):
    act = s.session_activity("claude_code", IDS)
    coord = s.coordination_calls("claude_code", IDS)
    ps = [(r["session"], r["started_at"], r["last_at"], r["lines"]) for r in s.project_sessions("p1")]
    ss = {r["session"]: (r["started_at"], r["last_at"], r["lines"]) for r in s.station_sessions("claude_code")}
    return act, coord, ps, ss


def wipe(s, marks=True, table=True):
    with s.sessions_db.begin() as c:
        if table:
            c.execute(delete(sessions))
        if marks:
            c.execute(delete(session_ingest_marks))


def has_row(s):
    with s.sessions_db.begin() as c:
        return c.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == session_ingest.CAUGHT_UP)).first() is not None


def table_counts(s):
    with s.sessions_db.begin() as c:
        return {r.session: (r.line_count, r.coordination_calls) for r in c.execute(select(sessions)).all()}


def failing_add(*a, **k):
    raise RuntimeError("down")


def upgraded_cell(fail_from=0):
    """A cell as an upgrade leaves it: its lines are in DuckDB, `record` failed for the batches from
    `fail_from` on (0: all of them, so the table is empty; 1: the second batch, so the table is short),
    and the cell has no caught-up row. Returns the data dir; the cell is closed."""
    d = new_dir()
    s = open_store(d)
    link(s)
    real = session_ingest._add
    out = io.StringIO()
    try:
        for i, batch in enumerate((BATCH1, BATCH2)):
            session_ingest._add = failing_add if i >= fail_from else real
            with contextlib.redirect_stdout(out):
                s.append(batch)
    finally:
        session_ingest._add = real
    wipe(s, marks=True, table=False)
    s.close()
    return d


def scan_views(s):
    return (s._session_activity_scan("claude_code", IDS), s._coordination_calls_scan("claude_code", IDS))


def same_as_expected(v, label):
    act, coord, ps, ss = v
    ck(f"{label}: session_activity has each session's first line, last line and line count", act == ACT, act)
    ck(f"{label}: coordination_calls counts the crew-only tool calls", coord == COORD, coord)
    ck(f"{label}: project_sessions lines, times and order", ps == PS, ps)
    ck(f"{label}: station_sessions lines and times, and a session never seen in events", ss == SS, ss)


def upgrade_reads_the_scan():
    print("== an upgraded cell reads the events scan, not the undercounted table ==")
    for fail_from, what in ((0, "an empty table"), (1, "a short table")):
        d = upgraded_cell(fail_from)
        s = open_store(d)
        t = table_counts(s)
        if fail_from == 0:
            ck("the table is empty (record failed for every batch)", t == {}, t)
        else:
            ck("the table is short: sA has 4 of its 7 lines, sB none", t == {"sA": (4, 0)}, t)
        ck("a cell with claude_code lines and no row stays on the scan after init",
           not has_row(s) and not session_ingest.caught_up(s))
        ck("a False answer is not kept", not getattr(s, "_sessions_caught_up", False))
        v = views(s)
        same_as_expected(v, what)
        sa, sc = scan_views(s)
        ck(f"{what}: the readers equal the scan functions", v[0] == sa and v[1] == sc, (v[0], sa, v[1], sc))
        ck(f"{what}: and not the table (sB has lines here and no row there)",
           v[0].get("sB") == ACT["sB"] and "sB" not in t and v[0]["sA"][2] > t.get("sA", (0,))[0], (v[0], t))
        ck("reads do not set the row", not has_row(s))
        s.close()


def reconcile_switches_to_the_table():
    print("== after a complete reconcile the readers read the table ==")
    d = upgraded_cell(0)
    s = open_store(d)
    n = session_ingest.reconcile_sessions(s)
    ck("the reconcile looked at two sessions", n == 2, n)
    ck("and set the caught-up row", has_row(s) and session_ingest.caught_up(s))
    ck("the answer is kept on the store", getattr(s, "_sessions_caught_up", False) is True)
    same_as_expected(views(s), "table, after the reconcile")
    # a difference only the table has: the readers must show it
    with s.sessions_db.begin() as c:
        c.execute(update(sessions).where(sessions.c.session == "sA").values(
            line_count=99, coordination_calls=7, last_line_at=T0 + timedelta(hours=2)))
    act, coord, ps, ss = views(s)
    ck("session_activity shows the table's count", act["sA"][2] == 99 and act["sA"][1] == T0 + timedelta(hours=2),
       act["sA"])
    ck("coordination_calls shows the table's count", coord["sA"] == 7, coord)
    ck("project_sessions shows the table's lines, and sorts by its last line",
       ps[0][0] == "sA" and ps[0][3] == 99, ps)
    ck("station_sessions shows the table's lines", ss["sA"][2] == 99, ss["sA"])
    session_ingest.reconcile_sessions(s)
    ck("a second pass leaves the row set", has_row(s))
    s.close()
    # the next start of the same cell is caught up from the start
    s2 = open_store(d)
    ck("a second Store on the same data dir finds the row before any read", has_row(s2))
    ck("and is caught up", session_ingest.caught_up(s2))
    with s2.sessions_db.begin() as c:
        c.execute(update(sessions).where(sessions.c.session == "sB").values(line_count=55))
    ck("and reads the table", s2.session_activity("claude_code", ["sB"])["sB"][2] == 55)
    s2.close()


def failed_reconcile_keeps_the_scan():
    print("== a reconcile that raises sets no row, and the next one does ==")
    d = upgraded_cell(0)
    s = open_store(d)
    real = session_ingest._reconcile_chunk
    calls = []

    def second_chunk_raises(store, sources, sids):
        calls.append(list(sids))
        if len(calls) == 2:
            raise RuntimeError("chunk two failed")
        return real(store, sources, sids)

    session_ingest._reconcile_chunk = second_chunk_raises
    try:
        err = None
        try:
            session_ingest.reconcile_sessions(s, chunk=1)
        except RuntimeError as e:
            err = e
        ck("the reconcile raised on the second chunk", err is not None and len(calls) == 2, (err, calls))
        ck("no caught-up row is set (nor the watermark)", not has_row(s) and session_ingest._mark(s) is None)
        t = table_counts(s)
        ck("the first chunk was written and the second was not", list(t) == ["sA"], t)
        ck("so the readers are still on the scan", not session_ingest.caught_up(s))
        same_as_expected(views(s), "after the failed pass")
        # the thread a start runs swallows the error and prints one line
        calls.clear()
        buf = io.StringIO()
        session_ingest._reconcile_chunk = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
        with contextlib.redirect_stdout(buf):
            s.start_session_reconcile().join(30)
        out = buf.getvalue().strip().splitlines()
        ck("start_session_reconcile prints one line about the failure",
           len(out) == 3 and "catch-up failed (RuntimeError: down)" in out[0], out)
        # the facts fill and the turns fill run after it and do not depend on it
        ck("and still sets no row", not has_row(s))
    finally:
        session_ingest._reconcile_chunk = real
    # the next start: no failure
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s.start_session_reconcile().join(30)
    ck("the next start's catch-up sets the row", has_row(s) and session_ingest.caught_up(s))
    ck("and says what it caught up", "caught up for 2 session(s)" in buf.getvalue(), buf.getvalue())
    same_as_expected(views(s), "after the retry")
    with s.sessions_db.begin() as c:
        c.execute(update(sessions).where(sessions.c.session == "sB").values(coordination_calls=9))
    ck("and the readers now read the table", s.coordination_calls("claude_code", ["sB"]) == {"sB": 9})
    s.close()


def new_cell_is_caught_up():
    print("== a new cell with no events is caught up at once ==")
    d = new_dir()
    s = open_store(d)
    ck("the row is set right after Store init", has_row(s))
    ck("caught_up answers True", session_ingest.caught_up(s))
    link(s)
    s.append(BATCH1 + BATCH2)
    same_as_expected(views(s), "new cell, lines through append")
    with s.sessions_db.begin() as c:           # a difference only the table has (no events behind it)
        c.execute(sessions.insert().values(session="only-table", source="claude_code", started_at=T0,
                                           last_line_at=T0, line_count=5, tool_calls=0,
                                           coordination_calls=3, updated_at=T0))
    s.link_session("p1", "only-table")
    ck("session_activity reads it", s.session_activity("claude_code", ["only-table"]) == {"only-table": (T0, T0, 5)})
    ck("coordination_calls reads it", s.coordination_calls("claude_code", ["only-table"]) == {"only-table": 3})
    ck("project_sessions reads it", [r["lines"] for r in s.project_sessions("p1") if r["session"] == "only-table"] == [5])
    ck("and the scan would not have it", s._session_activity_scan("claude_code", ["only-table"]) == {})
    s.close()
    print("== a cell whose events hold no claude_code line is caught up too ==")
    d = new_dir()
    s = open_store(d)
    s.append([Envelope("deploys", "event_stream", "api", "deploy", "deployed", T0)])
    s.close()
    s = open_store(d)
    ck("events of another source do not stop the shortcut", has_row(s))
    s.close()
    print("== an empty cell where the reconcile finds no session sets the row ==")
    d = new_dir()
    s = open_store(d)
    wipe(s)
    ck("(the row is gone)", not has_row(s))
    n = session_ingest.reconcile_sessions(s)
    ck("the reconcile found nothing and set the row", n == 0 and has_row(s), n)
    s.close()
    d = new_dir()
    s = open_store(d)
    s.append([line("", 0)])                       # a claude_code line with no session: no row, no record
    wipe(s)
    s.close()
    s = open_store(d)
    ck("claude_code events and no row: not caught up after init", not has_row(s))
    ck("their reads scan", not session_ingest.caught_up(s) and s.session_activity("claude_code", ["x"]) == {})
    n = session_ingest.reconcile_sessions(s)
    ck("a reconcile that finds 0 sessions sets the row", n == 0 and has_row(s), n)
    s.close()


def empty_and_missing_values():
    print("== no sessions, no lines, sessions never seen ==")
    for label, d in (("scan", upgraded_cell(0)), ("table", None)):
        if d is None:
            d = upgraded_cell(0)
            s = open_store(d)
            session_ingest.reconcile_sessions(s)
        else:
            s = open_store(d)
        ck(f"{label}: no sessions asked, nothing back",
           s.session_activity("claude_code", []) == {} and s.coordination_calls("claude_code", []) == {})
        ck(f"{label}: a project with no sessions lists none", s.project_sessions("nope") == [])
        ck(f"{label}: a session with no line is a row with no times and 0 lines",
           [r for r in s.project_sessions("p1") if r["session"] == "sD"][0]["lines"] == 0
           and [r for r in s.project_sessions("p1") if r["session"] == "sD"][0]["started_at"] is None)
        ck(f"{label}: an unknown session id is left out",
           "ghost" not in s.session_activity("claude_code", IDS) and "ghost" not in s.coordination_calls("claude_code", IDS))
        ck(f"{label}: a station session with no events has its seen_at for times and 0 lines",
           [r for r in s.station_sessions("claude_code") if r["session"] == "sC"][0]["lines"] == 0)
        ck(f"{label}: another source name finds no lines",
           all(r["lines"] == 0 for r in s.station_sessions("nosuch")))
        ck(f"{label}: sessions with no coordination call are left out", "sD" not in s.coordination_calls("claude_code", IDS))
        s.close()
    s = open_store(new_dir())
    ck("a cell with no station and no project lists none", s.station_sessions("claude_code") == []
       and s.project_sessions("p1") == [])
    s.close()


def main():
    for fn in (upgrade_reads_the_scan, reconcile_switches_to_the_table, failed_reconcile_keeps_the_scan,
               new_cell_is_caught_up, empty_and_missing_values):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nOK")


if __name__ == "__main__":
    main()
