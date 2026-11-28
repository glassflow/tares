# Tares enterprise licence, see ee/LICENSE
"""How many statements each factory page runs (TR-1501), and what each page answers.

A licensed factory cell with a team's worth of work: 25 projects of 10 tickets each (with history),
3 of them handed to a crew, 40 crew sessions over 6 stations, and one person who signs in with a key
(next to the cell's own `local` person, so the team parts of the pages run). That person reads the
pages the console opens on: Home, the Projects list, one project, one ticket, and the two raw ticket
routes (and Setup, its progress and the Crew page, counted only). Every statement a route runs is
counted, through SQLAlchemy's cursor events on the session store (a SQLite transaction's BEGIN is a
statement there). A route that runs more than its BUDGET fails the test.

(The entities page went with entity counts, TR-1810; the event reads of the source catalog, and the
check of what a deleted private project adds to them, with the catalog, TR-1811.)

The pages answer the same thing on every run: the clock is frozen, ids are numbered in the order they
appear, and the bodies are compared with ee/tests/fixtures/factory_pages_golden.json. A later change
that must move a body re-records it with `--write-golden` and says why in its PR.

Run: .venv/bin/python ee/tests/test_factory_query_budget.py [--write-golden]
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_factory_query_budget.py
"""
import asyncio
import contextvars
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)
LICENCE_LINE = "Tares enterprise licence, see ee/LICENSE"   # the golden file is JSON, so the line is a key
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "factory_pages_golden.json")
WRITE_GOLDEN = "--write-golden" in sys.argv


def _licence() -> str:
    """A licence that is valid on any day the test runs: the clock below is frozen, so one issued
    and ending on fixed dates."""
    kit = licensed.kit
    root_priv, _ = kit.keypair(kit.ROOT_PHRASE)
    iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
    cert = kit.issuer_cert(root_priv, iss_pub, "2099-01-01")
    return kit.sign(iss_priv, cert, "Test Org", 5, ("enterprise",), "2020-01-01T00:00:00Z",
                    "2099-01-01", "lic_ee_budget")


os.environ["TZ"] = "UTC"      # the golden file is one zone's
time.tzset()

