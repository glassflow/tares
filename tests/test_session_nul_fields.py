"""A NUL byte in any text field of a claude_code line (TR-1707; the session id: TR-1703).

Postgres text holds no NUL. A posted line loses every NUL, dict keys included, before anything
counts or stores it, so SQLite and Postgres keep the same text. DuckDB may hold lines from before:
the fills, the one-time move, the drain and the first start with a state database drop their NULs
on the way into the sessions tables and the state tables. Both engines are checked against the same
expected rows.

Plain:                python tests/test_session_nul_fields.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_nul_fields.py
The first start with a state database needs TARES_TEST_PG and is skipped without it.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import delete, event, select  # noqa: E402

from tares import session_drain, session_ingest, session_move  # noqa: E402
from tares import session_map as claude_code  # noqa: E402
from tares.session_map import ClaudeCodeLines  # noqa: E402
from tares import session_map  # noqa: E402
from tares.session_store.tables import (challenges, crews, metadata,  # noqa: E402
                                        project_sessions, session_archive_parts,
                                        session_commands, session_files, session_ingest_marks,
                                        session_lines, session_messages, session_meters,
                                        session_moves, session_said, session_states,
                                        session_stations, session_turns, session_usage, sessions)
from tares.store import Store  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _old_cell import append_duckdb_first, forget_fills  # noqa: E402

SRC = "claude_code"
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
Z = "\x00"
PG = os.environ.get("TARES_TEST_PG")
PROJECT = "uc_nul"
P = F = 0


def ck(name, ok, detail=""):
    global P, F
    P += 1 if ok else 0
    F += 0 if ok else 1
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))


def attempt(name, fn):
    """Run `fn` and check that it raised nothing; returns what it returned."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            got = fn()
    except Exception as e:
        ck(name, False, f"{type(e).__name__}: {(str(e).splitlines() or [''])[0]}")
        return None
    ck(name, True)
    return got


def new_store(**kw):
    os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp()
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"), **kw)
    s.create_project(PROJECT, "custom", "proj", {})
    return s


def connector(store):
    return ClaudeCodeLines(store, source=SRC, source_type="agent_session")


def when(sec):
    return (T0 + timedelta(seconds=sec)).isoformat()


def line(typ, sid, sec, **more):
    return {"type": typ, "sessionId": sid, "timestamp": when(sec), **more}


def said(content):
    return {"role": "assistant", "content": content}


def reply(sid, sec, mid, content=(), **more):
    return line("assistant", sid, sec, uuid=f"r{sec}", message={
        "id": mid, "role": "assistant", "model": f"claude-so{Z}nnet-4-5",
        "usage": {"input_tokens": 3, "output_tokens": 5}, "content": list(content)}, **more)


def call(block_id, name, inp):
    return {"type": "tool_use", "id": block_id, "name": name, "input": inp}


def good_lines():
    return [line("user", "good", 40, uuid="g40", message={"role": "user", "content": "turn 40"}),
            line("assistant", "good", 41, uuid="g41", message={
                "id": "m41", "role": "assistant", "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 3, "output_tokens": 5},
                "content": [{"type": "text", "text": "words 41"}]})]


def deep(levels):
    v = f"bot{Z}tom"
    for i in range(levels):
        v = [v] if i % 2 else {f"k{Z}": v}
    return v


def rows(s, *cols):
    with s.sessions_db.begin() as c:
        return sorted((tuple(r) for r in c.execute(select(*cols)).all()), key=repr)


def want(name, got, expected):
    ck(name, got == sorted(expected, key=repr), got)


def duck(s, sql, args=()):
    with s._lock:
        return s.con.execute(sql, list(args)).fetchall()


