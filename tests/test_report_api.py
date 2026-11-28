"""The Report page's one read, GET /api/factory/report (TR-1667, tares/report.py `page`).

The weeks a range covers (as a function, with a fixed day, and over HTTP), the shape of the answer,
a cell with nothing on it, a cell of one person, and each scope: which weekly rows reach a section,
which projects and teams are named and counted. A private project is in no scope of a caller who
cannot open it, and asked for by name it is unknown, like an id that names nothing. Who may read at
all, bad input, that no raw record is read on page load, the Delivered section's figures against
numbers worked out by hand, with those of the weeks before (TR-1668), the Time section's split into
waiting on a person and agent time and its start-to-merge buckets, what "stuck now" lists and who
reads it, that each stuck question has its row on the project page (TR-1669), the Review section's
pass rate, rounds, findings by kind and severity and rows per project, its recurring findings against
GET /api/recurring-findings, per reader and scope (TR-1670), and the time of the read on a large cell
(20 projects, 12 weeks, 600 merged tickets).

Run: python tests/test_report_api.py
"""
import asyncio
import contextlib
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = "root-token-report-api"
# The daemon reads its database, catalog and token when it is imported.
HOME = tempfile.mkdtemp(prefix="tares-test-report-api-")
os.environ.update({"TARES_HOME": os.path.join(HOME, "import"),
                   "TARES_CATALOG": os.path.join(HOME, "catalog.yaml"),
                   "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": ROOT,
                   "TARES_EDITION": "factory",
                   "ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key"})

import httpx

from tares import daemon, merge_ledger, merge_queue, report
from tares import factory as factory_mod
from session_helpers import seed_station, seed_usage
from sqlalchemy import update
from tares.session_store.tables import people

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


SEEN = "Work done outside Tares."
FIXED = ["delivered", "time", "review", "cost", "merges", "architecture"]
BAD_DATE = "A date is written as year-month-day, for example 2026-11-25."
BACKWARDS = "The first date is after the last date."
AHEAD = "The range starts in the future."
TOO_LONG = "Pick a range of 2 years or less."
BAD_SCOPE = "A scope is mine, factory, team:<id> or project:<id>."
TODAY = datetime.now(timezone.utc).date()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def week(back: int) -> date:
    """The Monday `back` weeks before this week's."""
    return MONDAY - timedelta(weeks=back)


def noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


def bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


@contextlib.asynccontextmanager
async def cell():
    """A factory cell with auth on and a database of its own: (store, root client, bare client)."""
    daemon.DATA_DIR = os.path.join(tempfile.mkdtemp(dir=HOME), "api")
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=bearer(ROOT)) as root:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as bare:
                yield app.state.store, root, bare


# ── seeding ───────────────────────────────────────────────────────────────────
def project(s, uid, name, lead=None, kind="software_factory", private=False, status="active"):
    s.create_project(uid, name, status=status, goal="g", lead_person_id=lead)
    if kind:
        s.set_project_kind(uid, kind)
    if private:
        s.set_project_scope(uid, "private")


def merge(s, uid, when, assignee=None) -> str:
    tid = s.create_ticket(uid, "tares", "t", assignee_person_id=assignee)
    s.add_ticket_history(tid, uid, "stage", "doing", None, None, None, when - timedelta(hours=2))
    s.add_ticket_history(tid, uid, "stage", "merged", None, None, None, when)
    return tid


def key(s, kid, scopes, person=None, uid=None) -> str:
    secret = f"nvf_{kid}_{kid}-secret-for-the-test"
    s.insert_api_key(kid, kid, f"nvf_{kid}", hashlib.sha256(secret.encode()).hexdigest(), scopes,
                     project=uid, person_id=person)
    return secret


async def get(cx, who=None, headers=None, **params):
    if "from_" in params:
        params["from"] = params.pop("from_")
    h = {**(bearer(who) if who else {}), **(headers or {})}
    return await cx.get("/api/factory/report", params=params, headers=h)


def probe(rows, weeks, before=()):
    """A section that says what it was handed."""
    return {"projects": sorted({r["project"] for r in rows}),
            "weeks": [w.isoformat() for w in weeks],
            "merged": sum(r["merged"] for r in rows),
            "before": sorted({r["project"] for r in before})}


# ── the weeks of a range ──────────────────────────────────────────────────────
def ranges():
    print("== the weeks of a range ==")
    day = date(2026, 11, 25)                      # a Wednesday
    last = date(2026, 11, 23)

    def mondays(first, n):
        return [first + timedelta(weeks=i) for i in range(n)]

    def refused(a, b):
        try:
            report.range_weeks(a, b, day)
        except ValueError as e:
            return str(e)
        return None

    ck("no dates: the last 8 weeks, the running one included",
       report.range_weeks(None, None, day) == mondays(date(2026, 10, 5), 8),
       report.range_weeks(None, None, day))
    got = report.range_weeks("2026-10-01", "2026-10-14", day)
    ck("two dates mid-week take the whole weeks around them", got == mondays(date(2026, 9, 28), 3), got)
    ck("a Sunday and the Monday after are two weeks",
       report.range_weeks("2026-10-11", "2026-10-12", day) == mondays(date(2026, 10, 5), 2))
    ck("one day is its week", report.range_weeks("2026-10-14", "2026-10-14", day) == [date(2026, 10, 12)])
    got = report.range_weeks(None, "2026-10-14", day)
    ck("only a last date: 8 weeks up to it", got == mondays(date(2026, 8, 24), 8), got)
    got = report.range_weeks("2026-11-04", "2027-03-01", day)
    ck("a last date ahead ends this week", got == mondays(date(2026, 11, 2), 4), got)
    got = report.range_weeks("2026-11-25", None, day)
    ck("a first date of today is this week", got == [last], got)
    got = report.range_weeks("2024-11-25", None, day)
    ck("2 years to today are a range", got == mondays(date(2024, 11, 25), 105), (got[0], len(got)))
    ck("a day more is not", refused("2024-11-24", None) == TOO_LONG, refused("2024-11-24", None))
    ck("2 years up to a last date, a day more is not",
       refused("2024-10-14", "2026-10-14") is None
       and refused("2024-10-13", "2026-10-14") == TOO_LONG,
       (refused("2024-10-14", "2026-10-14"), refused("2024-10-13", "2026-10-14")))
    ck("a last date ahead is measured to today", refused("2024-11-25", "2027-03-01") is None,
       refused("2024-11-25", "2027-03-01"))
    leap = date(2028, 2, 29)
    ck("2 years before 29 February is 28 February",
       len(report.range_weeks("2026-02-28", None, leap)) > 0
       and report.range_weeks("2026-02-28", None, leap)[0] == date(2026, 2, 23))
    for words in ("2026-13-01", "2026-02-30", "yesterday", "20261125", "2026-11-25T10:00:00",
                  "1999-12-31", "0002-01-01"):
        ck(f"{words!r} is no date, first or last",
           refused(words, None) == BAD_DATE and refused(None, words) == BAD_DATE,
           (refused(words, None), refused(None, words)))
    ck("a first date after the last", refused("2026-10-14", "2026-10-13") == BACKWARDS)
    ck("a first date ahead of today", refused("2026-11-26", None) == AHEAD)


