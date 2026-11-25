"""The factory's session facts are written when a line arrives, and the readers read them (TR-1477).

`protocol_messages` and `recorded_commands` used to parse the events table on every read. They now
read `session_messages` and `session_commands`, which `session_ingest.record` writes as each line is
stored and `fill_facts` writes for the lines a first start has not seen.

The old readers are copied below as the reference. The same lines go through `Store.append`, and the
new readers must return what the reference returns from the events: a result that arrives in a later
batch (or before its call), a call with no result, a resumed session that repeats lines, a request
copy (`api-request`), a long output, a long message, a tool named `mcp__x__SendMessage`.

Plain:                python tests/test_session_facts.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_facts.py
"""
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alembic import command  # noqa: E402
from sqlalchemy import delete, func, inspect, select, text  # noqa: E402

from tares import session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import (metadata, session_commands,  # noqa: E402
                                        session_ingest_marks, session_messages,
                                        sessions)
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
SRC = "claude_code"


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def at(sec):
    return T0 + timedelta(seconds=sec)


def use(session, sec, block_id, name, inp, source=SRC, extra=None):
    """A tool_use line, rendered as the connector renders it."""
    payload = {"message": {"content": [{"type": "tool_use", "id": block_id, "name": name,
                                        "input": inp}, *(extra or [])]}}
    return Envelope(source=source, source_type="agent_session", key_value=session,
                    event_type="tool_use", text=f"→ {name}({json.dumps(inp)[:60]})",
                    event_time=at(sec), payload=payload)


def result(session, sec, block_id, content, is_error=False, source=SRC):
    blk = {"type": "tool_result", "tool_use_id": block_id, "content": content}
    if is_error:
        blk["is_error"] = True
    return Envelope(source=source, source_type="agent_session", key_value=session,
                    event_type="tool_result", text="[tool_result] x", event_time=at(sec),
                    payload={"message": {"content": [blk]}})


def blob(session, sec, block_id, name, inp):
    """Claude Code's copy of a request, as an older plugin recorded it."""
    e = use(session, sec, block_id, name, inp)
    e.payload = dict(e.payload, type="api-request-blob")
    return e


def bash(session, sec, bid, cmd):
    return use(session, sec, bid, "Bash", {"command": cmd})


def send(session, sec, bid, to, msg, name="SendMessage"):
    return use(session, sec, bid, name, {"to": to, "message": msg})


LONG_OUT = "x" * 25000 + "TAIL"
LONG_MSG = "[TF:FYI] " + "m" * 5000
# batch 1: calls, a result that comes in a later batch, a result before its call
BATCH1 = [
    bash("sA", 1, "b1", "pytest -q"),
    send("sA", 2, "m1", "orch", "[TF:REVIEW] PR 1\nsecond line"),
    send("sA", 3, "m2", "orch", "not a protocol message"),
    result("sA", 4, "b2", "ran after its call arrives"),          # the result first
    bash("sA", 5, "b3", "echo no result"),                        # never gets a result
    send("sA", 6, "m3", "rev", LONG_MSG, name="mcp__plug__SendMessage"),
    blob("sA", 7, "m9", "SendMessage", {"to": "x", "message": "[TF:FYI] only in a copy"}),
    bash("sA", 8, "b5", "echo in a copy"),
    Envelope(source="other", source_type="agent_session", key_value="sZ", event_type="tool_use",
             text="→ Bash(x)", event_time=at(9),
             payload={"message": {"content": [{"type": "tool_use", "id": "bz", "name": "Bash",
                                               "input": {"command": "other source"}}]}}),
]
BATCH2 = [
    result("sA", 10, "b1", [{"type": "text", "text": "3 passed"}, {"type": "text", "text": "ok"}]),
    bash("sA", 11, "b2", "make build"),                           # its result came first
    bash("sA", 12, "b4", "long run"),
    result("sA", 13, "b4", LONG_OUT, is_error=True),
    result("sA", 14, "bq", "a result with no call"),              # never listed
]
# a resumed session copies the old lines under its own id, with the same call ids and times
RESUMED = [bash("sB", 1, "b1", "pytest -q"), send("sB", 2, "m1", "orch", "[TF:REVIEW] PR 1\nsecond line"),
           result("sB", 10, "b1", "3 passed\nok")]


def open_store(d):
    s = Store(os.path.join(d, "t.duckdb"))
    s.set_session_station("sA", "builder-1", "builder", "", "p1", at(0))
    s.set_session_station("sB", "builder-2", "builder", "", "p1", at(0))
    return s


