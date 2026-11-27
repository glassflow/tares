"""A NUL byte in a session id (TR-1703).

Postgres text holds no NUL. A posted line loses the NULs of its `sessionId` before anything counts
or stores it, so SQLite and Postgres keep it under the same id; an id that was nothing but NULs is
no id. Lines DuckDB already holds under an id with a NUL (stored before this) are passed over by the
fills and the one-time move on Postgres, which used to raise on them at every start, and the other
sessions are filled and moved. A recorded command under such an id does not fail the ticket view's
check of what the caller may read (TR-1710).

Plain:                python tests/test_session_nul_id.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_nul_id.py
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import select  # noqa: E402

from tares import factory_api, session_ingest, session_move  # noqa: E402
from tares.connectors.claude_code import ClaudeCodeConnector  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store.tables import (challenges, session_commands,  # noqa: E402
                                        session_meters, session_moves, session_states,
                                        session_turns, session_usage, sessions)
from tares.store import Store  # noqa: E402

SRC = "claude_code"
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
ODD = "ba\x00d"
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
        ck(name, False, f"{type(e).__name__}: {str(e).splitlines()[0]}")
        return None
    ck(name, True)
    return got


def open_store(edition):
    os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp()
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    s.set_setting("edition", edition)
    s.upsert_catalog_source(SRC, "agent_session", SRC, "10s", {"push": True})
    return s


def when(sec):
    return (T0 + timedelta(seconds=sec)).isoformat()


def user(sid, sec):
    return {"type": "user", "sessionId": sid, "uuid": f"u{sec}", "timestamp": when(sec),
            "message": {"role": "user", "content": f"turn {sec}"}}


def reply(sid, sec):
    return {"type": "assistant", "sessionId": sid, "uuid": f"a{sec}", "timestamp": when(sec),
            "message": {"id": f"m{sec}", "role": "assistant", "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 3, "output_tokens": 5},
                        "content": [{"type": "text", "text": f"words {sec}"}]}}


def column(s, col):
    with s.sessions_db.begin() as c:
        return sorted(set(c.execute(select(col)).scalars()))


def duck(s, sql):
    with s._lock:
        return [r[0] for r in s.con.execute(sql).fetchall()]


def posted(edition):
    print(f"== a posted batch, {edition} edition ==")
    s = open_store(edition)
    conn = ClaudeCodeConnector(SimpleNamespace(name=SRC, type="agent_session", config={}), store=s)
    no_id = {"type": "user", "uuid": "u9", "timestamp": when(9),
             "message": {"role": "user", "content": "no session"}}
    batch = [user("one", 1), user(ODD, 2), reply(ODD, 3), user("\x00\x00", 4), no_id,
             {**no_id, "uuid": "u10", "sessionId": ""}, {**no_id, "uuid": "u11", "sessionId": None},
             {"type": "session_state", "sessionId": ODD, "state": "idle", "timestamp": when(5)},
             {"type": "session_state", "sessionId": "\x00", "state": "idle", "timestamp": when(5)},
             {"type": "session_status", "sessionId": ODD, "status": {}, "timestamp": when(6)},
             user("two", 7)]
    envs = attempt("the batch is mapped", lambda: conn.map_payload(batch)) or []
    attempt("and appended", lambda: s.append(envs))
    want = ["bad", "one", "two"]
    ck("the id is stored without its NUL, and a line with only NULs or no id has no session",
       [e.key_value for e in envs] == ["one", "bad", "bad", "bad", "two"],
       [e.key_value for e in envs])
    ck("its label and payload carry the same id",
       all(e.labels["session"] == e.key_value == e.payload["sessionId"] for e in envs),
       [(e.key_value, e.labels.get("session"), e.payload.get("sessionId")) for e in envs])
    for name, col, ids in (("sessions", sessions.c.session, want),
                           ("session_turns", session_turns.c.session, want),
                           ("session_usage", session_usage.c.session, ["bad"]),
                           ("session_states", session_states.c.session, ["bad"]),
                           ("session_meters", session_meters.c.session, ["bad"])):
        got = column(s, col)
        ck(f"{name} holds {ids}", got == ids, got)
    got = duck(s, "SELECT DISTINCT key_value FROM events WHERE key_value LIKE '%' || chr(0) || '%'")
    ck("no id in DuckDB has a NUL", got == [], got)
    s.con.close()


def hold_in_duckdb(s):
    """Three sessions' lines in DuckDB alone, one under an id with a NUL: how a cell upgraded from
    before TR-1703 holds them. `s` is a platform store, which writes its lines to DuckDB."""
    def env(sid, etype, sec, text, payload):
        return Envelope(source=SRC, source_type="agent_session", key_value=sid, event_type=etype,
                        text=text, event_time=T0 + timedelta(seconds=sec), payload=payload,
                        labels={"session": sid, "type": etype})

    def lines(sid, n):
        block = {"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": "ls"}}
        return [env(sid, "user", n, f"turn {n}", user(sid, n)),
                env(sid, "tool_use", n + 1, "→ Bash(ls)", {"message": {"content": [block]}}),
                env(sid, "assistant", n + 2, f"words {n + 2}", reply(sid, n + 2)),
                env(sid, "challenge_commit", n + 3, "Challenger reviewed commit abc",
                    {"challenge": {"verdict": "PASS", "sha": f"abc{n}"}})]

    real, keep = session_ingest.record, s._keep_challenges
    session_ingest.record = lambda *a, **k: None          # DuckDB has the lines, nothing else does
    s._keep_challenges = lambda *a, **k: None
    try:
        s.append(lines("one", 10) + lines(ODD, 20) + lines("two", 30))
    finally:
        session_ingest.record, s._keep_challenges = real, keep


def held_by_duckdb():
    """Lines stored under an id with a NUL before TR-1703, and not yet in the sessions tables: what
    the fills of a start find."""
    print("== lines DuckDB already holds under an id with a NUL ==")
    s = open_store("platform")
    hold_in_duckdb(s)
    pg = s.sessions_db.dialect == "postgresql"
    want = ["one", "two"] if pg else [ODD, "one", "two"]     # SQLite takes the id as it is
    for turn in (1, 2):
        attempt(f"the turns fill completes, pass {turn}", lambda: session_ingest.fill_turns(s, chunk=2))
    got = column(s, session_turns.c.session)
    ck("every other session's turns are filled", got == want, got)
    ck("and it set its watermark", session_ingest._turns_mark(s) is not None)
    attempt("the facts fill completes", lambda: session_ingest.fill_facts(s))
    got = column(s, session_commands.c.session)
    ck("with the other sessions' commands", got == want, got)
    attempt("the sessions reconcile completes", lambda: session_ingest.reconcile_sessions(s))
    got = column(s, sessions.c.session)
    ck("with the other sessions counted", got == want, got)
    attempt("the usage fill of a start completes", s._claude_usage_fill)
    got = column(s, session_usage.c.session)
    ck("with the other sessions' replies", got == want, got)
    attempt("the challenger fill of a start completes", s._challenges_fill)
    got = column(s, challenges.c.key_value)
    ck("with the other sessions' challenger lines", got == want, got)
    s.con.close()


def moved():
    """The one-time move of a factory cell that holds such lines (`session_move.plan` raised on
    Postgres at every start, so nothing moved)."""
    print("== the move of a factory cell that holds them ==")
    s = open_store("platform")
    hold_in_duckdb(s)
    s.set_setting("edition", "factory")
    pg = s.sessions_db.dialect == "postgresql"
    want = ["one", "two"] if pg else [ODD, "one", "two"]
    with contextlib.redirect_stdout(io.StringIO()):
        session_ingest.reconcile_sessions(s)
    out = io.StringIO()
    stop = threading.Event()
    timer = threading.Timer(60, stop.set)      # a move that never ends fails the checks below
    timer.start()
    with contextlib.redirect_stdout(out):
        session_move.run(s, stop, pause=0, old_pause=0)
    timer.cancel()
    ck("the move runs to its end", "session move stopped" not in out.getvalue(), out.getvalue())
    ck("and is complete", session_move.complete(s))
    with s.sessions_db.begin() as c:
        got = sorted(c.execute(select(session_moves.c.session, session_moves.c.state)).all())
    ck("every other session is moved and verified",
       [tuple(r) for r in got] == [(sid, session_move.VERIFIED) for sid in want], got)
    kept = duck(s, "SELECT count(*) FROM events WHERE key_value LIKE '%' || chr(0) || '%'")
    ck("DuckDB keeps the lines under the id with a NUL", kept == [4], kept)
    s.con.close()


def ticket_view():
    """The ticket view hands the session ids of the recorded commands to `unread_sessions`
    (TR-1710). The session store holds no NUL on Postgres, so the commands are handed in here: the
    check must pass over such an id and still leave out what a session the caller cannot read ran."""
    print("== the ticket view with a NUL in a recorded command's session id ==")
    s = open_store("factory")
    s.create_project("uc_open", "software_factory", "Open", {})
    s.link_session("uc_open", "mine")
    s.link_session("uc_hidden", "closed")        # a project the caller cannot see
    tid = s.create_ticket("uc_open", "tares", "a ticket")
    t = s.get_ticket("uc_open", tid)

    def run(sid, sec, output):
        return {"at": T0 + timedelta(seconds=sec), "session": sid, "command": "pytest",
                "output": output, "is_error": True, "ended": True, "exit_code": 1}

    s.ticket_checks = lambda *a: [{"command": "pytest", "result": "3 passed", "at": None}]
    s.recorded_commands = lambda *a, **k: [run(ODD, 1, "1 failed"), run("closed", 2, "2 failed")]
    h = SimpleNamespace(store=s, hidden_ids=lambda: {"uc_hidden"}, auth_off=lambda: True)
    got = attempt("the ticket view is made",
                  lambda: factory_api.review_state(h, "uc_open", t, SRC))
    check = (got or {}).get("checks") or [{}]
    ck("the check still differs for every reader", check[0].get("match") == "differs", check)
    ck("and does not show what the session the caller cannot read printed",
       got is not None and check[0].get("recorded") is None, check)
    got = attempt("the ids are looked up", lambda: factory_api.unread_sessions(h, [ODD, "closed"]))
    ck("only the session of the hidden project is unread", got == {"closed"}, got)
    s.con.close()


def main():
    for edition in ("platform", "factory"):
        posted(edition)
    held_by_duckdb()
    moved()
    ticket_view()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


if __name__ == "__main__":
    main()