# ── the read over HTTP ────────────────────────────────────────────────────────
async def scopes():
    async with cell() as (s, root, bare):
        print("== a cell with nothing on it ==")
        r = await get(root)
        body = r.json()
        ck("it answers", r.status_code == 200, r.text[:200])
        ck("four keys", sorted(body) == ["cannot_see", "range", "scope", "sections"], sorted(body))
        ck("the range: 8 weeks to this Sunday, this week running, nothing merged",
           body.get("range") == {"from": week(7).isoformat(),
                                 "to": (MONDAY + timedelta(days=6)).isoformat(),
                                 "weeks": [week(n).isoformat() for n in range(7, -1, -1)],
                                 "running_week": MONDAY.isoformat(), "merged": 0}, body.get("range"))
        ck("the scope: the factory, no project, no team",
           body.get("scope") == {"value": "factory", "name": None, "count": 0, "teams": [],
                                 "projects": []}, body.get("scope"))
        lines = body.get("cannot_see") or []
        ck("what it cannot see: three sentences, work outside Tares, pushes past the queue, list prices",
           len(lines) == 3 and lines[0] == SEEN and "merge queue" in lines[1]
           and "API-equivalent" in lines[2], lines)
        built = body.get("sections") or {}
        ck("every section is built", sorted(built) == sorted(FIXED)
           and all(v is not None for v in built.values()), built)
        ck("with no finding and no decision: the Architecture section lists no repo and no decision",
           built.get("architecture") == {"repos": [],
                                         "unplaced": {"decisions_total": 0, "decisions": []}},
           built.get("architecture"))
        merges = built.get("merges") or {}
        ck("with nothing merged: no merge, no level, every week at 0 and a file of the header alone",
           {k: v for k, v in merges.items() if k != "csv"} == {
               "total": 0, "rows": [], "levels": [],
               "weeks": [{"week": week(n).isoformat(), "L3-review": 0, "L4-ship": 0, "unrecorded": 0}
                         for n in range(7, -1, -1)]}
           and merges.get("csv", "").splitlines() == [
               "Merged at (UTC),Project,Ticket,Title,Repository,Pull request,Level,Gate,"
               "Reviewed commit,Merged commit,Review rounds,Approver"], merges)
        none = {"cost_usd": 0.0, "unpriced": 0}
        ck("with no reply: nothing spent, a row for each role, no project, no ticket costs yet",
           built.get("cost") == {
               "total": none,
               "roles": [{"role": role, **none} for role in ("builder", "reviewer", "orchestrator",
                                                             "releaser", "person", "other")],
               "projects": [], "by_ticket": None,
               "weeks": [{"week": week(n).isoformat(), **none, "projects": {}}
                         for n in range(7, -1, -1)]}, built.get("cost"))
        ck("with nothing reviewed, no finding, nothing worked out yet as recurring",
           built.get("review") == {
               "first_pass": {"passed": 0, "reviewed": 0}, "rounds_per_ticket": None,
               "findings": [], "projects": [], "recurring": None,
               "weeks": [{"week": week(n).isoformat(),
                          "first_pass": {"passed": 0, "reviewed": 0}, "rounds_per_ticket": None}
                         for n in range(7, -1, -1)]}, built.get("review"))
        ck("with nothing delivered and nothing earlier",
           built.get("delivered") == {
               "merged": 0, "milestones_accepted": 0, "projects_merged": 0,
               "start_to_merge": {"p50": None, "p90": None, "tickets": 0},
               "first_pass": {"passed": 0, "reviewed": 0}, "rounds_per_ticket": None,
               "previous": None,
               "weeks": [{"week": week(n).isoformat(), "merged": 0, "p50": None}
                         for n in range(7, -1, -1)]}, built.get("delivered"))
        nothing = dict.fromkeys(report.STAGES)
        ck("and no time spent, no share in any week, nothing worked out yet as stuck",
           built.get("time") == {
               "total": 0, "waiting_percent": None,
               "stages": [{"stage": stage, "total": 0, "waiting": 0, "agent": 0, "share": None,
                           "waiting_percent": None, "agent_percent": None}
                          for stage in report.STAGES],
               "weeks": [{"week": week(n).isoformat(), "share": nothing, "waiting": nothing}
                         for n in range(7, -1, -1)],
               "start_to_merge": {"merged": 0, "tickets": 0, "buckets": [0, 0, 0, 0, 0]},
               "stuck": None}, built.get("time"))
        ck("the fixed section keys, in the page's order", list(report.SECTIONS) == FIXED,
           list(report.SECTIONS))

        print("== a cell of one person ==")
        project(s, "uc_alpha", "Alpha", lead="local")
        r = await get(root)
        ck("a lead is no team there", r.json()["scope"]["teams"] == [], r.text[:200])
        r = await get(root, scope="team:local")
        ck("so asking for it finds none", r.status_code == 404, f"{r.status_code} {r.text[:200]}")
        # the owner arrives: now `local` is somebody, and still the only person
        ck("the cell's owner is still its one person",
           s.claim_local("owner@example.com", "Owner") is not None and s.only_local_person())
        r = await get(root)
        ck("a named lead is no team there either", r.json()["scope"]["teams"] == [], r.text[:200])
        r = await get(root, scope="team:local")
        ck("and asking for theirs finds none", r.status_code == 404, f"{r.status_code} {r.text[:200]}")
        solo = key(s, "ksolo", ["read"])
        r = await get(root, solo, scope="mine")
        ck("mine is everything for the one person", r.status_code == 200
           and r.json()["scope"]["count"] == 1, f"{r.status_code} {r.text[:200]}")

        # five projects, each with its own count of tickets merged last week, so a sum names them
        ada, bo, cy, dee, mia, nina, pat = (
            s.create_person(n, f"{n.lower()}@example.com")["id"]
            for n in ("Ada", "Bo", "Cy", "Dee", "Mia", "Nina", "Pat"))
        s.set_project_lead("uc_alpha", ada)
        project(s, "uc_beta", "beta", lead=ada)
        project(s, "uc_gamma", "Gamma", lead=bo)
        project(s, "uc_priv", "Private", lead=pat, private=True)
        project(s, "uc_other", "Other", lead=nina, kind=None)
        s.add_project_member("uc_priv", mia)
        done = noon(week(1))
        for uid, n in (("uc_alpha", 1), ("uc_beta", 2), ("uc_gamma", 4), ("uc_priv", 8),
                       ("uc_other", 16)):
            for i in range(n):
                merge(s, uid, done, assignee=cy if uid in ("uc_gamma", "uc_priv") and i == 0 else None)
        merge(s, "uc_alpha", noon(week(10)))                       # before the default range
        seed_usage(s, "s-nobody", "m1", replied_at=done, cost_usd=1.5)      # no project's: ""
        report.rebuild_all(s)

        k_ada = key(s, "kada", ["read", "admin"], person=ada)      # an admin key, not on Private
        k_bo = key(s, "kbo", ["read"], person=bo)
        k_cy = key(s, "kcy", ["read"], person=cy)
        k_dee = key(s, "kdee", ["read"], person=dee)
        k_mia = key(s, "kmia", ["read"], person=mia)
        k_pat = key(s, "kpat", ["read"], person=pat)
        k_nobody = key(s, "knobody", ["read"])

        every = ["uc_alpha", "uc_beta", "uc_gamma", "uc_other", "uc_priv"]
        open_ = ["uc_alpha", "uc_beta", "uc_gamma", "uc_other"]
        offered = [{"id": "uc_alpha", "name": "Alpha"}, {"id": "uc_beta", "name": "beta"},
                   {"id": "uc_gamma", "name": "Gamma"}]
        private = {"id": "uc_priv", "name": "Private"}
        two_teams = [{"id": ada, "name": "Ada"}, {"id": bo, "name": "Bo"}]
        three_teams = two_teams + [{"id": pat, "name": "Pat"}]

        real = report.SECTIONS["delivered"]
        report.SECTIONS["delivered"] = probe
        try:
            async def reads(label, who, scope, ids, merged, name=None, headers=None):
                r = await get(root, who, headers=headers, scope=scope)
                if r.status_code != 200:
                    return ck(label, False, f"{r.status_code} {r.text[:200]}")
                b = r.json()
                handed = b["sections"].get("delivered", {})
                rows = sorted(ids + ([""] if scope == "factory" else []))
                ck(f"{label}: its rows, count, merged and name",
                   handed.get("projects") == rows and handed.get("merged") == merged
                   and b["range"]["merged"] == merged and b["scope"]["count"] == len(ids)
                   and b["scope"]["name"] == name and b["scope"]["value"] == scope,
                   (handed, b["range"]["merged"], b["scope"]))
                # only Alpha has anything in the 8 weeks before the default range
                ck(f"{label}: the weeks before are of the same scope",
                   handed.get("before") == [i for i in ids if i == "uc_alpha"], handed)

            print("== the whole factory ==")
            await reads("the cell's own token", None, "factory", every, 31)
            await reads("a person not on Private", k_bo, "factory", open_, 23)
            await reads("an admin not on it", k_ada, "factory", open_, 23)
            await reads("its lead", k_pat, "factory", every, 31)
            await reads("a member of it", k_mia, "factory", every, 31)
            await reads("the cell's token narrowed to what everyone sees", None, "factory", open_, 23,
                        headers={"x-tares-viewer": "public"})
            for who, label, teams, projects in (
                    (None, "the cell's token", three_teams, offered + [private]),
                    (k_bo, "a person not on Private", two_teams, offered),
                    (k_ada, "an admin not on it", two_teams, offered),
                    (k_mia, "a member of it", three_teams, offered + [private])):
                got = (await get(root, who)).json()["scope"]
                ck(f"{label} is offered the factory projects and the people who lead one",
                   got["teams"] == teams and got["projects"] == projects, got)

            print("== mine ==")
            await reads("a lead", k_ada, "mine", ["uc_alpha", "uc_beta"], 3)
            await reads("a ticket's assignee, not the private one they cannot open", k_cy, "mine",
                        ["uc_gamma"], 4)
            await reads("the lead of Private", k_pat, "mine", ["uc_priv"], 8)
            await reads("someone with no project", k_dee, "mine", [], 0)
            await reads("the cell's token: everything", None, "mine", every, 31)
            await reads("the cell's token narrowed", None, "mine", open_, 23,
                        headers={"x-tares-viewer": "public"})
            await reads("a key of no person: what it can see, as on Home", k_nobody, "mine", open_, 23)

            print("== a team ==")
            await reads("what Ada leads", k_bo, f"team:{ada}", ["uc_alpha", "uc_beta"], 3, name="Ada")
            await reads("what Pat leads, for a member", k_mia, f"team:{pat}", ["uc_priv"], 8,
                        name="Pat")
            for who, label, tid in ((k_bo, "a team of private projects only", pat),
                                    (k_ada, "the same for an admin not on them", pat),
                                    (None, "a lead of no factory project", nina),
                                    (None, "someone who leads nothing", dee),
                                    (None, "an id that names nobody", "p_nobody")):
                r = await get(root, who, scope=f"team:{tid}")
                ck(f"{label}: unknown",
                   r.status_code == 404 and r.json() == {"detail": f"unknown team {tid!r}"},
                   f"{r.status_code} {r.text[:200]}")

            # the built-in `local` person is nobody while it has no email, as before an owner claims it
            def local_email(value):
                with s.sessions_db.begin() as conn:
                    conn.execute(update(people).where(people.c.id == "local").values(email=value))

            s.set_project_lead("uc_gamma", "local")
            local_email(None)
            offered_teams = (await get(root, None)).json()["scope"]["teams"]
            r = await get(root, None, scope="team:local")
            ck("a lead nobody has claimed is no team",
               offered_teams == [three_teams[0], three_teams[2]] and r.status_code == 404,
               f"{offered_teams} {r.status_code}")
            local_email("owner@example.com")
            s.set_project_lead("uc_gamma", bo)

            print("== one project ==")
            await reads("a project", k_bo, "project:uc_beta", ["uc_beta"], 2, name="beta")
            await reads("one that is no factory project, asked by name", k_bo, "project:uc_other",
                        ["uc_other"], 16, name="Other")
            await reads("Private, for a member", k_mia, "project:uc_priv", ["uc_priv"], 8,
                        name="Private")
            for who, label, uid, headers in (
                    (k_bo, "Private for a person not on it", "uc_priv", None),
                    (k_ada, "Private for an admin not on it", "uc_priv", None),
                    (k_cy, "Private for the assignee who cannot open it", "uc_priv", None),
                    (None, "Private for the narrowed token", "uc_priv", {"x-tares-viewer": "public"}),
                    (k_bo, "an id that names nothing", "uc_nope", None)):
                r = await get(root, who, headers=headers, scope=f"project:{uid}")
                ck(f"{label}: unknown, with nothing of it in the answer",
                   r.status_code == 404 and r.json() == {"detail": f"unknown project {uid!r}"},
                   f"{r.status_code} {r.text[:200]}")

            print("== the range over HTTP ==")
            r = (await get(root, from_=week(10).isoformat(), scope="project:uc_alpha")).json()
            ck("a first date takes its weeks and what merged in them",
               len(r["range"]["weeks"]) == 11 and r["range"]["merged"] == 2, r["range"])
            r = (await get(root, from_=(TODAY - timedelta(days=21)).isoformat())).json()
            ck("three weeks back is four weeks", r["range"]["weeks"]
               == [week(n).isoformat() for n in (3, 2, 1, 0)]
               and r["sections"]["delivered"]["weeks"] == r["range"]["weeks"], r["range"])
            r = (await get(root, to=(week(3) + timedelta(days=2)).isoformat())).json()
            ck("a last date in the past ends there, with no running week and only what merged by then",
               r["range"]["weeks"][-1] == week(3).isoformat() and r["range"]["running_week"] is None
               and r["range"]["to"] == (week(3) + timedelta(days=6)).isoformat()
               and r["range"]["merged"] == 1, r["range"])
            r = (await get(root, from_="2021-01-04", to="2021-02-01")).json()
            ck("a range with nothing in it: its weeks, nothing merged, the projects still counted",
               len(r["range"]["weeks"]) == 5 and r["range"]["merged"] == 0
               and r["scope"]["count"] == 5 and r["sections"]["delivered"]["projects"] == [], r)

            print("== bad input ==")
            for params, words in (
                    ({"from_": "2026-13-01"}, BAD_DATE), ({"to": "last week"}, BAD_DATE),
                    ({"from_": "1999-12-31"}, BAD_DATE),
                    ({"from_": week(1).isoformat(), "to": week(2).isoformat()}, BACKWARDS),
                    ({"from_": (TODAY + timedelta(days=1)).isoformat()}, AHEAD),
                    ({"from_": (TODAY - timedelta(days=732)).isoformat()}, TOO_LONG),
                    ({"scope": "everything"}, BAD_SCOPE), ({"scope": ""}, BAD_SCOPE),
                    ({"scope": "team:"}, BAD_SCOPE), ({"scope": "project:"}, BAD_SCOPE),
                    ({"scope": "mine:x"}, BAD_SCOPE), ({"scope": "person:x"}, BAD_SCOPE)):
                r = await get(root, **params)
                ck(f"{params} is refused in words", r.status_code == 400
                   and r.json() == {"detail": words}, f"{r.status_code} {r.text[:200]}")

            print("== who may read ==")
            r = await get(bare)
            ck("no credential: refused", r.status_code == 401, r.status_code)
            r = await get(bare, "not-a-key")
            ck("a key nobody made: refused", r.status_code == 401, r.status_code)
            r = await get(bare, key(s, "kingest", ["ingest"], person=bo))
            ck("a key that cannot read: refused", r.status_code == 403, f"{r.status_code} {r.text[:200]}")
            r = await get(bare, k_bo)
            ck("a read key: answered", r.status_code == 200, r.status_code)
            station = {"x-tares-station": "crew-reviewer", "x-tares-role": "reviewer"}
            for uid in ("uc_alpha", "uc_priv"):
                k = key(s, f"kp{uid[3:]}", ["read", "findings"], uid=uid)
                got = [(await get(bare, k, headers=h, **p)).status_code
                       for h in (None, station)
                       for p in ({}, {"scope": "mine"}, {"scope": f"project:{uid}"})]
                ck(f"a project key of {uid}: refused in every scope, also as a station",
                   got == [403] * 6, got)
            plain, as_station = await get(bare, k_bo), await get(bare, k_bo, headers=station)
            ck("a station's headers change nothing", as_station.status_code == 200
               and as_station.json() == plain.json(), as_station.text[:200])

            print("== no raw records on page load ==")
            # decisions that name tickets, by id and by label: placing them reads no ticket
            by_id, by_label = (pulled(s, "uc_beta", f"acme/{n}") for n in ("by-id", "by-label"))
            decided(s, "uc_beta", 1, "Named by its id?", [by_id])
            decided(s, "uc_beta", 1, "Named by its label?",
                    [factory_mod.label(s.get_ticket("uc_beta", by_label))])
            report.rebuild_all(s)
            raw = ["all_stage_changes", "all_reviews", "all_review_findings", "usage_by_week",
                   "list_desk", "milestones_by_project", "milestone_acceptances", "tickets_by_project",
                   "get_ticket", "repo_findings", "all_holder_changes", "ticket_replies"]
            touched = []

            def trap(name):
                def hit(*a, **kw):
                    touched.append(name)
                    raise RuntimeError(f"{name} read on page load")
                return hit

            reads_of_weeks = []
            real_read = report.read_weeks

            def counted(*a, **kw):
                reads_of_weeks.append(1)
                return real_read(*a, **kw)

            report.read_weeks = counted
            for name in raw:
                setattr(s, name, trap(name))
            try:
                got, placed = [], []
                for scope in ("factory", f"team:{ada}", "project:uc_beta"):
                    try:
                        r = await get(root, k_bo, scope=scope)
                        got.append(r.status_code)
                        if r.status_code == 200:
                            placed.append(rows_of(r.json()["sections"]["architecture"]))
                    except RuntimeError as e:
                        got.append(str(e))
                ck("the factory, a team and a project read the weekly rows and no raw record",
                   got == [200, 200, 200] and not touched, (got, touched))
                ck("and place a decision under the repo of the ticket it names",
                   len(placed) == 3 and all(
                       asked(p.get("acme/by-id", {})) == ["Named by its id?"]
                       and asked(p.get("acme/by-label", {})) == ["Named by its label?"]
                       for p in placed), placed)
                ck("one read of the weekly table for each", len(reads_of_weeks) == 3, len(reads_of_weeks))
                del s.tickets_by_project          # mine is Home's rule, which reads the tickets
                try:
                    got = (await get(root, k_cy, scope="mine")).status_code
                except RuntimeError as e:
                    got = str(e)
                ck("mine also reads the tickets, for Home's rule, and nothing else raw",
                   got == 200 and not touched, (got, touched))
            finally:
                report.read_weeks = real_read
                for name in raw:
                    s.__dict__.pop(name, None)
        finally:
            report.SECTIONS["delivered"] = real


