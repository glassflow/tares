# Tares enterprise licence, see ee/LICENSE
"""Where the time of the ticket brief, the builder brief and merge-ready goes (TR-1476).

`tares_route_phase_seconds{route, phase}` splits each of the three routes into the steps it spends
time on, so a slow brief on factory shows which step it waited for. A licensed factory cell with one
project and one ticket in review (a pull request, a recorded check, a station session), GitHub a
fake that answers at once, and the three routes called once each. Covered:
 - the family is on /metrics before any call, one series per phase of each route, all at zero;
 - after one call of each route, every phase of that route has a sample in /metrics (a count above
   zero), and no phase of another route moved;
 - the phases leave each other out, so a route's phases add up to no more than the call took;
 - a phase that raises is still reported;
 - outside a bound route a `step` times nothing, and without the client library `phase` is a no-op.

Run: .venv/bin/python ee/tests/test_route_phase_metric.py
"""
import asyncio
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-route-phase-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-route-phase"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

from tares import factory_github, metrics
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


REPO = "acme/api"
N = 7


async def github(req: httpx.Request):
    path = req.url.path
    if path.endswith(f"/pulls/{N}"):
        return httpx.Response(200, json={
            "title": "PR", "state": "open", "merged": False, "draft": False,
            "head": {"sha": "0007abcdef", "ref": "factory/T1"}, "base": {"ref": "main"},
            "user": {"login": "crew-bot"}, "html_url": f"https://github.com/{REPO}/pull/{N}"})
    if path.endswith(f"/pulls/{N}/files"):
        return httpx.Response(200, json=[{"filename": "a.py", "additions": 3, "deletions": 1}])
    return httpx.Response(200, json={"statuses": [], "check_runs": []})


def sample(name, route, ph):
    return metrics.REGISTRY.get_sample_value(name, {"route": route, "phase": ph})


def counts(route):
    return {ph: sample("tares_route_phase_seconds_count", route, ph) for ph in metrics.ROUTE_PHASES[route]}


