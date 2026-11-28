"""A person's turns, a session's last words and the files it touched are written when a line
arrives, and the readers read them (TR-1505).

`FactoryStore.person_turns`, `Store.said_lately`, `Store.last_said` and `Store.files_touched` used to
scan the events table on every call. They now read `session_turns`, `session_said` and
`session_files`, which `session_ingest.record` writes as each line is stored and `fill_turns` writes
for the lines a first start has not seen.

The old readers are copied below as the reference. The same lines go through `Store.append`, and the
new readers must return what the reference returns from the events: every filter of a person's turn,
the person label, the last three things said, the files in the order they were last touched.

Plain:                python tests/test_session_turns.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_turns.py
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alembic import command  # noqa: E402
from sqlalchemy import delete, func, inspect, insert, select, text  # noqa: E402

from tares import session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import (metadata, session_commands,  # noqa: E402
                                        session_files, session_ingest_marks, session_said,
                                        session_turns, sessions)
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
SRC = "claude_code"
HEAD = "0026_drop_catalog_sources"


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def at(sec):
    return T0 + timedelta(seconds=sec)


_N = [0]


def nxt():
    """A second nobody used yet: every line of the data has its own time, so the order is exact."""
    _N[0] += 1
    return _N[0]


def line(session, etype, txt, payload=None, labels=None, sec=None, source=SRC):
    return Envelope(source=source, source_type="agent_session", key_value=session,
                    event_type=etype, text=txt, event_time=at(sec if sec is not None else nxt()),
                    payload=payload if payload is not None else {}, labels=labels or {})


def user(session, txt, uid="auto", payload=None, labels=None, sec=None):
    p = dict(payload or {})
    if uid == "auto":
        uid = "u-" + uuid.uuid4().hex[:8]
    if uid:
        p["uuid"] = uid
    p.setdefault("message", {"content": [{"type": "text", "text": txt}]})
    return line(session, "user", txt, p, labels, sec)


def said(session, txt, uid="auto", sec=None):
    p = {"message": {"content": [{"type": "text", "text": txt}]}}
    if uid == "auto":
        uid = "a-" + uuid.uuid4().hex[:8]
    if uid:
        p["uuid"] = uid
    return line(session, "assistant", txt, p, None, sec)


def use(session, name, inp, sec=None, blocks=None):
    b = blocks if blocks is not None else [{"type": "tool_use", "id": "t-" + uuid.uuid4().hex[:8],
                                            "name": name, "input": inp}]
    return line(session, "tool_use", f"→ {name}({json.dumps(inp)[:60]})",
                {"message": {"content": b}}, None, sec)


def edit(session, path, name="Edit", sec=None):
    key = "notebook_path" if name == "NotebookEdit" else "file_path"
    return use(session, name, {key: path}, sec)


def result(session, sec=None):
    return line(session, "tool_result", "[tool_result] x",
                {"message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "y"}]}},
                None, sec)


def resumed(lines, new):
    """The lines of an old session copied under a new session id: same payloads, same times."""
    out = []
    for e in lines:
        c = copy.copy(e)
        c.key_value = new
        out.append(c)
    return out


# ── the old readers, copied from before TR-1505, as the reference ────────────────────────────
def ref_person_turns(store, source, sessions_, since, person=None):
    q = ("SELECT key_value, event_time, text FROM events WHERE source = ? AND "
         "event_type = 'user' AND event_time >= ? AND "
         "coalesce(json_extract_string(labels, '$.sidechain'), 'false') <> 'true'")
    q += (" AND coalesce(json_extract_string(labels, '$.meta'), 'false') <> 'true'"
          " AND coalesce(json_extract_string(payload, '$.isMeta'), 'false') <> 'true'"
          " AND coalesce(json_extract_string(payload, '$.isCompactSummary'), 'false') <> 'true'"
          " AND coalesce(json_extract_string(payload, '$.origin.kind'), 'human') = 'human'")
    vals = [source, since]
    if person:
        who = "json_extract_string(labels, '$.tares_person')"
        q += f" AND ({who} = ?" + (f" OR {who} IS NULL" if person == "local" else "") + ")"
        vals.append(person)
    if sessions_ is not None:
        if not sessions_:
            return []
        q += f" AND key_value IN ({', '.join('?' for _ in sessions_)})"
        vals += sessions_
    with store._lock:
        rows = store.con.execute(q + " ORDER BY event_time DESC LIMIT 2000", vals).fetchall()
    return [{"session": r[0], "at": r[1], "text": r[2] or ""} for r in rows]


def ref_said_lately(store, source, session, n=3):
    with store._lock:
        rows = store.con.execute(
            "SELECT text, event_time FROM events WHERE source = ? AND key_value = ? AND "
            "event_type = 'assistant' ORDER BY event_time DESC LIMIT 60",
            [source, session]).fetchall()
    out = []
    for t, a in rows:
        t = (t or "").strip()
        if t and not t.startswith("→"):
            out.append({"text": t, "at": a})
        if len(out) >= n:
            break
    return list(reversed(out))


def ref_files_touched(store, source, session, limit=20):
    with store._lock:
        rows = store.con.execute(
            "SELECT payload FROM events WHERE source = ? AND key_value = ? AND event_type = "
            "'tool_use' ORDER BY event_time DESC LIMIT 400", [source, session]).fetchall()
    seen = []
    for (pj,) in rows:
        try:
            o = json.loads(pj) if pj else {}
        except (TypeError, ValueError):
            continue
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        for b in msg.get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if str(b.get("name") or "") not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            path = str(inp.get("file_path") or inp.get("notebook_path") or "")
            if path and path not in seen:
                seen.append(path)
        if len(seen) >= limit:
            break
    return seen


def ref_last_said(store, source, session):
    with store._lock:
        rows = store.con.execute(
            "SELECT text FROM events WHERE source = ? AND key_value = ? AND event_type = "
            "'assistant' ORDER BY event_time DESC LIMIT 20", [source, session]).fetchall()
    for (t,) in rows:
        t = (t or "").strip()
        if t and not t.startswith("→"):
            return t
    return None


def norm(rows):
    """The old read ordered by time only, so two turns at one second came in any order: a resumed
    session shares its old lines' times. The new one breaks the tie by session, newest key first."""
    return sorted(rows, key=lambda r: (r["at"], r["session"], r["text"]), reverse=True)