def commands(s):
    """(command, output) of every command row. A block with no id gets a stand-in made from the
    line's time as written, which is UTC at ingest and the machine's zone when a fill reads DuckDB,
    so off UTC a fill adds that row again (so for any block with no id): counted once here."""
    c = session_commands.c
    got = rows(s, c.tool_use_id, c.command, c.output_tail)
    named = [r[1:] for r in got if not r[0].startswith(("s:", "old:"))]
    stand_ins = {r[1:] for r in got if r[0].startswith(("s:", "old:"))}
    return sorted(named + list(stand_ins), key=repr)


def no_nul_in_sessions_db(s):
    """No stored value, in any table of the sessions database, holds a NUL, written out or escaped
    inside JSON text."""
    bad = []
    for table in metadata.sorted_tables:
        with s.sessions_db.begin() as c:
            for r in c.execute(select(table)).all():
                for col, v in zip(table.columns, r):
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v)
                    if isinstance(v, str) and (Z in v or "\\u0000" in v):
                        bad.append((table.name, col.name, v[:60]))
    ck("no table of the sessions database holds a NUL, escaped ones included", not bad, bad[:5])


# ── a posted batch ───────────────────────────────────────────────────────────
def posted_batch():
    """One NUL in every text field of a line that reaches a sessions table, between good lines."""
    return [
        good_lines()[0],
        line("user", "s", 1, uuid=f"u{Z}1", gitBranch=f"br{Z}anch", cwd=f"/home/re{Z}po",
             flow=f"fl{Z}ow", message={"role": "user", "content": f"tu{Z}rn one"}),
        # NULs in dict keys
        {f"ty{Z}pe": "user", f"session{Z}Id": "s", "uuid": "u2", "timestamp": when(2),
         f"mess{Z}age": {"role": "user", f"cont{Z}ent": "key turn"}},
        # a secret with a NUL inside it is still redacted
        line("user", "s", 3, uuid="u3",
             message={"role": "user", "content": f"key sk-ant-ab{Z}cdefghij0123"}),
        line("user", "s", 4, uuid=f"{Z}{Z}", message={"role": "user", "content": "no uuid"}),
        reply("s", 5, f"ms{Z}g1", [{"type": "text", "text": f"wo{Z}rds"}],
              tares_project=f"pr{Z}oj"),
        reply("s", 6, f"msg{Z}2", isSidechain=True, agentId=f"ag{Z}ent"),
        reply("s", 7, "msg3", subagent=f"su{Z}b"),
        reply("s", 8, Z),                                   # an id of only a NUL is no id
        line(f"assis{Z}tant", "s", 9, uuid="a9", message=said([
            call(f"tu{Z}bash", "Bash", {"command": f"ls{Z} -la"}),
            call(f"tu{Z}msg", "SendMessage",
                 {f"t{Z}o": f"rev{Z}iewer", "message": f"[TF:RE{Z}PORT] hel{Z}lo"}),
            call(f"tu{Z}edit", "Edit", {"file_path": f"/a/fi{Z}le.py"}),
            call("tu_nb", "NotebookEdit", {"notebook_path": f"/a/n{Z}b.ipynb"}),
            call(Z, "Bash", {"command": "echo no id"}),
            call("tu_deep", "Bash", {"command": "echo deep", "x": deep(300)}),
            call("tu_name", f"Ba{Z}sh", {"command": "named"})])),
        line("user", "s", 10, uuid="u10", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{Z}bash",
             "content": [{"type": "text", "text": f"out{Z}put"}]},
            {"type": f"tool_res{Z}ult", "tool_use_id": Z, "content": "orphan"}]}),
        line("user", "s", 11, uuid=f"n{Z}m"),                           # no message
        line("user", "s", 12, uuid="ns", message=f"ju{Z}st a string"),  # message not an object
        line("session_state", "s", 13, state=f"id{Z}le", reason=f"wai{Z}ting for you"),
        line("session_state", "s2", 14, state="idle", reason=Z),
        line("session_station", "s", 15, station=f"proj-bui{Z}ld", role=f"build{Z}er",
             parent=f"orche{Z}strator", tares_project=f"pr{Z}oj", factory_crew=f"cr{Z}ew",
             factory_machine=f"mac{Z}hine"),
        line("session_station", "s2", 16, station=Z),
        line("session_project", "s", 17, tares_project=f"pr{Z}oj", cwd=f"/x/re{Z}po2"),
        line("session_project", "s2", 18, tares_project=f"no{Z}pe", from_env=True),
        line("session_project", "s2", 19, tares_project=Z),
        line("session_status", "s", 20,
             status={"model": {"id": f"m{Z}", "display_name": f"Cl{Z}aude"},
                     "session_name": f"na{Z}me"}),
        line("session_context", "s", 21,
             context_usage={"model": f"cl{Z}aude", "categories": [{"name": f"Sk{Z}ills",
                                                                  "tokens": 1}]}),
        {"type": "claude_plan", "timestamp": when(22),
         "usage_report": {f"fi{Z}ve": f"ho{Z}ur"}},
        line(f"challenge_com{Z}mit", "s", 23, gitBranch=f"factory/TR-{Z}1",
             tares_project=f"pr{Z}oj", challenge={
                 "verdict": f"PA{Z}SS", "sha": f"ab{Z}c123", "finding_count": 1,
                 "blocking_count": 0, "findings": [{"priority": "P1", "title": f"ti{Z}tle"}]}),
        good_lines()[1],
    ]


