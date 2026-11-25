"""The hourly usage totals (P-TR-296, TR-1632).

`station_rollup`'s spend and `usage_by_project` read `session_usage_hourly` once it is built, instead of
summing every usage row. This checks that the answers are the ones the row scan gives, that every writer
keeps the totals right, and that the fill resumes, checks itself and sets its mark only on a match.

Plain:                python tests/test_usage_hourly.py
With the Postgres harness (every Store keeps its state in Postgres):
    TARES_TEST_PG=postgresql://postgres:pg@localhost:5432/postgres PYTHONPATH=tests/pg_harness:. \\
        python tests/test_usage_hourly.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from sqlalchemy import func, select  # noqa: E402

from session_helpers import seed_link, seed_station, seed_usage  # noqa: E402
from tares import claude_usage_hourly as hourly  # noqa: E402
from tares.session_store.tables import (session_rollup_state, session_usage,  # noqa: E402
                                        session_usage_hourly)
from tares.store import Store  # noqa: E402

FAILS = []
T0 = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def same(a, b):
    """Two answers equal, floats to a millionth."""
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def scan(fn, *a):
    """What the row scan answers: the readers' fallback, with the totals taken as not built."""
    with mock.patch.object(hourly, "ready", lambda store: False):
        return fn(*a)


def spend(s):
    """Each station's spend, as the pages read it: the totals over every session it played."""
    return {(r["crew_id"], r["station"]): r["usage"] for r in s.station_rollup("claude_code")}


def reply(s, session, message, at, project=None, model="claude-opus-5-5", i=10, o=20, cw=3, cr=4):
    s.record_reply_usage(session, message, at, model, project, None,
                         {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw,
                          "cache_read_input_tokens": cr})


def build():
    """A store with sessions of every kind: a builder with a project, a crew station, a session linked
    to a project, one with nothing, replies across hours, unpriced replies, a reply with no time."""
    s = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    print("  on " + s.sessions_db.dialect)
    seed_station(s, "s1", "builder-1", role="builder", project="p1", crew_id="crew")
    seed_station(s, "s2", "builder-1", role="builder", project="p1", crew_id="crew")
    seed_station(s, "s3", "orchestrator", role="orchestrator", crew_id="crew")
    seed_station(s, "s4", "reviewer", role="reviewer", crew_id="crew")
    seed_station(s, "s5", "helper", role="helper")
    seed_link(s, "p2", "s5")
    for n in range(1, 7):                                   # 6 sessions, the last two with no station
        for k in range(40):
            at = T0 + timedelta(minutes=17 * k + n)
            proj = "p3" if (n == 4 and k % 5 == 0) else None
            cost = None if (k % 7 == 0) else round(0.001 * (k + n), 6)
            seed_usage(s, f"s{n}", f"m{n}-{k}", replied_at=at, project=proj, model="claude-opus-5-5",
                       input_tokens=k + 1, output_tokens=2 * k, cache_write_tokens=k % 3,
                       cache_read_tokens=100 * k, cost_usd=cost)
    seed_usage(s, "s6", "no-time", replied_at=T0, input_tokens=5, output_tokens=5, cost_usd=0.5)
    with s.sessions_db.begin() as conn:                     # a reply with no time, set directly
        conn.execute(session_usage.update().where(session_usage.c.message == "no-time")
                     .values(replied_at=None))
    return s


def counts(s):
    with s.sessions_db.begin() as conn:
        return (conn.execute(select(func.count()).select_from(session_usage_hourly)).scalar_one(),
                conn.execute(select(func.coalesce(func.sum(session_usage_hourly.c.replies), 0))).scalar_one())


def answers(s, sinces):
    return {"by_project": [s.usage_by_project(x) for x in sinces], "spend": spend(s)}


