"""The readers of `events` answer the same for transcript lines only the session store holds (TR-1647).

A factory cell with an archive writes `claude_code` lines to the session store alone. The same lines
are given to a platform cell (everything in DuckDB: what every reader answered before) and to factory
cells that hold them in each of the ways they can be held: in the store alone, in the store after the
DuckDB rows are deleted (in this test only), partly in the archive, and in a session a failed write
left incomplete. Every reader of `events` answers each factory cell as it answers the platform one.
Then, through the app: the challenger's `session_ended` trigger fires on a marked session's end, and
the project page lists the session, with no claude_code row in DuckDB.

One difference is expected and left out of the comparison: a line the store holds has no ingest time
of its own, so it is read as the line's own time. The platform cell's rows are set to that. Nothing
else is left out: the counter's last_ingest, the clock of each cell's own write, is checked to be
from this run.

Plain:                python tests/test_session_readers_held.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_readers_held.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-held-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_ARCHIVE_URL"] = "file://" + tempfile.mkdtemp(prefix="tares-held-archive-")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
for _k in ("ANTHROPIC_API_KEY", "TARES_AUTH_TOKEN"):
    os.environ.pop(_k, None)

import httpx  # noqa: E402
from sqlalchemy import delete, func, select, update  # noqa: E402

from tares import session_archive, session_events  # noqa: E402
from tares.config import SourceCfg  # noqa: E402
from tares.connectors.claude_code import ClaudeCodeConnector  # noqa: E402
from tares.envelope import Envelope, now_utc  # noqa: E402
from tares.session_store.tables import session_archive_parts, session_lines, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
HOUR, MONTH = NOW - timedelta(hours=1), NOW - timedelta(days=30)
FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {str(detail)[:700]}"))
    if not ok:
        FAILS.append(name)


@contextlib.contextmanager
def patched(obj, name, value):
    real = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, real)


# ── the lines, as the connector maps them ────────────────────────────────────────────────────
_CONNECTOR = ClaudeCodeConnector(SourceCfg(SRC, "agent_session", SRC, 10.0, {"push": True}), None)
_N = [0]


def line(sid, etype, content=None, ago_s=0, repo="shop", **extra) -> Envelope:
    _N[0] += 1
    o = {"sessionId": sid, "type": etype, "uuid": f"u{_N[0]}", "cwd": f"/w/{repo}", "gitBranch": "main",
         "timestamp": (NOW - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), **extra}
    if content is not None:
        o["message"] = {"role": etype, "content": content}
    return _CONNECTOR._obj_to_envelope(o, True, False)


def old_lines() -> list:
    """A session that went quiet twelve days ago: old enough to be archived."""
    d = 12 * 86400
    return [line("old", "user", "old: build the header", d + 30),
            line("old", "assistant", [{"type": "text", "text": "old: done"}], d + 20),
            line("old", "session_end", ago_s=d + 10, flow="challenger", repo="legacy")]


def first_lines() -> list:
    return [
        line("S", "user", "build the footer", 600, flow="challenger"),
        line("S", "assistant", [{"type": "text", "text": "x" * 700}], 590, flow="challenger"),
        line("S", "challenge_commit", ago_s=580, flow="challenger",
             challenge={"verdict": "pass", "sha": "abc1234", "finding_count": 3, "blocking_count": 1,
                        "round": 1, "duration_seconds": 30, "findings": []}),
        line("T", "user", "no flow on this one", 570, repo="docs"),
        line("T", "assistant", [{"type": "tool_use", "id": "c1", "name": "Bash",
                                 "input": {"command": "ls"}}], 560, repo="docs"),
        line("T", "assistant", [{"type": "text", "text": "tagged"}], 550, repo="docs",
             tares_project="secret"),
        line("H", "user", "a hidden project's session", 540, repo="private"),
    ]


def second_lines() -> list:
    return [
        line("S", "challenge_commit", ago_s=300, flow="challenger",
             challenge={"verdict": "fail", "sha": "def5678", "finding_count": 5, "blocking_count": 2,
                        "round": 2, "duration_seconds": 60, "findings": []}),
        line("S", "session_end", ago_s=200, flow="challenger"),
        line("T", "session_end", ago_s=100, repo="docs"),
        line("H", "session_end", ago_s=90, repo="private", flow="challenger"),
    ]


def other_lines() -> list:
    """Another source's rows: DuckDB's in every edition, read beside the stored lines."""
    return [Envelope(source="web", source_type="webhook", key_value=k, event_type="deploy",
                     text=f"deployed {k}", event_time=NOW - timedelta(seconds=s), payload={"k": k},
                     labels={"repo": k}) for k, s in (("shop", 400), ("docs", 80))]


# ── the cells ────────────────────────────────────────────────────────────────────────────────
def open_store(edition):
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    s.set_setting("edition", edition)
    s.upsert_catalog_source(SRC, "agent_session", SRC, "10s", {"push": True})
    s.upsert_catalog_source("web", "webhook", "webhook", "10s", {})
    s.link_session("uc_hidden", "H")
    return s


def duck(s) -> int:
    with s._lock:
        return s.con.execute("SELECT count(*) FROM events WHERE source = ?", [SRC]).fetchone()[0]


def stored(s) -> int:
    with s.sessions_db.begin() as c:
        return c.execute(select(func.count()).select_from(session_lines)).scalar()


def seed(s, between=lambda: None):
    _N[0] = 0                                   # every cell is given the same lines
    s.append(old_lines())
    s.append(first_lines() + other_lines())
    between()
    s.append(second_lines())


def level(s):
    """A DuckDB row's ingest time set to its line time, as a stored line is read."""
    with s._lock:
        s.con.execute("UPDATE events SET ingest_time = event_time")
    return s