def check_posted_rows(s, say):
    t = session_turns.c
    want(f"{say}the turns", rows(s, t.session, t.text),
         [("good", "turn 40"), ("s", "turn one"), ("s", "key turn"), ("s", "key [REDACTED]"),
          ("s", "no uuid"), ("s", "user"), ("s", "user")])
    keys = [r[0] for r in rows(s, t.line_key)]
    ck(f"{say}the turns are keyed by their uuid without NULs",
       {"u1", "u2", "u3", "nm", "ns", "g40"} <= set(keys), keys)
    want(f"{say}the said lines", rows(s, session_said.c.session, session_said.c.text),
         [("good", "words 41"), ("s", "words")])
    t = session_commands.c
    ids = {r[1]: r[0] for r in rows(s, t.tool_use_id, t.command)}
    want(f"{say}the commands and the output", commands(s),
         [("ls -la", "output"), ("echo no id", None), ("echo deep", None), ("named", None)])
    ck(f"{say}the commands' ids, and a stand-in for an id of only a NUL",
       (ids.get("ls -la"), ids.get("named")) == ("tubash", "tu_name")
       and ids.get("echo no id", "").endswith(":4") and ids["echo no id"].startswith("s:"), ids)
    t = session_messages.c
    want(f"{say}the crew message", rows(s, t.tool_use_id, t.to_station, t.type, t.text),
         [("tumsg", "reviewer", "REPORT", "[TF:REPORT] hello")])
    want(f"{say}the files", rows(s, session_files.c.path), [("/a/file.py",), ("/a/nb.ipynb",)])
    t = session_usage.c
    want(f"{say}the replies' usage",
         rows(s, t.session, t.message, t.model, t.project, t.subagent),
         [("good", "m41", "claude-sonnet-4-5", None, None),
          ("s", "msg1", "claude-sonnet-4-5", PROJECT, None),
          ("s", "msg2", "claude-sonnet-4-5", None, "agent"),
          ("s", "msg3", "claude-sonnet-4-5", None, "sub")])
    t = challenges.c
    want(f"{say}the challenger line",
         rows(s, t.key_value, t.event_type, t.branch, t.tares_project),
         [("s", "challenge_commit", "factory/TR-1", "proj")])


