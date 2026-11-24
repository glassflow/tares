# Tares enterprise licence, see ee/LICENSE
"""Every route the console calls when it opens a page is held to a 300 ms time target (TR-1607).

The cell is the full-size factory seed (`factory_seed.seed`, TR-1604): the row counts of the real
factory cell. One person who signs in with a key reads each route in ROUTES in process
(`httpx.ASGITransport`, a frozen clock, a licence). A route gets one warm-up call, then 5 timed calls,
and the in-process caches in CACHES are cleared before each of them, so a call is the first read of
a page after a quiet minute, which is what a person opening it sees. The median of the 5 is the
route's time.

A route fails when its median is over `max(TARGET, its OVER entry) * SLACK[backend] * SPEED`. SPEED is
how much slower this machine is than the one the OVER medians were taken on (never under 1): a fixed
workload (`calibrate`) is timed in the same run, so a CI runner that is 1.5 times slower gets limits
1.5 times wider, and a route that really got slower still fails on any machine. An OVER entry is the
slowest median of several runs on that machine, not one run. A route that is over
TARGET today sits in OVER with the median that was measured and the ticket that brings it down; when
that ticket's change lands, the route meets TARGET, its entry is stale, and the test fails until the
entry is removed. A PR that moves a time updates OVER and says why; the PR that merges second
rebases and keeps both.

Run: .venv/bin/python ee/tests/test_factory_page_times.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_factory_page_times.py
"""
import asyncio
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()
sys.path.insert(0, licensed.REPO)

import factory_seed as fs

TARGET = 0.300                                       # seconds, the median of a route
# How much a median may exceed its base, per backend. Postgres gets more: a round trip to the database
# (a container here, a service on CI) varies from run to run in a way `calibrate()` cannot see. A CI
# runner is not given a larger slack: `SPEED` already widens its limits by how slow it measures.
SLACK = {"duckdb": 1.25, "postgres": 1.6}
RUNS = 5                                             # timed calls per route, after one warm-up
CALIBRATION_REF = 0.021                               # seconds `calibrate()` takes on the machine the OVER medians come from

# Routes over TARGET today, by backend: (the median in seconds when it was measured, the ticket that
# brings it down). A route absent from here must meet TARGET.
OVER: dict = {
    "duckdb": {
        "setup": (0.845, "TR-1628"), "setup_progress": (0.801, "TR-1634"),
        "home": (0.776, "TR-1629"), "crew": (0.803, "TR-1633"),
        "tickets": (0.561, "TR-1612"), "tickets_list": (0.541, "TR-1612"),
        "project_tickets": (0.530, "TR-1612"), "projects": (0.514, "TR-1610"),
    },
    "postgres": {
        "setup": (0.391, "TR-1628"), "setup_progress": (0.361, "TR-1634"),
        "home": (0.342, "TR-1629"), "crew": (0.396, "TR-1633"),
    },
}