# ── the old readers, copied from before TR-1477, as the reference ─────────────────────────────
def ref_protocol_messages(store, source, limit=3000):
    with store._lock:
        rows = store.con.execute(
            "SELECT e.key_value, e.event_time, e.payload FROM events e "
            "WHERE e.source = ? AND e.event_type = 'tool_use' AND e.text LIKE '%SendMessage%' "
            "AND CAST(e.payload AS VARCHAR) LIKE '%[TF:%' "
            "ORDER BY e.event_time DESC LIMIT ?", [source, limit]).fetchall()
    stations = store._station_of({r[0] for r in rows})
    out, seen = [], set()
    for sid, t, pj in reversed(rows):
        station = stations.get(sid)
        try:
            o = json.loads(pj) if pj else {}
        except (TypeError, ValueError):
            continue
        if str(o.get("type") or "").startswith("api-request"):
            continue
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        for b in msg.get("content") or []:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"
                    and str(b.get("name") or "").split("__")[-1] == "SendMessage"):
                continue
            if b.get("id"):
                if b["id"] in seen:
                    continue
                seen.add(b["id"])
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            text = str(inp.get("message") or inp.get("content") or "")
            m = re.match(r"\s*\[TF:([A-Z_]+)\]\s*(.*)", text)
            if not m:
                continue
            out.append({"at": t, "session": sid, "from": station or "", "to": str(inp.get("to") or ""),
                        "type": m.group(1),
                        "first_line": m.group(2).splitlines()[0][:300] if m.group(2) else "",
                        "text": text[:4000]})
    return out


