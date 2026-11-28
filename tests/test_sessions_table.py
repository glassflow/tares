"""The sessions table (TR-1471): each claude_code line is counted into its session's row as it is
stored, in the transaction that stores it; nothing is caught up at start.

Plain:                python tests/test_sessions_table.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness \\
        python tests/test_sessions_table.py
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import select  # noqa: E402

from tares import factory, metrics, session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store.tables import sessions  # noqa: E402
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
    return Store(os.path.join(tempfile.mkdtemp(), "t"))


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
    """The counts the rules give for the same lines, worked out here from the lines themselves with
    no reader of the table: {session: (lines, tool calls, coordination calls)}, and
    {session: (first time, last time, lines)}."""
    act, tools, coord = {}, {}, {}
    for e in envs:
        if e.source != "claude_code" or not e.key_value:
            continue
        first, last, n = act.get(e.key_value, (e.event_time, e.event_time, 0))
        act[e.key_value] = (min(first, e.event_time), max(last, e.event_time), n + 1)
        if e.event_type != "tool_use":
            continue
        tools[e.key_value] = tools.get(e.key_value, 0) + 1
        if not any(h in (e.text or "") for h in ("→ mcp__", "→ SendMessage(", "→ ListAgents(", "→ Skill(", "→ Cron")):
            continue
        msg = (e.payload or {}).get("message") or {}
        coord[e.key_value] = coord.get(e.key_value, 0) + sum(
            1 for b in msg.get("content") or []
            if isinstance(b, dict) and b.get("type") == "tool_use" and factory.is_coordination(b.get("name")))
    return {sid: (act[sid][2], tools.get(sid, 0), coord.get(sid, 0)) for sid in act}, act


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
    print("== the counts follow the rules, and the readers answer them ==")
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
    print("== a line of another source is not a session line (TR-1811: claude_code alone) ==")
    s = fresh_store()
    s.append([line("a", 0, source="laptop"), line("b", 0, source="deploys"), line("c", 0)])
    got = counts(s)
    ck("only the claude_code line is counted", "a" not in got and "b" not in got and "c" in got, got)
    s.close()


def start_output():
    print("== the start, and the daemon starting it ==")
    s = fresh_store()
    s.append(sample())
    before = counts(s)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s.start_session_reconcile().join(30)
    ck("a start has nothing to catch up on and prints nothing", buf.getvalue() == "", buf.getvalue())
    ck("and changes no count", counts(s) == before, (counts(s), before))
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
           "TARES_HOME": os.path.join(d, "t"), "TARES_CATALOG": os.path.join(d, "none.yaml"),
           "TARES_OTLP_GRPC_PORT": "off"}
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=d, capture_output=True, text=True,
                         timeout=120)
    ck("make_app starts the pass once", "CALLS 1" in out.stdout, (out.stdout[-300:], out.stderr[-300:]))


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


def concurrent():
    print("== four threads append at once ==")
    s = fresh_store()
    per = 25
    errors = []

    def work(k):
        try:
            for i in range(per):
                s.append([line("shared", i, tool="Skill" if i % 5 == 0 else None), line(f"own{k}", i)])
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


def failure_is_refused():
    print("== a session store that takes no write refuses the batch ==")
    s = fresh_store()
    before = []
    orig = metrics.session_ingest
    metrics.session_ingest = lambda seconds, ok=True: before.append(ok)
    broken = s.sessions_db.begin
    s.sessions_db.begin = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        s.append([line("f", 0), line("f", 1)])
        raised = None
    except Exception as e:
        raised = e
    s.sessions_db.begin = broken
    metrics.session_ingest = orig
    ck("append raises LinesNotStored, so /ingest answers 503 and the sender sends them again",
       isinstance(raised, session_ingest.LinesNotStored), repr(raised))
    ck("the failure is counted", before == [False], before)
    ck("nothing of it is stored", "f" not in counts(s), counts(s))
    s.append([line("f", 0), line("f", 1)])
    ck("sent again, the lines are counted once", counts(s).get("f") == (2, 0, 0), counts(s))
    s.close()


def main():
    for fn in (through_append, parity, catalog_source, start_output, coordination_text, concurrent,
               failure_is_refused):
        fn()
    if FAILS:
        print("FAIL", ", ".join(FAILS))
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