# What each route is, and where the console asks for it. `{p}` and `{t}` are the seed's anchor project
# and ticket, `{bp}` and `{bt}` its biggest project and a ticket with many reviews, `{station}` and
# `{crew}` a crew station and its crew. The shell routes run on every page; the others on their page.
# Statuses other than 200 that a page accepts are named (a brain that is not configured answers 404 and
# the console hides its menu).
ROUTES = [
    # the shell, on every page (ui/src/editions/edition.tsx, licence.ts, cloud.ts, Shell.tsx)
    ("whoami", "/api/whoami"),
    ("licence", "/api/licence"),
    ("health", "/health"),
    ("brain", "/api/brain", (200, 404)),
    ("setup_progress", "/api/factory/setup/progress"),
    # Home (FactoryHome.api.ts)
    ("home", "/api/home"),
    ("answers", "/api/answers"),
    # Projects and one project (FactoryProjects.tsx, FactoryProject.api.ts, ProjectShell.tsx)
    ("projects", "/api/factory/projects"),
    ("project", "/api/factory/projects/{p}"),
    ("project_big", "/api/factory/projects/{bp}"),
    ("project_github", "/api/factory/projects/{p}/github"),
    ("project_merge_queue", "/api/projects/{p}/merge-queue"),
    ("project_docs", "/api/factory/projects/{p}/docs"),
    ("project_tickets", "/api/factory/tickets?project={p}&sort=open&limit=200"),
    # A ticket and the ticket lists (FactoryTicket.api.ts, FactoryTickets.api.ts)
    ("ticket", "/api/factory/projects/{p}/tickets/{t}"),
    ("ticket_big", "/api/factory/projects/{bp}/tickets/{bt}"),
    ("tickets", "/api/factory/tickets?sort=open&limit=200"),
    ("tickets_list", "/api/factory/tickets?limit=25"),
    # Reviews, Crew, Docs, People (FactoryReviews.api.ts, FactoryCrew.api.ts, FactoryDocs.api.ts, FactoryPeople.api.ts)
    ("reviews", "/api/factory/reviews"),
    ("crew", "/api/factory/crew"),
    ("crew_member", "/api/crew/{station}?recent=0&conversation=200&crew={crew}"),
    ("docs", "/api/factory/docs"),
    ("people", "/api/factory/people"),
    # Settings, Setup, Integrations (FactorySettings.api.ts, FactorySetup.api.ts)
    ("settings", "/api/factory/settings"),
    ("setup", "/api/factory/setup"),
    # the tabs of Settings and the Brain pages (daemon.py, ee/tares_ee/brain)
    ("people_list", "/api/people"),
    ("settings_providers", "/api/settings/providers"),
    ("settings_agents", "/api/settings/agents"),
    ("settings_usage", "/api/usage"),
    ("settings_usage_model", "/api/usage/model?days=30"),
    ("settings_keys", "/api/keys"),
    ("settings_tracing", "/api/settings/tracing"),
    ("settings_mcp", "/api/settings/mcp"),
    ("areas", "/api/areas"),                               # AreasPanel.tsx
    ("projects_list", "/api/projects"),                    # the keys tab (ApiKeysPanel.tsx)
    # the repos the GitHub Issues form lists; the seed has no GitHub credential, so this is the route's own cost
    ("setup_github_repos", "/api/factory/setup/github/repos?via=app"),
    ("brain_decisions", "/api/brain/decisions?limit=25", (200, 404)),
    ("brain_available", "/api/brain/repos/available", (200, 404)),
]


def caches() -> list:
    """Every in-process cache a page read can hit, as (name, the function that empties it). The home
    cache is `factory_home`'s; the sessions' caught-up flag is a flag on the store that the next read
    sets again from `session_ingest_marks`; the 7-day usage keep exists once TR-1627 has merged."""
    from tares import factory_home as fh
    out = [("factory_home.clear_cache", fh.clear_cache)]
    out.append(("store._sessions_caught_up", lambda: setattr(STORE, "_sessions_caught_up", False)))
    try:
        from tares import claude_usage
        if hasattr(claude_usage, "clear_week_by_project"):
            out.append(("claude_usage.clear_week_by_project", claude_usage.clear_week_by_project))
    except ImportError:
        pass
    from tares import github_credentials as gc   # the repo list GitHub gave, kept 5 minutes per credential
    out.append(("github_credentials._repos_cache", gc._repos_cache.clear))
    return out


CACHES: list = []
STORE = None


def _licence() -> str:
    kit = licensed.kit
    root_priv, _ = kit.keypair(kit.ROOT_PHRASE)
    iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
    cert = kit.issuer_cert(root_priv, iss_pub, "2099-01-01")
    return kit.sign(iss_priv, cert, "Test Org", 5, ("enterprise",), "2020-01-01T00:00:00Z",
                    "2099-01-01", "lic_ee_page_times")