def posted():
    print("== a posted batch ==")
    s = new_store()
    conn = connector(s)
    batch = posted_batch()
    before = json.dumps(batch)
    envs = attempt("the batch is mapped", lambda: conn.map_payload(batch)) or []
    ck("the posted objects are not changed", json.dumps(batch) == before)
    attempt("and appended", lambda: s.append(envs))
    # all but the meters and the status line are lines of a session
    ck("every line of the batch is an envelope", len(envs) == len(batch) - 3,
       (len(envs), len(batch)))
    n = {}
    for e in envs:
        n[e.key_value] = n.get(e.key_value, 0) + 1
    ck("under their sessions", set(n) == {"good", "s", "s2"} and n["good"] == 2, n)
    with s.sessions_db.begin() as c:
        counted = dict(c.execute(select(metadata.tables["sessions"].c.session,
                                        metadata.tables["sessions"].c.line_count)).all())
    ck("and counted", counted == n, (counted, n))
    ck("and written", sorted(r[0] for r in rows(s, session_lines.c.session))
       == sorted(e.key_value for e in envs), len(rows(s, session_lines.c.session)))
    kept = [e.payload for e in envs if e.key_value == "good"]
    ck("the good lines are untouched", kept == good_lines(), kept)
    check_posted_rows(s, "")

    t = session_states.c
    want("the states", rows(s, t.session, t.state, t.reason),
         [("s", "idle", "waiting for you"), ("s2", "idle", None)])
    t = session_stations.c
    want("the station", rows(s, t.session, t.station, t.role, t.parent, t.project),
         [("s", "proj-build", "builder", "orchestrator", PROJECT)])
    got = rows(s, crews.c.name, crews.c.machine)
    ck("its crew", len(got) == 1 and got[0][0] == "crew" and got[0][1] in ("", "machine"), got)
    t = project_sessions.c
    want("the session's project", rows(s, t.project, t.session, t.repo), [(PROJECT, "s", "repo2")])
    got = rows(s, session_meters.c.session, session_meters.c.status)
    name = (got[0][1] if got else None)
    name = json.loads(name) if isinstance(name, str) else name
    ck("the meter reading", bool(name) and name["model"]["display_name"] == "Claude", got)
    no_nul_in_sessions_db(s)

    # the passes of a start over what was just posted add nothing
    attempt("the turns fill completes", lambda: session_ingest.fill_turns(s, chunk=2))
    attempt("the facts fill completes", lambda: session_ingest.fill_facts(s))
    attempt("the sessions reconcile completes", lambda: session_ingest.reconcile_sessions(s))
    attempt("the usage fill completes", s._claude_usage_fill)
    attempt("the challenger fill completes", s._challenges_fill)
    s.con.close()


# ── lines DuckDB holds from before ───────────────────────────────────────────
def old_lines():
    return [
        line("user", "old", 1, uuid=f"u{Z}1", gitBranch=f"br{Z}anch",
             message={"role": "user", "content": f"tu{Z}rn one"}),
        reply("old", 2, f"ms{Z}g1", [{"type": "text", "text": f"wo{Z}rds"}],
              tares_project=f"pr{Z}oj", subagent=f"su{Z}b"),
        reply("old", 3, Z),
        line("assistant", "old", 4, uuid="a4", message=said([
            call(f"tu{Z}bash", "Bash", {"command": f"ls{Z} -la"}),
            call(f"tu{Z}msg", "SendMessage",
                 {"to": f"rev{Z}iewer", "message": f"[TF:REPORT] hel{Z}lo"}),
            call(f"tu{Z}edit", "Edit", {"file_path": f"/a/fi{Z}le.py"}),
            call(Z, "Bash", {"command": "echo no id"})])),
        line("user", "old", 5, uuid="u5", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{Z}bash",
             "content": [{"type": "text", "text": f"out{Z}put"}]},
            {"type": "tool_result", "tool_use_id": Z, "content": "orphan"}]}),
        line("challenge_commit", "old", 6, gitBranch=f"factory/TR-{Z}1",
             tares_project=f"pr{Z}oj", challenge={"verdict": "PASS", "sha": "abc123"}),
        line("user", "old", 7, uuid="u7", message={"role": "user", "content": Z}),
        *good_lines(),
    ]