def ref_recorded_commands(store, source, sessions, limit=6000):
    marks = ", ".join("?" for _ in sessions)
    with store._lock:
        uses = store.con.execute(
            f"SELECT event_time, event_type, payload, key_value FROM events WHERE source = ? "
            f"AND key_value IN ({marks}) AND event_type = 'tool_use' "
            "AND text LIKE '%Bash(%' ORDER BY event_time DESC LIMIT ?",
            [source, *sessions, limit // 2]).fetchall()
        oldest = min((u[0] for u in uses), default=None)
        results = store.con.execute(
            f"SELECT event_time, event_type, payload, key_value FROM events WHERE source = ? "
            f"AND key_value IN ({marks}) AND event_type = 'tool_result' AND event_time >= ? "
            "ORDER BY event_time DESC LIMIT ?",
            [source, *sessions, oldest, limit * 2]).fetchall() if uses else []
        rows = uses + results
    uses, results = {}, {}
    for t, typ, pj, sid in sorted(rows, key=lambda r: r[0]):
        o = json.loads(pj) if isinstance(pj, str) else (pj or {})
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                uses[str(b.get("id"))] = {"at": t, "session": sid, "command": str(inp.get("command") or "")}
            elif b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                results[str(b.get("tool_use_id"))] = {"output": str(c or "")[-20000:],
                                                      "is_error": bool(b.get("is_error"))}
    out = []
    for tid, u in uses.items():
        out.append({**u, **(results.get(tid) or {"output": "", "is_error": False})})
    return sorted(out, key=lambda x: x["at"])


def dump(s):
    """Every row of both fact tables, in key order."""
    with s.sessions_db.begin() as c:
        m = c.execute(select(session_messages).order_by(session_messages.c.tool_use_id)).fetchall()
        k = c.execute(select(session_commands).order_by(session_commands.c.tool_use_id)).fetchall()
    return [tuple(r) for r in m], [tuple(r) for r in k]


def strip_session(rows):
    return [{k: v for k, v in r.items() if k not in ("session", "from")} for r in rows]


def revision():
    print("== revision 0004_session_facts ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "sf_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        insp = inspect(sdb.engine)
        ck("both tables exist", {"session_messages", "session_commands"} <= set(insp.get_table_names()))
        ck("session_messages columns", sorted(c["name"] for c in insp.get_columns("session_messages")) == sorted(
            ["tool_use_id", "source", "session", "at", "to_station", "type", "text"]))
        ck("session_commands columns", sorted(c["name"] for c in insp.get_columns("session_commands")) == sorted(
            ["tool_use_id", "source", "session", "at", "command", "exit_code", "output_tail", "result_at"]))
        ck("each is keyed by its tool_use id",
           insp.get_pk_constraint("session_messages")["constrained_columns"] == ["tool_use_id"]
           and insp.get_pk_constraint("session_commands")["constrained_columns"] == ["tool_use_id"])
        ck("each has an index on (session, at)", all(
            any(i["column_names"] == ["session", "at"] for i in insp.get_indexes(t))
            for t in ("session_messages", "session_commands")))
        with sdb.begin() as c:
            c.execute(text("INSERT INTO session_commands (tool_use_id, command) VALUES ('k', 'x')"))
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0003_sessions")
        names = inspect(sdb.engine).get_table_names()
        ck("a downgrade removes both tables and keeps sessions",
           "session_messages" not in names and "session_commands" not in names and "sessions" in names, names)
        sdb.upgrade()
        ck("an upgrade after it builds them again", {"session_messages", "session_commands"}
           <= set(inspect(sdb.engine).get_table_names()))
        # the stamped case: tables made by hand while the version is behind
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0003_sessions")
        with sdb.begin() as c:
            metadata.tables["session_messages"].create(c)
            metadata.tables["session_commands"].create(c)
            c.execute(text("INSERT INTO session_commands (tool_use_id, command) VALUES ('kept', 'x')"))
        sdb.upgrade()
        with sdb.begin() as c:
            v = [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]
            kept = c.execute(select(session_commands.c.command).where(
                session_commands.c.tool_use_id == "kept")).scalar()
        ck("an upgrade over tables that already exist stamps the revision and keeps the rows",
           v == ["0013_project_tables"] and kept == "x", (v, kept))
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


def main():
    revision()
    print("== the readers return what the old readers did ==")
    d = tempfile.mkdtemp()
    s = open_store(d)
    s.append(BATCH1)
    s.append(BATCH2)
    ref_m, ref_c = ref_protocol_messages(s, SRC), ref_recorded_commands(s, SRC, ["sA"])
    live_c = s.recorded_commands(SRC, ["sA"])
    ck("live, a result that came in an earlier batch than its call is not written yet",
       [c for c in live_c if c["command"] == "make build"][0]["output"] == "")
    ck("live, every other command equals the old reader's",
       [c for c in live_c if c["command"] != "make build"]
       == [c for c in ref_c if c["command"] != "make build"])
    ck("live, the messages equal the old reader's", s.protocol_messages(SRC) == ref_m)
    session_ingest.fill_facts(s)           # the fill pairs the result that came first
    got_m, got_c = s.protocol_messages(SRC), s.recorded_commands(SRC, ["sA"])
    ck("the reference sees the seeded calls", len(ref_m) == 2 and len(ref_c) == 5,
       (len(ref_m), len(ref_c)))
    ck("protocol_messages equals the old reader", got_m == ref_m, (got_m, ref_m))
    ck("recorded_commands equals the old reader", got_c == ref_c, (got_c, ref_c))
    by = {c["command"]: c for c in got_c}
    ck("a result that came in a later batch is paired", by["pytest -q"]["output"] == "3 passed\nok")
    ck("a result that came before its call is paired by the fill", by["make build"]["output"] == "ran after its call arrives")
    ck("a call with no result has no output and no failure",
       by["echo no result"]["output"] == "" and by["echo no result"]["is_error"] is False)
    ck("a long output keeps its last 20000 characters and the failure",
       by["long run"]["output"] == LONG_OUT[-20000:] and by["long run"]["is_error"] is True)
    ck("a long message keeps its first 4000 characters",
       [len(m["text"]) for m in got_m if m["type"] == "FYI"] == [4000])
    ck("the sender is the session's station and the call's own to", (got_m[0]["from"], got_m[0]["to"]) == ("builder-1", "orch"), got_m[0])
    ck("a result with no call, a request copy and another source's call are not listed",
       "a result with no call" not in str(got_c) and "only in a copy" not in str(got_m)
       and "in a copy" not in str(got_c).replace("echo in a copy", "") and "other source" not in str(got_c))
    ck("a request copy's Bash call is listed, as before", "echo in a copy" in by)
    ck("another source is read apart", s.protocol_messages("other") == [] and s.recorded_commands("other", ["sA"]) == [])

    print("== the readers do not read events ==")
    real = s.con

    class Boom:
        def __getattr__(self, n):
            raise AssertionError("the readers touched DuckDB")
    s.con = Boom()
    try:
        ok = s.protocol_messages(SRC) == ref_m and s.recorded_commands(SRC, ["sA"]) == ref_c
    except AssertionError as e:
        ok = False
        print("   ", e)
    finally:
        s.con = real
    ck("both answer without a DuckDB query", ok)
    ck("the two memos are gone", not hasattr(s, "_proto_cache") and not hasattr(s, "_coord_cache"))

    print("== rows by tool_use id: a batch written twice adds nothing ==")
    before = dump(s)
    s.append(BATCH1)
    s.append(BATCH2)
    ck("both tables are as they were", dump(s) == before)
    ck("the readers still equal the reference", s.protocol_messages(SRC) == ref_m
       and s.recorded_commands(SRC, ["sA"]) == ref_c)

    print("== the fill ==")
    with s.sessions_db.begin() as c:
        c.execute(delete(session_messages))
        c.execute(delete(session_commands))
    ck("emptied", dump(s) == ([], []))
    ck("so the readers show nothing", s.protocol_messages(SRC) == [] and s.recorded_commands(SRC, ["sA"]) == [])
    n = session_ingest.fill_facts(s)
    ck("a fill looks at the sessions that hold tool lines", n == 1, n)   # sA; the other source is not a claude_code source
    ck("it brings back the same rows", dump(s) == before)
    ck("and the readers return the reference again", s.protocol_messages(SRC) == ref_m
       and s.recorded_commands(SRC, ["sA"]) == ref_c)
    ck("it set the watermark", session_ingest._facts_mark(s) is not None)
    session_ingest.fill_facts(s)
    ck("a second fill changes nothing", dump(s) == before)
    with s.sessions_db.begin() as c:
        c.execute(delete(session_ingest_marks).where(session_ingest_marks.c.name == session_ingest.FACTS_MARK))
    session_ingest.fill_facts(s, chunk=1)
    ck("a fill with a small chunk and no watermark changes nothing either", dump(s) == before)

    print("== a fill over rows live ingest already wrote ==")
    live = [bash("sC", 20, "c1", "live one")]
    s.append(live)                                  # record() writes the call
    with s.sessions_db.begin() as c:                # a result row from a fill that ran in between
        c.execute(session_commands.update().where(session_commands.c.tool_use_id == "c1").values(
            exit_code=1, output_tail="kept", result_at=at(21)))
    session_ingest.fill_facts(s)                    # the fill sees the call (no result line): it must keep the result
    with s.sessions_db.begin() as c:
        r = c.execute(select(session_commands.c.command, session_commands.c.exit_code,
                             session_commands.c.output_tail).where(session_commands.c.tool_use_id == "c1")).one()
    ck("a call written again does not blank the result on its row", tuple(r) == ("live one", 1, "kept"), tuple(r))
    s.append([result("sC", 22, "c1", "later result")])
    with s.sessions_db.begin() as c:
        r = c.execute(select(session_commands.c.command, session_commands.c.output_tail).where(
            session_commands.c.tool_use_id == "c1")).one()
    ck("a result line sets the output and keeps the command", tuple(r) == ("live one", "later result"), tuple(r))

    print("== a result makes no row of its own ==")
    s.append([result("sC", 30, "a_read_tool", "text some other tool printed"),
              result("sC", 31, "never_called", "x" * 20000)])
    with s.sessions_db.begin() as c:
        rows = c.execute(select(func.count()).select_from(session_commands)).scalar()
        nocmd = c.execute(select(func.count()).select_from(session_commands).where(
            session_commands.c.command.is_(None))).scalar()
        ghost = c.execute(select(func.count()).select_from(session_commands).where(
            session_commands.c.tool_use_id.in_(["a_read_tool", "never_called", "bq"]))).scalar()
    ck("a non-Bash result and a result with no call leave no row", ghost == 0, ghost)
    ck("every row is a call", nocmd == 0 and rows == 6, (rows, nocmd))
    ck("and no reader lists a result with no call", all(c["command"] for c in s.recorded_commands(SRC, ["sA", "sC"])))

    print("== NUL bytes ==")
    s.append([bash("sN", 40, "n1", "echo a\x00b"), result("sN", 41, "n1", "out\x00put"),
              send("sN", 42, "n2", "or\x00ch", "[TF:FYI] he\x00llo")])
    cm = [c for c in s.recorded_commands(SRC, ["sN"])]
    mm = [m for m in s.protocol_messages(SRC) if m["type"] == "FYI" and m["to"].startswith("or")]
    ck("a NUL in a command, an output and a message is dropped, the line is kept",
       len(cm) == 1 and cm[0]["command"] == "echo ab" and cm[0]["output"] == "output"
       and len(mm) == 1 and mm[0]["to"] == "orch" and "\x00" not in str(mm), (cm, mm))

    print("== a facts failure keeps the counts ==")
    real_write = session_ingest._write_facts

    def broken(*a, **k):
        raise RuntimeError("facts refused")
    session_ingest._write_facts = broken
    try:
        with s.sessions_db.begin() as c:
            before_n = c.execute(select(sessions.c.line_count).where(
                sessions.c.session == "sF")).scalar()
        s.append([bash("sF", 50, "f1", "echo f")])
    finally:
        session_ingest._write_facts = real_write
    with s.sessions_db.begin() as c:
        n_f = c.execute(select(sessions.c.line_count).where(sessions.c.session == "sF")).scalar()
        f_rows = c.execute(select(func.count()).select_from(session_commands).where(
            session_commands.c.tool_use_id == "f1")).scalar()
    ck("the session's count is kept and the fact row is not written", (before_n, n_f, f_rows) == (None, 1, 0),
       (before_n, n_f, f_rows))
    session_ingest.fill_facts(s)
    ck("the next fill writes it", [c["command"] for c in s.recorded_commands(SRC, ["sF"])] == ["echo f"])

    print("== purging a source removes its facts ==")
    s.purge_events(SRC)
    with s.sessions_db.begin() as c:
        left = (c.execute(select(func.count()).select_from(session_commands).where(
                    session_commands.c.source == SRC)).scalar(),
                c.execute(select(func.count()).select_from(session_messages).where(
                    session_messages.c.source == SRC)).scalar())
    ck("no command or message of the source is left", left == (0, 0), left)
    ck("and the readers show none", s.protocol_messages(SRC) == []
       and s.recorded_commands(SRC, ["sA", "sC", "sN", "sF"]) == [])
    s.con.close()

    print("== a resumed session repeats lines ==")
    d2 = tempfile.mkdtemp()
    s2 = open_store(d2)
    s2.append(BATCH1)
    s2.append(BATCH2)
    s2.append(RESUMED)
    session_ingest.fill_facts(s2)           # a result that came before its call waits for the fill
    ref_m, ref_c = ref_protocol_messages(s2, SRC), ref_recorded_commands(s2, SRC, ["sA", "sB"])
    got_m, got_c = s2.protocol_messages(SRC), s2.recorded_commands(SRC, ["sA", "sB"])
    ck("each call is listed once", len(got_m) == 2 and len(got_c) == 5, (len(got_m), len(got_c)))
    ck("the messages equal the old reader's, apart from which copy's session names the call",
       strip_session(got_m) == strip_session(ref_m), (got_m, ref_m))
    ck("the commands equal the old reader's, apart from the same",
       strip_session(got_c) == strip_session(ref_c), (got_c, ref_c))
    ck("only sessions asked for are listed", {c["session"] for c in s2.recorded_commands(SRC, ["sB"])} <= {"sB"})
    before2 = dump(s2)
    with s2.sessions_db.begin() as c:
        c.execute(delete(session_messages))
        c.execute(delete(session_commands))
    session_ingest.fill_facts(s2)
    ck("a fill over the repeated lines gives the same rows", strip_session(s2.protocol_messages(SRC)) == strip_session(ref_m)
       and len(dump(s2)[0]) == len(before2[0]) and len(dump(s2)[1]) == len(before2[1]))
    s2.con.close()

    print("== nulls and empties ==")
    d3 = tempfile.mkdtemp()
    s3 = open_store(d3)
    s3.append([
        Envelope(source=SRC, source_type="agent_session", key_value="sA", event_type="tool_use",
                 text="→ Bash(x)", event_time=at(1), payload={}),                            # no blocks
        Envelope(source=SRC, source_type="agent_session", key_value="sA", event_type="tool_use",
                 text="→ Bash()", event_time=at(2),
                 payload={"message": {"content": [{"type": "tool_use", "id": "e1", "name": "Bash",
                                                   "input": {}}]}}),                       # no command
        Envelope(source=SRC, source_type="agent_session", key_value="sA", event_type="tool_use",
                 text="→ SendMessage(x)", event_time=at(3),
                 payload={"message": {"content": [{"type": "tool_use", "id": "e2", "name": "SendMessage",
                                                   "input": {"to": None, "content": "[TF:FYI]"}}]}}),
        result("sA", 4, "e1", None),
        result("sA", 5, "e3", [{"type": "image"}]),
    ])
    c = s3.recorded_commands(SRC, ["sA"])
    ck("a call with no command and a null result is listed with empty strings",
       c == [{"at": at(2), "session": "sA", "command": "", "output": "", "is_error": False}], c)
    m = s3.protocol_messages(SRC)
    ck("a protocol message with no body after its tag and no recipient", len(m) == 1 and m[0]["to"] == ""
       and m[0]["first_line"] == "" and m[0]["type"] == "FYI", m)
    ck("no sessions asked for", s3.recorded_commands(SRC, []) == [])
    s3.con.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