def dedupe(rows):
    out = []
    for r in rows:
        if r not in out:
            out.append(r)
    return out


# ── the data ────────────────────────────────────────────────────────────────────────────────
ALICE = {"tares_person": "alice"}
LONG_TURN = "a long paste " + "x" * 5000

USERS_P = [
    user("sP", "typed by alice", labels=ALICE),
    user("sP", "from a subagent", labels={**ALICE, "sidechain": "true"}),
    user("sP", "a sidechain flagged false", labels={**ALICE, "sidechain": "false"}),
    user("sP", "meta label", labels={**ALICE, "meta": "true"}),
    user("sP", "meta label as a JSON bool", labels={**ALICE, "meta": True}),
    user("sP", "is meta", payload={"isMeta": True}, labels=ALICE),
    user("sP", "is meta as text", payload={"isMeta": "true"}, labels=ALICE),
    user("sP", "is not meta", payload={"isMeta": False}, labels=ALICE),
    user("sP", "a compaction summary", payload={"isCompactSummary": True}, labels=ALICE),
    user("sP", "a task notification", payload={"origin": {"kind": "task-notification"}}, labels=ALICE),
    user("sP", "typed, origin human", payload={"origin": {"kind": "human"}}, labels=ALICE),
    user("sP", "an origin with no kind", payload={"origin": {"other": 1}}, labels=ALICE),
    user("sP", "an origin that is a string", payload={"origin": "peer"}, labels=ALICE),
    user("sP", LONG_TURN, labels=ALICE),
    user("sP", "", labels=ALICE),                                  # an empty turn
    result("sP"),
    said("sP", "an assistant reply"),
]
USERS_OTHER = [
    user("sL", "typed with no person label"),
    user("sB", "typed by bob", labels={"tares_person": "bob"}),
    user("sB", "bob again", labels={"tares_person": "bob", "sidechain": "false"}),
    user("sNone", "only assistant lines here follow"),
    user("sNU", "x", labels={"sidechain": "true"}),                # a session with no turn of a person
    user("sNoId", "no uuid in the payload", uid=None, labels=ALICE),
    user("sNoId", "an empty uuid", uid="", labels=ALICE),
]
RESUMED_P = resumed([e for e in USERS_P if e.event_type == "user"][:3], "sR")