def hold_in_duckdb(s):
    """The lines above as a version before TR-1707 mapped them, in DuckDB alone: how a cell holds
    them after a write of its sessions tables failed (tests/_old_cell.py, record=False)."""
    real = session_map._without_nul
    session_map._without_nul = lambda o: o
    try:
        envs = connector(None).map_payload(old_lines())      # no store: nothing else is recorded
    finally:
        session_map._without_nul = real
    assert any(Z in (e.text or "") for e in envs) and any(Z in e.labels.get("branch", "")
                                                          for e in envs), "the lines lost their NULs"
    keep = s._keep_challenges
    s._keep_challenges = lambda *a, **k: None
    try:
        append_duckdb_first(s, envs, record=False)
    finally:
        s._keep_challenges = keep
    forget_fills(s)                    # the fills of the next start read them
    return envs


def check_old_facts(s, say):
    t = session_turns.c
    # the turn whose text is only a NUL is a turn with no words
    want(f"{say}the turns", rows(s, t.session, t.text),
         [("good", "turn 40"), ("old", "turn one"), ("old", "")])
    ck(f"{say}with their keys", {"u1", "u7", "g40"} <= {r[0] for r in rows(s, t.line_key)},
       rows(s, t.line_key))
    want(f"{say}the said lines", rows(s, session_said.c.session, session_said.c.text),
         [("good", "words 41"), ("old", "words")])
    t = session_commands.c
    got = rows(s, t.tool_use_id)
    want(f"{say}the commands and the output", commands(s),
         [("ls -la", "output"), ("echo no id", None)])
    ck(f"{say}their ids", any(r[0] == "tubash" for r in got)
       and any(r[0].startswith("old:") for r in got), got)
    t = session_messages.c
    want(f"{say}the crew message", rows(s, t.tool_use_id, t.to_station, t.type, t.text),
         [("tumsg", "reviewer", "REPORT", "[TF:REPORT] hello")])
    want(f"{say}the files", rows(s, session_files.c.path), [("/a/file.py",)])


def held_by_duckdb():
    print("== lines DuckDB already holds with NULs: the fills of a start ==")
    s = new_store()
    hold_in_duckdb(s)
    for turn in (1, 2):
        attempt(f"the turns fill completes, pass {turn}",
                lambda: session_ingest.fill_turns(s, chunk=2))
        attempt(f"the facts fill completes, pass {turn}", lambda: session_ingest.fill_facts(s))
        attempt(f"the sessions reconcile completes, pass {turn}",
                lambda: session_ingest.reconcile_sessions(s))
        attempt(f"the usage fill completes, pass {turn}", s._claude_usage_fill)
        attempt(f"the challenger fill completes, pass {turn}", s._challenges_fill)
    ck("the turns fill set its watermark", session_ingest._turns_mark(s) is not None)
    ck("the facts fill set its watermark", session_ingest._facts_mark(s) is not None)
    check_old_facts(s, "")
    t = session_usage.c
    want("the replies' usage", rows(s, t.session, t.message, t.model, t.project, t.subagent),
         [("good", "m41", "claude-sonnet-4-5", None, None),
          ("old", "msg1", "claude-sonnet-4-5", PROJECT, "sub")])
    ck("priced", all(r[0] for r in rows(s, t.cost_usd)), rows(s, t.cost_usd))
    t = challenges.c
    want("the challenger line", rows(s, t.key_value, t.branch, t.tares_project),
         [("old", "factory/TR-1", "proj")])
    n = rows(s, metadata.tables["sessions"].c.session, metadata.tables["sessions"].c.line_count)
    ck("every session is counted", n == [("good", 2), ("old", 7)], n)
    s.con.close()