def moved(s, uid, back, hours=None) -> str:
    """A ticket merged at noon on the Monday `back` weeks ago, `hours` after it was taken up
    (None: it was never marked doing)."""
    tid = s.create_ticket(uid, "tares", "t")
    when = noon(week(back))
    if hours is not None:
        s.add_ticket_history(tid, uid, "stage", "doing", None, None, None,
                             when - timedelta(hours=hours))
    s.add_ticket_history(tid, uid, "stage", "merged", None, None, None, when)
    return tid


def reviewed(s, uid, tid, back, verdict, minute=0, by="crew-reviewer"):
    s.add_review(uid, tid, "7", "abc1234", verdict, "read the diff", "", by, [], {},
                 factory_mod.finding_kind, noon(week(back)) + timedelta(minutes=minute))


def accepted(s, uid, back):
    mid = s.create_milestone(uid, f"m{back}")
    s.accept_milestone(uid, mid, "Ada", None, noon(week(back)))


def close(got, want) -> bool:
    """Equal, with hours and averages equal to a millionth."""
    if isinstance(want, dict):
        return (isinstance(got, dict) and got.keys() == want.keys()
                and all(close(got[k], v) for k, v in want.items()))
    if isinstance(want, list):
        return (isinstance(got, list) and len(got) == len(want)
                and all(close(g, w) for g, w in zip(got, want)))
    if isinstance(want, float):
        return isinstance(got, (int, float)) and abs(got - want) < 1e-6
    return got == want


def figures(merged, milestones, projects, p50, p90, tickets, passed, first, rounds) -> dict:
    return {"merged": merged, "milestones_accepted": milestones, "projects_merged": projects,
            "start_to_merge": {"p50": p50, "p90": p90, "tickets": tickets},
            "first_pass": {"passed": passed, "reviewed": first}, "rounds_per_ticket": rounds}


async def delivered():
    async with cell() as (s, root, bare):
        print("== the Delivered section ==")
        ada, pat = (s.create_person(n, f"{n.lower()}@example.com")["id"] for n in ("Ada", "Pat"))
        project(s, "uc_one", "One", lead=ada)
        project(s, "uc_two", "Two", lead=ada)
        project(s, "uc_hid", "Hidden", lead=pat, private=True)
        # the range: the 4 weeks to this one
        a, b = moved(s, "uc_one", 1, 1), moved(s, "uc_one", 1, 3)
        moved(s, "uc_one", 1)                                   # no start
        c = moved(s, "uc_one", 3, 5)
        moved(s, "uc_two", 1, 7)
        moved(s, "uc_hid", 1, 50)
        reviewed(s, "uc_one", a, 1, "changes")
        reviewed(s, "uc_one", a, 1, "pass", minute=5)           # a second round
        reviewed(s, "uc_one", b, 1, "pass")
        reviewed(s, "uc_one", b, 1, "pass", minute=5, by=merge_queue.BY)    # no round
        reviewed(s, "uc_one", c, 3, "pass")
        for uid, back in (("uc_one", 2), ("uc_two", 0), ("uc_two", 0), ("uc_hid", 1)):
            accepted(s, uid, back)
        # the 4 weeks before it, from its first week to its last
        d = moved(s, "uc_one", 4, 10)
        moved(s, "uc_one", 7, 20)
        moved(s, "uc_hid", 5, 40)
        reviewed(s, "uc_one", d, 4, "pass")
        for uid, back in (("uc_one", 6), ("uc_one", 7), ("uc_hid", 5)):
            accepted(s, uid, back)
        # one week more, in neither
        moved(s, "uc_one", 8, 100)
        accepted(s, "uc_one", 8)
        # long ago: a review with no merge, and cost nothing claims in the week before it
        e = s.create_ticket("uc_two", "tares", "open")
        reviewed(s, "uc_two", e, 30, "changes")
        seed_usage(s, "s-nobody", "m1", replied_at=noon(week(31)), cost_usd=1.5)
        report.rebuild_all(s)
        k_ada = key(s, "kdada", ["read"], person=ada)

        async def read(who=None, **params):
            params.setdefault("from_", week(3).isoformat())
            r = await get(root, who, **params)
            return r.json()["sections"]["delivered"] if r.status_code == 200 else {"error": r.text}

        def weekly(*values):
            return [{"week": week(3 - i).isoformat(), "merged": n, "p50": p}
                    for i, (n, p) in enumerate(values)]

        got = await read(k_ada)
        want = figures(5, 3, 2, 4.0, 6.4, 4, 2, 3, 4 / 3)
        ck("the range: merged, milestones, projects, start to merge, first pass and rounds",
           close({k: got.get(k) for k in want}, want), got)
        ck("the weeks before: the same figures for the 4 weeks just before, and no further",
           close(got.get("previous"), figures(2, 2, 1, 15.0, 19.0, 2, 1, 1, 1.0)),
           got.get("previous"))
        ck("week by week: merged, and the median of the week, none in a week without a merge",
           close(got.get("weeks"), weekly((1, 5.0), (0, None), (4, 3.0), (0, None))),
           got.get("weeks"))
        got = await read()
        ck("a reader who sees Hidden has it in the range and in the weeks before",
           close({k: got.get(k) for k in want}, figures(6, 4, 3, 5.0, 32.8, 5, 2, 3, 4 / 3))
           and close(got.get("previous"), figures(3, 3, 2, 20.0, 36.0, 3, 1, 1, 1.0)), got)
        got = await read(k_ada, scope="project:uc_two")
        ck("one project: every figure is its own, and it has no earlier week",
           close(got, {**figures(1, 2, 1, 7.0, 7.0, 1, 0, 0, None), "previous": None,
                       "weeks": weekly((0, None), (0, None), (1, 7.0), (0, None))}), got)
        got = await read(from_=week(30).isoformat(), to=week(30).isoformat())
        ck("a review with no merge: nothing merged, no start to merge, the review counted",
           close({k: got.get(k) for k in want}, figures(0, 0, 0, None, None, 0, 0, 1, 1.0))
           and got.get("weeks") == [{"week": week(30).isoformat(), "merged": 0, "p50": None}], got)
        ck("cost nothing claims in the week before is nothing earlier", got.get("previous", 0) is None,
           got.get("previous"))


def staged(s, uid, *moves, title="t") -> str:
    """A ticket moved through `moves`, (stage, moment) in order, and left in the last one."""
    tid = s.create_ticket(uid, "tares", title)
    for stage, at in moves:
        s.add_ticket_history(tid, uid, "stage", stage, None, None, None, at)
    s.update_ticket(tid, stage=moves[-1][0])
    return tid


def ask(s, uid, text, at, tid=None, closed=None, state="answered", blocking=True, **more) -> str:
    """A desk question of `uid` (None: of no project) asked at `at`, naming `tid`, and closed at
    `closed` as `state` when given. Returns its label (D7)."""
    tickets = [factory_mod.label(s.get_ticket(uid, tid))] if tid else []
    d = s.add_desk_item(uid, text, "", [], "", "", "", tickets, blocking, "builder",
                        more.pop("assumptions", []), at, **more)
    if closed:
        s.close_desk_item(d["id"], state, None, closed)
    return d["label"]


