"""The reads the transcript digest answers (TR-1695) answer as the stored lines do.

`session_digest` keeps each session's line times in memory and answers the counts, the ingest gaps and
hours and the label lists from them. Here the same read is answered twice on one cell, from the digest
and from the stored lines themselves (`raw`, the read as it was before), and the two must be equal:
over many windows, conditions, groupings and values, after every writer of a session's lines, before
and after the digest is warm, with one session id under two claude_code sources, and with the lines
written by a second process on the same Postgres.

Plain:                python tests/test_session_digest.py
With the Postgres harness (every Store keeps its state in Postgres, and the two process case runs):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_session_digest.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_session_readers_held as held  # noqa: E402  (sets the factory environment)
from test_session_readers_held import (  # noqa: E402
    HIDDEN, HOUR, NOW, SRC, ck, duck, level, line, open_store, patched, stored)

from sqlalchemy import func, insert, select, update  # noqa: E402

from tares import (session_archive, session_digest, session_drain, session_events,  # noqa: E402
                   session_ingest, session_move)
from tares.envelope import Envelope  # noqa: E402
from tares.session_store.tables import session_lines, sessions  # noqa: E402
from tares.store import Store  # noqa: E402

OTHER = "claude_code_b"
LONG = NOW - timedelta(days=100)

# ── which reads the digest answered ──────────────────────────────────────────────────────────
USED: Counter = Counter()
BROKE: list = []                                    # a read the digest raised on (store falls back)


def _spy(name, real):
    def read(*args):
        try:
            got = real(*args)
        except session_digest.NotANumber:           # answered: the statement fails on that value
            USED[name, True] += 1
            raise
        except Exception as e:
            BROKE.append(f"{name}: {type(e).__name__}: {e}")
            raise
        USED[name, got is not None] += 1
        return got
    return read


for _name in ("aggregate", "newest_times", "hours"):
    setattr(session_digest, _name, _spy(_name, getattr(session_digest, _name)))


@contextlib.contextmanager
def raw(s):
    """The reads as the stored lines answer them, without the digest."""
    with patched(s, "_from_digest", lambda *a: None), \
            patched(session_digest, "matching", lambda store, conn, born, *a: born):
        yield


def digest(s):
    return session_digest.of(s)


def forget(s):
    """As a process that has just started: nothing kept."""
    s.__dict__.pop("_session_digest", None)
    s.__dict__.pop("_duck_sources", None)


# ── the lines ────────────────────────────────────────────────────────────────────────────────
def at(ago: float) -> str:
    return (NOW - timedelta(seconds=ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def one(sid, etype="assistant", ago=0.0, source=SRC, **extra):
    """A line with its time to the microsecond."""
    e = line(sid, etype, extra.pop("content", f"{etype} of {sid}"), timestamp=at(ago), **extra)
    e.source = source
    return e


def challenge(sid, ago, findings, repo="shop"):
    return one(sid, "challenge_commit", ago, repo=repo, flow="challenger",
               challenge={"verdict": "pass", "sha": "abc1234", "finding_count": findings,
                          "blocking_count": 1, "round": 1, "duration_seconds": 30, "findings": []})


AGES = (5.0, 30.5, 240.25, 299.999999, 360, 599.9995, 3540, 7200, 82800, 90000, 86400 * 8,
        86400 * 29, 86400 * 32, 86400 * 40)
DAY = 86400


def many_lines() -> list:
    """Sessions of every shape: busy now, spread over weeks, quiet for long, with and without the
    labels the conditions look at."""
    out = []
    for n, ago in enumerate(AGES):
        out.append(one("wide", ("user", "assistant", "tool_use", "tool_result")[n % 4], ago,
                       repo=("shop", "docs", "")[n % 3],
                       flow="challenger" if n % 2 else None, isSidechain=n % 5 == 0,
                       agentId=f"a{n % 2}"))
        out.append(one("busy", "assistant", ago / 100, repo="docs"))
    out += [one("old", "user", DAY * 45, repo="legacy"), one("old", "session_end", DAY * 44,
                                                             repo="legacy", flow="challenger")]
    out += [one("S", "user", 700, flow="challenger", repo="shop"), challenge("S", 580, 3),
            challenge("S", 280, 5), challenge("S", 270, 0), challenge("S", 260, 5),
            one("S", "session_end", 200, flow="challenger", repo="shop"),
            one("T", "tool_use", 550, repo="docs", tares_project="secret"),
            one("T", "session_end", 100, repo="docs"),
            one("H", "session_end", 90, repo="private", flow="challenger"),
            one("N", "user", 80, repo=None, gitBranch=None),
            one("12", "assistant", 70, repo="7")]
    return out


def odd_rows(s) -> None:
    """Rows no connector writes but the table can hold: no time, no type, no labels, empty ones."""
    rows = [(None, "assistant", {"repo": "shop"}), (NOW - timedelta(seconds=40), None, {"repo": "shop"}),
            (NOW - timedelta(seconds=41), "assistant", {}), (NOW - timedelta(seconds=42), "user",
                                                            {"repo": ""}),
            (NOW - timedelta(seconds=43), "user", None), (None, None, None),
            (NOW - timedelta(seconds=44), "user", {"repo": "shop", "finding_count": 2.5})]
    with s.sessions_db.begin() as c:
        top = c.execute(select(func.max(session_lines.c.seq)).where(
            session_lines.c.session == "S")).scalar()
        c.execute(insert(session_lines), [
            {"session": "S", "seq": top + i + 1, "at": t, "type": kind,
             "labels": None if labels is None else json.dumps(labels), "payload": "{}"}
            for i, (t, kind, labels) in enumerate(rows)])
        c.execute(update(sessions).where(sessions.c.session == "S").values(
            line_count=sessions.c.line_count + len(rows), updated_at=datetime.now(timezone.utc)))


def cell(old=False, odd=False) -> Store:
    """A cell with the lines; `old`: one that wrote them to DuckDB first, as a platform cell did."""
    s = open_store(old)
    held._N[0] = 0
    s.append(many_lines() + held.other_lines())
    if odd:
        odd_rows(s)
    return s


# ── the reads ────────────────────────────────────────────────────────────────────────────────
SINCE = [NOW - timedelta(seconds=a) for a in (0, 31, 300, 600, 3600, DAY, 30 * DAY, 41 * DAY)] + [
    LONG,
    NOW - timedelta(seconds=299.999999),            # exactly a line's time: it counts
    NOW - timedelta(seconds=299.999998),            # one microsecond later: it does not
    NOW - timedelta(seconds=599.9995)]
UNTIL = [NOW - timedelta(seconds=a) for a in (280, 299.999999, 3600, 30 * DAY)]


def f(field, op, value):
    return {"field": field, "op": op, "value": value}


SHAPES = {
    "count by session": {},
    "by repo": {"group_by": "repo"},
    "by two labels": {"group_by": ["repo", "branch"]},
    "by a label few have": {"group_by": "flow"},
    "by type": {"group_by": "type"},
    "by session and model": {"group_by": ["key_value", "sidechain"]},
    "event_type eq": {"filters": [f("event_type", "eq", "session_end")]},
    "event_type in": {"filters": [f("event_type", "in", ["User", "assistant"])]},
    "event_type neq": {"filters": [f("event_type", "neq", "user")]},
    "label eq": {"filters": [f("flow", "eq", "challenger")]},
    "label neq": {"filters": [f("flow", "neq", "challenger")]},
    "label contains": {"filters": [f("repo", "contains", "HO")]},
    "label empty": {"filters": [f("repo", "eq", "")], "group_by": "repo"},
    "number over": {"filters": [f("finding_count", "gt", 1)]},
    "number at most": {"filters": [f("finding_count", "lte", 3)]},
    "key eq": {"filters": [f("key_value", "eq", "S")]},
    "key neq": {"filters": [f("key_value", "neq", "S")]},
    "source eq": {"filters": [f("source", "eq", SRC)], "group_by": "repo"},
    "text": {"filters": [f("text", "contains", "session_end of")]},
    "where label": {"where": {"repo": "shop"}},
    "where session": {"where": {"key_value": "T"}, "group_by": "type"},
    "where empty": {"where": {"repo": ""}},
    "where number-like": {"where": {"repo": "7"}},
    "two sources": {"sources": [SRC, "web"], "group_by": "repo"},
}
for _agg in ("sum", "avg", "max", "min", "any"):
    SHAPES[f"{_agg} of a number"] = {"field": "finding_count", "agg": _agg}
    SHAPES[f"{_agg} of a number by repo"] = {"field": "finding_count", "agg": _agg,
                                              "group_by": "repo"}


def agg(s, since, until=None, shape=None):
    kw = dict(shape or {})
    return s.aggregate(kw.pop("sources", [SRC]), kw.pop("field", None), kw.pop("agg", "count"),
                       since, until=until, **kw)


def counts(s) -> dict:
    return {f"{name} {i}/{j}": agg(s, since, until, shape)
            for name, shape in SHAPES.items()
            for i, since in enumerate(SINCE)
            for j, until in enumerate([None] + (UNTIL if i % 3 == 0 else []))}


def listing(got) -> list:
    return sorted(((e["value"], e["events"], str(e["last_ingest"])) for e in got), key=repr)


def gaps(s, source, since, *limit) -> list:
    """Seconds between lines. DuckDB turns a time into float seconds one bit off a plain division,
    far below the microsecond the times are kept to, so the answers are compared to 10 microseconds."""
    return [round(g, 5) for g in s.recent_ingest_gaps(source, since, *limit)]


def rhythm(s, source=SRC) -> dict:
    out = {}
    for i, since in enumerate(SINCE):
        out[f"gaps {i}"] = gaps(s, source, since)
        out[f"three gaps {i}"] = gaps(s, source, since, 3)
        out[f"hours {i}"] = sorted(s.recent_ingest_hours(source, since))
    return out


def by_label(s, label, sources, since=None) -> list:
    """The lines counted by one label's values: the read the entity listing made through the digest
    (the listing went with entity counts, TR-1810), as an aggregate grouped by the label."""
    got = s.aggregate(list(sources), None, "count", since or LONG, group_by=label)
    return sorted(got.items(), key=repr)


def labels_read(s) -> dict:
    out = {}
    for label in ("repo", "key_value", "flow", "type", "branch"):
        out[label] = by_label(s, label, [SRC])
    return out


def windows(s) -> dict:
    """The reads that narrow by type or label load only the sessions the digest says may match."""
    out = {}
    for i, since in enumerate((HOUR, NOW - timedelta(days=30), LONG)):
        out[f"flow {i}"] = s.read_window([SRC], None, since, where={"flow": "challenger"}, cap=100)
        out[f"ended {i}"] = s.read_window([SRC], None, since,
                                          filters=[f("event_type", "eq", "session_end")], cap=100)
        out[f"private repo {i}"] = s.read_window([SRC], None, since, where={"repo": "private"},
                                                 exclude=HIDDEN, cap=100)
    return {k: [(r[0], r[1], r[2], str(r[3])) for r in v] for k, v in out.items()}


def everything(s) -> dict:
    return {**counts(s), **rhythm(s), **labels_read(s), **windows(s)}


def quick(s) -> dict:
    """A smaller set, for the cases that change the lines: whole windows, parts of them, values."""
    out = {}
    for name in ("count by session", "by repo", "by type", "sum of a number"):
        for i, since in enumerate((NOW - timedelta(seconds=600), LONG)):
            out[f"{name} {i}"] = agg(s, since, shape=SHAPES[name])
    out["gaps"] = gaps(s, SRC, LONG)
    out["hours"] = sorted(s.recent_ingest_hours(SRC, LONG))
    for label in ("repo", "key_value"):
        out[label] = by_label(s, label, [SRC])
    return out


def same(s, name, read=quick):
    """The read from the digest equals the read from the stored lines. Returns it."""
    got = read(s)
    with raw(s):
        want = read(s)
    wrong = [k for k in want if got[k] != want[k]]
    ck(name, not wrong, f"{len(wrong)} differ, first {wrong[:1]}: got "
                        f"{got[wrong[0]] if wrong else ''} want {want[wrong[0]] if wrong else ''}")
    return got


# ── the cases ────────────────────────────────────────────────────────────────────────────────
def equality():
    print("== from the digest as from the lines ==")
    p, s = level(cell(old=True)), cell()
    ck("the lines are only in the session store", duck(s) == 0 and stored(s) == duck(p),
       (duck(s), stored(s), duck(p)))
    USED.clear()
    got = same(s, "every read, factory cell", everything)
    ck("the digest answered them (all but the text conditions)",
       USED["aggregate", True] > 500 and USED["newest_times", True] and USED["hours", True], USED)
    ck("and they are not empty", sum(1 for v in got.values() if v) > len(got) * 0.5,
       (sum(1 for v in got.values() if v), len(got)))
    want = everything(p)
    level(s)                                    # web's rows: one ingest time for both
    got = everything(s)
    wrong = [k for k in want if got[k] != want[k]
             and not (k.startswith(("repo", "key_value", "flow", "type", "branch")))]
    ck("and as an older cell answers them", not wrong,
       (wrong[:3], got[wrong[0]] if wrong else "", want[wrong[0]] if wrong else ""))
    scan = {k: [e[:2] for e in v] for k, v in labels_read(s).items()}
    base = {k: [e[:2] for e in v] for k, v in labels_read(p).items()}
    ck("label lists name the same values with the same counts as an older cell", scan == base,
       [(k, scan[k], base[k]) for k in scan if scan[k] != base[k]][:1])
    same(s, "a second time, from what is kept", everything)
    p.close()
    s.close()

    s = cell(odd=True)
    same(s, "lines with no time, type or labels, empty and fractional values", everything)
    forget(s)
    same(s, "the same, first read after a start", everything)
    s.close()

    s = cell()
    with s.sessions_db.begin() as c:
        c.execute(update(sessions).where(sessions.c.session.in_(["S", "wide"])).values(
            lines_complete=False))
    same(s, "sessions not marked complete", everything)
    s.close()

    s = cell()
    quick(s)
    assert session_archive.archive_pass(s) >= 1
    same(s, "after the hourly archive moved sessions into parts", everything)
    forget(s)
    same(s, "and read from the parts", everything)
    s.close()


def writers():
    print("== every writer of a session's lines: change, read again ==")

    def after(s, name, change, changed=True):
        before = same(s, f"{name}: before")
        change()
        got = same(s, f"{name}: after")
        ck(f"{name}: the answer {'changed' if changed else 'stayed'}", (got != before) == changed,
           "same as before" if changed else
           [(k, before[k], got[k]) for k in got if got[k] != before[k]][:2])
        return got

    def to_duck(s, *lines):
        s._append_events(list(lines))

    s = cell()
    after(s, "append", lambda: s.append([one("S", ago=3, repo="new")]))
    after(s, "append of a new session", lambda: s.append([one("born_later", ago=2, repo="new")]))
    after(s, "append of an older line", lambda: s.append([one("S", ago=DAY * 3, repo="older")]))

    written = {}
    before = same(s, "take_back: before")
    written["a"] = session_ingest.record_first(s, [one("S", ago=2, repo="gone"),
                                                   one("S", ago=1, repo="gone")])
    taken = after(s, "take_back", lambda: session_ingest.take_back(s, written["a"]))
    ck("take_back: as before the batch", taken == before)
    written["b"] = session_ingest.record_first(s, [one("S", ago=2, repo="first"),
                                                   one("S", ago=1, repo="first")])
    same(s, "take_back, then other lines: the first batch read")

    def swap():
        session_ingest.take_back(s, written["b"])
        session_ingest.record_first(s, [one("S", ago=8, repo="second"),
                                        one("S", ago=7, repo="second")])
    after(s, "take_back, then as many other lines", swap)
    gone = session_ingest.record_first(s, [one("only", ago=2, repo="only")])
    after(s, "take_back of a session's only batch",
          lambda: session_ingest.take_back(s, gone))
    after(s, "and the session born again with as many lines",
          lambda: s.append([one("only", ago=4, repo="again")]))

    def refused():
        real = session_ingest._write_whole

        def refuse(store, envelopes, w):
            if any(e.key_value == "S" for e in envelopes):
                raise RuntimeError("refused")
            return real(store, envelopes, w)
        with patched(session_ingest, "_write_whole", refuse), \
                contextlib.redirect_stdout(io.StringIO()):
            s.append([one("S", ago=1.5, repo="refused")])
    after(s, "a line the store refuses (_write_refused)", refused)

    def counted_without_lines():
        def broken(*a):
            raise RuntimeError("no lines")
        e = one("T", ago=1.25, repo="counted")
        to_duck(s, e)
        with patched(session_ingest, "_line_rows", broken), \
                contextlib.redirect_stdout(io.StringIO()):
            assert not session_ingest.record(s, [e])
    after(s, "counts without their lines (record, _lines_incomplete)", counted_without_lines)

    # the drain: DuckDB has a line of a complete session that the store lacks
    to_duck(s, one("wide", ago=1, repo="drained"))
    sources = sorted(session_ingest.claude_code_sources(s))
    after(s, "the drain rebuilds a session",
          lambda: session_drain._drain_session(s, sources, "wide"))
    after(s, "a drain that failed (_later)",
          lambda: session_drain._later(s, "wide", RuntimeError("no")), changed=False)

    # the reconcile: DuckDB counts more of a session than the store holds
    to_duck(s, *[one("busy", ago=1 + i / 10, repo="reconciled") for i in range(40)])
    after(s, "the reconcile raises the counts and finds the session short",
          lambda: session_ingest._reconcile_chunk(s, sources, ["busy"]))

    to_duck(s, one("12", ago=1, repo="flagged"))
    after(s, "a session marked incomplete (_lines_incomplete)",
          lambda: session_ingest._lines_incomplete(s, {"12": None}))
    after(s, "and marked complete again (repair_table_sessions)",
          lambda: session_ingest.repair_table_sessions(s))

    forget(s)
    quick(s)
    after(s, "the hourly archive job (rows into a part)",
          lambda: session_archive.archive_pass(s), changed=False)
    after(s, "a line for an archived session", lambda: s.append([one("old", ago=6, repo="back")]))
    after(s, "the same sessions shipped again", lambda: s.append(many_lines()))
    s.close()

    # the move: lines only DuckDB holds become a session of the store
    for name, move in (("the move", lambda s: session_move.move_session(s, "S")),
                       ("the move into the archive", lambda s: session_move.move_old_session(
                           s, "old", session_archive.backend_of(s)))):
        s = open_store(old=True)
        held._N[0] = 0
        s.append(many_lines())
        level(s)                                    # a held line's ingest time is its own time
        del s.append                                # from here on, as a cell writes now
        s.append([one("fresh", ago=3)])         # one session born in the table beside them
        with s.sessions_db.begin() as c:
            c.execute(update(sessions).where(sessions.c.session != "fresh").values(
                lines_origin=None, lines_complete=False))
            c.execute(session_lines.delete().where(session_lines.c.session != "fresh"))
        session_move.plan(s)
        def quietly():
            with contextlib.redirect_stdout(io.StringIO()):
                move(s)
        after(s, name, quietly, changed=False)
        with s.sessions_db.read() as c:
            born = set(c.execute(select(sessions.c.session).where(
                sessions.c.lines_origin == "table")).scalars())
        ck(f"{name}: the session is now one of the store's", len(born) == 2, born)
        s.close()


def _duck_rows(s) -> list:
    with s._lock:
        return s.con.execute("SELECT source, key_value FROM events WHERE key_value = 'X'").fetchall()


def cold_and_warm():
    print("== before the digest is warm, while it warms, after ==")
    s = cell()
    forget(s)
    with raw(s):
        want = quick(s)
    ck("nothing is kept before the first read", digest(s).entries == {}, len(digest(s).entries))
    ck("a read before the warm-up answers as the lines do", quick(s) == want)
    forget(s)
    out = io.StringIO()
    during = []
    with contextlib.redirect_stdout(out):
        with patched(session_digest, "WARM_BATCH", 1):
            th = session_digest.start(s)
            for _ in range(5):
                during.append(quick(s))
            th.join(60)
    ck("reads while it warms answer the same", all(g == want for g in during) and not th.is_alive())
    said = [x for x in out.getvalue().splitlines() if "transcript digest warmed" in x]
    ck("one line says how long, how many sessions and lines, and its size",
       len(said) == 1 and all(w in said[0] for w in ("session(s)", "line(s)", "MiB", "s: ")), said)
    forget(s)
    done = session_digest.warm(s)
    with s.sessions_db.read() as c:
        n = c.execute(select(func.count()).select_from(sessions).where(
            sessions.c.lines_origin == "table")).scalar_one()
    ck("the warm-up keeps every session and every line",
       done["sessions"] == n == len(digest(s).entries) and done["lines"] == stored(s), (done, n))
    ck("and after it the answers are the same", quick(s) == want)
    stop = threading.Event()
    stop.set()
    forget(s)
    ck("a warm-up told to stop keeps nothing",
       session_digest.warm(s, stop) is None and digest(s).entries == {})
    s.close()
    ck("closing the store ends the warm-up thread", not th.is_alive())


def bounded():
    print("== bounded: old lines cost no memory ==")
    s = cell()

    def old(n, start):
        s.append([one("old", ago=DAY * 41 + start + i, repo="legacy") for i in range(n)])

    def read():
        agg(s, LONG)
        by_label(s, "repo", [SRC])

    old(200, 0)
    read()
    small, entry = digest(s).bytes, digest(s).entries["old"]
    ck("a session older than the horizon keeps no times", not entry.full and entry.lines == 202,
       (entry.full, entry.lines))
    old(2000, 1000)
    read()
    ck("ten times the old lines: the digest is no bigger",
       digest(s).entries["old"].lines == 2202 and digest(s).bytes <= small + 100,
       (small, digest(s).bytes))
    same(s, "a window that reaches the old lines is exact")
    recent = digest(s).entries["busy"]
    ck("a session inside the horizon keeps its times", recent.full)
    with patched(session_digest, "HORIZON_DAYS", 0), patched(session_digest, "_horizon",
                                                               lambda store: timedelta(seconds=30)):
        before = digest(s).bytes
        session_digest.sweep(s, force=True)
        ck("a session that ages past the horizon loses its times",
           not digest(s).entries["busy"].full or digest(s).entries["busy"].hi
           >= session_digest._micros(datetime.now(timezone.utc) - timedelta(seconds=30)))
        ck("and the digest shrinks", digest(s).bytes < before, (before, digest(s).bytes))
        same(s, "windows older than a short horizon are exact")
    digest(s).horizon = timedelta(days=session_digest.HORIZON_DAYS)
    session_digest.sweep(s, force=True)
    ck("the horizon is HORIZON_DAYS (no trigger widens it since TR-1810)",
       digest(s).horizon == timedelta(days=session_digest.HORIZON_DAYS), digest(s).horizon)
    with patched(session_digest, "MAX_BYTES", 3000):
        forget(s)
        same(s, "past the size limit the reads still answer")
        ck("and no more is kept", digest(s).bytes <= 3000, digest(s).bytes)
    s.close()


@contextlib.contextmanager
def loads():
    """Counts the stored lines a read loads, and the builds of each session's entry."""
    seen = Counter()
    load, publish = session_events.load, session_digest.Digest.publish

    def counted(*args, **kw):
        rows, parts = load(*args, **kw)
        seen["lines"] += len(rows)
        return rows, parts

    def built(self, session, entry, read):
        seen[session] += 1
        publish(self, session, entry, read)

    with patched(session_events, "load", counted), \
            patched(session_digest.Digest, "publish", built):
        yield seen