def platform():
    s = open_store("platform")
    seed(s)
    return level(s)


def held_only():
    s = open_store("factory")
    seed(s)
    return s


def deleted():
    s = open_store("factory")
    with patched(s, "_lines_skip_duckdb", lambda: False):      # written to both, as before the gate
        seed(s)
    with s._lock:
        s.con.execute("DELETE FROM events WHERE source = ?", [SRC])
    return s


def archived():
    s = open_store("factory")
    seed(s)
    assert session_archive.archive_pass(s) == 1
    return s


def incomplete():
    """S and T missed a write: their later lines are in DuckDB too, and one of them is not in the
    session store at all."""
    s = open_store("factory")

    def spoil():
        with s.sessions_db.begin() as c:
            c.execute(update(sessions).where(sessions.c.session.in_(["S", "T"])).values(
                lines_complete=False))

    seed(s, spoil)
    with s.sessions_db.begin() as c:
        last = c.execute(select(func.max(session_lines.c.seq)).where(
            session_lines.c.session == "S")).scalar()
        c.execute(delete(session_lines).where(session_lines.c.session == "S",
                                              session_lines.c.seq == last))
    return s


# ── the readers ──────────────────────────────────────────────────────────────────────────────
def obj(v):
    return json.loads(v) if isinstance(v, str) else v


def rows(got) -> list:
    return [(r[0], r[1], r[2], obj(r[3]), *([obj(r[4])] if len(r) > 4 else [])) for r in got]


def entity(got, stamp=True) -> list:
    # `stamp=False`: the counter's last_ingest is the clock of the write, so each cell has its own
    return sorted(((e["value"], e["events"],
                    e["last_ingest"] if stamp else e["last_ingest"] >= NOW) for e in got),
                  key=repr)


ENDED = [{"field": "event_type", "op": "eq", "value": "session_end"},
         {"field": "flow", "op": "eq", "value": "challenger"}]
HIDDEN = {"ids": ["uc_hidden"], "names": ["secret"], "shared": []}