async def time_section():
    async with cell() as (s, root, bare):
        print("== the Time section ==")
        project(s, "uc_time", "Time")
        project(s, "uc_edges", "Edges")
        seven = noon(week(2)) - timedelta(hours=5)

        def at(hour, minute=0):                       # on the Monday two weeks back
            return seven + timedelta(hours=hour - 7, minutes=minute)

        # 2 hours in review, 90 minutes of them with an answered blocking question
        a = staged(s, "uc_time", ("doing", at(7)), ("review", at(8)), ("merged", at(10)))
        ask(s, "uc_time", "answered", at(8), a, closed=at(9, 30))
        # none of these waits: each would add to the 30 minutes left if it were counted
        ask(s, "uc_time", "open", at(9, 35), a)
        ask(s, "uc_time", "withdrawn", at(9, 35), a, closed=at(9, 50), state="withdrawn")
        ask(s, "uc_time", "holds nothing up", at(9, 35), a, closed=at(9, 55), blocking=False)
        # no start: it was never marked doing
        staged(s, "uc_time", ("changes", at(7)), ("blocked", at(8)), ("merged", at(12)))
        for hours in (0.5, 1, 4, 24, 72, 100):        # start to merge, on and between the edges
            end = noon(week(1))
            staged(s, "uc_edges", ("doing", end - timedelta(hours=hours)), ("merged", end))
        report.rebuild_all(s)

        async def read(uid, first, last):
            r = await get(root, scope=f"project:{uid}", from_=week(first).isoformat(),
                          to=week(last).isoformat())
            return r.json()["sections"]["time"] if r.status_code == 200 else {"error": r.text}

        def row(stage, total, waiting, agent, share, waiting_percent, agent_percent):
            return {"stage": stage, "total": total, "waiting": waiting, "agent": agent,
                    "share": share, "waiting_percent": waiting_percent,
                    "agent_percent": agent_percent}

        def by_stage(*values):
            return dict(zip(("doing", "review", "changes", "blocked"), values))

        got = await read("uc_time", 3, 2)
        stages = {x["stage"]: x for x in got.get("stages", [])}
        ck("2 hours in review with a 90-minute blocking question: 90 minutes waiting, 30 agent",
           close(stages.get("review"), row("review", 2.0, 1.5, 0.5, 25, 75, 25)),
           stages.get("review"))
        ck("the stages, in order: total, waiting and agent time, share and the split",
           close(got.get("stages"), [row("doing", 1.0, 0.0, 1.0, 13, 0, 100),
                                     row("review", 2.0, 1.5, 0.5, 25, 75, 25),
                                     row("changes", 1.0, 0.0, 1.0, 12, 0, 100),
                                     row("blocked", 4.0, 0.0, 4.0, 50, 0, 100)]), got.get("stages"))
        ck("waiting and agent time add up to each stage's total",
           bool(stages) and all(abs(x["waiting"] + x["agent"] - x["total"]) < 1e-6
                                for x in stages.values()), stages)
        shares = [x.get("share") or 0 for x in stages.values()]
        ck("shares of 12.5, 25, 12.5 and 50 still add to 100", sum(shares) == 100, shares)
        ck("all stages together: 8 hours, 19 percent of them waiting",
           close(got.get("total"), 8.0) and got.get("waiting_percent") == 19,
           (got.get("total"), got.get("waiting_percent")))
        nothing = by_stage(None, None, None, None)
        ck("week by week: a week with nothing is no value, not 0",
           got.get("weeks") == [
               {"week": week(3).isoformat(), "share": nothing, "waiting": nothing},
               {"week": week(2).isoformat(), "share": by_stage(13, 25, 12, 50),
                "waiting": by_stage(0, 75, 0, 0)}], got.get("weeks"))
        ck("merged and start to merge: the ticket with no start is in the first count only",
           got.get("start_to_merge") == {"merged": 2, "tickets": 1, "buckets": [0, 1, 0, 0, 0]},
           got.get("start_to_merge"))
        got = await read("uc_edges", 3, 1)
        ck("a time on an edge (1, 4, 24, 72 hours) is in the bucket above it",
           got.get("start_to_merge") == {"merged": 6, "tickets": 6, "buckets": [1, 1, 1, 1, 2]},
           got.get("start_to_merge"))
        ck("a stage with no time has a share of 0 and no split",
           [(x["share"], x["waiting_percent"], x["agent_percent"]) for x in got.get("stages", [])]
           == [(100, 0, 100)] + [(0, None, None)] * 3, got.get("stages"))
        got = await read("uc_time", 3, 3)
        ck("a range with nothing in it: no time, no share, no percent",
           got.get("total") == 0 and got.get("waiting_percent") is None
           and [x["share"] for x in got.get("stages", [])] == [None] * 4, got)


async def stuck():
    async with cell() as (s, root, bare):
        print("== stuck now ==")
        ada, bo, mia = (s.create_person(n, f"{n.lower()}@example.com")["id"]
                        for n in ("Ada", "Bo", "Mia"))
        project(s, "uc_a", "Alpha", lead=ada)
        project(s, "uc_b", "Beta", lead=bo)
        project(s, "uc_priv", "Hush", lead=mia, private=True)
        project(s, "uc_later", "Parked", lead=bo, status="later")
        project(s, "uc_done", "Closed", lead=bo, status="done")
        project(s, "uc_fin", "Finished", lead=ada)
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def ago(hours):
            return now - timedelta(hours=hours)

        folder = "/Users/builder/code/alpha"
        for uid, path in (("uc_a", folder), ("uc_fin", "/w/finished")):
            s.set_handover(uid, [{"path": path, "name": "acme/alpha"}], None, None, ago(900))
        s.finish_handover("uc_fin", "done", None, None, ago(1))

        # questions: six that are stuck across two projects, and some that are not
        asked = {h: ask(s, uid, f"Question {h}", ago(h))
                 for uid, h in (("uc_a", 8), ("uc_b", 9), ("uc_b", 7), ("uc_a", 6), ("uc_b", 5))}
        asked[10] = ask(s, "uc_a", f"Delete {folder}/data?", ago(10))
        ask(s, "uc_a", "answered long ago", ago(100), closed=ago(99))
        ask(s, "uc_a", "withdrawn long ago", ago(98), closed=ago(97), state="withdrawn")
        ask(s, "uc_a", "holds nothing up", ago(96), blocking=False)
        asked[50] = ask(s, "uc_priv", "hush question", ago(50))
        # the oldest of all: each would head the list if its project were not resting
        ask(s, "uc_fin", "Of a finished project", ago(403))
        ask(s, "uc_later", "Of a project kept for later", ago(402))
        ask(s, "uc_done", "Of a done project", ago(401))
        ask(s, None, "For Ada, of no project", ago(20), to_person_id=ada)
        ask(s, None, "For Bo, of no project", ago(21), to_person_id=bo)

        # tickets: six that are stuck, oldest first, and some that are not
        want = {}

        def ticket(uid, hours, *moves, title="t", **fields):
            tid = staged(s, uid, *[(stage, ago(h)) for stage, h in moves], title=title)
            if fields:   # closed, then put back in a stage: only its status says it is not stuck
                s.update_ticket(tid, **fields)
                s.update_ticket(tid, stage=moves[-1][0])
                s.add_ticket_history(tid, uid, "stage", moves[-1][0], None, None, None,
                                     datetime.now(timezone.utc))
            want[hours] = (uid, factory_mod.label(s.get_ticket(uid, tid)), moves[-1][0])
            return tid

        ticket("uc_a", 60, ("doing", 60), title=f"Fix {folder}/x.py")
        ticket("uc_b", 50, ("review", 50))
        ticket("uc_a", 40, ("changes", 40))
        ticket("uc_b", 30, ("blocked", 30))
        # in review twice, and the move repeated: its age counts from when it came back
        ticket("uc_a", 20, ("review", 100), ("changes", 80), ("review", 20), ("review", 15))
        ticket("uc_b", 10, ("doing", 10))
        staged(s, "uc_a", ("doing", ago(320)), ("merged", ago(300)))
        staged(s, "uc_a", ("todo", ago(300)))
        s.create_ticket("uc_a", "tares", "never moved")
        ticket("uc_a", 310, ("review", 310), status="done")
        ticket("uc_a", 311, ("review", 311), status="canceled")
        s.delete_ticket(staged(s, "uc_a", ("review", ago(500))))
        ticket("uc_priv", 200, ("review", 200), title="hush ticket")
        ticket("uc_later", 400, ("review", 400))
        ticket("uc_done", 401, ("review", 401))
        ticket("uc_fin", 402, ("review", 402))

        k_ada = key(s, "kada", ["read"], person=ada)
        k_bo = key(s, "kbo", ["read"], person=bo)
        k_mia = key(s, "kmia", ["read"], person=mia)

        async def read(who, **params):
            r = await get(root, who, **params)
            if r.status_code != 200:
                return {"error": r.text}, r.text
            return r.json()["sections"]["time"].get("stuck"), r.text

        got, _ = await read(k_ada)
        ck("before the first pass there is nothing to show, not an empty list", got is None, got)
        report.rebuild_all(s)

        def texts(got):
            return [q["text"] for q in (got or {}).get("questions", {}).get("rows", [])]

        def tickets(got):
            return [(t["project"]["id"], t["label"], t["stage"])
                    for t in (got or {}).get("tickets", {}).get("rows", [])]

        def counts(got):
            return tuple((got or {}).get(k, {}).get("count") for k in ("questions", "tickets"))

        oldest = ["Delete acme/alpha/data?", "Question 9", "Question 8", "Question 7"]
        got, raw = await read(k_ada)
        ck("the five oldest open blocking questions, oldest first, one of no project among them",
           texts(got) == ["For Ada, of no project"] + oldest, texts(got))
        ck("the six and her question of no project are counted, none of a resting project",
           counts(got)[0] == 7, counts(got))
        ck("the five tickets longest in one stage, across projects, longest first",
           tickets(got) == [want[h] for h in (60, 50, 40, 30, 20)], tickets(got))
        ck("six are stuck: none merged, to do, done, canceled, deleted, finished or kept for later",
           counts(got)[1] == 6, counts(got))
        rows = (got or {}).get("tickets", {}).get("rows", [])
        ck("each ticket since its latest move into the stage, a repeated move not counting",
           [t["at"] for t in rows] == [report._stamp(ago(h)) for h in (60, 50, 40, 30, 20)],
           [t["at"] for t in rows])
        q = (got or {}).get("questions", {}).get("rows", [{}, {}])
        ck("a question: its number, label, text, when it was asked and its project",
           q[1] == {"number": int(asked[10][1:]), "label": asked[10], "text": oldest[0],
                    "at": report._stamp(ago(10)), "project": {"id": "uc_a", "name": "Alpha"}}
           and q[0].get("project", 0) is None and "to_person_id" not in q[0], q[:2])
        ck("a ticket's title names no folder of the crew's machine",
           bool(rows) and rows[0]["title"] == "Fix acme/alpha/x.py" and folder not in raw,
           rows[:1])
        ck("somebody not on the private project reads no word of it",
           "hush" not in raw.lower() and "uc_priv" not in raw and "For Bo" not in raw, raw[:400])

        got, _ = await read(k_bo)
        ck("a question of no project is read by whom it is for",
           texts(got) == ["For Bo, of no project"] + oldest and counts(got) == (7, 6),
           (texts(got), counts(got)))
        got, _ = await read(k_mia)
        ck("who is on the private project has it first, in the list and in the count",
           texts(got) == ["hush question"] + oldest and counts(got) == (7, 7)
           and tickets(got) == [want[h] for h in (200, 60, 50, 40, 30)],
           (texts(got), counts(got), tickets(got)))
        got, raw = await read(k_ada, scope="project:uc_a")
        ck("one project: its own questions and tickets, none of no project",
           texts(got) == ["Delete acme/alpha/data?", "Question 8", "Question 6"]
           and counts(got) == (3, 3) and tickets(got) == [want[h] for h in (60, 40, 20)],
           (texts(got), counts(got), tickets(got)))
        got, _ = await read(k_ada, scope=f"team:{bo}")
        ck("one team: its projects only",
           texts(got) == ["Question 9", "Question 7", "Question 5"] and counts(got) == (3, 3)
           and tickets(got) == [want[h] for h in (50, 30, 10)],
           (texts(got), counts(got), tickets(got)))
        got, _ = await read(k_ada, scope="mine")
        ck("mine: the projects that are mine, none of no project",
           texts(got) == ["Delete acme/alpha/data?", "Question 8", "Question 6"]
           and counts(got) == (3, 3), (texts(got), counts(got)))
        for uid, word in (("uc_fin", "finished"), ("uc_later", "Later"), ("uc_done", "done")):
            got, _ = await read(k_ada, scope=f"project:{uid}")
            ck(f"a {word} project read alone: no question and no ticket of it is stuck",
               texts(got) == [] and tickets(got) == [] and counts(got) == (0, 0),
               (texts(got), counts(got), tickets(got)))
        now_, _ = await read(k_ada)
        past, _ = await read(k_ada, from_=week(30).isoformat(), to=week(30).isoformat())
        ck("a past range still shows what is stuck now",
           past == now_ and counts(past) == (7, 6), (past, now_))

        # one project with more than the five shown: it keeps its oldest, and counts them all
        project(s, "uc_many", "Many")
        for h in (1, 7, 3, 5, 2, 6, 4):
            ask(s, "uc_many", f"Many {h}", ago(h))
            ticket("uc_many", 1000 + h, ("doing", 1000 + h))
        report.rebuild_all(s)
        got, _ = await read(k_ada, scope="project:uc_many")
        ck("a project with seven of each shows its five oldest and says seven",
           texts(got) == [f"Many {h}" for h in (7, 6, 5, 4, 3)] and counts(got) == (7, 7)
           and tickets(got) == [want[1000 + h] for h in (7, 6, 5, 4, 3)],
           (texts(got), counts(got), tickets(got)))