os.environ["TZ"] = "UTC"
time.tzset()
_TMP = tempfile.mkdtemp(prefix="tares-page-times-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = _licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-page-times"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def calibrate() -> float:
    """Seconds a fixed workload takes here (the median of 7 runs): sorting, hashing and JSON, the kind
    of work a page read does in Python. It does not touch the database or the cell."""
    import hashlib
    import json
    times = []
    for _ in range(7):
        t = time.perf_counter()
        rows = [{"id": i, "name": f"row-{i * 7919 % 10007}", "n": (i * 31) % 977} for i in range(20000)]
        rows.sort(key=lambda r: (r["name"], r["n"]))
        json.loads(json.dumps(rows))
        h = hashlib.sha256()
        for r in rows[:5000]:
            h.update(r["name"].encode())
        times.append(time.perf_counter() - t)
    return statistics.median(times)


SPEED = 1.0   # set in main(): how many times slower than the OVER machine this one is, never under 1


def allowed(over: dict, name: str, slack: float) -> float:
    """The most a route's median may be: its OVER entry or TARGET, whichever is more, times the
    backend's slack and SPEED."""
    base = max(TARGET, over[name][0]) if name in over else TARGET
    return base * slack * SPEED


async def main():
    global STORE, CACHES, SPEED
    from tares.daemon import make_app
    fs.freeze_clock()
    app = make_app()
    STORE = app.state.store
    backend = "postgres" if os.environ.get("TARES_TEST_PG") else "duckdb"
    over = OVER[backend]
    slack = SLACK[backend]
    cal = calibrate()
    SPEED = max(1.0, cal / CALIBRATION_REF)
    print(f"calibration {cal * 1000:.0f} ms (reference {CALIBRATION_REF * 1000:.0f} ms): limits times {SPEED:.2f}")
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root,
                                     timeout=120) as cx:
            r = await cx.post("/api/keys", json={"name": "sam-laptop", "scopes": ["admin"],
                                                 "person": {"email": "sam-times@example.com", "name": "Sam"}})
            assert r.status_code in (200, 201), r.text
            sam = {"Authorization": f"Bearer {r.json()['secret']}"}
        print(f"== seeding ({backend}) ==")
        made = fs.seed(STORE, log=lambda *_: None)
        ids = {"p": made["anchor_project"], "t": made["anchor_ticket"], "bp": made["big_project"],
               "bt": made["big_ticket"], "station": made["station"], "crew": made["crew_id"]}
        CACHES = caches()
        print("caches cleared before each timed call: " + ", ".join(n for n, _ in CACHES))
        results = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=sam,
                                     timeout=300) as cx:
            for route in ROUTES:
                name, path = route[0], route[1].format(**ids)
                statuses = route[2] if len(route) > 2 else (200,)
                for _, clear in CACHES:
                    clear()
                warm = await cx.get(path)
                times = []
                bad = None if warm.status_code in statuses else (warm.status_code, warm.text[:200])
                for _ in range(RUNS):
                    for _, clear in CACHES:
                        clear()
                    t = time.perf_counter()
                    resp = await cx.get(path)
                    times.append(time.perf_counter() - t)
                    if resp.status_code not in statuses and bad is None:
                        bad = (resp.status_code, resp.text[:200])
                results[name] = (statistics.median(times), bad, path)
        print(f"\n== medians ({backend}, target {TARGET * 1000:.0f} ms, slack {slack}, speed {SPEED:.2f}) ==")
        print(f"  {'route':<24} {'median ms':>10} {'allowed ms':>11}  status")
        for name, (med, bad, path) in results.items():
            lim = allowed(over, name, slack)
            status = "ok" if med <= lim and not bad else "FAIL"
            if name in over:
                status += f" (over target, {over[name][1]})"
            print(f"  {name:<24} {med * 1000:>10.0f} {lim * 1000:>11.0f}  {status}")
        print()
        for name, (med, bad, path) in results.items():
            ck(f"{name} answers as the page expects", bad is None, f"{path} {bad}")
            ck(f"{name} median {med * 1000:.0f} ms is within {allowed(over, name, slack) * 1000:.0f} ms",
               med <= allowed(over, name, slack), f"{path} {med * 1000:.0f} ms")
        for name, (med, bad, path) in results.items():
            if name in over:
                # stale only when the route is clearly under target on the reference machine (the median
                # divided by SPEED), so a route near it does not flap on a faster or slower one
                ck(f"the OVER entry of {name} ({over[name][1]}) is not stale", med / SPEED >= TARGET / slack,
                   f"{med / SPEED * 1000:.0f} ms is under {TARGET / slack * 1000:.0f} ms: remove the entry")
        ck("every OVER entry names a route in ROUTES", all(n in results for n in over),
           str([n for n in over if n not in results]))
        ck("every OVER entry names a ticket", all(str(v[1]).startswith("TR-") for v in over.values()))
    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