def answer(read) -> str:
    """A read's answer, or its failure, as text: NaN equals nothing, its text does."""
    try:
        got = read()
    except Exception as e:
        return f"{type(e).__name__}: {str(e).splitlines()[0]}"
    return repr(sorted(got.items(), key=repr) if isinstance(got, dict) else got)


# what one value beside a 5 sums to, as v1.61.0-rc.1 answered (None: it raised)
NO_NUMBER = ["abc", "", True, False, "true", "0x10", "1,5", "12abc", " ", {"a": 1}, [1], [], {}]
NUMBERS = [(None, 5.0), ("1e3", 1005.0), (" 12 ", 17.0), ("\t12\n", 17.0), ("+5", 10.0),
           (".5", 5.5), ("1_000", 1005.0), (7, 12.0), (2.5, 7.5), ("NaN", float("nan")),
           ("nan", float("nan")), ("inf", float("inf")), ("-inf", float("-inf")),
           ("Infinity", float("inf")), ("1e999", float("inf")), ("1e308", 1e308)]


def not_numbers():
    print("== a value that is no number (TR-1706) ==")
    values = NO_NUMBER + [v for v, _ in NUMBERS]
    s = open_store()
    # per value two labels: `in<i>` has it on a line 60 s old, `out<i>` on one two hours old
    good = one("good", ago=30)
    inside, outside = one("recent", ago=60), one("past", ago=7200)
    for i, v in enumerate(values):
        good.labels.update({"g": "g", f"in{i}": 5, f"out{i}": 5})
        inside.labels.update({"g": "g", f"in{i}": v})
        outside.labels.update({"g": "g", f"out{i}": v})
    web = Envelope(source="web", source_type="webhook", key_value="k", event_type="deploy",
                   text="x", event_time=NOW - timedelta(seconds=50), payload={},
                   labels={"g": "g", "web": "abc"})
    s.append([good, inside, outside, web])

    def ask(field, how, sources=(SRC,)):
        return answer(lambda: s.aggregate(list(sources), field, how, HOUR, group_by="g"))

    wrong, loaded = [], 0
    out = io.StringIO()
    for i, v in enumerate(values):
        for field in (f"in{i}", f"out{i}"):
            for how in ("count", "sum", "avg", "max", "min", "any"):
                with raw(s):
                    want = ask(field, how)
                with loads() as seen, contextlib.redirect_stdout(out):
                    got = ask(field, how)
                loaded += seen["lines"]
                if got != want:
                    wrong.append((v, field, how, got, want))
    ck("every value, in the window and outside it: the answer is the lines'", not wrong, wrong[:4])
    ck("and no read loaded a line for it", loaded == 0 and not out.getvalue(),
       (loaded, out.getvalue()[:300]))
    n = len(NO_NUMBER)
    got = [ask(f"in{i}", "sum") for i in range(n)]
    ck("inside the window a text that is no number fails the read",
       all(g.startswith("ConversionException: Conversion Error: Could not convert") for g in got),
       got[:3])
    got = [ask(f"out{i}", "sum") for i in range(n)]
    ck("and outside it changes nothing", got == ["[('g', 5.0)]"] * n, got[:3])
    got = [ask(f"in{n + i}", "sum") for i in range(len(NUMBERS))]
    want = [repr([("g", total)]) for _, total in NUMBERS]
    ck("what DuckDB reads as a number counts as one", got == want,
       [(v, g, w) for (v, _), g, w in zip(NUMBERS, got, want) if g != w])
    with raw(s):
        want = [ask("web", how, (SRC, "web")) for how in ("count", "sum")]
    with loads() as seen:
        got = [ask("web", how, (SRC, "web")) for how in ("count", "sum")]
    ck("a DuckDB row's text fails it the same way, no stored line loaded",
       got == want and "ConversionException" in got[1] and seen["lines"] == 0,
       (got, want, seen["lines"]))
    # a line without the group's label is in no group: the statement never casts its value
    loose = one("loose", ago=60)
    loose.labels.update({f"in{n}": "abc"})
    s.append([loose])
    with raw(s):
        want = ask(f"in{n}", "sum")
    with loads() as seen:
        got = ask(f"in{n}", "sum")
    ck("a bad value on a line in no group changes nothing, as in the lines",
       got == want == repr([("g", 5.0)]) and seen["lines"] == 0, (got, want, seen["lines"]))
    s.close()

    # NaN and the infinities, added up across sessions in either order
    s = open_store()
    groups = {"a": ("nan", 3), "b": (3, "nan"), "c": ("inf", "-inf"), "d": ("inf", 1),
              "e": (1, "-inf"), "f": ("-inf", "nan"), "g": (1e308, 1e308)}
    lines = []
    for group, pair in groups.items():
        for i, v in enumerate(pair):
            e = one(f"{group}{i}", ago=60 + i)
            e.labels.update({"g": group, "n": v})
            lines.append(e)
    s.append(lines)
    for how in ("sum", "avg", "max", "min", "any"):
        with raw(s):
            want = answer(lambda: s.aggregate([SRC], "n", how, HOUR, group_by="g"))
        with loads() as seen:
            got = answer(lambda: s.aggregate([SRC], "n", how, HOUR, group_by="g"))
        ck(f"{how} over NaN and the infinities is the lines'", got == want and seen["lines"] == 0,
           (got, want, seen["lines"]))
    s.close()