async def stuck_rows():
    async with cell() as (s, root, bare):
        print("== every stuck question has its row on the project page ==")
        uid, repo, head = "uc_shop", "example-org/shop", "aaaaaaa1111"
        project(s, uid, "Shop")
        now = datetime.now(timezone.utc)
        s.set_handover(uid, [{"path": "/w/shop", "name": repo}], None, None, now)
        tid = s.create_ticket(uid, "tares", "Add the cart", repo=repo)
        s.update_ticket(tid, stage="review", pr={
            "url": f"https://github.com/{repo}/pull/12", "repo": repo, "number": 12,
            "head": head, "base": "main", "state": "open", "checked_at": None})
        s.add_review(uid, tid, None, head, "pass", "ran the tests", None, "crew-reviewer", [], {},
                     factory_mod.finding_kind, now)
        # a merge the queue asks for, a call kept on the desk, a question and a permission
        queued = s.merge_queue_add(repo, "main", 12, head, tid, uid)
        merge_queue._ask(s, queued, s.get_ticket(uid, tid))
        merge_ask = s.get_desk_item(queued["desk_item"])["label"]
        call = s.add_assumption(uid, "Which logger?", "the standard one", "no new package", "", [],
                                "shop-build", "builder", now)
        batch = ask(s, uid, "Keep these calls?", now, assumptions=[call["label"]])
        plain = ask(s, uid, "Which colour?", now)
        permission = ask(s, uid, "May the crew clear the build folder?", now, command="rm -rf build")
        report.rebuild_all(s)
        r = await get(root, scope=f"project:{uid}")
        listed = sorted(q["label"] for q in r.json()["sections"]["time"]["stuck"]["questions"]["rows"])
        ck("stuck now lists the merge ask, the batch of calls, the question and the permission",
           listed == sorted([merge_ask, batch, plain, permission]), listed)
        page = (await root.get(f"/api/factory/projects/{uid}")).json()
        shown = page.get("waiting", []) + page.get("waiting_others", [])

        def names(item) -> list[str]:
            own = re.match(r"^(?:question|permission|desk):(D\d+)$", item["id"])
            return ([own.group(1)] if own else []) + item.get("desk_labels", [])

        missing = [d for d in listed if not any(d in names(item) for item in shown)]
        ck("each has a row on the project page that names it", bool(listed) and not missing,
           (missing, [(i["id"], i.get("desk_labels")) for i in shown]))
        took_in = {i["kind"]: i.get("desk_labels") for i in shown if i.get("desk_labels")}
        ck("the pull request row names the merge ask it took in, the calls row its batch",
           took_in == {"prs_to_review": [merge_ask], "calls_to_confirm": [batch]}, took_in)


_NUMBERS = itertools.count(1)


def pulled(s, uid, repo) -> str:
    """A ticket of `uid` with a pull request in `repo`."""
    tid, n = s.create_ticket(uid, "tares", "t", repo=repo), next(_NUMBERS)
    s.update_ticket(tid, pr={"url": f"https://github.com/{repo}/pull/{n}", "repo": repo, "number": n,
                             "head": "abc1234", "base": "main", "state": "open", "checked_at": None})
    return tid


def raised(s, uid, tid, back, verdict, findings=(), resolved=None, minute=0):
    """A review round with new findings, each (severity, blocking, text), and what became of
    earlier ones."""
    new = [{"severity": severity, "file": None, "line": None, "blocking": blocking, "text": text}
           for severity, blocking, text in findings]
    s.add_review(uid, tid, "7", "abc1234", verdict, "read the diff", "", "crew-reviewer", new,
                 resolved or {}, factory_mod.finding_kind, noon(week(back)) + timedelta(minutes=minute))


def rate(passed, first, rounds) -> dict:
    return {"first_pass": {"passed": passed, "reviewed": first}, "rounds_per_ticket": rounds}


def split(kind, severity, fixed, still) -> dict:
    return {"kind": kind, "severity": severity, "fixed": fixed, "open": still}


async def review_section():
    async with cell() as (s, root, bare):
        print("== the Review section ==")
        ada, pat = (s.create_person(n, f"{n.lower()}@example.com")["id"] for n in ("Ada", "Pat"))
        project(s, "uc_one", "One", lead=ada)
        project(s, "uc_two", "beta", lead=ada)
        project(s, "uc_hid", "Gamma", lead=pat, private=True)
        project(s, "uc_quiet", "Quiet", lead=ada)
        a, b, c, d = (s.create_ticket("uc_one", "tares", "t") for _ in range(4))
        f = s.create_ticket("uc_two", "tares", "t")
        g, h = (s.create_ticket("uc_hid", "tares", "t") for _ in range(2))
        q = s.create_ticket("uc_quiet", "tares", "t")
        # the range: the 4 weeks to this one. None of these tickets names a repo
        raised(s, "uc_one", a, 1, "changes", [
            ("P1", True, "No migration for the new column"),
            ("P2", False, "The error handling swallows the timeout"),
            ("P3", False, "Zebra stripes look wrong"),
            ("P3", False, "Purple button wants padding"),
            ("P2", False, "A secret is written to the log")])
        raised(s, "uc_one", a, 1, "pass", minute=5,
               resolved={"F1": "fixed", "F2": "open", "F3": "fixed", "F4": "open", "F5": "fixed"})
        raised(s, "uc_one", b, 1, "pass")
        reviewed(s, "uc_one", b, 1, "pass", minute=5, by=merge_queue.BY)    # no round
        raised(s, "uc_one", c, 3, "pass", [("P3", False, "The README is stale")])
        raised(s, "uc_two", f, 2, "changes", [
            ("P1", False, "This migration has no way back"),
            ("P2", True, "Uncaught exception in the worker"),
            ("P3", False, "Odd spacing between cards"),
            ("P3", False, "Heading colour drifts")])
        s.add_ticket_history(f, "uc_two", "stage", "merged", None, None, None, noon(week(1)))
        s.update_ticket(f, stage="merged")                     # merged with its findings open
        raised(s, "uc_hid", g, 0, "changes", [("P1", False, "A race condition on save")])
        reviewed(s, "uc_hid", g, 0, "changes", minute=5)
        reviewed(s, "uc_hid", g, 0, "pass", minute=10)
        # the 4 weeks before it
        raised(s, "uc_one", d, 4, "pass", [("P2", False, "The caller leaves it unhandled")])
        reviewed(s, "uc_hid", h, 5, "pass")
        reviewed(s, "uc_quiet", q, 6, "pass")
        m = s.create_ticket("uc_quiet", "tares", "t")          # merged in the range, never reviewed
        s.add_ticket_history(m, "uc_quiet", "stage", "merged", None, None, None, noon(week(1)))
        s.update_ticket(m, stage="merged")
        report.rebuild_all(s)
        k_ada = key(s, "krada", ["read"], person=ada)

        async def read(who=None, **params):
            params.setdefault("from_", week(3).isoformat())
            r = await get(root, who, **params)
            return r.json()["sections"] if r.status_code == 200 else {"error": r.text}

        def weekly(*values):
            return [{"week": week(3 - i).isoformat(), **rate(*v)} for i, v in enumerate(values)]

        sections = await read(k_ada)
        got, other = sections.get("review", {}), sections.get("delivered", {})
        want = rate(2, 4, 5 / 4)
        ck("the range: passed in the first round, and rounds per ticket",
           close({k: got.get(k) for k in want}, want), got)
        ck("the same two figures as Delivered gives",
           all(got.get(k, "none") == other.get(k) for k in want), (got, other))
        ck("week by week, none in a week without a first review",
           close(got.get("weeks"), weekly((1, 1, 1.0), (0, 1, 1.0), (1, 2, 1.5), (0, 0, None))),
           got.get("weeks"))
        ck("findings first raised in the range by kind and severity, fixed and still open, the "
           "unnamed kinds one other row, blocking and not in one row, most open first",
           got.get("findings") == [
               split("other", "P3", 1, 3), split("error handling", "P2", 0, 2),
               split("migration", "P1", 1, 1), split("docs out of date", "P3", 0, 1),
               split("security", "P2", 1, 0)], got.get("findings"))
        total = sum(x["fixed"] + x["open"] for x in got.get("findings", []))
        ck("fixed and still open add up to the 10 findings raised in the range", total == 10, total)
        ck("each project with a first review in the range, lowest pass rate first, with the same "
           "weeks before, none where nothing was reviewed then",
           close(got.get("projects"), [
               {"project": "uc_two", "name": "beta", **rate(0, 1, 1.0), "previous": None},
               {"project": "uc_one", "name": "One", **rate(2, 3, 4 / 3),
                "previous": rate(1, 1, 1.0)}]), got.get("projects"))
        got = (await read()).get("review", {})
        ck("an equal pass rate goes by name, whatever its case",
           close(got.get("projects", [])[:2], [
               {"project": "uc_two", "name": "beta", **rate(0, 1, 1.0), "previous": None},
               {"project": "uc_hid", "name": "Gamma", **rate(0, 1, 3.0),
                "previous": rate(1, 1, 1.0)}]) and len(got.get("projects", [])) == 3,
           got.get("projects"))
        ck("equal open counts go by severity, then kind",
           [(x["severity"], x["kind"]) for x in got.get("findings", []) if x["open"] == 1]
           == [("P1", "concurrency"), ("P1", "migration"), ("P3", "docs out of date")],
           got.get("findings"))
        got = (await read(k_ada, from_=week(7).isoformat(), to=week(4).isoformat())).get("review", {})
        ck("the weeks before, asked for as a range, hold their own finding",
           got.get("findings") == [split("error handling", "P2", 0, 1)]
           and got.get("first_pass") == {"passed": 2, "reviewed": 2}, got)
        got = (await read(scope="project:uc_hid", from_=week(5).isoformat(),
                          to=week(5).isoformat())).get("review", {})
        ck("reviewed with no finding: the figures, and an empty table",
           got.get("findings") == [] and got.get("first_pass") == {"passed": 1, "reviewed": 1}, got)
        got = (await read(scope="project:uc_quiet")).get("review", {})
        ck("a merge and no first review in the range: no figure, no finding, no project row",
           close({k: got.get(k) for k in ("first_pass", "rounds_per_ticket", "weeks", "findings",
                                          "projects")},
                 {**rate(0, 0, None), "weeks": weekly(*[(0, 0, None)] * 4), "findings": [],
                  "projects": []}), got)


