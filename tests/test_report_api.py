"""The Report page's one read, GET /api/factory/report (TR-1667, tares/report.py `page`).

The weeks a range covers (as a function, with a fixed day, and over HTTP), the shape of the answer,
a cell with nothing on it, a cell of one person, and each scope: which weekly rows reach a section,
which projects and teams are named and counted. A private project is in no scope of a caller who
cannot open it, and asked for by name it is unknown, like an id that names nothing. Who may read at
all, bad input, that no raw record is read on page load, and the time of the read on a large cell
(20 projects, 12 weeks, 600 merged tickets).

Run: python tests/test_report_api.py
"""
import asyncio
import contextlib
import hashlib
import os
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
os.environ.update({"TARES_DB": os.path.join(HOME, "import.duckdb"),
                   "TARES_CATALOG": os.path.join(HOME, "catalog.yaml"),
                   "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": ROOT,
                   "TARES_EDITION": "factory",
                   "ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key"})

import httpx

from tares import daemon, report
from session_helpers import seed_usage

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
    daemon.DB_PATH = os.path.join(tempfile.mkdtemp(dir=HOME), "api.duckdb")
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=bearer(ROOT)) as root:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as bare:
                yield app.state.store, root, bare


# ── seeding ───────────────────────────────────────────────────────────────────
def project(s, uid, name, lead=None, kind="software_factory", private=False):
    s.create_project(uid, "custom", name, {}, goal="g", lead_person_id=lead)
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


def probe(rows, weeks):
    """A section that says what it was handed."""
    return {"projects": sorted({r["project"] for r in rows}),
            "weeks": [w.isoformat() for w in weeks],
            "merged": sum(r["merged"] for r in rows)}


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
    two_years = (last - timedelta(weeks=103)).isoformat()
    ck("104 weeks are a range", len(report.range_weeks(two_years, None, day)) == 104)
    ck("105 are not", refused((last - timedelta(weeks=104)).isoformat(), None) == TOO_LONG)
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
        ck("no section is built yet", body.get("sections") == {}, body.get("sections"))
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
                    ({"from_": week(104).isoformat()}, TOO_LONG),
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
            raw = ["all_stage_changes", "all_reviews", "all_review_findings", "usage_by_week",
                   "list_desk", "milestones_by_project", "milestone_acceptances", "tickets_by_project"]
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
                got = []
                for scope in ("factory", f"team:{ada}", "project:uc_beta"):
                    try:
                        got.append((await get(root, k_bo, scope=scope)).status_code)
                    except RuntimeError as e:
                        got.append(str(e))
                ck("the factory, a team and a project read the weekly rows and no raw record",
                   got == [200, 200, 200] and not touched, (got, touched))
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


async def large():
    print("== a large cell: 20 projects, 12 weeks, 600 merged tickets ==")
    async with cell() as (s, root, bare):
        people = [s.create_person(f"Person {i}", f"p{i}@example.com")["id"] for i in range(8)]
        uids = [f"uc_large{i:02d}" for i in range(20)]
        for i, uid in enumerate(uids):
            project(s, uid, f"Project {i:02d}", lead=people[i % 6])
        for i in range(600):
            uid, back = uids[i % 20], i % 12
            merge(s, uid, noon(week(back)), assignee=people[i % 8] if i % 3 == 0 else None)
        for i in range(200):
            tid = s.create_ticket(uids[i % 20], "tares", "open")
            s.add_ticket_history(tid, uids[i % 20], "stage", "doing", None, None, None, noon(week(1)))
        for i, uid in enumerate(uids):
            for back in range(12):
                seed_usage(s, f"s-{i}-{back}", f"m-{i}-{back}", replied_at=noon(week(back)),
                           project=uid, cost_usd=2.0)
        report.rebuild_all(s)
        k = key(s, "klarge", ["read"], person=people[7])     # leads nothing: mine walks the tickets
        for scope, who in (("factory", None), ("mine", k), (f"team:{people[0]}", k),
                           (f"project:{uids[0]}", k)):
            took = []
            for _ in range(5):
                start = time.perf_counter()
                r = await get(root, who, scope=scope, from_=week(11).isoformat())
                took.append(time.perf_counter() - start)
            label = scope.split(":")[0]
            if scope == "factory":
                ck("the seed is all there", r.json()["range"]["merged"] == 600, r.text[:300])
            ck(f"{label}: answered under 300 ms, the slowest of 5 ({max(took) * 1000:.0f} ms)",
               r.status_code == 200 and r.json()["scope"]["count"] > 0 and max(took) < 0.3,
               (r.status_code, [round(t * 1000) for t in took]))


def main():
    ranges()
    asyncio.run(scopes())
    asyncio.run(large())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