# a session whose newest assistant lines are tool calls and blanks
SAID_A = ([said("sA", f"said {i}") for i in range(1, 6)]
          + [said("sA", "  padded  "), said("sA", ""), said("sA", "   \n "), said("sA", "→ Read(x)"),
             said("sA", "→ Bash(y)")])
SAID_B = [said("sB2", "first words"), said("sB2", "→ Edit(a)"), said("sB2", "→ Write(b)")]
SAID_C = [said("sC", "→ Read(only)"), said("sC", "")]
SAID_D = [said("sD", "one"), said("sD", "two")]
SAID_BIG = [said("sBig", f"big {i}") for i in range(70)] + [said("sBig", "→ tool")] * 3

FILES_F = [edit("sF", "/r/a.py"), edit("sF", "/r/b.py", "Write"), edit("sF", "/r/a.py"),
           edit("sF", "/r/n.ipynb", "NotebookEdit"), edit("sF", "/r/m.py", "MultiEdit"),
           use("sF", "Bash", {"command": "ls"}), use("sF", "Read", {"file_path": "/r/read.py"}),
           use("sF", "Edit", {"file_path": ""}), use("sF", "Edit", {}),
           use("sF", "Edit", {"file_path": "/r/c.py"}),
           # one line with two blocks: each is a file
           use("sF", "Edit", {}, blocks=[
               {"type": "tool_use", "id": "x1", "name": "Edit", "input": {"file_path": "/r/two1.py"}},
               {"type": "tool_use", "id": "x2", "name": "Write", "input": {"file_path": "/r/two2.py"}}])]
FILES_MANY = [edit("sMany", f"/r/f{i:02d}.py") for i in range(30)]


def batches():
    return [USERS_P + USERS_OTHER + SAID_A, SAID_B + SAID_C + SAID_D + FILES_F,
            RESUMED_P + SAID_BIG + FILES_MANY + [
                # a line of another source is not recorded and is not read
                line("sO", "user", "other source", {"uuid": "o1"}, source="other")]]


def open_store(d, record=True):
    s = Store(os.path.join(d, "t.duckdb"))
    return s


def dump(s):
    with s.sessions_db.begin() as c:
        return ([tuple(r) for r in c.execute(select(session_turns).order_by(
                    session_turns.c.session, session_turns.c.line_key)).fetchall()],
                [tuple(r) for r in c.execute(select(session_said).order_by(
                    session_said.c.session, session_said.c.line_key)).fetchall()],
                [tuple(r) for r in c.execute(select(session_files).order_by(
                    session_files.c.session, session_files.c.path)).fetchall()])


def count(s, table):
    with s.sessions_db.begin() as c:
        return c.execute(select(func.count()).select_from(table)).scalar()