async def recurring_rows():
    async with cell() as (s, root, bare):
        print("== recurring findings ==")
        ada, bo, mia, pat = (s.create_person(n, f"{n.lower()}@example.com")["id"]
                             for n in ("Ada", "Bo", "Mia", "Pat"))
        project(s, "uc_shop", "Shop", lead=ada)
        project(s, "uc_site", "Site", lead=ada)
        project(s, "uc_far", "Far", lead=bo)
        project(s, "uc_vault", "Vault", lead=pat, private=True)
        s.add_project_member("uc_vault", mia)
        k_ada, k_mia, k_pat = (key(s, f"krec{i}", ["read"], person=p)
                               for i, p in enumerate((ada, mia, pat)))

        async def section(who=None, **params):
            r = await get(root, who, **params)
            return r.json()["sections"]["review"] if r.status_code == 200 else {"error": r.text}

        async def rows(who=None, **params):
            return ((await section(who, **params)).get("recurring") or {}).get("rows")

        def brief(got):
            return None if got is None else [(r["repo"], r["kind"], r["count"]) for r in got]

        got = await section()
        ck("before the first pass the list is not worked out", got.get("recurring", 0) is None, got)
        report.rebuild_all(s)
        got = (await section()).get("recurring") or {}
        ck("a pass with no finding: no row, over the last 30 days",
           got.get("rows") == [] and got.get("days") == 30 and bool(got.get("at")), got)

        shop, docs, many = "example-org/shop", "example-org/docs", "example-org/many"

        def found(repo, uid, text, tickets=1, back=1, each=1):
            """`tickets` tickets of `uid` with a pull request in `repo` (None: with none), each with
            `each` findings of `text` raised `back` weeks ago."""
            for _ in range(tickets):
                tid = pulled(s, uid, repo) if repo else s.create_ticket(uid, "tares", "t")
                raised(s, uid, tid, back, "changes", [("P2", False, text)] * each)

        found(shop, "uc_shop", "No migration for it", tickets=2)
        found(shop, "uc_site", "No migration for it")
        found(shop, "uc_vault", "No migration for it", tickets=2)
        found(shop, "uc_vault", "Uncaught exception", tickets=2)
        found(shop, "uc_shop", "Uncaught exception")
        found(shop, "uc_shop", "The README is stale", tickets=4, back=6)    # older than 30 days
        found(shop, "uc_shop", "A secret in the log", tickets=2, each=2)    # 2 tickets, 4 findings
        found(None, "uc_shop", "A race condition", tickets=3)               # no repo
        found(docs, "uc_site", "A race condition", tickets=3)
        stale = await rows()
        ck("the list is what the last pass saved", stale == [], stale)
        report.rebuild_all(s)

        got = await rows()
        ck("a repo and kind on 3 tickets or more, most tickets first, then by repo",
           brief(got) == [(shop, "migration", 5), (docs, "concurrency", 3),
                          (shop, "error handling", 3)], brief(got))
        for who, label in ((None, "the cell's token"), (k_ada, "a lead"), (k_mia, "a member")):
            want = []
            for repo in (shop, docs):
                r = await root.get("/api/recurring-findings", params={"repo": repo},
                                   headers=bearer(who) if who else {})
                want += [{"repo": repo, "rule_accepted": False,      # no rule is accepted yet
                          **{k: v for k, v in row.items() if k != "examples"}}
                         for row in r.json().get("recurring", [])]
            got = await rows(who)
            by = lambda r: (r["repo"], r["kind"])
            ck(f"{label}, the whole factory: row for row what GET /api/recurring-findings says",
               bool(want) and sorted(got or [], key=by) == sorted(want, key=by), (got, want))
        answer = await section(k_ada)
        got = (answer.get("recurring") or {}).get("rows")
        ck("a reader's own projects are counted, and only then held to 3 tickets",
           brief(got) == [(docs, "concurrency", 3), (shop, "migration", 3)], brief(got))
        ck("and no other project is named in the section",
           "Vault" not in str(answer) and "uc_vault" not in str(answer), answer)
        for scope, who, label, expected in (
                (f"team:{ada}", None, "a team", [(docs, "concurrency", 3), (shop, "migration", 3)]),
                ("mine", k_ada, "mine", [(docs, "concurrency", 3), (shop, "migration", 3)]),
                ("project:uc_shop", None, "a project with 2 tickets of a kind", []),
                ("mine", k_pat, "the lead of Vault, with 2 of each", [])):
            got = await rows(who, scope=scope)
            ck(f"{label}: counted over the projects of the scope", brief(got) == expected, brief(got))
        past = await rows(from_=week(30).isoformat(), to=week(30).isoformat())
        ck("the range picked does not change the list", past == await rows(), past)

        # a row is marked from the accepted offers the caller's offer list returns, at once
        now = datetime.now(timezone.utc)
        for repo, kind, bound in ((shop, "migration", "uc_vault"), (docs, "concurrency", None),
                                  (shop, "error handling", None)):
            s.upsert_rule_proposal(repo, kind, bound, "project" if bound else "repo",
                                   "Write it", [], now)
        offers = (await root.get("/api/rule-proposals", params={"state": "open"})).json()
        for offer in offers["proposals"]:
            if offer["kind"] != "error handling":          # that one stays open
                s.decide_rule_proposal(offer["id"], "accepted", offer["text"], now)
        both = [(docs, "concurrency"), (shop, "migration")]
        for who, scope, label, expected in (
                (None, "factory", "the cell's token", both),
                (k_mia, "factory", "a member of Vault", both),
                (k_ada, "factory", "a reader not on Vault, whose rule it is", both[:1]),
                (None, f"team:{ada}", "a scope without the rule's project", both)):
            got = await rows(who, scope=scope) or []
            marked = sorted((r["repo"], r["kind"]) for r in got if r.get("rule_accepted") is True)
            ck(f"{label}: the rows marked as having an accepted rule",
               marked == expected and (shop, "migration") in [(r["repo"], r["kind"]) for r in got],
               got)

        for i, text in enumerate(("No migration for it", "No tests here", "Uncaught exception",
                                  "A new env var", "A null value", "A deadlock")):
            found(many, "uc_far", text, tickets=8 - i)
        report.rebuild_all(s)
        got = await rows(scope="project:uc_far")
        ck("six kinds recur: the five with the most tickets are listed",
           brief(got) == [(many, "migration", 8), (many, "missing test", 7),
                          (many, "error handling", 6), (many, "config key", 5),
                          (many, "null or empty", 4)], brief(got))


def held(s, uid, station, doing, merged=None, review=None, canceled=None, milestone=None) -> str:
    """A ticket `station` held from `doing`, with the other stages at the times given."""
    tid = s.create_ticket(uid, "tares", "t")
    for field, value, at in (("stage", "doing", doing), ("holder", station, doing),
                             ("stage", "review", review), ("stage", "canceled", canceled),
                             ("stage", "merged", merged)):
        if at:
            s.add_ticket_history(tid, uid, field, value, None, None, None, at)
    if milestone:
        s.update_ticket(tid, milestone=milestone)
    return tid


def money(cost, unpriced=0) -> dict:
    return {"cost_usd": float(cost), "unpriced": unpriced}


async def cost_section():
    import json
    async with cell() as (s, root, bare):
        print("== the Cost section ==")
        ada, bo, pat = (s.create_person(n, f"{n.lower()}@example.com")["id"] for n in ("Ada", "Bo", "Pat"))
        project(s, "uc_open", "Open", lead=ada)
        project(s, "uc_other", "Other work", lead=bo)
        project(s, "uc_limited", "Limited", lead=pat, private=True)
        k_ada = key(s, "kcost0", ["read"], person=ada)
        m, m2 = noon(week(1)), noon(week(2))
        h = timedelta(hours=1)

        async def read(who=None, **params):
            r = await get(root, who, **params)
            return (r.json()["sections"].get("cost") or {}) if r.status_code == 200 else {"error": r.text}

        ms, ml, waiting = (s.create_milestone(uid, name) for uid, name in (
            ("uc_open", "First step"), ("uc_limited", "Limited step"), ("uc_open", "Next step")))
        one = held(s, "uc_open", "build-open", m - 3 * h, review=m - h, merged=m, milestone=ms)
        held(s, "uc_open", "build-open", m2 - 5 * h, canceled=m2 - 3 * h, milestone=ms)
        held(s, "uc_open", "build-three", m - 30 * h, milestone=waiting)
        lim = held(s, "uc_limited", "build-lim", m - 3 * h, review=m - h, merged=m + 2 * h, milestone=ml)
        small = [held(s, "uc_other", f"build-o{i}", noon(week(20)) if i == 1 else m2 - 3 * h, merged=m2)
                 for i in range(1, 6)]
        s.add_review("uc_open", one, "7", "abc1234", "pass", "read the diff", "", "crew-reviewer", [], {},
                     factory_mod.finding_kind, m - h * 3 / 4)
        s.accept_milestone("uc_open", ms, "Ada", None, m + h)
        s.accept_milestone("uc_limited", ml, "Pat", None, m + 3 * h)

        for session, station, role, home in (
                ("bo", "build-open", "builder", "uc_open"), ("bt", "build-three", "builder", "uc_open"),
                ("bl", "build-lim", "builder", "uc_limited"), ("rev", "crew-reviewer", "reviewer", None),
                ("orch", "crew-orchestrator", "orchestrator", None), ("rel", "crew-releaser", "releaser", None),
                *((f"o{i}", f"build-o{i}", "builder", "uc_other") for i in range(1, 6))):
            seed_station(s, session, station, role, project=home)
        for n, (session, at, cost, uid) in enumerate((
                ("bo", m - 2 * h, 4.0, None), ("bo", m - h * 5 / 2, None, None), ("rev", m - h / 2, 3.0, None),
                ("bo", m2 - 4 * h, 2.0, None), ("bt", m - 29 * h, 6.0, None), ("bl", m - 2 * h, 5.0, None),
                ("pat-own", m - 2 * h, 8.0, "uc_limited"), ("orch", m, 0.5, "uc_other"), ("rel", m, 0.25, None),
                *((f"o{i}", m2 - 2 * h, i / 10, None) for i in range(1, 6)),
                ("o1", noon(week(20)) + h, 0.05, None))):
            seed_usage(s, session, f"cost-{n}", replied_at=at, project=uid, cost_usd=cost)

        got = await read()
        ck("before the first pass ticket costs are not worked out", got.get("by_ticket", 0) is None, got)
        report.rebuild_all(s)
        got = await read()
        ck("the section's parts", set(got) == {"total", "roles", "projects", "weeks", "by_ticket"}, sorted(got))
        ck("the total, with the reply with no price counted beside it",
           close(got["total"], money(30.25, 1)), got["total"])
        roles = {r["role"]: (r["cost_usd"], r["unpriced"]) for r in got["roles"]}
        ck("the split by role", [r["role"] for r in got["roles"]]
           == ["builder", "reviewer", "orchestrator", "releaser", "person", "other"]
           and close({k: list(v) for k, v in roles.items()},
                     {"builder": [18.5, 1], "reviewer": [3.0, 0], "orchestrator": [0.5, 0],
                      "releaser": [0.25, 0], "person": [8.0, 0], "other": [0.0, 0]}), got["roles"])
        ck("the roles add up to the total",
           abs(sum(r["cost_usd"] for r in got["roles"]) - got["total"]["cost_usd"]) < 1e-6, got["roles"])
        ck("by project, highest first, with what has no project",
           close([[p["project"], p["name"], p["cost_usd"]] for p in got["projects"]],
                 [["uc_limited", "Limited", 13.0], ["uc_open", "Open", 12.0], ["", "No project", 3.25],
                  ["uc_other", "Other work", 2.0]]), got["projects"])
        weekly = {w["week"]: w["cost_usd"] for w in got["weeks"]}
        ck("by week", len(weekly) == 8 and close([weekly[week(b).isoformat()] for b in (1, 2, 3)], [20.75, 9.5, 0.0]),
           weekly)

        def tops(by, kind="top_tickets"):
            return [[r["project"], r["cost_usd"], r["unpriced"], r["rounds"], r["hours"]] for r in by[kind]]

        def per_week(by):
            rows = {w["week"]: [w["merged"], w["mean_cost_usd"], w["unpriced_tickets"]] for w in by["weeks"]}
            return [rows[week(b).isoformat()] for b in (1, 2, 3)]

        by = got["by_ticket"]
        ck("a ticket costs its builder's replies and its share of review, over its whole life",
           by["top_tickets"][0]["label"] == factory_mod.label(s.get_ticket("uc_open", one))
           and close(tops(by)[:2], [["uc_open", 7.0, 1, 1, 3.0], ["uc_limited", 6.5, 0, 0, 5.0]]), tops(by))
        ck("the five most expensive merged tickets, no more",
           close([r[:2] for r in tops(by)], [["uc_open", 7.0], ["uc_limited", 6.5], ["uc_other", 0.5],
                                             ["uc_other", 0.4], ["uc_other", 0.3]]), tops(by))
        ck("the mean per ticket first merged in each week, no value with none",
           close(per_week(by), [[2, 6.75, 1], [5, 0.31, 0], [0, None, 0]]), per_week(by))
        ck("a milestone sums its tickets, the canceled one included",
           [(r["name"], r["tickets"]) for r in by["top_milestones"]] == [("First step", 2), ("Limited step", 1)]
           and close(tops(by, "top_milestones"), [["uc_open", 9.0, 1, 1, 174.0], ["uc_limited", 6.5, 0, 0, 6.0]]),
           by["top_milestones"])
        ck("the mean per accepted milestone, one not accepted left out",
           close(by["milestones"], {"accepted": 2, "mean_cost_usd": 7.75, "unpriced_milestones": 1}), by["milestones"])
        ck("what is tied to no ticket, with the part of no project",
           close(by["untied"], {**money(8.75), "no_project": money(0.25)}), by["untied"])

        got = await read(k_ada)
        by = got["by_ticket"]
        ck("without access to a project: its spend is in no total, role or week",
           close(got["total"], money(17.25, 1)) and [p["project"] for p in got["projects"]] == ["uc_open", "", "uc_other"]
           and close([r["cost_usd"] for r in got["roles"]], [13.5, 3.0, 0.5, 0.25, 0.0, 0.0])
           and close(sum(w["cost_usd"] for w in got["weeks"]), 17.25), got)
        ck("and none of its tickets, milestones or unattributed amount is there",
           "uc_limited" not in json.dumps(got) and "Limited" not in json.dumps(got)
           and close(per_week(by)[0], [1, 7.0, 1]) and by["milestones"]["accepted"] == 1
           and close(by["untied"], {**money(0.75), "no_project": money(0.25)}), by)
        ck("an open project's ticket costs the same for this reader", close(tops(by)[0][:2], ["uc_open", 7.0]), tops(by))

        for scope, label in ((f"team:{ada}", "a team"), ("mine", "mine"), ("project:uc_open", "one project")):
            got = await read(k_ada, scope=scope)
            by = got["by_ticket"]
            ck(f"{label}: its projects' spend only, nothing of no project",
               close(got["total"], money(12.0, 1)) and [p["project"] for p in got["projects"]] == ["uc_open"]
               and close(by["untied"], {**money(0.0), "no_project": None})
               and close([r[:2] for r in tops(by)], [["uc_open", 7.0]])
               and close(tops(by, "top_milestones")[0][:2], ["uc_open", 9.0]), (got["total"], by))
        got = await read(scope="project:uc_other")
        by = got["by_ticket"]
        ck("a project with no milestone: no milestone figure, its own unattributed amount",
           close(by["milestones"], {"accepted": 0, "mean_cost_usd": None, "unpriced_milestones": 0})
           and by["top_milestones"] == [] and close(by["untied"], {**money(0.5), "no_project": None})
           and close([r["cost_usd"] for r in by["top_tickets"]], [0.5, 0.4, 0.3, 0.2, 0.15])
           and close(got["total"], money(2.0)), (got["total"], by))

        got = await read(from_=week(40).isoformat(), to=week(38).isoformat())
        by = got["by_ticket"]
        ck("a range with nothing: zero spend, no project, no mean and empty lists",
           got["total"] == money(0) and got["projects"] == []
           and all(w["cost_usd"] == 0 and w["projects"] == {} for w in got["weeks"])
           and [(w["merged"], w["mean_cost_usd"]) for w in by["weeks"]] == [(0, None)] * 3
           and by["milestones"] == {"accepted": 0, "mean_cost_usd": None, "unpriced_milestones": 0}
           and by["top_tickets"] == [] and by["top_milestones"] == []
           and by["untied"] == {**money(0), "no_project": money(0)}, got)

        s.delete_ticket(small[4])
        report.rebuild_all(s)
        by = (await read(scope="project:uc_other"))["by_ticket"]
        ck("a deleted ticket is in no per-ticket figure",
           close([r["cost_usd"] for r in by["top_tickets"]], [0.4, 0.3, 0.2, 0.15]), by["top_tickets"])


