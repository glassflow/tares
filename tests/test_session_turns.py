"""A person's turns, a session's last words and the files it touched are written when a line
arrives, and the readers read them (TR-1505).

`FactoryStore.person_turns`, `Store.said_lately`, `Store.last_said` and `Store.files_touched` used to
scan the stored lines on every call. They now read `session_turns`, `session_said` and
`session_files`, which `session_ingest.record_first` writes as each line is stored.

The old readers are copied below as the reference, over the lines the test sent. The same lines go
through `Store.append`, and the new readers must return what the reference returns: every filter of a person's turn,
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
from sqlalchemy import func, inspect, select, text  # noqa: E402

from tares import session_ingest  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.session_store import SessionStore  # noqa: E402
from tares.session_store.tables import (metadata, session_commands,  # noqa: E402
                                        session_files, session_said, session_turns, sessions)
from tares.store import Store  # noqa: E402

PG = os.environ.get("TARES_TEST_PG")
FAILS = []
T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
SRC = "claude_code"
HEAD = "0027_drop_move_bookkeeping"


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


# ── the old readers, from before TR-1505, as the reference, over the lines the test sent ─────
def _jx(obj, *path):
    """SQL's json_extract_string for the values the old filters read: a JSON true or false as
    'true' or 'false', a string as itself, a number as its text, a missing key or null as None."""
    v = obj
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return None
        v = v[k]
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    return json.dumps(v)


def _sent(store, source, session=None, etype=None):
    """The lines sent, newest first: (session, time, type, text, payload, labels)."""
    rows = [(e.key_value, e.event_time, e.event_type, e.text, e.payload if isinstance(e.payload, dict) else {},
             e.labels or {}) for e in store.sent if e.source == source
            and (session is None or e.key_value == session) and (etype is None or e.event_type == etype)]
    return sorted(rows, key=lambda r: r[1], reverse=True)


def ref_person_turns(store, source, sessions_, since, person=None):
    if sessions_ is not None and not sessions_:
        return []
    out = []
    for sid, t, _typ, text_, pay, lab in _sent(store, source, etype="user"):
        if t < since or (sessions_ is not None and sid not in sessions_):
            continue
        if ((_jx(lab, "sidechain") or "false") == "true" or (_jx(lab, "meta") or "false") == "true"
                or (_jx(pay, "isMeta") or "false") == "true"
                or (_jx(pay, "isCompactSummary") or "false") == "true"
                or (_jx(pay, "origin", "kind") or "human") != "human"):
            continue
        who = _jx(lab, "tares_person")
        if person and not (who == person or (person == "local" and who is None)):
            continue
        out.append({"session": sid, "at": t, "text": text_ or ""})
    return out[:2000]


def ref_said_lately(store, source, session, n=3):
    out = []
    for _sid, a, _typ, t, _p, _l in _sent(store, source, session, "assistant")[:60]:
        t = (t or "").strip()
        if t and not t.startswith("→"):
            out.append({"text": t, "at": a})
        if len(out) >= n:
            break
    return list(reversed(out))


def ref_files_touched(store, source, session, limit=20):
    seen = []
    for _sid, _t, _typ, _text, o, _l in _sent(store, source, session, "tool_use")[:400]:
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
    for _sid, _a, _typ, t, _p, _l in _sent(store, source, session, "assistant")[:20]:
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


def open_store(d):
    """A store that keeps the lines it was sent in `s.sent`, for the reference readers."""
    s = Store(os.path.join(d, "t"))
    s.sent = []
    real = s.append

    def append(envs):
        real(envs)
        s.sent.extend(envs)
    s.append = append
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
    with s.sessions_db.begin() as c:
        done = c.execute(select(sessions.c.lines_complete).where(sessions.c.session == "sX")).scalar()
    ck("and the session says its lines are not all kept", done is False, done)

    s.close()

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
    s4.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