def counted_before():
    """SQLite takes a NUL, so a version before TR-1707 stored such a reply and challenger line as
    they were. The fills, which read them again at a start, do not add them twice."""
    s = new_store()
    if s.sessions_db.dialect != "sqlite":
        s.con.close()
        return
    print("== rows an older version stored on SQLite ==")
    envs = hold_in_duckdb(s)
    s.record_reply_usage("old", f"ms{Z}g1", T0 + timedelta(seconds=2), "claude-sonnet-4-5", None,
                         None, {"input_tokens": 3, "output_tokens": 5})
    s._keep_challenges([e for e in envs if e.event_type == "challenge_commit"])
    attempt("the usage fill completes", s._claude_usage_fill)
    attempt("the challenger fill completes", s._challenges_fill)
    got = rows(s, session_usage.c.session, session_usage.c.message)
    want("the reply is counted once", got, [("good", "m41"), ("old", f"ms{Z}g1")])
    got = rows(s, challenges.c.key_value, challenges.c.branch)
    want("the challenger line is kept once", got, [("old", f"factory/TR-{Z}1")])
    # the look for what is already there only reads: it takes no write lock, so ingest never waits
    reply = {"session": "old", "message": f"ms{Z}g1", "model": None, "project": None,
             "subagent": None}
    got = begins(s, lambda: s._usage_without_nul([dict(reply)]))
    want("the reply is looked up without a write lock", got, ["BEGIN"])
    line = ("claude_code", "old", T0, "challenge_commit", f"factory/TR-{Z}1", None, "{}")
    got = begins(s, lambda: s._challenges_without_nul([line]))
    want("the challenger line is looked up without a write lock", got, ["BEGIN"])
    s.con.close()


def begins(s, call):
    """Each BEGIN `call` sent to the SQLite session store."""
    seen = []

    def after(conn, cursor, statement, *a):
        if statement.startswith("BEGIN"):
            seen.append(statement)

    engine = s.sessions_db.engine
    event.listen(engine, "after_cursor_execute", after)
    try:
        call()
    finally:
        event.remove(engine, "after_cursor_execute", after)
    return seen


def moved(age, target):
    """The move writes a session whose last line is inside the archive window into `session_lines`,
    an older one into the archive: both with lines `age` old, taken from now."""
    global T0
    print(f"== the one-time move of a factory cell that holds them, to the {target} ==")
    was, T0 = T0, (datetime.now(timezone.utc) - age).replace(microsecond=0)
    try:
        _moved(target)
    finally:
        T0 = was


def _moved(target):
    s = new_store()
    envs = hold_in_duckdb(s)
    with contextlib.redirect_stdout(io.StringIO()):
        attempt("the sessions reconcile completes", lambda: session_ingest.reconcile_sessions(s))
    out = io.StringIO()
    stop = threading.Event()
    timer = threading.Timer(60, stop.set)      # a move that never ends fails the checks below
    timer.start()
    with contextlib.redirect_stdout(out):
        session_move.run(s, stop, pause=0, old_pause=0)
    timer.cancel()
    ck("the move runs to its end", "session move stopped" not in out.getvalue(), out.getvalue())
    ck("and is complete", session_move.complete(s), out.getvalue())
    got = rows(s, session_moves.c.session, session_moves.c.state)
    ck("both sessions are moved and verified",
       got == [("good", session_move.VERIFIED), ("old", session_move.VERIFIED)],
       (got, out.getvalue()))
    got = rows(s, session_moves.c.session, session_moves.c.target)
    ck(f"to the {target}", got == [("good", target), ("old", target)], got)
    in_table = len(rows(s, session_lines.c.session, session_lines.c.seq))
    archived = sum(r[1] for r in rows(s, session_archive_parts.c.session,
                                      session_archive_parts.c.lines))
    expected = (len(envs), 0) if target == session_move.TARGET else (0, len(envs))
    ck("with every line", (in_table, archived) == expected, (in_table, archived, len(envs)))
    check_old_facts(s, "")
    if target == session_move.TARGET:
        drained(s, len(envs))
    s.con.close()