_TMP = tempfile.mkdtemp(prefix="tares-query-budget-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = _licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-query-budget"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import claude_usage, factory_home as fh
from tares.envelope import Envelope

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

# The budget is shared by every lane of the project factory-perf-m1: any later ticket whose change
# moves a count updates BUDGET in its own PR and says why; only TR-1472 and TR-1503 lower it on
# purpose.
#
# Statements per route for the cell seeded below, by the backend the store is on (SQLite, or Postgres
# when TARES_TEST_PG is set): the count measured plus 2 percent, rounded up. Each backend has its own
# entry, so a change that moves one can move only that one. The history of each count, ticket by
# ticket from TR-1472 to TR-1725, is in this file's git log; since TR-1815 everything is in the session
# store, so on SQLite every read is a transaction of its own and costs its BEGIN, and on Postgres a
# statement is a round trip.
_TODAY = {"home": 54, "projects": 44, "project": 90, "ticket": 28, "raw_tickets": 9, "raw_ticket": 14}
# The Projects list with 12 finished projects added to the cell (the team path, TR-1503: a finished
# project's row and its "tickets yours" line come from grouped counts, not its tickets): at most
# DONE_EXTRA more statements than the same page without them, so the count does not grow per project.
DONE_PROJECTS = 12
DONE_EXTRA = 5
_SQLITE = {**_TODAY, "home": 101, "projects": 83, "project": 174, "ticket": 54, "raw_tickets": 17,
           "raw_ticket": 26}
# Setup, its progress and Crew had no entry before TR-1634 and TR-1633; their counts are kept the
# same way.
BUDGET = {"sqlite": {**_SQLITE, "setup": 99, "setup_progress": 95, "crew": 59},
          "postgres": {**_TODAY, "setup": 50, "setup_progress": 48, "crew": 31}}

FLOOR = 0.9   # a count under this share of its budget means BUDGET is stale: the PR that saved the queries lowers it

ROUTES = [
    ("home", "/api/home"),
    ("projects", "/api/factory/projects"),
    ("project", "/api/factory/projects/{uid}"),
    ("ticket", "/api/factory/projects/{uid}/tickets/{ref}"),
    ("raw_tickets", "/api/projects/{uid}/tickets"),
    ("raw_ticket", "/api/projects/{uid}/tickets/{ref}"),
]
# counted against BUDGET like the routes above, with no body in the golden file
SETUP_ROUTES = [
    ("setup", "/api/factory/setup"),
    ("setup_progress", "/api/factory/setup/progress"),
]
# counted the same way, with no body in the golden file
CREW_ROUTES = [
    ("crew", "/api/factory/crew"),
]

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the counting stand-in ────────────────────────────────────────────────────────────────────
_COUNTER: contextvars.ContextVar = contextvars.ContextVar("query_counter", default=None)


def _hit(n: int = 1) -> None:
    c = _COUNTER.get()
    if c is not None:
        c[0] += n


def install_counters(store) -> str:
    """Count every statement the store runs from now on. Returns the backend: sqlite or postgres.
    The counter is a context variable, so only the route being measured counts, whichever thread
    it runs a statement on (the daemon's threads copy the context of the request that started
    them), and a background thread of the daemon does not."""
    from sqlalchemy import event
    event.listen(store.sessions_db.engine, "before_cursor_execute", lambda *a, **kw: _hit())
    return "postgres" if store.sessions_db.dialect == "postgresql" else "sqlite"


# ── a fixed clock, and bodies that read the same on every run ───────────────────────────────────
def freeze_clock() -> None:
    """Every `now_utc` the package imported answers NOW, so the words that say how long ago
    something was (and the times a body holds) are the same on every run."""
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in ("tares", "tares_ee") and getattr(mod, "now_utc", None) is not None:
            mod.now_utc = lambda: NOW


_ID = re.compile(r"\b([a-z]{1,6})_([0-9a-f]{8,32})\b")


def steady(body):
    """The body with each generated id (tk_1a2b..., ms_..., per_...) replaced by its prefix and the
    order it first appears in, the same id by the same number."""
    seen: dict[str, str] = {}
    n: dict[str, int] = {}

    def sub(m):
        if m.group(0) not in seen:
            n[m.group(1)] = n.get(m.group(1), 0) + 1
            seen[m.group(0)] = f"{m.group(1)}_{n[m.group(1)]}"
        return seen[m.group(0)]

    def walk(v):
        if isinstance(v, str):
            return _ID.sub(sub, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        return v
    return walk(body)


def ago(**kw):
    return NOW - timedelta(**kw)


def seed(store):
    """25 projects of 10 tickets each with history, 3 handed over, 40 sessions over 6 stations."""
    stages = ["merged", "review", "changes", "doing", "blocked", None, None, "doing", "review", None]
    by_project = {}
    for i in range(25):
        uid = f"uc_budget{i:02d}"
        store.create_project(uid, "custom", f"Budget project {i:02d}", {}, goal=f"Goal of project {i:02d}")
        store.set_project_kind(uid, "software_factory")
        repo = f"example-org/budget-{i:02d}"
        m1 = store.create_milestone(uid, "M1: first pages", "The pages are live.",
                                    [{"check": "open the home page", "expect": "it loads"}])
        m2 = store.create_milestone(uid, "M2: search", "Search works.", [])
        tickets = []
        for j in range(10):
            stage = stages[j]
            done = stage == "merged"
            holder = f"budget{i % 3:02d}-build-m1" if stage in ("doing", "changes") else None
            tid = store.create_ticket(uid, "tares", f"Ticket {j:02d} of project {i:02d}",
                                      milestone=m1 if j < 5 else m2, repo=repo)
            fields = {"stage": stage, "status": "done" if done else ("in_progress" if stage else "todo"),
                      "holder": holder}
            if stage in ("review", "changes", "merged"):
                fields["pr"] = {"url": f"https://github.com/{repo}/pull/{j + 1}", "repo": repo,
                                "number": j + 1, "head": f"aaaaaaa{j:04d}",
                                "state": "closed" if done else "open", "merged": done,
                                "ci": "success" if stage != "changes" else "failure"}
            store.update_ticket(tid, **fields)
            for k, (field, value) in enumerate((("status", "todo"), ("stage", "doing"), ("holder", holder),
                                                ("stage", stage))):
                store.add_ticket_history(tid, uid, field, value, None, holder or "crew-orchestrator",
                                         "builder", ago(days=3, hours=-k))
            tickets.append(tid)
        by_project[uid] = tickets
        # each project moved at its own minute, so the list's order does not hang on a tie
        stamp = ago(minutes=i * 3)
        from sqlalchemy import update
        from tares.session_store.tables import tickets as tickets_t
        from tares.session_store.tables import usecases as projects_t
        with store.sessions_db.begin() as c:
            c.execute(update(tickets_t).where(tickets_t.c.project == uid).values(updated_at=stamp))
            c.execute(update(projects_t).where(projects_t.c.id == uid).values(
                created_at=ago(days=30, minutes=i), updated_at=stamp))
        if i < 3:
            store.set_handover(uid, [{"path": f"/Users/sam/code/budget-{i:02d}", "name": repo}],
                               None, None, ago(days=2))
    stations = [("crew-orchestrator", "orchestrator", ""), ("crew-reviewer", "reviewer", "crew-orchestrator"),
                ("crew-releaser", "releaser", "crew-orchestrator")] + [
        (f"budget{k:02d}-build-m1", "builder", "crew-orchestrator") for k in range(3)]
    envs = []
    for n in range(40):
        station, role, parent = stations[n % 6]
        sid = f"sess-{n:02d}"
        store.set_session_station(sid, station, role, parent, f"uc_budget{n % 3:02d}" if role == "builder" else None,
                                  ago(hours=n % 5 + 1))
        for e in range(2):
            t = ago(hours=n % 5, minutes=e * 7 + n)
            envs.append(Envelope("claude_code", "claude_code", sid, "assistant", f"line {e} of {sid}",
                                 t, {"session_id": sid}, {"session_id": sid}, t))
    store.append(envs)
    return by_project


async def done_projects(store, sam, call, base):
    """12 finished projects of 5 tickets (assigned to sam, to the cell's own person and to no one, one
    canceled) join the cell. The Projects list on a team cell reads each project's tickets for its
    "tickets yours" line; for a finished project it must read counts instead: a few statements more
    than the page without them, not a few per project, and none of its ticket rows. The same page
    with `done_ticket_facts` turned off (every finished project's tickets loaded, as before TR-1503)
    gives the before: the same body, 60 more ticket rows read (the statements are about the same, as
    the tickets of all projects come in one query)."""
    sam_id = store.find_person_by_email("sam-budget@example.com")["id"]
    local_id = next(p["id"] for p in store.list_people() if p["id"] != sam_id)
    who = [sam_id, local_id, None]
    for i in range(DONE_PROJECTS):
        uid = f"uc_budgetdone{i:02d}"
        store.create_project(uid, "custom", f"Finished project {i:02d}", {}, goal=f"Done goal {i:02d}",
                             status="done")
        store.set_project_kind(uid, "software_factory")
        for j in range(5):
            tid = store.create_ticket(uid, "tares", f"Done ticket {j} of {i:02d}", repo=f"example-org/done-{i:02d}",
                                      assignee_person_id=who[(i + j) % 3])
            store.update_ticket(tid, stage="merged", status="canceled" if j == 4 else "done")
        store.set_handover(uid, [{"path": f"/Users/sam/code/done-{i:02d}", "name": f"example-org/done-{i:02d}"}],
                           None, None, ago(days=9))
        store.finish_handover(uid, "done", None, "crew", ago(days=1, hours=i))

    rows = [0]
    real_tickets = store.tickets_by_project

    def counted_tickets(ids):
        out = real_tickets(ids)
        rows[0] += sum(len(v) for v in out.values())
        return out

    async def measure():
        fh.clear_cache()
        claude_usage.clear_week_by_project()    # the kept week (TR-1627) must not hide the counts
        counter = [0]
        rows[0] = 0
        token = _COUNTER.set(counter)
        store.tickets_by_project = counted_tickets
        try:
            resp = await call(sam, "/api/factory/projects")
        finally:
            store.tickets_by_project = real_tickets
            _COUNTER.reset(token)
        return resp, counter[0], rows[0]

    after_resp, after, after_rows = await measure()
    real = store.done_ticket_facts
    store.done_ticket_facts = lambda ids: {}          # the way it read before: every ticket row
    try:
        before_resp, before, before_rows = await measure()
    finally:
        store.done_ticket_facts = real
    ck("the Projects list with finished projects answers 200", after_resp.status_code == 200
       and before_resp.status_code == 200, f"{after_resp.status_code} {before_resp.text[:200]}")
    after_body, before_body = steady(after_resp.json()), steady(before_resp.json())
    done_rows = [p for p in after_body["projects"] if p["state"] == "done"]
    print(f"  without finished projects: {base} statements; with {DONE_PROJECTS}: {after} statements and "
          f"{after_rows} ticket rows from counts, {before} and {before_rows} from ticket rows")
    ck(f"all {DONE_PROJECTS} finished projects are listed", len(done_rows) == DONE_PROJECTS, len(done_rows))
    ck("a finished project's yours_text names the assignees",
       all(p.get("yours_text") for p in done_rows) and any("yours" in p["yours_text"] for p in done_rows),
       [p.get("yours_text") for p in done_rows][:3])
    ck("the canceled ticket is not counted in yours_text",
       all(p["yours_text"].count("ticket") <= 1 and "5 " not in p["yours_text"] for p in done_rows),
       [p["yours_text"] for p in done_rows][:3])
    ck("the body from counts is the body from ticket rows", after_body == before_body,
       "the team path changed what the page answers")
    ck(f"finished projects add at most {DONE_EXTRA} statements ({after} against {base})",
       after <= base + DONE_EXTRA, f"{after} over {base} + {DONE_EXTRA}")
    ck(f"a finished project's tickets are not read ({after_rows} ticket rows against {before_rows})",
       before_rows - after_rows == DONE_PROJECTS * 5, (after_rows, before_rows))


# Crew's standup reads the handed-over projects together (TR-1633): this many more of them, half open
# and half finished in the last day, add no statement of its own. What each still adds is one: the
# page's cut of the open desk to the caller (team.own_desk) reads the project of each desk item.
CREW_MORE = 8


async def crew_growth(store, sam, call):
    sam_id = store.find_person_by_email("sam-budget@example.com")["id"]

    def add(i):
        """A handed-over project with a ticket merged, in review and blocked, a milestone and an
        open question for sam; every other one finished an hour ago."""
        uid = f"uc_budgetcrew{i:02d}"
        store.create_project(uid, "custom", f"Crew project {i:02d}", {}, goal=f"Goal {i:02d}")
        store.set_project_kind(uid, "software_factory")
        mid = store.create_milestone(uid, "M1: the page", "It loads.", [])
        for stage in ("merged", "review", "blocked"):
            tid = store.create_ticket(uid, "tares", f"A {stage} ticket of {i:02d}", milestone=mid)
            store.update_ticket(tid, stage=stage, status="done" if stage == "merged" else "in_progress")
            store.add_ticket_history(tid, uid, "stage", stage, None, "crew-orchestrator", "builder",
                                     ago(hours=2))
        store.set_handover(uid, [{"path": f"/Users/sam/code/crew-{i:02d}", "name": f"example-org/crew-{i:02d}"}],
                           None, None, ago(days=2), person_id=sam_id)
        store.add_desk_item(uid, "Which colour?", "", [], "blue", "", "", [], True, "crew", [],
                            ago(hours=1), to_person_id=sam_id)
        if i % 2:
            store.finish_handover(uid, "done", None, "crew", ago(hours=1), person_id=sam_id)

    async def measure():
        fh.clear_cache()
        claude_usage.clear_week_by_project()
        counter = [0]
        token = _COUNTER.set(counter)
        try:
            resp = await call(sam, "/api/factory/crew")
        finally:
            _COUNTER.reset(token)
        return resp, counter[0]

    add(0)
    add(1)   # one open and one finished already there, so the names of people are read both times
    before_resp, before = await measure()
    for i in range(2, 2 + CREW_MORE):
        add(i)
    after_resp, after = await measure()
    ck("the Crew page answers 200 with more handed-over projects",
       before_resp.status_code == 200 and after_resp.status_code == 200, after_resp.text[:200])
    was, now = before_resp.json()["standup"], after_resp.json()["standup"]
    ck(f"its standup counts the added projects ({2 + CREW_MORE} questions asked)",
       bool(now) and f"{2 + CREW_MORE} questions asked" in now["text"], (was, now))
    print(f"  crew: {before} statements; with {CREW_MORE} more handed-over projects: {after}")
    # the one read left for each project is the desk cut's get_project: BEGIN and SELECT on SQLite
    counter = [0]
    token = _COUNTER.set(counter)
    try:
        store.get_project("uc_budgetcrew00")
    finally:
        _COUNTER.reset(token)
    allowed = before + CREW_MORE * counter[0]
    ck(f"{CREW_MORE} more handed-over projects add only the desk cut's {CREW_MORE} project reads "
       f"({after} against {before})", 0 < counter[0] <= 2 and after <= allowed, f"{after} over {allowed}")


_EVENTS_SCAN = re.compile(r"\bFROM\s+events\b", re.IGNORECASE)


async def main():
    from tares.daemon import make_app
    freeze_clock()
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    bodies, counts = {}, {}

    async def call(hdr, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr, timeout=60) as cx:
            return await cx.get(path)

    async with app.router.lifespan_context(app):
        r = await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                    headers=root).post("/api/keys", json={
                                        "name": "sam-laptop", "scopes": ["admin"],
                                        "person": {"email": "sam-budget@example.com", "name": "Sam"}})
        assert r.status_code in (200, 201), r.text
        sam = {"Authorization": f"Bearer {r.json()['secret']}"}
        by_project = seed(store)
        ck("one person beside local, so the team parts run", not store.only_local_person())
        backend = install_counters(store)
        print(f"backend: {backend}")
        uid = "uc_budget00"
        ref = store.get_ticket(uid, by_project[uid][1])["id"]   # a ticket in review with a pull request
        # how often a request builds the team home (TR-1472: at most once, whichever page)
        from tares_ee import team_home as th
        builds = {"read": 0, "home": 0}
        real_init, real_home = th.HomeRead.__init__, th._team_home

        def counted_init(self, *a, **kw):
            builds["read"] += 1
            return real_init(self, *a, **kw)

        def counted_home(t):
            builds["home"] += 1
            return real_home(t)
        th.HomeRead.__init__, th._team_home = counted_init, counted_home
        # how often a request reads the stations roll-up (TR-1629: Home reads it once), and how often
        # with the totals over every session (TR-1634: Setup never)
        real_rollup = store.station_rollup

        def counted_rollup(*a, **kw):
            builds["rollup"] += 1
            builds["totals"] += bool(kw.get("totals", True))
            return real_rollup(*a, **kw)
        store.station_rollup = counted_rollup
        built = {}
        for name, tmpl in ROUTES + SETUP_ROUTES + CREW_ROUTES:
            path = tmpl.format(uid=uid, ref=ref)
            fh.clear_cache()
            claude_usage.clear_week_by_project()
            counter = [0]
            builds["read"] = builds["home"] = builds["rollup"] = builds["totals"] = 0
            token = _COUNTER.set(counter)
            try:
                resp = await call(sam, path)
            finally:
                _COUNTER.reset(token)
            built[name] = dict(builds)
            ck(f"GET {tmpl} answers 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}")
            counts[name] = counter[0]
            if (name, tmpl) in ROUTES:
                bodies[name] = steady(resp.json())
            print(f"  {name:<12} {counter[0]:>5} statements   {path}")
        print("== finished projects on a team cell (TR-1503) ==")
        await done_projects(store, sam, call, counts["projects"])
        print("== Crew does not grow with the handed-over projects (TR-1633) ==")
        await crew_growth(store, sam, call)
        print("== event reads after a private project is deleted (TR-1716) ==")

    print("== the team home is built once per request ==")
    th.HomeRead.__init__, th._team_home = real_init, real_home
    store.station_rollup = real_rollup
    ck("Home reads the stations roll-up once (TR-1629)", built["home"]["rollup"] == 1, str(built["home"]))
    ck("Projects reads the stations roll-up once (TR-1631)", built["projects"]["rollup"] == 1,
       str(built["projects"]))
    for name, _ in SETUP_ROUTES:
        b = built[name]
        ck(f"{name} reads who runs once, with no totals, and no team home (TR-1634)",
           (b["rollup"], b["totals"], b["read"], b["home"]) == (1, 0, 0, 0), str(b))
    print("  roll-up reads per route:", {n: b["rollup"] for n, b in built.items()})
    for name, _ in ROUTES:
        ck(f"{name}: the team home is read once at most ({built[name]['read']} reads, "
           f"{built[name]['home']} builds)", built[name]["read"] <= 1 and built[name]["home"] <= 1,
           str(built[name]))
    ck("home builds it (the counter sees a build)", built["home"]["read"] == 1, str(built["home"]))
    for name in ("home", "projects"):
        ck(f"{name} runs under 200 statements at 25 projects", counts[name] < 200, counts[name])


    print("== the budget ==")
    budget = BUDGET[backend]
    for name, _ in ROUTES + SETUP_ROUTES + CREW_ROUTES:
        ck(f"{name} runs statements the counter can see", counts[name] > 0, counts[name])
        ck(f"{name}: {counts[name]} statements, budget {budget.get(name)}",
           name in budget and counts[name] <= budget[name],
           f"{counts[name]} over a budget of {budget.get(name)}")
        # a floor, so a counter that stops seeing statements, or a saving that is not written down, fails
        ck(f"{name}: {counts[name]} statements is not far under the budget (lower BUDGET when a change saves queries)",
           name in budget and counts[name] >= math.floor(budget[name] * FLOOR),
           f"{counts[name]} is under {FLOOR:.0%} of the budget {budget.get(name)}: lower BUDGET")
    print("  a budget at this run's counts plus 2 percent:", {k: math.ceil(v * 1.02) for k, v in counts.items()})

    print("== the bodies ==")
    if WRITE_GOLDEN:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, "w") as f:
            json.dump({"_licence": LICENCE_LINE, **bodies}, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"  wrote {GOLDEN}")
    else:
        with open(GOLDEN) as f:
            want = json.load(f)
        ck("the golden file keeps its licence line (tests/test_ee_headers.py reads it)",
           want.get("_licence") == LICENCE_LINE, want.get("_licence"))
        for name, _ in ROUTES:
            same = json.loads(json.dumps(bodies[name], sort_keys=True)) == want.get(name)
            ck(f"{name} answers what the golden file has", same, "differs: run with --write-golden "
               "to see the diff in git, and say why the body moved")
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
