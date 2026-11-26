"""The sessions table (TR-1471): each claude_code line is counted into its session's row as it is
stored, and the reconcile at start brings the table up to what DuckDB holds.

Plain:                python tests/test_sessions_table.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness \\
        python tests/test_sessions_table.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alembic import command  # noqa: E402
from sqlalchemy import inspect, select, text  # noqa: E402

from tares import factory, metrics, session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import metadata, session_ingest_marks, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, i, kind="assistant", tool=None, source="claude_code", at=None):
    """One line of a Claude Code session. A tool_use line renders its tool as `→ name(...)` and
    carries it in payload.message.content, as the connector writes it."""
    payload = {}
    text_ = f"line {i}"
    if tool:
        kind = "tool_use"
        text_ = f"→ {tool}(x=1)"
        payload = {"message": {"content": [{"type": "tool_use", "name": tool}]}}
    return Envelope(source=source, source_type="agent_session", key_value=session, event_type=kind,
                    text=text_, event_time=at or T0 + timedelta(seconds=i), payload=payload)


def fresh_store():
    d = tempfile.mkdtemp()
    return Store(os.path.join(d, "t.duckdb"))


def rows(store):
    with store.sessions_db.begin() as c:
        return {r.session: (r.line_count, r.tool_calls, r.coordination_calls, r.started_at,
                            r.last_line_at, r.source, r.lines_complete)
                for r in c.execute(select(sessions)).all()}


def counts(store):
    return {k: v[:3] for k, v in rows(store).items()}


def sample():
    """Three sessions: plain lines, tool calls, coordination calls (some by mcp, some not)."""
    out = []
    for i in range(5):
        out.append(line("s1", i))
    out.append(line("s1", 5, tool="Bash"))
    out.append(line("s1", 6, tool="SendMessage"))
    out.append(line("s2", 0, tool="mcp__plugin_tares_tares__get_ticket"))
    out.append(line("s2", 1, tool="mcp__other__thing"))        # a tool call, not coordination
    out.append(line("s2", 2, tool="Skill"))
    out.append(line("s2", 3, tool="CronList"))
    out.append(line("s3", 0, tool="Read"))
    return out


def expected_for(envs):
    """The counts a scan of DuckDB gives for the same data: the rules the table keeps, computed
    straight from events with no reader of the table (the readers over events were removed in TR-1499)."""
    s = fresh_store()
    s._append(envs)                       # DuckDB only: nothing reaches the table
    sids = sorted({e.key_value for e in envs})
    with s._lock:
        act = {sid: (first, last, int(n)) for sid, first, last, n in s.con.execute(
            "SELECT key_value, min(event_time), max(event_time), count(*) FROM events "
            "WHERE source = 'claude_code' GROUP BY key_value").fetchall()}
        tools = dict(s.con.execute("SELECT key_value, count(*) FROM events WHERE event_type = 'tool_use' "
                                   "GROUP BY key_value").fetchall())
        lines = s.con.execute("SELECT key_value, text, payload FROM events WHERE event_type = 'tool_use'").fetchall()
    s.close()
    coord = {}
    for sid, text_, payload in lines:         # the rule of the scan the table replaced, written out
        if not any(h in (text_ or "") for h in ("→ mcp__", "→ SendMessage(", "→ ListAgents(", "→ Skill(", "→ Cron")):
            continue
        msg = (json.loads(payload) if payload else {}).get("message") or {}
        coord[sid] = coord.get(sid, 0) + sum(
            1 for b in msg.get("content") or []
            if isinstance(b, dict) and b.get("type") == "tool_use" and factory.is_coordination(b.get("name")))
    return {sid: (act[sid][2], tools.get(sid, 0), coord.get(sid, 0)) for sid in sids}, act


def through_append():
    print("== lines through Store.append ==")
    s = fresh_store()
    envs = sample()
    s.append(envs)
    got = counts(s)
    ck("one row per session", sorted(got) == ["s1", "s2", "s3"], sorted(got))
    ck("s1: 7 lines, 2 tool calls, 1 coordination", got["s1"] == (7, 2, 1), got["s1"])
    ck("s2: 4 lines, 4 tool calls, 3 coordination", got["s2"] == (4, 4, 3), got["s2"])
    ck("s3: 1 line, 1 tool call, none coordinating", got["s3"] == (1, 1, 0), got["s3"])
    r = rows(s)
    ck("started_at and last_line_at are the first and last line", r["s1"][3] == T0
       and r["s1"][4] == T0 + timedelta(seconds=6), (r["s1"][3], r["s1"][4]))
    ck("the row names its source", r["s1"][5] == "claude_code", r["s1"][5])
    ck("a row Store.append makes is born in the table, so lines_complete is true", all(v[6] for v in r.values()))
    s.append([line("s1", 7), line("s1", 8, tool="ListAgents")])
    got = counts(s)
    ck("a second batch adds to the row", got["s1"] == (9, 3, 2), got["s1"])
    s.append([line("s1", 0, at=T0 - timedelta(hours=1))])
    r = rows(s)
    ck("an older line moves started_at back and leaves last_line_at", r["s1"][3] == T0 - timedelta(hours=1)
       and r["s1"][4] == T0 + timedelta(seconds=8), (r["s1"][3], r["s1"][4]))
    s.append([line("other", 0, source="deploys"), Envelope("deploys", "event_stream", "", "deploy", "x", T0)])
    ck("a line of another source writes no row", "other" not in counts(s), sorted(counts(s)))
    ck("nor does a claude_code line without a session",
       not any(k == "" for k in counts(s)))
    s.close()


def parity():
    print("== the counts equal a scan of events, and the readers answer them ==")
    envs = sample()
    want, act = expected_for(envs)
    s = fresh_store()
    s.append(envs)
    got = counts(s)
    ck("line, tool call and coordination counts agree for every session", got == want, (got, want))
    r = rows(s)
    ck("and so do first and last line times", all(
        r[sid][3] == act[sid][0] and r[sid][4] == act[sid][1] for sid in act),
        {sid: (r[sid][3], act[sid][0]) for sid in act})
    sids = sorted(want)
    ra = s.session_activity("claude_code", sids)
    ck("session_activity() reads the same lines and times", {k: (v[2], v[0], v[1]) for k, v in ra.items()} == {
        k: (want[k][0], act[k][0], act[k][1]) for k in want}, ra)
    ck("coordination_calls() reads the same counts, zeros left out", s.coordination_calls("claude_code", sids) == {
        k: v[2] for k, v in want.items() if v[2]}, s.coordination_calls("claude_code", sids))
    ck("a session asked for under another source is not read", s.session_activity("deploys", sids) == {}
       and s.coordination_calls("deploys", sids) == {})
    s.close()


def catalog_source():
    print("== a claude_code source with another name ==")
    s = fresh_store()
    s.upsert_catalog_source("laptop", "claude_code", "claude_code", "", {})
    s.append([line("a", 0, source="laptop"), line("a", 1, tool="Skill", source="laptop"),
              line("b", 0, source="deploys")])
    got = counts(s)
    ck("its lines are counted", got.get("a") == (2, 1, 1), got)
    ck("a source of another connector is not", "b" not in got, got)
    s.close()


def reconcile():
    print("== the reconcile ==")
    envs = sample()
    want, _ = expected_for(envs)
    s = fresh_store()
    s._append(envs)                          # stored without record(): an older image, or a failed record
    ck("the table is empty before it", counts(s) == {}, counts(s))
    n = session_ingest.reconcile_sessions(s)
    ck("it looked at the three sessions", n == 3, n)
    ck("and the rows equal what DuckDB holds", counts(s) == want, (counts(s), want))
    before = rows(s)
    n2 = session_ingest.reconcile_sessions(s)
    ck("a second run changes no counter", counts(s) == want and {
        k: v[3:] for k, v in rows(s).items()} == {k: v[3:] for k, v in before.items()}, counts(s))
    ck("the second run looked only at the recent sessions", n2 == 3, n2)   # all within the 10 minute margin
    # lines written through append after the pass are counted once, not twice, by the next pass
    s.append([line("s1", 20), line("s4", 0, tool="Skill")])
    session_ingest.reconcile_sessions(s)
    got = counts(s)
    ck("a line counted by record() is not counted again by the reconcile", got["s1"] == (8, 2, 1)
       and got["s4"] == (1, 1, 1), got)
    # a line stored without record() after the first pass is picked up
    s._append([line("s1", 30), line("s5", 0)])
    session_ingest.reconcile_sessions(s)
    got = counts(s)
    ck("a line stored without record() is picked up", got["s1"] == (9, 2, 1) and got["s5"] == (1, 0, 0), got)
    # the mark moved, and it is the newest ingest_time read
    with s.sessions_db.begin() as c:
        mark = c.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == session_ingest.MARK)).scalar_one()
    ck("the watermark is stored and aware", mark is not None and mark.tzinfo is not None, mark)
    # in chunks: more sessions than one chunk
    s2 = fresh_store()
    many = [line(f"m{i:03d}", 0, tool="Skill") for i in range(25)] + [line("m000", 1)]
    s2._append(many)
    session_ingest.reconcile_sessions(s2, chunk=7)
    g = counts(s2)
    ck("chunks of 7 cover 25 sessions", len(g) == 25 and g["m000"] == (2, 1, 1) and g["m024"] == (1, 1, 1), len(g))
    # a stored counter larger than DuckDB's (lines pruned from DuckDB) is kept
    with s2.sessions_db.begin() as c:
        c.execute(sessions.update().where(sessions.c.session == "m001").values(line_count=50))
    with s2._lock:
        s2.con.execute("UPDATE events SET ingest_time = now() WHERE key_value = 'm001'")
    session_ingest.reconcile_sessions(s2)
    ck("the larger of the stored and the computed count stays", counts(s2)["m001"][0] == 50, counts(s2)["m001"])
    # the thread starter runs one pass
    s3 = fresh_store()
    s3._append(envs)
    s3.start_session_reconcile().join(30)
    ck("start_session_reconcile fills the table in the background", counts(s3) == want, counts(s3))
    for x in (s, s2, s3):
        x.close()


def reconcile_extras():
    print("== the reconcile keeps the earliest start and the latest line ==")
    s = fresh_store()
    s._append(sample())
    session_ingest.reconcile_sessions(s)
    with s.sessions_db.begin() as c:       # a row that starts late and ends early, as an older image leaves it
        c.execute(sessions.update().where(sessions.c.session == "s1").values(
            started_at=T0 + timedelta(hours=1), last_line_at=T0 - timedelta(hours=1)))
    session_ingest.reconcile_sessions(s)
    r = rows(s)["s1"]
    ck("started_at goes back to DuckDB's first line (least)", r[3] == T0, r[3])
    ck("last_line_at goes forward to DuckDB's last line (greatest)", r[4] == T0 + timedelta(seconds=6), r[4])
    s.close()


def start_output():
    print("== the catch-up line, and the daemon starting it ==")
    s = fresh_store()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s.start_session_reconcile().join(30)
    ck("a start with nothing to catch up on prints nothing", buf.getvalue() == "", buf.getvalue())
    s._append(sample())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s.start_session_reconcile().join(30)
    ck("a start that caught up sessions says so", "caught up for 3 session(s)" in buf.getvalue(), buf.getvalue())
    broken = session_ingest.reconcile_sessions
    session_ingest.reconcile_sessions = lambda store, chunk=0: (_ for _ in ()).throw(RuntimeError("down"))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            s.start_session_reconcile().join(30)
    finally:
        session_ingest.reconcile_sessions = broken
    ck("a failing catch-up says so", "catch-up failed (RuntimeError: down)" in buf.getvalue(), buf.getvalue())
    s.close()
    # make_app starts it: a process of its own, the daemon module reads its paths at import
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = tempfile.mkdtemp()
    code = ("import os\n"
            "from tares.store import Store\n"
            "calls = []\n"
            "orig = Store.start_session_reconcile\n"
            "Store.start_session_reconcile = lambda self: (calls.append(1), orig(self))[1]\n"
            "from tares import daemon\n"
            "daemon.make_app()\n"
            "print('CALLS', len(calls), flush=True)\n"
            "os._exit(0)\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": d, "PYTHONPATH": root,
           "TARES_DB": os.path.join(d, "t.duckdb"), "TARES_CATALOG": os.path.join(d, "none.yaml"),
           "TARES_OTLP_GRPC_PORT": "off"}
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=d, capture_output=True, text=True,
                         timeout=120)
    ck("make_app starts the catch-up once", "CALLS 1" in out.stdout, (out.stdout[-300:], out.stderr[-300:]))


def coordination_text():
    print("== coordination_in: the text hint and the payload ==")
    pay = {"message": {"content": [{"type": "tool_use", "name": "mcp__plugin_tares_tares__get_ticket"},
                                   {"type": "tool_use", "name": "Read"}]}}
    f = session_ingest.coordination_in
    ck("a hinted text with a coordination tool in the payload counts it", f("→ mcp__plugin_tares_tares__get_ticket(x)", pay) == 1)
    ck("the payload may be a JSON string", f("→ mcp__plugin_tares_tares__get_ticket(x)", json_dumps(pay)) == 1)
    ck("a text without a hint counts nothing, whatever the payload says", f("→ Read(x)", pay) == 0)
    ck("no text counts nothing", f(None, pay) == 0 and f("", pay) == 0)
    ck("a payload that is not JSON counts nothing", f("→ Skill(x)", "{not json") == 0)
    ck("a payload that is not an object counts nothing", f("→ Skill(x)", [1]) == 0)
    ck("each coordination block counts once", f("→ Skill(x)", {"message": {"content": [
        {"type": "tool_use", "name": "Skill"}, {"type": "tool_use", "name": "CronList"}]}}) == 2)


def json_dumps(o):
    import json
    return json.dumps(o)


def purge():
    print("== purge_events drops the source's sessions rows ==")
    s = fresh_store()
    s.upsert_catalog_source("laptop", "claude_code", "claude_code", "", {})
    batch = [line("a", 0, source="laptop"), line("a", 1, tool="Skill", source="laptop"), line("a", 2, source="laptop")]
    s.append(batch + [line("keep", 0, tool="Skill")])
    ck("the lines are counted", counts(s)["a"] == (3, 1, 1), counts(s))
    n = s.purge_events("laptop")
    ck("purge_events returns the events it removed", n == 3, n)
    ck("the purged source's row is gone", "a" not in counts(s), counts(s))
    ck("another source's row stays", counts(s).get("keep") == (1, 1, 1), counts(s))
    s.append(batch)
    ck("the same lines shipped again are counted once", counts(s)["a"] == (3, 1, 1), counts(s))
    session_ingest.reconcile_sessions(s)
    ck("and the reconcile agrees", counts(s)["a"] == (3, 1, 1), counts(s))
    s.close()


def lock_orders_record_and_reconcile():
    print("== a reconcile does not run between a commit and its record ==")
    s = fresh_store()
    committed, release = threading.Event(), threading.Event()
    real = session_ingest.record

    def slow_record(store, envs):
        committed.set()                      # the lines are in DuckDB, not yet counted
        release.wait(30)
        real(store, envs)

    session_ingest.record = slow_record
    errors = []
    try:
        a = threading.Thread(target=lambda: s.append([line("x", i) for i in range(3)]))
        a.start()
        ck("the batch reached the commit", committed.wait(30))

        def run_reconcile():
            try:
                session_ingest.reconcile_sessions(s)
            except Exception as e:
                errors.append(e)

        b = threading.Thread(target=run_reconcile)
        b.start()
        b.join(1.0)                          # with the lock, the reconcile waits; without it, it is done
        release.set()
        a.join(30)
        b.join(30)
    finally:
        session_ingest.record = real
        release.set()
    with s._lock:
        truth = s.con.execute("SELECT count(*) FROM events WHERE key_value = 'x'").fetchone()[0]
    ck("neither writer failed", not errors, errors[:1])
    ck("the row holds DuckDB's count, not a line twice", counts(s).get("x", (None,))[0] == truth == 3,
       (counts(s), truth))
    s.close()


def concurrent():
    print("== four threads record at once ==")
    s = fresh_store()
    per = 25
    errors = []

    def work(k):
        try:
            for i in range(per):
                session_ingest.record(s, [line("shared", i, tool="Skill" if i % 5 == 0 else None),
                                          line(f"own{k}", i)])
        except Exception as e:
            errors.append(e)

    ths = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    got = counts(s)
    ck("no thread failed", not errors, errors[:1])
    ck("the shared session lost no line, tool call or coordination call",
       got["shared"] == (4 * per, 4 * 5, 4 * 5), got["shared"])
    ck("each own session holds its lines", all(got[f"own{k}"] == (per, 0, 0) for k in range(4)), got)
    s.close()


def failure_is_quiet():
    print("== a failing record never fails the append ==")
    s = fresh_store()
    before = []
    orig = metrics.session_ingest
    metrics.session_ingest = lambda seconds, ok=True: before.append(ok)
    broken = s.sessions_db.begin
    s.sessions_db.begin = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        s.append([line("f", 0), line("f", 1)])
        ok_append = True
    except Exception as e:
        ok_append = False
    s.sessions_db.begin = broken
    metrics.session_ingest = orig
    ck("append returns", ok_append)
    ck("the failure is counted", before == [False], before)
    with s._lock:
        n = s.con.execute("SELECT count(*) FROM events WHERE key_value = 'f'").fetchone()[0]
    ck("the lines are in DuckDB", n == 2, n)
    session_ingest.reconcile_sessions(s)
    ck("and the reconcile counts them", counts(s).get("f") == (2, 0, 0), counts(s))
    s.close()


def revision():
    print("== revision 0003_sessions ==")
    d = tempfile.mkdtemp()
    schema = None
    dsn = None
    if PG:
        import psycopg
        schema = "ss_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        names = inspect(sdb.engine).get_table_names()
        ck("both tables exist", "sessions" in names and "session_ingest_marks" in names, names)
        cols = {c["name"]: c for c in inspect(sdb.engine).get_columns("sessions")}
        ck("sessions has its ten columns", sorted(cols) == sorted(
            ["session", "source", "started_at", "last_line_at", "line_count", "tool_calls",
             "coordination_calls", "lines_complete", "updated_at", "lines_origin"]), sorted(cols))
        ck("its counters are not null", all(not cols[c]["nullable"] for c in
                                            ("line_count", "tool_calls", "coordination_calls", "lines_complete")))
        ck("last_line_at has an index", any(i["column_names"] == ["last_line_at"]
                                            for i in inspect(sdb.engine).get_indexes("sessions")))
        with sdb.begin() as c:
            c.execute(text("INSERT INTO sessions (session) VALUES ('x')"))
            r = c.execute(select(sessions)).one()
        ck("a row made with only its key gets zero counters and lines_complete false",
           (r.line_count, r.tool_calls, r.coordination_calls, r.lines_complete) == (0, 0, 0, False), tuple(r))
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0002_session_tables")
        names = inspect(sdb.engine).get_table_names()
        ck("a downgrade removes both tables", "sessions" not in names and "session_ingest_marks" not in names, names)
        ck("and keeps the older ones", "session_stations" in names, names)
        # the stamped case: the tables are already there (made by hand) while the version is behind
        with sdb.begin() as c:
            metadata.tables["sessions"].create(c)
            metadata.tables["session_ingest_marks"].create(c)
            c.execute(text("INSERT INTO sessions (session, line_count) VALUES ('kept', 3)"))
        sdb.upgrade()
        with sdb.begin() as c:
            v = [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]
            kept = c.execute(select(sessions.c.line_count).where(sessions.c.session == "kept")).scalar()
        ck("an upgrade over tables that already exist stamps the revision and keeps the rows",
           v == ["0018_integration_tables"] and kept == 3, (v, kept))
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


def main():
    for fn in (through_append, parity, catalog_source, reconcile, reconcile_extras, start_output,
               coordination_text, purge, lock_orders_record_and_reconcile, concurrent, failure_is_quiet,
               revision):
        fn()
    if FAILS:
        print("FAIL", ", ".join(FAILS))
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