LAYER = "This module reaches into the internals of another layer"
_DECISIONS = itertools.count(1)


def decided(s, uid, back, question, tickets=(), answer="Keep it", who=None, minute=0):
    """A decision of `uid` (None: asked with no project) made `back` weeks ago."""
    s.add_decision(uid, f"dk_arch{next(_DECISIONS)}", question, answer, answer, False, "project",
                   True, None, False, list(tickets), noon(week(back)) + timedelta(minutes=minute),
                   person_id=who)


_ROUNDS = itertools.count(100)           # minutes past noon: no two rounds at the same moment


def layered(s, uid, tid, counts):
    """On `tid`, `counts[back]` new architecture findings in the week `back` weeks ago."""
    for back, n in sorted(counts.items(), reverse=True):
        raised(s, uid, tid, back, "changes",
               [("P2", False, f"{LAYER}, place {i}") for i in range(n)], minute=next(_ROUNDS))


def rows_of(section) -> dict:
    return {r["repo"]: r for r in section.get("repos", [])}


def counts_of(row) -> list[int]:
    return [w["count"] for w in row.get("weeks", [])]


def asked(group) -> list[str]:
    return [d["question"] for d in group.get("decisions", [])]


NOTHING_DECIDED = {"decisions_total": 0, "decisions": []}


async def architecture_section():
    async with cell() as (s, root, bare):
        print("== the Architecture section ==")
        ada, pat = (s.create_person(n, f"{n.lower()}@example.com")["id"] for n in ("Ada", "Pat"))
        project(s, "uc_one", "One", lead=ada)
        project(s, "uc_two", "Two", lead=ada)
        project(s, "uc_hid", "Gamma", lead=pat, private=True)
        api, web, docs = (pulled(s, "uc_one", f"acme/{n}") for n in ("api", "web", "docs"))
        zed, early, small, capital = (pulled(s, "uc_two", f"acme/{n}")
                                      for n in ("Zed", "early", "b", "C"))
        loose = s.create_ticket("uc_two", "tares", "t")           # no pull request
        hid_api, secret = (pulled(s, "uc_hid", f"acme/{n}") for n in ("api", "secret"))
        # new findings per week, the key counting weeks back from the running one
        layered(s, "uc_one", api, {3: 1, 2: 2, 1: 3, 0: 1})       # rises, from none
        layered(s, "uc_one", web, {4: 1, 3: 2, 2: 2, 1: 3})       # one week as many as before
        layered(s, "uc_two", zed, {4: 3, 3: 2, 2: 1, 0: 9})       # falls, then a big running week
        layered(s, "uc_two", early, {5: 1, 4: 2, 3: 3, 2: 4})     # rose up to 2 weeks ago
        layered(s, "uc_two", small, {5: 1})
        layered(s, "uc_two", capital, {5: 1})
        layered(s, "uc_two", loose, {3: 1, 2: 2, 1: 3})           # no repo to say it rises in
        layered(s, "uc_hid", hid_api, {1: 2})
        layered(s, "uc_hid", secret, {1: 1})
        # one api finding is fixed and one is the gravest: both still count
        raised(s, "uc_one", api, 1, "pass", resolved={"F1": "fixed"}, minute=30)
        raised(s, "uc_one", docs, 1, "changes", [("P1", True, "The README is stale")])

        made = factory_mod.label(s.get_ticket("uc_one", web))
        decided(s, "uc_one", 1, "Which layer owns the retry rule?", [api], who=ada)
        decided(s, "uc_one", 2, "One client for both?", [api, made])
        decided(s, "uc_one", 1, "Where do the docs live?", [docs], answer="")
        decided(s, "uc_two", 1, "A queue or a table?", [loose, "ZZ-999"])
        decided(s, "uc_two", 1, "Which name for it?", minute=5)
        decided(s, "uc_one", 1, "Merge PR #12 of acme/api (T1)?", [api], answer="Approve")
        decided(s, None, 1, "A question for the whole cell?", [api])
        decided(s, "uc_hid", 1, "A hidden choice?", [secret])
        decided(s, "uc_one", 9, "An old choice?", [api])
        report.rebuild_all(s)
        k_ada = key(s, "karch", ["read"], person=ada)

        async def read(who=None, **params):
            r = await get(root, who, **params)
            return (r.json()["sections"]["architecture"] if r.status_code == 200
                    else {"error": r.text})

        got = await read(k_ada)
        rows = rows_of(got)
        ck("a row per repo: a rising one first, then the most findings, the name whatever its case "
           "on a tie, no repo last",
           [r["repo"] for r in got.get("repos", [])]
           == ["acme/api", "acme/Zed", "acme/early", "acme/web", "acme/b", "acme/C", "acme/docs",
               None], [r["repo"] for r in got.get("repos", [])])
        row = rows.get("acme/api", {})
        ck("each week of the range with the findings first raised in it, fixed or open, and the total",
           [w["week"] for w in row.get("weeks", [])] == [week(n).isoformat() for n in range(7, -1, -1)]
           and counts_of(row) == [0, 0, 0, 0, 1, 2, 3, 1] and row.get("total") == 7, row)
        ck("a finding of another kind is in no count",
           counts_of(rows.get("acme/docs", {})) == [0] * 8
           and rows.get("acme/docs", {}).get("total") == 0, rows.get("acme/docs"))
        ck("rising: each of the last three finished weeks above the one before, from none too, the "
           "running week left out",
           row.get("rising") is True, row)
        ck("a week with as many as the one before is no rise",
           rows.get("acme/web", {}).get("rising") is False, rows.get("acme/web"))
        ck("a large running week makes no rise",
           rows.get("acme/Zed", {}).get("rising") is False
           and counts_of(rows.get("acme/Zed", {}))[-1:] == [9], rows.get("acme/Zed"))
        ck("a rise that stopped last week is no rise",
           rows.get("acme/early", {}).get("rising") is False, rows.get("acme/early"))
        none = rows.get(None, {})
        ck("a finding on a ticket with no pull request is counted with no repo, never as rising",
           counts_of(none) == [0, 0, 0, 0, 1, 2, 3, 0] and none.get("rising") is False
           and {k: none.get(k) for k in NOTHING_DECIDED} == NOTHING_DECIDED, none)
        ck("a decision is beside the repo of its ticket's pull request, newest first, with the total",
           asked(row) == ["Which layer owns the retry rule?", "One client for both?"]
           and row.get("decisions_total") == 2, row.get("decisions"))
        web_row = rows.get("acme/web", {})
        ck("a decision about tickets in two repos is beside both, one named by its label included",
           asked(web_row) == ["One client for both?"] and web_row.get("decisions_total") == 1,
           web_row.get("decisions"))
        first = (row.get("decisions") or [{}])[0]
        ck("a decision says its project, question, answer, day, who made it and its tickets",
           {k: v for k, v in first.items() if k != "at"} == {
               "project": "uc_one", "name": "One", "question": "Which layer owns the retry rule?",
               "answer": "Keep it", "person": {"id": ada, "name": "Ada"}, "tickets": [api]}
           and str(first.get("at", "")).startswith(week(1).isoformat()), first)
        bare_one = (rows.get("acme/docs", {}).get("decisions") or [{}])[0]
        ck("with no answer and no person recorded, neither is made up",
           bare_one.get("question") == "Where do the docs live?" and not bare_one.get("answer")
           and not bare_one.get("person"), bare_one)
        unplaced = got.get("unplaced", {})
        ck("a decision with no repo to go by, with or without a ticket, is listed apart",
           asked(unplaced) == ["Which name for it?", "A queue or a table?"]
           and unplaced.get("decisions_total") == 2, unplaced)
        every = json.dumps(got)
        ck("a merge approval, a decision asked with no project and one before the range are in no list",
           bool(rows) and not any(words in every for words in
                                  ("Merge PR #12", "whole cell", "An old choice")), every[:600])
        ck("a private project is neither counted nor named for a reader without access",
           bool(rows) and not any(words in every for words in
                                  ("acme/secret", "A hidden choice", "Gamma", "uc_hid", secret))
           and counts_of(row)[-2:] == [3, 1], every[:600])

        full = await read()
        rows = rows_of(full)
        ck("who may open it gets its findings in the shared repo, its own repo and its decision",
           counts_of(rows.get("acme/api", {})) == [0, 0, 0, 0, 1, 2, 5, 1]
           and rows.get("acme/api", {}).get("total") == 9
           and rows.get("acme/secret", {}).get("total") == 1
           and asked(rows.get("acme/secret", {})) == ["A hidden choice?"], full)

        got = await read(k_ada, from_=week(1).isoformat())
        rows = rows_of(got)
        ck("a range of 2 weeks counts those, and rising still looks at the four finished weeks",
           counts_of(rows.get("acme/api", {})) == [3, 1] and rows.get("acme/api", {}).get("total") == 4
           and rows.get("acme/api", {}).get("rising") is True
           and rows.get("acme/web", {}).get("rising") is False, got)
        ck("a decision made before those weeks is not listed",
           asked(rows.get("acme/api", {})) == ["Which layer owns the retry rule?"]
           and "acme/early" not in rows, rows)
        got = await read(k_ada, from_=week(0).isoformat())
        ck("the running week alone: rising from the weeks before it, and only repos with something "
           "in the range",
           [(r["repo"], r["total"], r["rising"]) for r in got.get("repos", [])]
           == [("acme/api", 1, True), ("acme/Zed", 9, False)], got)
        got = await read(k_ada, from_=week(3).isoformat(), to=week(2).isoformat())
        rows = rows_of(got)
        ck("a range that ends in the past: rising up to its last week, nothing after it",
           rows.get("acme/early", {}).get("rising") is True
           and counts_of(rows.get("acme/early", {})) == [3, 4]
           and rows.get("acme/api", {}).get("rising") is False
           and asked(rows.get("acme/api", {})) == ["One client for both?"]
           and got.get("unplaced") == NOTHING_DECIDED, got)

        got = await read(k_ada, scope="project:uc_two")
        ck("one project: its repos and its decisions only",
           [r["repo"] for r in got.get("repos", [])]
           == ["acme/Zed", "acme/early", "acme/b", "acme/C", None]
           and all(r["decisions_total"] == 0 for r in got.get("repos", []))
           and asked(got.get("unplaced", {})) == ["Which name for it?", "A queue or a table?"], got)
        got = await read(k_ada, from_=week(40).isoformat(), to=week(38).isoformat())
        ck("weeks with nothing in them: no repo and no decision",
           got == {"repos": [], "unplaced": NOTHING_DECIDED}, got)

    async with cell() as (s, root, bare):
        project(s, "uc_found", "Found")
        project(s, "uc_said", "Said")
        project(s, "uc_many", "Many")
        found = pulled(s, "uc_found", "acme/found")
        layered(s, "uc_found", found, {1: 1})
        said = pulled(s, "uc_said", "acme/said")
        decided(s, "uc_said", 1, "Only a decision?", [said])
        many = pulled(s, "uc_many", "acme/many")
        for n in range(7):
            decided(s, "uc_many", 1, f"Choice {n}?", [many], minute=n)
        for n in range(6):
            decided(s, "uc_many", 1, f"Loose choice {n}?", minute=n)
        report.rebuild_all(s)

        async def read(uid):
            r = await get(root, scope=f"project:{uid}")
            return (r.json()["sections"]["architecture"] if r.status_code == 200
                    else {"error": r.text})

        got = await read("uc_found")
        ck("findings and no decision: the repo with none beside it",
           [(r["repo"], r["total"], r["decisions_total"], r["decisions"]) for r in got.get("repos", [])]
           == [("acme/found", 1, 0, [])] and got.get("unplaced") == NOTHING_DECIDED, got)
        got = await read("uc_said")
        ck("a decision and no finding: its repo is listed with every week at 0, not rising",
           [(r["repo"], r["total"], r["rising"], counts_of(r), asked(r)) for r in got.get("repos", [])]
           == [("acme/said", 0, False, [0] * 8, ["Only a decision?"])], got)
        got = await read("uc_many")
        row = rows_of(got).get("acme/many", {})
        ck("a long list shows the 5 newest of a repo, and says how many there are",
           row.get("decisions_total") == 7 and asked(row) == [f"Choice {n}?" for n in (6, 5, 4, 3, 2)],
           row)
        unplaced = got.get("unplaced", {})
        ck("and the 5 newest of those with no repo",
           unplaced.get("decisions_total") == 6
           and asked(unplaced) == [f"Loose choice {n}?" for n in (5, 4, 3, 2, 1)], unplaced)

        layered(s, "uc_found", found, {1: 2})
        decided(s, "uc_found", 1, "Decided since the pass?", [found])
        got = await read("uc_found")
        ck("a decision shows at once, a finding at the next pass of the weekly rows",
           [(r["repo"], r["total"], asked(r)) for r in got.get("repos", [])]
           == [("acme/found", 1, ["Decided since the pass?"])], got)

        project(s, "uc_late", "Late")
        late = pulled(s, "uc_late", "acme/late")
        decided(s, "uc_late", 1, "On a ticket newer than the pass?", [late])
        got = await read("uc_late")
        ck("a decision on a ticket the last pass has not recorded is listed apart",
           got.get("repos") == [] and asked(got.get("unplaced", {}))
           == ["On a ticket newer than the pass?"], got)
        report.rebuild_all(s)
        got = await read("uc_late")
        ck("and under its repo after the next pass",
           [(r["repo"], asked(r)) for r in got.get("repos", [])]
           == [("acme/late", ["On a ticket newer than the pass?"])]
           and got.get("unplaced") == NOTHING_DECIDED, got)
        s.set_setting(report.TICKET_REPOS_SETTING, "")
        got = await read("uc_late")
        ck("with no repo recorded at all, as on a cell just upgraded, the page still answers",
           got.get("repos") == [] and asked(got.get("unplaced", {}))
           == ["On a ticket newer than the pass?"], got)

        project(s, "uc_path", "Path")
        eve = s.create_person("Eve", "eve@example.com")["id"]
        s.set_handover("uc_path", [{"path": "/home/dev/work/shop", "name": "shop"}], "L3-review",
                       None, noon(week(2)), person_id=eve)
        decided(s, "uc_path", 1, "Keep /home/dev/work/shop/config.yml?",
                answer="Move it to /home/dev/work/shop/notes.md")
        got = await read("uc_path")
        told = (got.get("unplaced", {}).get("decisions") or [{}])[0]
        ck("a folder path of the hand-over in a question or an answer is shortened, as in a merge title",
           "/home/dev/work" not in json.dumps(got) and "config.yml" in told.get("question", "")
           and "notes.md" in told.get("answer", ""), got)