def edges():
    print("== the quarter hour `since` cuts, a type in another case (TR-1706) ==")
    s = open_store()
    top = (NOW - timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)

    def ago(minutes):
        return (NOW - top - timedelta(minutes=minutes)).total_seconds()

    caps = one("caps", ago=30)
    caps.event_type = "Assistant"
    s.append([one("q", ago=ago(5)), one("q", ago=ago(125)), caps, one("plain", "user", ago=20)])

    def hours(minutes):
        return sorted(s.recent_ingest_hours(SRC, top + timedelta(minutes=minutes)))

    for minutes, n, what in ((10, 2, "only lines before `since` in its quarter: no such hour"),
                             (4, 3, "a line after `since` in its quarter: that hour")):
        with raw(s):
            want = hours(minutes)
        got = hours(minutes)
        ck(what, got == want and len(got) == n, (got, want))

    for op, value in (("in", ["assistant"]), ("eq", "Assistant")):
        def window():
            return [(str(r[0]), r[2]) for r in s.read_window(
                [SRC], None, NOW - timedelta(seconds=600), cap=100,
                filters=[f("event_type", op, value)])]
        with raw(s):
            want = window()
        got = window()
        ck(f"a stored type in another case is found: event_type {op}",
           got == want and len(got) == 1, (got, want))
    s.close()