def readers() -> dict:
    srcs = [SRC, "web"]

    def windowed(s, **kw):
        key, since = kw.pop("key", None), kw.pop("since", HOUR)
        return rows(s.read_window([SRC], key, since, **kw))

    def owners(s, **kw):
        found: set = set()
        got = rows(s.read_window([SRC], None, HOUR, owners=found, shared=[], **kw))
        return got, sorted(found)

    return {
        "aggregate: the challenger trigger's count": lambda s: s.aggregate(
            [SRC], None, "count", HOUR, filters=ENDED),
        "aggregate: by two labels": lambda s: s.aggregate(
            [SRC], None, "count", MONTH, group_by=["repo", "key_value"]),
        "aggregate: by a label some lines lack": lambda s: s.aggregate(
            [SRC], None, "count", MONTH, group_by="flow"),
        "aggregate: sum, avg, max of a number label": lambda s: [
            s.aggregate([SRC], "finding_count", a, HOUR) for a in ("sum", "avg", "max", "min", "any")],
        "aggregate: where, until, two sources": lambda s: (
            s.aggregate(srcs, None, "count", HOUR, where={"repo": "shop"}),
            s.aggregate(srcs, None, "count", HOUR, until=NOW - timedelta(seconds=250)),
            s.aggregate(srcs, None, "count", HOUR, where={"key_value": "T"}, group_by="type")),
        "aggregate: filters on key and text": lambda s: (
            s.aggregate([SRC], None, "count", HOUR,
                        filters=[{"field": "key_value", "op": "eq", "value": "S"}]),
            s.aggregate([SRC], None, "count", HOUR,
                        filters=[{"field": "text", "op": "contains", "value": "footer"}]),
            s.aggregate([SRC], None, "count", HOUR,
                        filters=[{"field": "flow", "op": "neq", "value": "challenger"}])),
        "read_window: one session, with payloads": lambda s: windowed(s, key="S", include_payload=True),
        "read_window: the old session, long window": lambda s: windowed(s, key="old", since=MONTH),
        "read_window: by label, capped": lambda s: (
            windowed(s, where={"flow": "challenger"}, cap=3),
            windowed(s, where={"flow": "challenger"}, cap=100, since=MONTH)),
        "read_window: the newest per source": lambda s: rows(s.read_window(srcs, None, HOUR, cap=2)),
        "read_window: filtered": lambda s: windowed(s, filters=ENDED, cap=50),
        "read_window: an unknown session": lambda s: windowed(s, key="nobody"),
        "read_window: hidden project left out": lambda s: owners(s, exclude=HIDDEN, cap=100),
        "read_window: hidden, payloads of shared sources unknown": lambda s: windowed(
            s, cap=100, exclude={"ids": ["uc_hidden"], "names": ["secret"]}),
        "read_window: whose rows these are": lambda s: owners(s, cap=100),
        "newest_key": lambda s: (
            s.newest_key([SRC], ENDED, HOUR), s.newest_key([SRC], None, HOUR),
            s.newest_key([SRC], None, HOUR, exclude=HIDDEN),
            s.newest_key([SRC], [{"field": "repo", "op": "eq", "value": "nowhere"}], HOUR),
            s.newest_key([], None, HOUR)),
        "recent_events: the newest two, and all": lambda s: (
            s.recent_events(SRC, 2), s.recent_events(SRC, 100)),
        "recent_events: every source": lambda s: s.recent_events(None, 7),
        "recent_events: hidden project left out": lambda s: s.recent_events(SRC, 3, exclude=HIDDEN),
        "source_schema": lambda s: (s.source_schema(SRC), s.source_schema(SRC, sample=2)),
        "list_entities: the counter": lambda s: [
            entity(s.list_entities(label, [SRC]), stamp=False) for label in ("repo", "key_value", "flow")],
        "list_entities: the scan": lambda s: [
            entity(s._list_entities_scan(label, [SRC], 50)) for label in ("repo", "key_value", "flow")],
        "list_entities: hidden project left out": lambda s: (
            entity(s.list_entities("repo", [SRC], exclude=HIDDEN)),
            entity(s.list_entities("repo", None, exclude=HIDDEN)),
            entity(s.list_entities("repo", [SRC], exclude={"ids": ["uc_hidden"], "names": ["secret"]}))),
        "ingest gaps and hours": lambda s: (
            s.recent_ingest_gaps(SRC, HOUR), sorted(s.recent_ingest_hours(SRC, HOUR)),
            s.recent_ingest_gaps(SRC, MONTH, 3)),
    }


