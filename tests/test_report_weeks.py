"""The weekly figures per project (TR-1666, tares/report.py).

Two projects over three ISO weeks, every figure against numbers worked out by hand: a stage that
spans a week boundary, a ticket merged and then reopened, one merged with no doing, a deleted
ticket, blocking questions that overlap, a withdrawn, an open and a non-blocking one, the merge
queue's automatic pass, cost nothing claims. Then a changed stage row and late events through
`refresh_recent`, rebuilds that change nothing, one week alone, a deleted project (also under a
pass that read before the delete), the background pass, the migration both ways and the rebuild
route. The process runs in a zone far ahead of UTC, so a week taken from a local time shows.

Run: python tests/test_report_weeks.py
With TARES_TEST_PG set and PYTHONPATH=tests/pg_harness the state and session tables are in Postgres.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()
os.environ["TZ"] = "Pacific/Auckland"     # UTC+13: Sunday evening UTC is already Monday here
if hasattr(time, "tzset"):
    time.tzset()

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


from alembic import command
from sqlalchemy import inspect, insert, text, update

from tares import factory as factory_mod
from tares import merge_queue, report
from tares.session_store import SessionStore
from tares.session_store.tables import report_weeks, ticket_history
from tares.store import Store
from session_helpers import seed_link, seed_station, seed_usage

PG = os.environ.get("TARES_TEST_PG")
W1, W2, W3 = date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)   # Mondays
K_MIGRATION, K_ERRORS, K_DOCS = "migration", "error handling", "docs out of date"
tmp = tempfile.mkdtemp()


def at(day, hour=0, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


# ── seeding ───────────────────────────────────────────────────────────────────
def stages(s, project, ticket, *moves):
    for value, when in moves:
        s.add_ticket_history(ticket, project, "stage", value, None, None, None, when)


def ask(s, project, tickets, blocking, asked, closed=None, state="answered", by="build-1"):
    d = s.add_desk_item(project, "which way?", "", [], "", "", "", tickets, blocking, by, [], asked)
    if closed:
        s.close_desk_item(d["id"], state, None, closed)
    return d["id"]


def review(s, project, ticket, verdict, when, findings=(), resolved=None, by="crew-reviewer",
           verified="read the diff"):
    new = [{"severity": sev, "file": None, "line": None, "blocking": blocking, "text": words}
           for sev, blocking, words in findings]
    s.add_review(project, ticket, "7", "abc1234", verdict, verified, "", by, new, resolved or {},
                 factory_mod.finding_kind, when)


def automatic(s, project, ticket, when):
    review(s, project, ticket, "pass", when, by=merge_queue.BY,
           verified="Same changes against main as the reviewed commit abc1234; only the base moved")


def seed(s) -> dict:
    s.create_project("pa", "blank", "A", {})
    s.create_project("pb", "blank", "B", {})
    ids = {n: s.create_ticket("pa", "tares", n) for n in ("a1", "a2", "a3")}   # T1, T2, T3
    ids["b1"] = s.create_ticket("pb", "linear", "b1", external_id="x9", identifier="ENG-9")
    ids["b2"] = s.create_ticket("pb", "tares", "b2")
    # a1, all in W1: doing 6h (10-14, and 2h after it is reopened), review 5h, changes 1h
    stages(s, "pa", ids["a1"], ("doing", at(6, 10)), ("doing", at(6, 12)), ("review", at(6, 14)),
           ("changes", at(6, 16)), ("review", at(6, 17)), ("merged", at(6, 20)),
           ("doing", at(7, 9)), ("merged", at(7, 11)))
    s.add_ticket_history(ids["a1"], "pa", "status", "blocked", None, None, None, at(6, 13))
    # a2 crosses Monday 00:00 in doing; its stage is taken off for a day (counted nowhere)
    stages(s, "pa", ids["a2"], ("doing", at(11, 22)), ("review", at(12, 3)), (None, at(12, 5)),
           ("blocked", at(13, 5)), ("doing", at(13, 6)), ("merged", at(13, 8)))
    # a3 merged with no doing before it, so the later one is not its start; that one is still going,
    # and being set again does not end it. b2 is deleted below and still counts
    stages(s, "pa", ids["a3"], ("review", at(14, 9)), ("merged", at(14, 12)), ("doing", at(14, 13)),
           ("doing", at(14, 15)))
    stages(s, "pb", ids["b2"], ("doing", at(7, 8)), ("merged", at(7, 9)))
    stages(s, "pb", ids["b1"], ("doing", at(19, 9)), ("merged", at(19, 12)))
    s.delete_ticket(ids["b2"])

    assert factory_mod.label(s.get_ticket("pa", ids["a2"])) == "T2"
    ids["q1"] = ask(s, "pa", ["T2"], True, at(11, 23), at(12, 4))
    ids["q2"] = ask(s, "pa", ["T2"], True, at(12, 0, 30), at(12, 2))        # inside q1
    ask(s, "pa", ["T2"], False, at(13, 6), at(13, 8))                       # blocks nothing
    ask(s, "pa", ["T2"], True, at(13, 6), at(13, 7), state="withdrawn")
    ids["q5"] = ask(s, "pa", ["T2"], True, at(13, 6))                       # open, answered later
    ask(s, None, [], False, at(7, 12))                                      # no project
    ask(s, "pb", ["eng-9"], True, at(19, 10), at(19, 11), by=merge_queue.BY)
    ask(s, "pa", ["T1", "T3", "T99"], True, at(15, 1), at(15, 2))   # both tickets wait; no T99
    ask(s, "pa", [], True, at(15, 3), at(15, 8))                    # names no ticket: no waiting

    review(s, "pa", ids["a1"], "changes", at(6, 14, 30), findings=[
        ("P1", True, "No migration for the new column"),
        ("P2", False, "The error handling swallows the timeout")])
    review(s, "pa", ids["a1"], "pass", at(6, 17, 30), resolved={"F1": "fixed", "F2": "open"})
    automatic(s, "pa", ids["a1"], at(6, 18))
    review(s, "pa", ids["a2"], "pass", at(12, 3, 30))
    automatic(s, "pa", ids["a2"], at(12, 4, 30))
    review(s, "pa", ids["a3"], "changes", at(14, 10), findings=[("P3", False, "The README is stale")])
    review(s, "pa", ids["a3"], "pass", at(14, 11))

    ids["m1"] = s.create_milestone("pa", "one")
    ids["m2"] = s.create_milestone("pa", "two")
    s.accept_milestone("pa", ids["m1"], "Ada", None, at(15, 9))
    s.accept_milestone("pa", ids["m2"], "Ada", None, at(6, 9))
    s.accept_milestone("pa", ids["m2"], "Ada", None, at(20, 9))             # the later one counts
    mb = s.create_milestone("pb", "one")
    s.accept_milestone("pb", mb, "Ada", None, at(11, 23, 30))               # Sunday, W1 in UTC

    seed_station(s, "s-build", "build-1", role="builder", project="pa")
    seed_station(s, "s-orch", "crew-orchestrator", role="orchestrator")
    seed_station(s, "s-pb", "build-2", role="builder", project="pb")
    seed_link(s, "pa", "s-orch")
    seed_link(s, "pb", "s-me")
    seed_link(s, "pa", "s-me")
    seed_link(s, "pb", "s-pbme")
    for session, message, when, cost, project in (
            ("s-build", "1", at(6, 12), 2.0, None),
            ("s-build", "2", at(11, 23, 59), 0.25, None),        # the last minute of W1
            ("s-build", "3", at(12, 0, 0), 0.125, None),         # the first of W2
            ("s-orch", "1", at(6, 12), 5.0, None),               # a crew station, unstamped
            ("s-orch", "2", at(7, 12), 0.5, "pa"),
            ("s-me", "1", at(13, 12), 1.0, None),                # a person: the lowest linked
            ("s-lone", "1", at(13, 12), None, None),             # unpriced, claimed by nothing
            ("s-gone", "1", at(6, 12), 3.0, "gone"),             # a project that is no more
            ("s-pb", "1", at(19, 12), 4.0, None),
            ("s-pbme", "1", at(19, 12), 0.75, "pb")):
        seed_usage(s, session, message, replied_at=when, cost_usd=cost, project=project)
    return ids


# ── what the rows should say ──────────────────────────────────────────────────
def spent(**by):
    return {name: {"total": float(by.get(name, (0, 0))[0]), "waiting": float(by.get(name, (0, 0))[1])}
            for name in ("doing", "review", "changes", "blocked")}


def asked(n=0, blocking=0, answered=0, hours=()):
    return {"asked": n, "blocking": blocking, "answered": answered, "answer_hours": list(hours)}


def role(cost, replies=1, unpriced=0):
    return {"cost_usd": cost, "replies": replies, "unpriced": unpriced}


def found(kind, severity, blocking, opened=0, still_open=0, since_fixed=0, fixed=0):
    return {"kind": kind, "severity": severity, "blocking": blocking, "opened": opened,
            "opened_open": still_open, "opened_fixed": since_fixed, "fixed": fixed}


def row(**over):
    base = {"merged": 0, "start_to_merge": [], "stage_hours": spent(), "waited_hours": 0.0,
            "milestones_accepted": 0, "reviewed": 0, "passed_first": 0, "rounds": [],
            "findings": [], "questions": asked(), "cost_usd": 0.0, "unpriced": 0,
            "cost_by_role": {}}
    assert set(over) <= set(base), over
    return {**base, **over}


EXPECTED = {
    ("pa", W1): row(
        merged=1, start_to_merge=[10.0],
        stage_hours=spent(doing=(8, 1), review=(5, 0), changes=(1, 0)), waited_hours=1.0,
        reviewed=1, passed_first=0, rounds=[2],
        findings=[found(K_ERRORS, "P2", False, opened=1, still_open=1),
                  found(K_MIGRATION, "P1", True, opened=1, since_fixed=1, fixed=1)],
        questions=asked(1, 1, 1, [5.0]), cost_usd=2.75,
        cost_by_role={"builder": role(2.25, 2), "orchestrator": role(0.5)}),
    ("pa", W2): row(
        merged=2, start_to_merge=[34.0],
        stage_hours=spent(doing=(5, 3), review=(5, 1), blocked=(1, 0)), waited_hours=6.0,
        milestones_accepted=1, reviewed=2, passed_first=1, rounds=[1, 2],
        findings=[found(K_DOCS, "P3", False, opened=1, still_open=1)],
        questions=asked(6, 5, 4, [1.0, 1.5, 2.0, 5.0]), cost_usd=1.125,
        cost_by_role={"builder": role(0.125), "person": role(1.0)}),
    ("pa", W3): row(milestones_accepted=1),
    ("pb", W1): row(merged=1, start_to_merge=[1.0], stage_hours=spent(doing=(1, 0)),
                    milestones_accepted=1),
    ("pb", W3): row(merged=1, start_to_merge=[3.0], stage_hours=spent(doing=(3, 1)),
                    waited_hours=1.0, questions=asked(1, 1, 1, [1.0]), cost_usd=4.75,
                    cost_by_role={"builder": role(4.0), "person": role(0.75)}),
    ("", W1): row(questions=asked(1), cost_usd=8.0,
                  cost_by_role={"orchestrator": role(5.0), "person": role(3.0)}),
    ("", W2): row(unpriced=1, cost_by_role={"person": role(0.0, 1, 1)}),
}


def table(s, **kw):
    return {(r["project"], r["week"]): {k: v for k, v in r.items()
                                        if k not in ("project", "week", "updated_at")}
            for r in report.read_weeks(s, **kw)}


def stamps(s):
    return {(r["project"], r["week"]): r["updated_at"] for r in report.read_weeks(s)}


def same(label, got, want):
    diff = {k: {c: (got.get(k, {}).get(c), want.get(k, {}).get(c))
                for c in set(got.get(k, {})) | set(want.get(k, {}))
                if got.get(k, {}).get(c) != want.get(k, {}).get(c)}
            for k in set(got) | set(want) if got.get(k) != want.get(k)}
    ck(label, got == want, f"(got, want) {diff}")


# ── the checks ────────────────────────────────────────────────────────────────
def rules():
    print("== the rules ==")
    ck("Sunday 23:59 UTC is the week before Monday 00:00",
       report.week_of(at(11, 23, 59)) == W1 and report.week_of(at(12)) == W2)
    local = at(11, 23, 30).astimezone(timezone(timedelta(hours=13)))
    ck("a local time counts in its UTC week", report.week_of(local) == W1, local)
    ck("a time with no zone is UTC", report.week_of(datetime(2026, 10, 11, 23, 30)) == W1)
    ck("a span is cut at each Monday",
       list(report.split_weeks(at(11, 20), at(19, 1))) == [
           (W1, at(11, 20), at(12)), (W2, at(12), at(19)), (W3, at(19), at(19, 1))])
    ck("a span that does not move forward is nothing",
       list(report.split_weeks(at(12), at(12))) == [] and list(report.split_weeks(at(13), at(12))) == [])
    p = report.percentiles([4.0, 1.0, 3.0, 2.0])
    ck("percentiles interpolate as percentile_cont does",
       p["p50"] == 2.5 and p["p75"] == 3.25 and abs(p["p90"] - 3.7) < 1e-9, p)
    ck("one value is every percentile", report.percentiles([5.0]) == {"p50": 5.0, "p75": 5.0, "p90": 5.0})
    ck("no values, no percentile", report.percentiles([]) == {"p50": None, "p75": None, "p90": None})
    ck("only the merge queue's pass is automatic",
       factory_mod.automatic_pass({"by": merge_queue.BY, "verdict": "pass"})
       and not factory_mod.automatic_pass({"by": "crew-reviewer", "verdict": "pass"})
       and not factory_mod.automatic_pass({"by": merge_queue.BY, "verdict": "changes"}))
    item = {"blocking": True, "state": "answered", "created_at": at(6), "closed_at": at(7)}
    ck("waiting is an answered blocking item", report.waiting_span(item) == (at(6), at(7))
       and report.waiting_span({**item, "state": "withdrawn"}) is None
       and report.waiting_span({**item, "state": "open", "closed_at": None}) is None
       and report.waiting_span({**item, "blocking": False}) is None)


def figures(s):
    print("== rebuild: every figure ==")
    stage_at = s.all_stage_changes()[0]["at"]
    ck("the sessions database hands times back in UTC, whatever zone the process is in",
       stage_at.utcoffset() == timedelta(), stage_at)
    ck("nothing before the first rebuild", report.read_weeks(s) == [])
    out = report.rebuild_all(s)
    ck("the rebuild says what it wrote", out == {"rows": 7, "changed": 7, "removed": 0}, out)
    got = table(s)
    ck("rows only for weeks with something to say", set(got) == set(EXPECTED), sorted(got))
    for key in sorted(EXPECTED):
        same(f"{key[0] or 'no project'} {key[1]}", {key: got.get(key, {})}, {key: EXPECTED[key]})
    stage = [v for r in got.values() for v in r["stage_hours"].values()]
    ck("waiting never exceeds the stage's time", all(v["waiting"] <= v["total"] for v in stage), stage)
    ck("the weeks add up to everything spent",
       round(sum(r["cost_usd"] for r in got.values()), 6) == 16.625,
       sum(r["cost_usd"] for r in got.values()))


def reading(s):
    print("== read_weeks ==")
    ordered = [(r["week"], r["project"]) for r in report.read_weeks(s)]
    ck("ordered by week, then project", ordered == sorted(ordered), ordered)
    ck("one project", set(table(s, projects=["pa"])) == {("pa", W1), ("pa", W2), ("pa", W3)})
    ck("no projects, no rows", report.read_weeks(s, []) == [])
    ck("what belongs to none is asked for as an empty name",
       set(table(s, projects=[""])) == {("", W1), ("", W2)})
    got = set(table(s, from_=date(2026, 10, 14), to=at(18, 23)))
    ck("a range takes the whole weeks its ends are in", got == {("pa", W2), ("", W2)}, got)
    ck("an open end", set(table(s, from_=W3)) == {("pa", W3), ("pb", W3)})
    first = report.read_weeks(s)[0]
    ck("week is a date and updated_at a UTC time", type(first["week"]) is date
       and first["updated_at"].utcoffset() == timedelta(), first)


def again(s):
    print("== a second rebuild, one week alone ==")
    before, written = table(s), stamps(s)
    out = report.rebuild_all(s)
    ck("a second rebuild changes nothing", out == {"rows": 7, "changed": 0, "removed": 0}, out)
    ck("not even the stamps", table(s) == before and stamps(s) == written)
    for key in sorted(before):
        out = report.rebuild_week(s, key[0], key[1] + timedelta(days=3))
        ck(f"rebuild_week of {key[0] or 'no project'} {key[1]} agrees with the full one",
           out == {"rows": 1, "changed": 0, "removed": 0}, out)
    with s.sessions_db.begin() as conn:
        conn.execute(report_weeks.delete().where(report_weeks.c.project == "",
                                                 report_weeks.c.week == W2))
    out = report.rebuild_week(s, "", at(14, 9))
    ck("rebuild_week writes the one row that is missing",
       out["changed"] == 1 and table(s) == before, (out, sorted(table(s))))
    stale = {**{c.name: None for c in report_weeks.columns}, "project": "pa",
             "week": date(2026, 9, 7), "merged": 9,
             **{c: "[]" for c in report.JSON_COLUMNS}}
    for how, call in (("rebuild_week", lambda: report.rebuild_week(s, "pa", date(2026, 9, 9))),
                      ("rebuild_all", lambda: report.rebuild_all(s))):
        with s.sessions_db.begin() as conn:
            conn.execute(insert(report_weeks).values(**stale))
        out = call()
        ck(f"{how} removes a row with nothing behind it", out["removed"] == 1 and table(s) == before,
           (out, sorted(table(s))))


def late(s, ids):
    print("== refresh_recent: a changed stage row and late events ==")
    written = stamps(s)
    with s.sessions_db.begin() as conn:
        h = ticket_history.c
        conn.execute(update(ticket_history).where(h.ticket == ids["a1"], h.value == "doing",
                                                  h.changed_at == at(6, 10))
                     .values(changed_at=at(6, 8)))
    out = report.refresh_recent(s)
    ck("the stage row changes its week only", out["changed"] == 1 and out["removed"] == 0, out)
    want = dict(EXPECTED)
    want["pa", W1] = {**EXPECTED["pa", W1], "start_to_merge": [12.0],
                      "stage_hours": spent(doing=(10, 1), review=(5, 0), changes=(1, 0))}
    same("start to merge and doing grow by the two hours", table(s), want)

    s.close_desk_item(ids["q5"], "answered", None, at(20, 6))        # a week after it was asked
    review(s, "pa", ids["a1"], "pass", at(21, 10), resolved={"F2": "fixed"})   # two weeks on
    out = report.refresh_recent(s)
    ck("the late answer and fix change three weeks of pa", out["changed"] == 3, out)
    want["pa", W1] = {**want["pa", W1], "rounds": [3], "findings": [
        found(K_ERRORS, "P2", False, opened=1, since_fixed=1),
        found(K_MIGRATION, "P1", True, opened=1, since_fixed=1, fixed=1)]}
    want["pa", W2] = {**want["pa", W2],
                      "questions": asked(6, 5, 5, [1.0, 1.5, 2.0, 5.0, 168.0]),
                      "waited_hours": 144.0,
                      "stage_hours": spent(doing=(5, 5), review=(5, 1), blocked=(1, 0))}
    want["pa", W3] = row(milestones_accepted=1, waited_hours=30.0,
                         findings=[found(K_ERRORS, "P2", False, fixed=1)])
    same("each figure lands in its week", table(s), want)
    now = stamps(s)
    ck("the other projects' rows are not rewritten",
       all(now[k] == written[k] for k in written if k[0] != "pa"), (written, now))
    out = report.rebuild_all(s)
    ck("a full rebuild finds nothing the refresh missed", out["changed"] == 0 and out["removed"] == 0, out)
    s.accept_milestone("pa", ids["m1"], "Ada", None, at(22, 9))
    report.refresh_recent(s)
    got = table(s)
    ck("a milestone accepted again moves week", got["pa", W2]["milestones_accepted"] == 0
       and got["pa", W3]["milestones_accepted"] == 2, (got["pa", W2], got["pa", W3]))


def deleting(s):
    print("== a deleted project ==")
    report.rebuild_all(s)
    pa = table(s, projects=["pa"])
    read_before = report._compute(s)            # a pass that read while pb was still there
    s.delete_project("pb")
    ck("its rows go with it", table(s, projects=["pb"]) == {}, sorted(table(s)))
    report._write(s, read_before)
    ck("a pass that read before the delete does not bring them back",
       table(s, projects=["pb"]) == {}, sorted(table(s)))
    report.rebuild_all(s)
    got = table(s)
    ck("nor does a rebuild", {k[0] for k in got} == {"pa", ""}, sorted(got))
    same("what its sessions spent belongs to no project now", {("", W3): got.get(("", W3), {})},
         {("", W3): row(cost_usd=4.75, cost_by_role={"builder": role(4.0), "person": role(0.75)})})
    ck("the other project is as it was", table(s, projects=["pa"]) == pa)


def background():
    print("== the background pass ==")
    s = Store(os.path.join(tempfile.mkdtemp(), "bg.duckdb"))   # a data dir has one session store
    s.create_project("p", "blank", "P", {})
    stages(s, "p", s.create_ticket("p", "tares", "t"), ("doing", at(6, 1)), ("merged", at(6, 2)))
    ck("no marker on a new cell", s.get_setting(report.BUILT_SETTING) is None)
    full, calls = report.rebuild_all, []

    def failing_once(store):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the database is busy")
        return full(store)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(report.run(s, stop, every=0.01, first=0.01))
        for _ in range(1000):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)
        return task.done()

    report.rebuild_all = failing_once
    try:
        done = asyncio.run(go())
    finally:
        report.rebuild_all = full
    ck("a failed pass does not end the job, and stop does", done and len(calls) >= 3, calls)
    ck("the first full build is marked", bool(s.get_setting(report.BUILT_SETTING)))
    ck("and the week is there", table(s)["p", W1]["merged"] == 1, table(s))
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        report.refresh(s)
    ck("a later pass does not say it again", said.getvalue() == "", said.getvalue())
    s.set_setting(report.BUILT_SETTING, "")
    with contextlib.redirect_stdout(said):
        report.refresh(s)
    ck("a first build says so", "report weeks built (1 rows)" in said.getvalue(), said.getvalue())


HEAD = "0023_brain_tables"  # the newest session store revision: an upgrade ends there


def migration():
    print("== revision 0008_report_weeks ==")
    d = tempfile.mkdtemp()
    schema = dsn = None
    if PG:
        import psycopg
        schema = "rw_" + uuid.uuid4().hex[:12]
        dsn = f"{PG}?options=-csearch_path%3D{schema}"
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")

    def tables(sdb):
        return set(inspect(sdb.engine).get_table_names())

    def version(sdb):
        with sdb.begin() as c:
            return c.execute(text("SELECT version_num FROM session_store_version")).scalar()

    def down(sdb):
        with sdb.begin() as c:
            cfg = sdb._config()
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0007_session_archive")

    try:
        sdb = SessionStore(d, dsn)
        sdb.upgrade()
        insp = inspect(sdb.engine)
        ck("the table has the declared columns", [c["name"] for c in insp.get_columns("report_weeks")]
           == [c.name for c in report_weeks.columns])
        ck("its key is (project, week)", insp.get_pk_constraint("report_weeks")[
            "constrained_columns"] == ["project", "week"])
        down(sdb)
        ck("a downgrade drops it alone", "report_weeks" not in tables(sdb)
           and "session_archive_parts" in tables(sdb) and version(sdb) == "0007_session_archive")
        sdb.upgrade()
        ck("an upgrade builds it again", "report_weeks" in tables(sdb)
           and version(sdb) == HEAD, version(sdb))
        down(sdb)
        with sdb.begin() as c:
            report_weeks.create(c)
            c.execute(insert(report_weeks).values(project="kept", week=W1, merged=1))
        sdb.upgrade()
        with sdb.begin() as c:
            kept = c.execute(text("SELECT merged FROM report_weeks WHERE project = 'kept'")).scalar()
        ck("a table already there is taken over with its rows",
           kept == 1 and version(sdb) == HEAD, (kept, version(sdb)))
        sdb.close()
    finally:
        if PG:
            import psycopg
            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")


async def route():
    print("== POST /api/factory/report/rebuild ==")
    root = "root-token-report-weeks"
    home = tempfile.mkdtemp()
    os.environ.update({"TARES_DB": os.path.join(home, "api.duckdb"),
                       "TARES_CATALOG": os.path.join(home, "catalog.yaml"),
                       "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": root,
                       "ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key"})
    import httpx
    from tares import daemon
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        s = app.state.store
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {root}"})
        async with cx:
            made = await cx.post("/api/projects",
                                 json={"template": "custom", "name": "Report", "objects": []})
            assert made.status_code == 201, made.text
            uid = made.json()["id"]
            stages(s, uid, s.create_ticket(uid, "tares", "t"), ("doing", at(6, 1)), ("merged", at(6, 2)))
            r = await cx.post("/api/factory/report/rebuild")
            ck("an admin rebuilds", r.status_code == 200 and r.json()["rows"] == 1
               and r.json()["changed"] == 1, f"{r.status_code} {r.text[:200]}")
            ck("and the row is there", table(s).get((uid, W1), {}).get("merged") == 1, table(s))
            for label, body in (("a read key", {"name": "r", "scopes": ["read"]}),
                                ("a project key", {"name": "p", "project": uid,
                                                   "scopes": ["read", "findings"]})):
                k = await cx.post("/api/keys", json=body)
                assert k.status_code in (200, 201), k.text
                r = await cx.post("/api/factory/report/rebuild",
                                  headers={"Authorization": f"Bearer {k.json()['secret']}"})
                ck(f"{label} is refused", r.status_code == 403, f"{r.status_code} {r.text[:200]}")
            r = await cx.post("/api/factory/report/rebuild", headers={"Authorization": "Bearer no"})
            ck("no credential is refused", r.status_code == 401, r.status_code)


def main():
    rules()
    s = Store(os.path.join(tmp, "t.duckdb"))
    ids = seed(s)
    figures(s)
    reading(s)
    again(s)
    late(s, ids)
    deleting(s)
    background()
    migration()
    asyncio.run(route())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