def compare(s, label):
    """Every reader against its reference, for the cases the data holds."""
    since0 = at(0)
    allsess = ["sP", "sL", "sB", "sNone", "sNU", "sNoId", "sR", "sA", "sB2", "sC", "sD", "sBig",
               "sF", "sMany", "sO"]
    ok = True
    cases = [("all, every person", None, since0, None), ("a session list", ["sP", "sB"], since0, None),
             ("sessions [a]", ["sP"], since0, None),
             ("alice", None, since0, "alice"), ("bob", None, since0, "bob"),
             ("local", None, since0, "local"), ("someone", None, since0, "someone"),
             ("local in a list", ["sL", "sP"], since0, "local"),
             ("alice in a list", ["sR", "sNoId"], since0, "alice")]
    for name, ss, since, person in cases:
        ref = ref_person_turns(s, SRC, ss, since, person)
        got = s.person_turns(SRC, ss, since, person)
        same = norm(got) == norm(ref) and got == sorted(got, key=lambda r: r["at"], reverse=True)
        ck(f"{label} person_turns: {name} ({len(ref)} turns)", same, (got[:3], ref[:3]))
        ok = ok and same
    ck(f"{label} sessions=[] gives nothing", s.person_turns(SRC, [], since0) == [])
    ck(f"{label} another source is read apart", s.person_turns("other", None, since0) == [])
    ref_all = ref_person_turns(s, SRC, None, since0, None)
    cut = ref_all[len(ref_all) // 2]["at"]
    ck(f"{label} the since boundary includes a turn at that second",
       norm(s.person_turns(SRC, None, cut)) == norm(ref_person_turns(s, SRC, None, cut))
       and s.person_turns(SRC, None, cut)[-1]["at"] == cut)
    ck(f"{label} and leaves out one a second older",
       norm(s.person_turns(SRC, None, cut + timedelta(seconds=1))) == norm(ref_person_turns(
           s, SRC, None, cut + timedelta(seconds=1))))
    ck(f"{label} nothing since the future", s.person_turns(SRC, None, at(10 ** 6)) == [])
    for sid in allsess:
        for n in (1, 2, 3):
            if s.said_lately(SRC, sid, n) != ref_said_lately(s, SRC, sid, n):
                ok = False
                ck(f"{label} said_lately {sid} n={n}", False, (s.said_lately(SRC, sid, n),
                                                             ref_said_lately(s, SRC, sid, n)))
        if s.last_said(SRC, sid) != ref_last_said(s, SRC, sid):
            ok = False
            ck(f"{label} last_said {sid}", False, (s.last_said(SRC, sid), ref_last_said(s, SRC, sid)))
        for lim in (20, 3, 1):
            if s.files_touched(SRC, sid, lim) != ref_files_touched(s, SRC, sid, lim)[:lim]:
                ok = False
                ck(f"{label} files_touched {sid} limit {lim}", False, (
                    s.files_touched(SRC, sid, lim), ref_files_touched(s, SRC, sid, lim)[:lim]))
    ck(f"{label} said_lately, last_said and files_touched equal the old readers for every session", ok)


def revision():
    print("== revision 0005_session_turns ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "st_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        insp = inspect(sdb.engine)
        new = {"session_turns", "session_said", "session_files"}
        ck("the three tables exist", new <= set(insp.get_table_names()))
        cols = lambda t: sorted(c["name"] for c in insp.get_columns(t))  # noqa: E731
        ck("session_turns columns", cols("session_turns") == sorted(
            ["session", "line_key", "source", "at", "person", "text"]))
        ck("session_said columns", cols("session_said") == sorted(
            ["session", "line_key", "source", "at", "text"]))
        ck("session_files columns", cols("session_files") == sorted(
            ["session", "path", "source", "last_at"]))
        pk = lambda t: insp.get_pk_constraint(t)["constrained_columns"]  # noqa: E731
        ck("the keys", pk("session_turns") == ["session", "line_key"]
           and pk("session_said") == ["session", "line_key"] and pk("session_files") == ["session", "path"])
        ix = lambda t: sorted(tuple(i["column_names"]) for i in insp.get_indexes(t))  # noqa: E731
        ck("the indexes", ix("session_turns") == [("at",), ("session", "at")]
           and ix("session_said") == [("session", "at")]
           and ix("session_files") == [("session", "last_at")], (ix("session_turns"), ix("session_said"), ix("session_files")))
        with sdb.begin() as c:
            c.execute(text("INSERT INTO session_files (session, path) VALUES ('k', 'x')"))
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0004_session_facts")
        names = inspect(sdb.engine).get_table_names()
        ck("a downgrade removes the three tables and keeps the facts tables",
           not (new & set(names)) and {"session_messages", "session_commands", "sessions"} <= set(names), names)
        sdb.upgrade()
        ck("an upgrade after it builds them again", new <= set(inspect(sdb.engine).get_table_names()))
        sdb.upgrade()
        ck("an upgrade at the head again changes nothing", new <= set(inspect(sdb.engine).get_table_names()))
        # the stamped case: tables made by hand while the version is behind
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0004_session_facts")
        with sdb.begin() as c:
            for t in ("session_turns", "session_said", "session_files"):
                metadata.tables[t].create(c)
            c.execute(text("INSERT INTO session_files (session, path) VALUES ('kept', 'p')"))
        sdb.upgrade()
        with sdb.begin() as c:
            v = [x[0] for x in c.execute(text("SELECT version_num FROM session_store_version")).all()]
            kept = c.execute(select(session_files.c.path).where(session_files.c.session == "kept")).scalar()
        ck("an upgrade over tables that already exist stamps the revision and keeps the rows",
           v == [HEAD] and kept == "p", (v, kept))
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


def extraction():
    print("== the extraction rules ==")
    jx = session_ingest._jx
    ck("json_extract_string: bool, string, number, null, missing, a step into a string",
       (jx({"a": True}, "a"), jx({"a": False}, "a"), jx({"a": "x"}, "a"), jx({"a": 3}, "a"),
        jx({"a": None}, "a"), jx({}, "a"), jx({"a": "s"}, "a", "b")) == ("true", "false", "x", "3", None, None, None))
    ck("a line key is the payload's uuid, else the hash of its JSON",
       session_ingest.line_key({"uuid": "u1", "z": 1}) == "u1"
       and session_ingest.line_key({"uuid": "", "a": 1}) == hashlib.sha256(
           json.dumps({"uuid": "", "a": 1}, sort_keys=True).encode()).hexdigest()
       and session_ingest.line_key(json.dumps({"b": 2, "a": 1})) == session_ingest.line_key({"a": 1, "b": 2}))
    rows = session_ingest.turns_of([
        (SRC, "s", at(2), "user", "b", {"uuid": "kb"}, {}), (SRC, "s", at(1), "user", "a", {"uuid": "ka"}, "{}"),
        (SRC, "s", at(3), "user", "a again", {"uuid": "ka"}, {}), (SRC, "s", at(4), "tool_result", "x", {}, {})])
    ck("turns_of keeps a line once, the first, in key order, and reads labels given as text",
       [(r["line_key"], r["text"]) for r in rows["turns"]] == [("ka", "a"), ("kb", "b")], rows["turns"])


def main():
    revision()
    extraction()
    print("== the readers return what the old readers did ==")
    d = tempfile.mkdtemp()
    s = open_store(d)
    for b in batches():
        s.append(b)
    ck("the tables were written as the lines arrived",
       count(s, session_turns) > 0 and count(s, session_said) > 0 and count(s, session_files) > 0)
    compare(s, "live")
    ck("sP typed turns: only the person's own are kept",
       sorted(r["text"] for r in s.person_turns(SRC, ["sP"], at(0)))
       == sorted(["typed by alice", "a sidechain flagged false", "is not meta", "typed, origin human",
                  "an origin with no kind", "an origin that is a string", LONG_TURN, ""]))
    ck("local also owns the turns that carry no person", {r["session"] for r in s.person_turns(
        SRC, None, at(0), "local")} >= {"sL", "sNone"} and not {r["session"] for r in s.person_turns(
            SRC, None, at(0), "local")} & {"sP", "sB"})
    ck("a session with no turn of a person has none", s.person_turns(SRC, ["sNU"], at(0)) == [])
    ck("the last three said by sA are the last non-blank, non-arrow lines",
       [m["text"] for m in s.said_lately(SRC, "sA", 10)] == ["said 4", "said 5", "padded"],
       s.said_lately(SRC, "sA", 10))
    ck("last_said skips the newest arrow lines and blanks", s.last_said(SRC, "sA") == "padded")
    ck("a session that said only tool calls has no last words", s.last_said(SRC, "sC") is None
       and s.said_lately(SRC, "sC") == [])
    ck("an unknown session has none", s.last_said(SRC, "nope") is None and s.files_touched(SRC, "nope") == []
       and s.said_lately(SRC, "nope") == [])
    ck("files: most recent first, a repeat moves up, notebook_path and MultiEdit count, Bash and Read do not",
       s.files_touched(SRC, "sF")[2:] == ["/r/c.py", "/r/m.py", "/r/n.ipynb", "/r/a.py", "/r/b.py"],
       s.files_touched(SRC, "sF"))
    ck("more than 20 files: the limit cuts, the default is 20", len(s.files_touched(SRC, "sMany")) == 20
       and s.files_touched(SRC, "sMany", 5) == [f"/r/f{i:02d}.py" for i in (29, 28, 27, 26, 25)]
       and len(s.files_touched(SRC, "sMany", 50)) == 30)
    ck("the said rows are at most three a session", all(
        n <= 3 for n in [len([r for r in dump(s)[1] if r[0] == sid]) for sid in {r[0] for r in dump(s)[1]}]))

    print("== the readers do not read events ==")
    real = s.con

    class Boom:
        def __getattr__(self, n):
            raise AssertionError("the readers touched DuckDB")
    s.con = Boom()
    try:
        ok = (s.person_turns(SRC, None, at(0))
              and s.said_lately(SRC, "sA") and s.last_said(SRC, "sA") and s.files_touched(SRC, "sF"))
    except AssertionError as e:
        ok = False
        print("   ", e)
    finally:
        s.con = real
    ck("all four answer without a DuckDB query", bool(ok))

    print("== a batch written twice ==")
    before = dump(s)
    for b in batches():
        s.append(b)
    ck("the tables are as they were", dump(s) == before)
    compare_dup = ref_person_turns(s, SRC, None, at(0))
    ck("the reference sees the duplicates and the new reader the lines once",
       len(compare_dup) > len(s.person_turns(SRC, None, at(0)))
       and norm(s.person_turns(SRC, None, at(0))) == norm(dedupe(compare_dup)))
    ck("said and files equal the reference as well (a duplicate adds nothing)",
       all(s.files_touched(SRC, x) == ref_files_touched(s, SRC, x) for x in ("sF", "sMany"))
       and s.said_lately(SRC, "sA") == dedupe(ref_said_lately(s, SRC, "sA", 100))[-3:])

    print("== a file written late with an older time ==")
    s.append([edit("sLate", "/r/late.py", sec=900), edit("sLate", "/r/other.py", sec=905)])
    s.append([edit("sLate", "/r/late.py", sec=800)])
    with s.sessions_db.begin() as c:
        last = c.execute(select(session_files.c.last_at).where(
            session_files.c.session == "sLate", session_files.c.path == "/r/late.py")).scalar()
    ck("an older edit of a file seen later keeps its newest time, and the order is by that time",
       last == at(900) and s.files_touched(SRC, "sLate") == ["/r/other.py", "/r/late.py"],
       (last, s.files_touched(SRC, "sLate")))

    print("== NUL bytes ==")
    s.append([user("sN", "he\x00llo", labels={"tares_person": "ni\x00ck"}), said("sN", "sa\x00id"),
              edit("sN", "/r/a\x00b.py")])
    ck("a NUL is dropped from a turn, its person, a saying and a path, and the lines are kept",
       s.person_turns(SRC, ["sN"], at(0), "nick") == [{"session": "sN", "at": s.person_turns(
           SRC, ["sN"], at(0))[0]["at"], "text": "hello"}]
       and s.last_said(SRC, "sN") == "said" and s.files_touched(SRC, "sN") == ["/r/ab.py"],
       (s.person_turns(SRC, ["sN"], at(0)), s.last_said(SRC, "sN"), s.files_touched(SRC, "sN")))

    print("== a turn failure keeps the counts and the facts ==")
    real_write = session_ingest._write_turns

    def broken(*a, **k):
        raise RuntimeError("turns refused")
    session_ingest._write_turns = broken
    try:
        s.append([user("sX", "lost for now", labels=ALICE),
                  use("sX", "Bash", {"command": "echo x"}, blocks=[
                      {"type": "tool_use", "id": "bx", "name": "Bash", "input": {"command": "echo x"}}])])
    finally:
        session_ingest._write_turns = real_write
    with s.sessions_db.begin() as c:
        n_x = c.execute(select(sessions.c.line_count).where(sessions.c.session == "sX")).scalar()
        f_x = c.execute(select(func.count()).select_from(session_commands).where(
            session_commands.c.tool_use_id == "bx")).scalar()
    ck("the session's count and its command are kept, the turn is not written",
       (n_x, f_x) == (2, 1) and s.person_turns(SRC, ["sX"], at(0)) == [], (n_x, f_x))
    session_ingest.fill_turns(s)
    ck("the next fill writes it", [r["text"] for r in s.person_turns(SRC, ["sX"], at(0))] == ["lost for now"])

    s.con.close()

    print("== the fill ==")
    d2 = tempfile.mkdtemp()
    s2 = open_store(d2)
    real_record = session_ingest.record
    session_ingest.record = lambda *a, **k: None         # the lines are stored and never written
    try:
        for b in batches():
            s2.append(b)
    finally:
        session_ingest.record = real_record
    ck("nothing was written as the lines arrived", dump(s2) == ([], [], []))
    ck("so the readers show nothing", s2.person_turns(SRC, None, at(0)) == [] and s2.last_said(SRC, "sA") is None)
    n = session_ingest.fill_turns(s2)
    ck("a fill looks at every session with a user, assistant or editing line", n >= 12, n)
    compare(s2, "filled")
    ck("it set the watermark", session_ingest._turns_mark(s2) is not None)
    got = dump(s2)
    session_ingest.fill_turns(s2)
    ck("a second fill changes nothing", dump(s2) == got)
    with s2.sessions_db.begin() as c:
        c.execute(delete(session_ingest_marks).where(session_ingest_marks.c.name == session_ingest.TURNS_MARK))
    session_ingest.fill_turns(s2, chunk=1)
    ck("a fill with a small chunk and no watermark changes nothing either", dump(s2) == got)
    s3 = open_store(tempfile.mkdtemp())
    for b in batches():
        s3.append(b)
    ck("the filled rows are the rows live ingest writes", dump(s3) == got)
    s3.con.close()

    print("== a later fill reads only what is new ==")
    real_record = session_ingest.record
    session_ingest.record = lambda *a, **k: None
    try:
        s2.append([user("sNew", "a later turn", labels=ALICE), said("sA", "said after the fill"),
                   edit("sF", "/r/late.py")])
    finally:
        session_ingest.record = real_record
    ck("not there before the fill", s2.person_turns(SRC, ["sNew"], at(0)) == [])
    session_ingest.fill_turns(s2)
    ck("there after it, and equal to the reference", s2.person_turns(SRC, ["sNew"], at(0)) == ref_person_turns(
        s2, SRC, ["sNew"], at(0)) and s2.last_said(SRC, "sA") == "said after the fill"
       and s2.files_touched(SRC, "sF")[0] == "/r/late.py"
       and s2.files_touched(SRC, "sF") == ref_files_touched(s2, SRC, "sF"))
    compare(s2, "after a later fill")

    print("== a fill over rows live ingest already wrote ==")
    s2.append([user("sLive", "live turn", uid="live-1", labels=ALICE), said("sLive", "live words", uid="lw-1"),
               edit("sLive", "/r/live.py")])
    before = dump(s2)
    session_ingest.fill_turns(s2)
    with s2.sessions_db.begin() as c:
        c.execute(delete(session_ingest_marks).where(session_ingest_marks.c.name == session_ingest.TURNS_MARK))
    session_ingest.fill_turns(s2)
    ck("a fill after record changes nothing", dump(s2) == before)
    s2.con.close()

    print("== the said rows are cut in the transaction that adds a newer one ==")
    s4 = open_store(tempfile.mkdtemp())
    s4.append([said("sT", f"w{i}", sec=100 + i) for i in range(5)])
    ck("five lines in one batch keep the newest three",
       [m["text"] for m in s4.said_lately(SRC, "sT")] == ["w2", "w3", "w4"])
    s4.append([said("sT", "w5", sec=105)])
    ck("a sixth later drops the oldest", [m["text"] for m in s4.said_lately(SRC, "sT")] == ["w3", "w4", "w5"])
    s4.append([said("sT", "late arrival of an old line", sec=50)])
    ck("a line older than the kept three is not kept", [m["text"] for m in s4.said_lately(SRC, "sT")] == ["w3", "w4", "w5"]
       and count(s4, session_said) == 3)
    s4.append([edit("sT", "/r/x.py", sec=200), edit("sT", "/r/x.py", sec=150)])
    ck("a file keeps its greatest time, whatever the order of arrival",
       s4.files_touched(SRC, "sT") == ["/r/x.py"] and dump(s4)[2][0][3] == at(200))
    s4.con.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