def parity():
    print("== every reader answers a factory cell as it answers a platform one ==")
    base = platform()
    want = {name: fn(base) for name, fn in readers().items()}
    ck("c933t: the challenger trigger's count over the platform cell",
       want["aggregate: the challenger trigger's count"] == {"S": 1, "H": 1},
       want["aggregate: the challenger trigger's count"])
    empty = [n for n, v in want.items() if not v and n != "read_window: an unknown session"]
    ck("no reader is compared on an empty answer", not empty, empty)
    total = duck(base)
    cells = (("the store alone", held_only), ("the DuckDB rows deleted", deleted),
             ("partly archived", archived), ("two sessions incomplete", incomplete))
    for label, make in cells:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s = level(make())
        if make is archived:
            with s.sessions_db.begin() as c:
                parts = len(c.execute(select(session_archive_parts.c.session)).all())
            ck(f"{label}: the old session's lines left the table", parts == 1 and stored(s) == total - 3,
               (parts, stored(s)))
        elif make is incomplete:
            ck(f"{label}: DuckDB holds the later lines of S and T, the store all but one",
               duck(s) == 3 and stored(s) == total - 1, (duck(s), stored(s), total))
        else:
            ck(f"{label}: DuckDB holds no claude_code row", duck(s) == 0 and stored(s) == total,
               (duck(s), stored(s), total))
        for name, fn in readers().items():
            try:
                got = fn(s)
            except Exception as e:
                got = f"raised {type(e).__name__}: {e}"
            ck(f"{label}: {name}", got == want[name], f"\n    got  {got}\n    want {want[name]}")
        n = s.purge_events(SRC)
        ck(f"{label}: purge counts every line and leaves none",
           n == total and duck(s) == 0 and stored(s) == 0 and s.recent_events(SRC, 5) == [],
           (n, total, duck(s), stored(s)))
        s.close()
    base.close()


def edges():
    print("== nothing stored, and a platform cell ==")
    s = open_store("factory")
    ck("no session row: every reader answers empty",
       s.aggregate([SRC], None, "count", MONTH) == {} and s.read_window([SRC], "S", MONTH) == []
       and s.newest_key([SRC], None, MONTH) is None and s.recent_events(SRC) == []
       and s.source_schema(SRC)["sampled_events"] == 0
       and s._list_entities_scan("repo", [SRC], 10) == [] and s.recent_ingest_gaps(SRC, MONTH) == [])
    s.append([line("E", "user", "hello", 60)])
    with s.sessions_db.begin() as c:
        c.execute(delete(session_lines))
    ck("a session with no line: empty", s.read_window([SRC], "E", MONTH) == []
       and s.aggregate([SRC], None, "count", MONTH) == {} and s.recent_events(SRC) == [])
    s.close()
    # a factory cell on Postgres with no archive keeps writing DuckDB; on SQLite the folder is there
    p = platform()
    with patched(p, "sessions_db", None):
        got = (p.aggregate([SRC], None, "count", HOUR, filters=ENDED), len(p.recent_events(SRC, 100)),
               len(p.read_window([SRC], "S", HOUR, cap=100)))
    ck("a platform cell asks the session store nothing", got == ({"S": 1, "H": 1}, 14, 5), got)
    p.close()


def counted():
    print("== lines with the same type and labels: counted and added up as one row each ==")

    def many(s):
        marked = lambda n, ago: line("S", "challenge_commit", ago_s=ago, flow="challenger",
                                     challenge={"verdict": "pass", "sha": "abc1234", "finding_count": n,
                                                "blocking_count": 1, "round": 1,
                                                "duration_seconds": 30, "findings": []})
        _N[0] = 0
        s.append([marked(5, 300), marked(5, 290), marked(5, 280), marked(2, 270),
                  line("S", "user", "no number on this one", 260, flow="challenger")])
        got = {a: s.aggregate([SRC], "finding_count", a, HOUR) for a in ("sum", "avg", "max", "min", "any")}
        got["count"] = s.aggregate([SRC], None, "count", HOUR)
        got["until"] = s.aggregate([SRC], None, "count", HOUR, until=NOW - timedelta(seconds=285))
        return got

    p, f = open_store("platform"), open_store("factory")
    want, got = many(p), many(f)
    ck("every aggregate answers as over the platform cell", got == want, (got, want))
    ck("the numbers are the lines', not the groups'",
       got["count"] == {"S": 5} and got["sum"] == {"S": 17.0} and got["avg"] == {"S": 4.25}
       and got["until"] == {"S": 2} and type(got["count"]["S"]) is int, got)
    ck("the lines are only in the session store", duck(f) == 0 and stored(f) == 5, (duck(f), stored(f)))
    view = session_events.view(f, [SRC], HOUR, counted=True)
    ck("the store hands over fewer rows than there are lines",
       len(view.rows) < 5 and sum(r["w"] for r in view.rows) == 5, [r["w"] for r in view.rows])
    p.close()
    f.close()