def total(route):
    return sum(sample("tares_route_phase_seconds_sum", route, ph) or 0 for ph in metrics.ROUTE_PHASES[route])


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    async def tokens(store, repo):
        return [("tok", "https://api.github.com")]

    factory_github._transport = httpx.MockTransport(github)
    factory_github._tokens = tokens
    cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                           headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)

    uid = "uc_routephase"
    store.create_project(uid, "route-phase", goal="A goal")
    store.set_project_kind(uid, "software_factory")
    m1 = store.create_milestone(uid, "M1: first", "Done.", [{"check": "open it", "expect": "it loads"}])
    tid = store.create_ticket(uid, "tares", "The ticket", milestone=m1, repo=REPO)
    store.update_ticket(tid, stage="review", status="in_progress", holder="phase-build-m1",
                        pr={"url": f"https://github.com/{REPO}/pull/{N}", "repo": REPO, "number": N,
                            "head": "0007abcdef", "branch": "factory/T1", "base": "main",
                            "state": "open", "merged": False, "checked_at": "2020-01-01T00:00:00+00:00"})
    store.add_check(uid, tid, "pytest -q", "3 passed", "0007abcdef", "", "phase-build-m1", "s1", now_utc())
    store.set_session_station("s1", "phase-build-m1", "builder", "crew-orchestrator", uid, now_utc())

    print("== the family is there before any call ==")
    r = await cx.get("/metrics")
    text = r.text
    ck("/metrics answers", r.status_code == 200, r.status_code)
    for route, phases in metrics.ROUTE_PHASES.items():
        for ph in phases:
            ck(f"{route}/{ph}: a series at zero",
               f'tares_route_phase_seconds_count{{phase="{ph}",route="{route}"}} 0.0' in text, ph)
    ck("the three routes are the ones the ticket names",
       set(metrics.ROUTE_PHASES) == {"brief", "builder_brief", "merge_ready"}, list(metrics.ROUTE_PHASES))

    calls = {
        "brief": f"/api/projects/{uid}/tickets/{tid}/brief",
        "builder_brief": f"/api/projects/{uid}/tickets/{tid}/builder-brief",
        "merge_ready": f"/api/factory/merge-ready?pr=https://github.com/{REPO}/pull/{N}",
    }
    for route, path in calls.items():
        print(f"== {route} ==")
        before_all = {r_: counts(r_) for r_ in calls if r_ != route}
        before_sum = total(route)
        t0 = time.perf_counter()
        resp = await cx.get(path)
        took = time.perf_counter() - t0
        ck(f"GET answers 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}")
        got = counts(route)
        for ph in metrics.ROUTE_PHASES[route]:
            ck(f"{ph} has a sample", (got[ph] or 0) >= 1, got[ph])
        text = (await cx.get("/metrics")).text
        for ph in metrics.ROUTE_PHASES[route]:
            m = re.search(rf'tares_route_phase_seconds_count{{phase="{ph}",route="{route}"}} ([0-9.e+]+)', text)
            ck(f"/metrics shows {ph} above zero", bool(m) and float(m.group(1)) >= 1, m and m.group(1))
        ck("no phase of another route moved", all(counts(r_) == b for r_, b in before_all.items()),
           {r_: counts(r_) for r_ in before_all})
        spent = total(route) - before_sum
        ck(f"the phases leave each other out: they add up to no more than the call ({spent:.3f}s of {took:.3f}s)",
           0 <= spent <= took + 0.01, (spent, took))

    print("== merge_ready for a pull request no ticket has reports nothing it did not do ==")
    before = counts("merge_ready")
    resp = await cx.get("/api/factory/merge-ready?pr=https://github.com/acme/api/pull/999")
    ck("answers 200, not ready", resp.status_code == 200 and resp.json()["ready"] is False, resp.text[:200])
    ck("no phase ran, none reported", counts("merge_ready") == before, counts("merge_ready"))

    print("== a phase that raises is still reported ==")
    before = sample("tares_route_phase_seconds_count", "brief", "render")
    try:
        with metrics.phase("brief", "render"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    ck("the phase that raised has its sample",
       sample("tares_route_phase_seconds_count", "brief", "render") == before + 1)

    print("== nested phases leave the inside out of the outside ==")
    s0 = sample("tares_route_phase_seconds_sum", "brief", "render")
    i0 = sample("tares_route_phase_seconds_sum", "brief", "repo_map")
    with metrics.phase("brief", "render"):
        time.sleep(0.05)
        with metrics.phase("brief", "repo_map"):
            time.sleep(0.2)
    outer = sample("tares_route_phase_seconds_sum", "brief", "render") - s0
    inner = sample("tares_route_phase_seconds_sum", "brief", "repo_map") - i0
    ck(f"the outer keeps its own time ({outer:.2f}s), the inner its own ({inner:.2f}s)",
       0.04 <= outer < 0.15 and 0.19 <= inner < 0.35, (outer, inner))

    print("== step names the bound route, and outside one it times nothing ==")
    before = counts("brief")
    with metrics.step("render"):
        pass
    ck("no bound route: nothing reported", counts("brief") == before)
    with metrics.bind_route("brief"):
        with metrics.step("render"):
            pass
    ck("a bound route: reported under it",
       sample("tares_route_phase_seconds_count", "brief", "render") == before["render"] + 1)
    with metrics.step("render"):
        pass
    ck("and the binding ends with the block", counts("brief")["render"] == before["render"] + 1)

    print("== without the client library, phase does nothing ==")
    metrics.AVAILABLE = False
    try:
        with metrics.phase("brief", "render"):
            pass
        with metrics.step("render"):
            pass
        ok = True
    except Exception as e:   # noqa: BLE001
        ok = False
        print("   ", e)
    finally:
        metrics.AVAILABLE = True
    ck("no error", ok)

    await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