def drained(s, n):
    """The M1 drain reads the same DuckDB lines again for a session with the todo mark."""
    with s.sessions_db.begin() as c:
        for table in (session_lines, session_turns, session_said, session_commands,
                      session_messages, session_files):
            c.execute(delete(table))
    session_ingest.mark_drain_todo(s, ["good", "old"])
    ck("the drain takes both sessions", attempt("the drain runs",
                                                lambda: session_drain.drain(s)) == 2)
    got = len(rows(s, session_lines.c.session, session_lines.c.seq))
    ck("every line is back", got == n, (got, n))
    got = rows(s, sessions.c.session, sessions.c.lines_complete, sessions.c.line_count)
    ck("and the sessions are complete", got == [("good", True, 2), ("old", True, 7)], got)
    got = rows(s, session_ingest_marks.c.name)
    ck("with no drain mark left", not [r for r in got if r[0].startswith("session_drain")], got)
    check_old_facts(s, "after the drain, ")


def first_state_start():
    """A cell that ran without a state database holds the NULs in its own sessions tables too; its
    first start with Postgres copies them over and fills from DuckDB."""
    print("== the first start with a state database ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    import psycopg
    s = new_store(state_dsn="")
    path = s.path
    envs = hold_in_duckdb(s)
    s.set_session_state("old", "idle", f"wai{Z}ting", T0)
    s.record_reply_usage("old", f"ms{Z}g1", T0 + timedelta(seconds=2), "claude-sonnet-4-5", None,
                         f"su{Z}b", {"input_tokens": 3, "output_tokens": 5})
    s.set_session_station("old", f"proj-bui{Z}ld", "builder", f"orche{Z}strator", None, T0)
    s.link_session(PROJECT, "old", f"re{Z}po")
    s._keep_challenges([e for e in envs if e.event_type == "challenge_commit"])
    # SQLite takes a key with its NUL and without as two rows; Postgres takes it once
    for station in (f"tw{Z}in", "twin"):
        s.record_reply_usage("old", station.replace("tw", "msg-tw"), T0, "claude-sonnet-4-5",
                             None, None, {"input_tokens": 1, "output_tokens": 1})
    s.close()
    s.con.close()

    schema = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    dsn = f"{PG}?options=-csearch_path%3D{schema}"
    got = attempt("the start succeeds", lambda: Store(path, state_dsn=dsn))
    if got is None:
        return
    s = got
    ck("on Postgres", s.sessions_db.dialect == "postgresql", s.sessions_db.dialect)
    t = session_states.c
    want("the state came over", rows(s, t.session, t.reason), [("old", "waiting")])
    t = session_stations.c
    want("the station", rows(s, t.session, t.station, t.parent),
         [("old", "proj-build", "orchestrator")])
    want("the session's project", rows(s, project_sessions.c.repo), [("repo",)])
    t = session_usage.c
    want("the reply once, beside the one the fill adds, and one of the twins",
         rows(s, t.session, t.message, t.subagent),
         [("good", "m41", None), ("old", "msg1", "sub"), ("old", "msg-twin", None)])
    t = challenges.c
    want("the challenger line once", rows(s, t.key_value, t.branch, t.tares_project),
         [("old", "factory/TR-1", "proj")])

    s.con.close()


def unit():
    print("== the walk over a line ==")
    clean = {"a": [1, None, True, {"b": "c"}]}
    ck("a line with no NUL is the same object", claude_code._without_nul(clean) is clean)
    v = deep(200_000)
    got = attempt("a line nested 200000 deep is walked", lambda: claude_code._without_nul(v))
    for _ in range(200_000):
        got = got[0] if isinstance(got, list) else got["k"]
    ck("and cleaned to the bottom", got == "bottom", repr(got)[:60])
    ck("other values pass", [claude_code._without_nul(x) for x in (None, 3, "", Z, [], {})]
       == [None, 3, "", "", [], {}])


def main():
    unit()
    posted()
    held_by_duckdb()
    counted_before()
    moved(timedelta(hours=1), session_move.TARGET)
    moved(timedelta(days=30), session_move.ARCHIVE)
    first_state_start()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


if __name__ == "__main__":
    main()