def no_write_lock():
    print("== a held read takes no write lock, so ingest never waits for it ==")
    f = held_only()
    with f.sessions_db.begin() as c:             # an incomplete one, so held_count loads lines
        c.execute(update(sessions).where(sessions.c.session == "S").values(lines_complete=False))
    reads = (("aggregate", lambda: session_events.view(f, [SRC], MONTH, counted=True)),
             ("the newest lines", lambda: session_events.newest(f, [SRC], 10)),
             ("the grouped labels", lambda: session_events.grouped(f, [SRC])),
             ("the held count", lambda: session_events.held_count(f, SRC)))
    with f.sessions_db.begin() as writer:        # a write in progress, as an ingest holds it
        writer.execute(update(sessions).values(updated_at=NOW))
        for name, read in reads:
            t0, failed = time.monotonic(), None
            try:
                read()
            except Exception as e:
                failed = f"{type(e).__name__}: {e}"
            took = time.monotonic() - t0
            ck(f"{name}: answers while a write is open", failed is None and took < 3,
               (round(took, 1), failed))
    f.close()


async def app_checks():
    print("== the challenger's trigger and page, through the app ==")
    from tares.daemon import make_app
    from tares.projects.challenger_workflow import SOURCE, TRIGGER
    from tares.reads import resolve_trigger
    from tares.triggers import eval_triggers
    app = make_app()

    def wire(sid, etype, ago_s, flow="challenger", **extra):
        o = {"sessionId": sid, "type": etype, "cwd": "/home/me/shop", "gitBranch": "pricing",
             "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).strftime(
                 "%Y-%m-%dT%H:%M:%SZ"), **extra}
        if flow:
            o["flow"] = flow
        return json.dumps(o)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            r = await cx.post("/api/sources", json={"name": SOURCE, "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            assert r.status_code < 300, r.text

            async def ingest(*lines):
                r = await cx.post(f"/ingest/{SOURCE}", content="\n".join(lines) + "\n",
                                  headers={"content-type": "application/x-ndjson"})
                assert r.status_code == 202, r.text

            await ingest(wire("s0", "session_flow", 600))
            projects = (await cx.get("/api/projects")).json()["projects"]
            uid = next(p["id"] for p in projects if p["template"] == "challenger_workflow")
            await ingest(wire("s1", "user", 300, message={"role": "user", "content": "a pricing page"}),
                         wire("s1", "session_end", 60), wire("s2", "session_end", 50, flow=None))
            st = app.state
            ck("DuckDB holds no claude_code row", duck(st.store) == 0, duck(st.store))
            await eval_triggers(st.store, st.runtime.catalog, st.runtime.dispatcher, None)
            await asyncio.sleep(0.5)
            ck("challenger_session_ended fired for the marked session",
               st.store.last_fired(TRIGGER, "s1") is not None)
            ck("and not for the unmarked one", st.store.last_fired(TRIGGER, "s2") is None)
            trig = next(t for t in st.runtime.catalog.triggers if t.name == TRIGGER)
            handed = resolve_trigger(st.store, trig, key="s1", window="24h")
            ck("it hands over the session's end line", "session_end" in handed, handed[:300])
            r = await cx.post("/read", json={"selector": {"session": "s1"}, "window": "30d",
                                             "project": uid})
            ck("a read of the session has its transcript", "a pricing page" in r.text, r.text[:300])
            got = (await cx.get(f"/api/projects/{uid}/summary")).json()
            ck("the project page lists the marked sessions",
               {x["session"] for x in got["sessions"]} == {"s0", "s1"}, got["sessions"])
            r = await cx.get(f"/api/sources/{SOURCE}/events")
            ck("the source's recent events show the lines", "a pricing page" in r.text, r.text[:300])


async def main():
    parity()
    edges()
    counted()
    no_write_lock()
    await app_checks()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