def the_answers_are_the_scan():
    s = build()
    sinces = [None, T0 + timedelta(hours=3, minutes=25), T0 + timedelta(hours=5),
              T0 - timedelta(days=1), T0 + timedelta(days=3)]
    ck("the totals are not built before the fill", not hourly.ready(s))
    before = scan(answers, s, sinces)
    spent = [u for u in before["spend"].values() if u]
    ck("the scan has something to say", len(before["by_project"][0]) >= 3 and len(spent) >= 3,
       (before["by_project"][0], before["spend"]))
    n = hourly.fill(s)
    ck("the fill rebuilt the six sessions", n == 6, n)
    ck("the mark is set", hourly.ready(s))
    after = answers(s, sinces)
    for i, x in enumerate(sinces):
        ck(f"usage_by_project since {x} is the scan's answer", same(before["by_project"][i], after["by_project"][i]),
           (before["by_project"][i], after["by_project"][i]))
    ck("the station spend is the scan's answer", same(before["spend"], after["spend"]),
       (before["spend"], after["spend"]))
    rows, replies = counts(s)
    ck("one bucket per session, hour and project, far fewer than the replies", 0 < rows < replies == 241,
       (rows, replies))

    # a window that starts mid-hour counts that hour's replies from the rows, not from its bucket
    mid = T0 + timedelta(hours=2, minutes=30)
    got = sum(r["replies"] for r in s.usage_by_project(mid))
    want = sum(1 for k in range(40) for n in range(1, 7)
               if T0 + timedelta(minutes=17 * k + n) >= mid)
    ck("a window that starts mid-hour counts exactly the replies after it", got == want, (got, want))
    s.close()
    return s


def writers_keep_the_totals():
    s = build()
    hourly.fill(s)
    sinces = [None, T0 + timedelta(hours=1, minutes=10)]

    def agree(label):
        a, b = scan(answers, s, sinces), answers(s, sinces)
        ck(label, same(a, b), (a, b))

    reply(s, "s1", "new-1", T0 + timedelta(hours=30), i=7)
    agree("a new reply is in the totals")
    reply(s, "s1", "new-1", T0 + timedelta(hours=40), i=70, o=90)      # same reply, more tokens, later time
    agree("a repeated reply keeps the larger counts, in the hour it was first written")
    reply(s, "s2", "new-2", T0 + timedelta(hours=31), project=None, i=3)
    reply(s, "s2", "new-2", T0 + timedelta(hours=31), project="p9", i=3)
    agree("a reply that gets its project later moves to it")
    reply(s, "s5", "unpriced", T0 + timedelta(hours=32), model="a-model-nobody-priced")
    agree("an unpriced reply is counted as unpriced")
    s.record_reply_usage("s5", "no-clock", None, "claude-opus-5-5", None, None, {"input_tokens": 4})
    agree("a reply with no time is in the all-time totals only")

    s._usage_forget_project("p3")
    agree("a project forgotten moves its replies to the session's own project")
    with s.sessions_db.begin() as conn:
        left = conn.execute(select(func.count()).select_from(session_usage).where(
            session_usage.c.project == "p3")).scalar_one()
    ck("it left no reply on p3", left == 0, left)

    # the start-up fill deletes synthetic rows and prices the unpriced
    with s.sessions_db.begin() as conn:
        conn.execute(session_usage.update().where(session_usage.c.session == "s4").values(model="<synthetic>"))
        conn.execute(session_usage.update().where(session_usage.c.session == "s3").values(cost_usd=None))
    hourly_ok, _ = hourly.check(s)
    ck("a change made behind the writers shows in the check", not hourly_ok)
    s._claude_usage_fill()
    agree("the start-up fill's deletes and pricing are in the totals")
    ok, why = hourly.check(s)
    ck("the totals match the rows at the end", ok, why)
    s.close()