def sound(s) -> tuple:
    """The digest's count of itself is what it holds, within its limits, and no share keeps an
    entry that is no longer its session's."""
    d = digest(s)
    queries = list(d.queries.values())
    kept = sum(e.bytes for e in d.entries.values())
    return (d.bytes == kept + d.query_bytes,
            d.query_bytes == sum(q.bytes for q in queries),
            all(q.memo_bytes >= session_digest._MEMO_BYTES * len(q.memo) for q in queries),
            all(q.bytes == q.memo_bytes + sum(v[2] for v in q.sessions.values()) for q in queries),
            all(v[0] is d.entries.get(sid) for q in queries for sid, v in q.sessions.items()),
            kept <= session_digest.MAX_BYTES - session_digest.MAX_BYTES // 4,
            d.query_bytes <= session_digest.MAX_BYTES // 4)


def full_cache():
    print("== at the size limit (TR-1706) ==")
    ok = (True,) * 7
    s = open_store()
    mine = [f"m{i:02d}" for i in range(20)]
    s.append([one(sid, ago=100 + i, repo="shop") for i, sid in enumerate(mine)])
    s.append([one(f"o{i:02d}", ago=100 + i, source=OTHER, repo="shop") for i in range(20)])

    def count(source=SRC, **kw):
        return s.aggregate([source], None, "count", HOUR, **kw)

    def only(*sids):
        return [count(where={"key_value": sid}) for sid in sids]

    count(), count(group_by="repo"), by_label(s, "repo", [SRC])
    q = list(digest(s).queries.values())
    ck("the queries' memos and shares are counted", digest(s).query_bytes > 0
       and any(x.memo for x in q) and any(x.sessions for x in q) and sound(s) == ok, sound(s))
    s.append([one("m00", ago=50, repo="shop")])
    count(where={"key_value": "m00"})                    # one shape reads the new entry
    ck("a replaced entry takes its shares with it", sound(s) == ok, sound(s))
    size = max(e.bytes for e in digest(s).entries.values())
    with patched(session_digest, "MAX_BYTES", size * 8):         # room for about six entries
        forget(s)
        with raw(s):
            want, theirs = count(), count(OTHER)
        ck("a read over more sessions than fit answers as the lines do", count() == want)
        held_now = set(digest(s).entries)
        ck("and keeps what fits", 3 <= len(held_now) < len(mine) and sound(s) == ok,
           (len(held_now), sound(s)))
        with loads() as seen:
            again = count()
        ck("and again: what it kept is not built again, nothing is put out for the rest",
           again == want and set(digest(s).entries) == held_now
           and not any(seen[sid] for sid in held_now), (dict(seen), held_now))
        hot = next(sid for sid in mine if sid not in digest(s).entries)
        with loads() as seen:
            got = [only(hot) for _ in range(6)]
        ck("a session read again and again in a full cache is built once",
           seen[hot] == 1 and got == [[{hot: want[hot]}]] * 6 and hot in digest(s).entries,
           (seen[hot], got[:2]))
        warmest = [hot, sorted(set(digest(s).entries) - {hot})[0]]
        only(*warmest)
        got = count(OTHER)                               # a wide read that takes none of them
        ck("a wide read of other sessions answers, and leaves the ones just read",
           got == theirs and set(warmest) <= set(digest(s).entries) and sound(s) == ok,
           (warmest, sorted(digest(s).entries), sound(s)))
        with loads() as seen:
            only(*warmest)
        ck("which are not built again", not any(seen[sid] for sid in warmest), dict(seen))
        for i in range(30):                              # many shapes
            count(where={"repo": "shop"}, group_by="repo",
                  filters=[f("branch", "neq", f"b{i}")])
            by_label(s, "repo", [SRC])
        ck("many query shapes stay within their room", sound(s) == ok, sound(s))
        forget(s)
        session_digest.warm(s)
        newest = {"m00", "m01"}
        ck("the warm-up stops when full and keeps the newest",
           newest <= set(digest(s).entries) and len(digest(s).entries) < 40 and sound(s) == ok,
           (sorted(digest(s).entries), sound(s)))

        failed, stop = [], threading.Event()

        def reader(n):
            try:
                while not stop.is_set():
                    count(), only(mine[n], hot), count(OTHER)
                    by_label(s, "repo", [SRC])
            except Exception as e:
                failed.append(f"{type(e).__name__}: {e}")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            readers = [threading.Thread(target=reader, args=(n,)) for n in range(4)]
            for th in readers:
                th.start()
            for i in range(30):
                s.append([one(mine[i % 5], ago=40 - i, repo="live")])
                session_digest.sweep(s, force=True)
            stop.set()
            for th in readers:
                th.join(60)
        ck("readers beside an ingest in a full cache: none failed, the count holds",
           not failed and "digest could not answer" not in out.getvalue() and sound(s) == ok,
           (failed[:2], out.getvalue()[:300], sound(s)))
        with raw(s):
            want = count(), count(OTHER), count(group_by="repo")
        ck("and the answers are the lines'",
           (count(), count(OTHER), count(group_by="repo")) == want)
    s.close()


def published_meanwhile():
    print("== an entry published while a read is under way (TR-1706) ==")
    s = cell()
    assert session_archive.archive_pass(s) >= 1
    count = lambda: s.aggregate([SRC], None, "count", LONG, group_by=["key_value"])
    want = count()
    forget(s)
    decoded: Counter = Counter()
    iter_part, finish, first = session_archive.iter_part, session_digest.finish, [True]

    def counted(backend, part):
        decoded[part.key] += 1
        return iter_part(backend, part)

    def beside(store, plan):
        if first.pop() if first else False:      # another read builds them all, after the load
            count()
        return finish(store, plan)

    with patched(session_archive, "iter_part", counted), \
            patched(session_digest, "_old", lambda *_: False), \
            patched(session_digest, "finish", beside):        # kept with their lines' times
        got = count()
    ck("its archive parts are not read a second time",
       got == want and decoded and set(decoded.values()) == {1}, dict(decoded))

    def evicting(store, plan):                       # another read evicts them all, after the load
        forget(store)
        return finish(store, plan)

    s.append([one("old", ago=40 * DAY, repo="shop")])         # its entry is out of date
    decoded.clear()
    with patched(session_archive, "iter_part", counted), \
            patched(session_digest, "_old", lambda *_: False), \
            patched(session_digest, "finish", evicting):
        got = count()
    ck("nor those of an entry evicted after the read held it",
       got != want and not decoded, (dict(decoded), got, want))
    s.close()


def threads():
    print("== reads while lines arrive ==")
    s = cell()
    failed, stop = [], threading.Event()

    def reader():
        try:
            while not stop.is_set():
                agg(s, LONG)
                agg(s, NOW - timedelta(seconds=600), shape=SHAPES["by repo"])
                s.recent_ingest_gaps(SRC, LONG)
                by_label(s, "repo", [SRC])
        except Exception as e:
            failed.append(f"{type(e).__name__}: {e}")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for th in readers:
            th.start()
        for i in range(60):
            s.append([one("S", ago=60 - i, repo="live"), one(f"live{i % 7}", ago=i, repo="live")])
        stop.set()
        for th in readers:
            th.join(60)
    ck("four readers beside an ingest: none failed", not failed
       and "digest could not answer" not in out.getvalue(), (failed[:2], out.getvalue()[:400]))
    same(s, "and the last answer is the lines'")
    s.close()


def two_processes():
    print("== lines written by a second process on the same Postgres ==")
    dsn = os.environ.get("TARES_TEST_PG")
    if not dsn:
        print("  skip  TARES_TEST_PG is not set")
        return
    import duckdb
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    cells = []
    for _ in range(2):
        path = os.path.join(tempfile.mkdtemp(), "t.duckdb")
        duckdb.connect(path).close()
        with open(path + ".pgschema", "w") as note:      # the harness reads the schema from here
            note.write(schema)
        cells.append(Store(path, state_dsn=f"{dsn}?options=-csearch_path%3D{schema}"))
    a, b = cells
    a.append([one("S", ago=300, repo="a")])
    first = agg(a, LONG)
    ck("the first process counts its own line", first == {"S": 1}, first)
    kept = digest(a).entries.get("S")
    b.append([one("S", ago=200, repo="b"), one("theirs", ago=100, repo="b")])
    got = agg(a, LONG)
    ck("and the second one's at its next read", got == {"S": 2, "theirs": 1} and kept is not None,
       (got, kept))
    got = (agg(a, LONG, shape=SHAPES["by repo"]), gaps(a, SRC, LONG),
           by_label(a, "repo", [SRC]))
    ck("in every read", got == ({"a": 1, "b": 2}, [100.0, 100.0], [("a", 1), ("b", 2)]), got)
    written = session_ingest.record_first(b, [one("S", ago=50, repo="b")])
    agg(a, LONG)
    session_ingest.take_back(b, written)
    got = agg(a, LONG)
    ck("and a batch the second one took back is gone", got == {"S": 2, "theirs": 1}, got)
    a.close()
    b.close()


def main():
    equality()
    writers()
    cold_and_warm()
    bounded()
    not_numbers()
    edges()
    full_cache()
    published_meanwhile()
    threads()
    two_processes()
    ck("no read fell back because the digest raised", not BROKE, BROKE[:3])
    print(f"\n{len(held.FAILS)} failed" if held.FAILS else "\nall passed")
    sys.exit(1 if held.FAILS else 0)


if __name__ == "__main__":
    main()