async def large():
    print("== a large cell: 20 projects, 12 weeks, 600 merged tickets ==")
    async with cell() as (s, root, bare):
        people = [s.create_person(f"Person {i}", f"p{i}@example.com")["id"] for i in range(8)]
        uids = [f"uc_large{i:02d}" for i in range(20)]
        for i, uid in enumerate(uids):
            project(s, uid, f"Project {i:02d}", lead=people[i % 6])
        for i, uid in enumerate(uids):
            seed_station(s, f"build-large{i}", f"build-large{i}", "builder", project=uid)
        seed_station(s, "review-large", "crew-reviewer", "reviewer")
        for i in range(600):
            uid, back, builder = uids[i % 20], i % 12, f"build-large{i % 20}"
            when = noon(week(back))
            tid = merge(s, uid, when, assignee=people[i % 8] if i % 3 == 0 else None)
            s.add_ticket_history(tid, uid, "holder", builder, None, None, None, when - timedelta(hours=2))
            s.add_ticket_history(tid, uid, "stage", "review", None, None, None, when - timedelta(hours=1))
            for n in range(5):
                seed_usage(s, builder, f"b-{i}-{n}", replied_at=when - timedelta(minutes=90 + n), cost_usd=0.1)
            seed_usage(s, "review-large", f"r-{i}", replied_at=when - timedelta(minutes=30), cost_usd=0.1)
            queued = s.merge_queue_add(f"example-org/merged-{i % 3}", "main", i + 1, "abc1234", tid, uid)
            s.merge_queue_update(queued["id"], state="merged")
            if i % 10:      # one in ten has no record, so the queue row stands for it
                merge_ledger.save(s, queued, level="L3-review", reviewed_sha="abc1234", ci_state="success",
                                  rounds=1, outside=False, approver_id=people[i % 8], merged_at=when)
        for i in range(200):
            tid = pulled(s, uids[i % 20], f"example-org/repo-{i % 3}")
            s.add_ticket_history(tid, uids[i % 20], "stage", "doing", None, None, None, noon(week(1)))
            raised(s, uids[i % 20], tid, 1, "changes",
                   [("P2", False, "No migration for it"), ("P2", False, "Uncaught exception"),
                    ("P3", False, f"Nit number {i} here"), ("P3", False, LAYER)],
                   minute=i // 20)   # one moment per review in a project, which places its finding
            decided(s, uids[i % 20], 1, f"Choice {i}?", [tid])
        for i, uid in enumerate(uids):
            for back in range(12):
                seed_usage(s, f"s-{i}-{back}", f"m-{i}-{back}", replied_at=noon(week(back)),
                           project=uid, cost_usd=2.0)
        report.rebuild_all(s)
        k = key(s, "klarge", ["read"], person=people[7])     # leads nothing: mine walks the tickets
        for scope, who in (("factory", None), ("mine", k), (f"team:{people[0]}", k),
                           (f"project:{uids[0]}", k)):
            # one warm-up call, then 5 timed ones; the second slowest is judged, so one moment
            # of a busy runner among the 5 does not fail it, and 4 of the 5 must meet the target
            await get(root, who, scope=scope, from_=week(11).isoformat())
            took = []
            for _ in range(5):
                start = time.perf_counter()
                r = await get(root, who, scope=scope, from_=week(11).isoformat())
                took.append(time.perf_counter() - start)
            label = scope.split(":")[0]
            if scope == "factory":
                ck("the seed is all there", r.json()["range"]["merged"] == 600, r.text[:300])
                review = r.json()["sections"]["review"]
                ck("with its reviews and recurring findings",
                   review["first_pass"]["reviewed"] == 200
                   and len(review["recurring"]["rows"]) == 5, str(review)[:600])
                by = r.json()["sections"]["cost"]["by_ticket"]
                ck("and the cost of every merged ticket",
                   sum(w["merged"] for w in by["weeks"]) == 600
                   and all(w["mean_cost_usd"] > 0 for w in by["weeks"])
                   and len(by["top_tickets"]) == 5 and all(t["cost_usd"] > 0 for t in by["top_tickets"]),
                   str(by)[:600])
                layers = r.json()["sections"]["architecture"]["repos"]
                ck("and 200 architecture findings and decisions, 5 decisions shown per repo",
                   sum(x["total"] for x in layers) == 200
                   and sum(x["decisions_total"] for x in layers) == 200
                   and all(len(x["decisions"]) == 5 for x in layers), str(layers)[:600])
                merged = r.json()["sections"]["merges"]
                ck("and every merge through the queue, the newest 100 listed",
                   merged["total"] == 600 and len(merged["rows"]) == 100
                   and merged["csv"].count("\r\n") == 601
                   and sum(w["unrecorded"] for w in merged["weeks"]) == 60
                   and sum(w["L3-review"] for w in merged["weeks"]) == 540,
                   (merged["total"], len(merged["rows"]), merged["weeks"]))
            second = sorted(took)[-2]
            ck(f"{label}: answered under 300 ms, the second slowest of 5 ({second * 1000:.0f} ms)",
               r.status_code == 200 and r.json()["scope"]["count"] > 0 and second < 0.3,
               (r.status_code, [round(t * 1000) for t in took]))


def main():
    ranges()
    asyncio.run(scopes())
    asyncio.run(delivered())
    asyncio.run(time_section())
    asyncio.run(stuck())
    asyncio.run(stuck_rows())
    asyncio.run(review_section())
    asyncio.run(recurring_rows())
    asyncio.run(cost_section())
    asyncio.run(architecture_section())
    asyncio.run(large())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