def the_fill_resumes_and_checks():
    s = build()
    n1 = hourly.fill(s, chunk=2, stop_after=1)
    ck("one chunk of two sessions", n1 == 2, n1)
    ck("the mark is not set", not hourly.ready(s))
    with s.sessions_db.begin() as conn:
        cur = conn.execute(select(session_rollup_state.c.cursor)).scalar_one()
    ck("the cursor is the last session done", cur == "s2", cur)
    # a write for a session the fill has not reached is left to it; one it has passed is rebuilt now
    reply(s, "s1", "while-1", T0 + timedelta(hours=50))
    reply(s, "s6", "while-6", T0 + timedelta(hours=50))
    with s.sessions_db.begin() as conn:
        has = {r[0] for r in conn.execute(select(session_usage_hourly.c.session).where(
            session_usage_hourly.c.hour == T0 + timedelta(hours=50)))}
    ck("a write before the cursor is in the totals, one after it waits", has == {"s1"}, has)
    s2 = hourly.fill(s, chunk=2)
    ck("the next pass finishes the rest", s2 >= 4 and hourly.ready(s), s2)
    a, b = scan(answers, s, [None]), answers(s, [None])
    ck("and the answers are the scan's", same(a, b), (a, b))
    ck("a second fill does nothing", hourly.fill(s) == 0)

    # a mismatch does not set the mark, and starts again
    t = build()
    orig = hourly.rebuild

    def lossy(db, conn, sessions, hour=None):
        orig(db, conn, sessions, hour)
        if hour is None and "s3" in sessions:
            conn.execute(session_usage_hourly.delete().where(session_usage_hourly.c.session == "s3"))
    with mock.patch.object(hourly, "rebuild", lossy):
        try:
            hourly.fill(t)
            err = None
        except RuntimeError as e:
            err = str(e)
    ck("a fill whose totals do not match the rows raises and says so", err and "do not match" in err, err)
    ck("and the mark is not set", not hourly.ready(t))
    with t.sessions_db.begin() as conn:
        cur = conn.execute(select(session_rollup_state.c.cursor)).scalar_one()
    ck("it starts again from the first session", cur is None, cur)
    ck("the readers still scan", same(scan(answers, t, [None]), answers(t, [None])))
    hourly.fill(t)
    ck("the next pass, with nothing wrong, sets the mark", hourly.ready(t))
    s.close()
    t.close()


def the_readers_do_not_scan_the_rows():
    s = build()
    hourly.fill(s)
    from sqlalchemy import event
    engine = s.sessions_db.engine

    def statements(fn):
        seen = []

        def watch(conn, cursor, statement, parameters, context, executemany):
            seen.append(" ".join(statement.split()))
        event.listen(engine, "before_cursor_execute", watch)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", watch)
        # a read of the raw rows names the table with a space after it (the buckets' table does not)
        return [q for q in seen if "FROM session_usage " in q or "JOIN session_usage " in q]

    ck("all-time totals read no usage row", not statements(lambda: s.usage_by_project(None)))
    ck("a window that starts on an hour reads no usage row",
       not statements(lambda: s.usage_by_project(T0 + timedelta(hours=2))))
    ck("the stations' spend reads no usage row", not statements(lambda: s.station_rollup("claude_code")))
    part = statements(lambda: s.usage_by_project(T0 + timedelta(hours=2, minutes=20)))
    ck("a window that starts inside an hour reads the usage rows of that hour only, by time",
       len(part) == 1 and "replied_at >=" in part[0] and "replied_at <" in part[0], part)
    s.close()


def the_migration_is_additive():
    from alembic import command
    from sqlalchemy import inspect
    s = build()
    hourly.fill(s)
    sdb = s.sessions_db
    with sdb.begin() as conn:
        raw = conn.execute(select(func.count()).select_from(session_usage)).scalar_one()
        cfg = sdb._config()
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0011_session_moves")
    names = inspect(sdb.engine).get_table_names()
    ck("a downgrade drops the two new tables", "session_usage_hourly" not in names
       and "session_rollup_state" not in names, names)
    idx = {i["name"] for i in inspect(sdb.engine).get_indexes("session_usage")}
    ck("and the replied_at index", "session_usage_replied_at" not in idx, idx)
    with sdb.begin() as conn:
        left = conn.execute(select(func.count()).select_from(session_usage)).scalar_one()
    ck("the usage rows are untouched", left == raw == 241, (left, raw))
    sdb.upgrade()
    s._usage_hourly_ready = False
    ck("an upgrade builds them empty, not ready", counts(s) == (0, 0) and not hourly.ready(s))
    sdb.upgrade()
    hourly.fill(s)
    ck("and a fill builds them again", hourly.ready(s) and counts(s)[1] == 241, counts(s))
    s.close()


def main():
    for fn in (the_answers_are_the_scan, writers_keep_the_totals, the_fill_resumes_and_checks,
               the_readers_do_not_scan_the_rows, the_migration_is_additive):
        print(f"== {fn.__name__.replace('_', ' ')} ==")
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nOK")


if __name__ == "__main__":
    main()
