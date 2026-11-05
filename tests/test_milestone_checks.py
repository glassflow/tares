"""A milestone's acceptance checks, run and accepted (TR-1017).

The crew runs its checks and records each result; only checks marked for a person reach the
person; "Milestone to check" ends once the person answers. Checked here: a check can be marked
`for: "person"`; a milestone whose crew checks were never recorded waits for the person, who may
accept it as it is; a crew pass with no person checks accepts it without asking; a person check
waits until the person says it works; "something's wrong" needs a sentence, opens a fix ticket in
the milestone on a project whose tickets are Tares's, and the milestone leaves the person's list
(it is the crew's work now); a crew fail never reaches the person; a station cannot record a
person check or accept a milestone, a person cannot record a crew check; a project ready to close
folds its milestone items into its Ready to close item, naming the ones not accepted.

Run: .venv/bin/python tests/test_milestone_checks.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-milestone-checks-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-ms-checks"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

from tares import factory as factory_mod
from tares import factory_home as fh
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    crew = {**root, "X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"}

    async def call(method, path, body=None, hdr=root):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    async def waiting():
        fh._cache.clear()
        r = await call("GET", "/api/home")
        return [i for i in r.json()["waiting_for_you"] if i["kind"] in ("milestone_to_check", "signoff")]

    def project(name):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        return uid

    def milestone(uid, name, checks, merged=True, open_ticket=False):
        mid = store.create_milestone(uid, name, "goal", factory_mod.checks_ok(checks))
        t = store.create_ticket(uid, "tares", f"{name} work", milestone=mid)
        if merged:
            store.update_ticket(t, stage="merged", status="done")
        if open_ticket:
            store.create_ticket(uid, "tares", f"{name} more", milestone=mid)
        return mid

    async with app.router.lifespan_context(app):
        print("== who a check is for ==")
        got = factory_mod.checks_ok([{"check": "run the tests", "expect": "green"},
                                     {"check": "open Reviews", "expect": "three lists", "for": "person"}])
        ck("a check marked for a person keeps the mark, the others are the crew's", got == [
            {"check": "run the tests", "expect": "green"},
            {"check": "open Reviews", "expect": "three lists", "for": "person"}], got)
        try:
            factory_mod.checks_ok([{"check": "x", "for": "robot"}])
            ck("an unknown 'for' is refused", False)
        except Exception as e:
            ck("an unknown 'for' is refused", "crew" in str(e), str(e))

        px = project("project-x")
        # an open ticket elsewhere keeps project-x from being ready to close
        m_old = milestone(px, "M1 old", [{"check": "curl it", "expect": "200"}, {"check": "pytest", "expect": "0 failed"}])
        m_crew = milestone(px, "M2 crew only", [{"check": "pytest", "expect": "0 failed"}])
        m_person = milestone(px, "M3 person", [{"check": "pytest", "expect": "0 failed"},
                                               {"check": "Open the Reviews page", "expect": "three lists", "for": "person"}])
        m_none = milestone(px, "M4 no checks", [])
        milestone(px, "M5 still building", [{"check": "pytest"}], merged=False)

        print("== what waits for the person ==")
        w = {i["title"]: i for i in await waiting()}
        ck("a milestone whose crew checks were never recorded waits, and says so",
           "project-x, M1 old" in w and "the crew has not recorded its 2 checks" in w["project-x, M1 old"]["detail"], list(w))
        ck("a milestone without checks is accepted once merged: no item", "project-x, M4 no checks" not in w)
        ck("a milestone still building: no item", "project-x, M5 still building" not in w)

        print("== the crew records its checks ==")
        r = await call("POST", f"/api/projects/{px}/milestones/{m_crew}/checks/1", {"result": "pass", "note": "41 passed"}, crew)
        ck("the crew records a pass", r.status_code == 200 and r.json()["accepted"] and r.json()["check_results"][0]["result"] == "pass", r.text)
        ck("crew checks passed, no person checks: accepted without asking",
           "project-x, M2 crew only" not in {i["title"] for i in await waiting()})
        r = await call("POST", f"/api/projects/{px}/milestones/{m_person}/checks/1", {"result": "pass"}, crew)
        w = {i["title"]: i for i in await waiting()}
        ck("the crew passed its part; the person check still waits, and the item says it needs them",
           "1 check needs you to try it" in w.get("project-x, M3 person", {}).get("detail", ""), w.get("project-x, M3 person"))
        r = await call("POST", f"/api/projects/{px}/milestones/{m_person}/checks/2", {"result": "pass"}, crew)
        ck("a station cannot record a check meant for a person", r.status_code == 409 and "person" in r.text, r.text)

        print("== the person answers ==")
        r = await call("POST", f"/api/projects/{px}/milestones/{m_old}/checks/1", {"result": "pass"})
        ck("a person cannot record a crew check", r.status_code == 409 and "crew" in r.text, r.text)
        mp = project("project-p")
        m_p = milestone(mp, "M1 look", [{"check": "Open the Reviews page", "expect": "three lists", "for": "person"}])
        milestone(mp, "M2 later", [], merged=False)
        ck("a person check waits", "project-p, M1 look" in {i["title"] for i in await waiting()})
        r = await call("POST", f"/api/projects/{mp}/milestones/{m_p}/checks/1", {"result": "fail"})
        ck("something's wrong needs a sentence", r.status_code == 400, r.text)
        r = await call("POST", f"/api/projects/{mp}/milestones/{m_p}/checks/1", {"result": "fail", "note": "Only two lists show"})
        ck("something's wrong opens a fix ticket in the milestone", r.status_code == 200 and r.json()["opened"]
           and any(t["title"].startswith("Fix: Open the Reviews page") and t["milestone"] == m_p for t in store.list_tickets(mp)), r.text)
        ck("and the milestone is the crew's work now: no item for the person",
           "project-p, M1 look" not in {i["title"] for i in await waiting()})
        ck("the result keeps the person's sentence", r.json()["check_results"][0]["note"] == "Only two lists show"
           and r.json()["person_wrong"] == [1], r.json()["check_results"])
        fix = next(t for t in store.list_tickets(mp) if t["title"].startswith("Fix:"))
        store.update_ticket(fix["id"], stage="merged", status="done")
        r = await call("POST", f"/api/projects/{mp}/milestones/{m_p}/checks/1", {"result": "pass"})
        ck("once fixed, Works accepts it", r.json()["accepted"], r.json())

        print("== accept as it is ==")
        r = await call("POST", f"/api/projects/{px}/milestones/{m_old}/accept", {}, crew)
        ck("a station cannot accept a milestone", r.status_code == 403, r.text)
        r = await call("POST", f"/api/projects/{px}/milestones/{m_old}/accept", {})
        ck("the person accepts it as it is", r.status_code == 200 and r.json()["accepted"], r.text)
        ck("and its item ends", "project-x, M1 old" not in {i["title"] for i in await waiting()})

        print("== a crew failure stays with the crew ==")
        mf = project("project-f")
        m_f = milestone(mf, "M1 red", [{"check": "pytest", "expect": "0 failed"}])
        milestone(mf, "M2 later", [], merged=False)
        r = await call("POST", f"/api/projects/{mf}/milestones/{m_f}/checks/1", {"result": "fail", "note": "3 failed"}, crew)
        ck("a crew fail is recorded and listed for the crew", r.json()["crew_failed"] == [1] and not r.json()["accepted"], r.json())
        ck("and never reaches the person", "project-f, M1 red" not in {i["title"] for i in await waiting()})

        print("== ready to close folds the milestone items ==")
        pr = project("project-r")
        milestone(pr, "M1 a", [{"check": "pytest"}])
        milestone(pr, "M2 b", [{"check": "pytest"}, {"check": "Look at it", "for": "person"}])
        w = await waiting()
        titles = [i["title"] for i in w]
        ck("no milestone items for a project ready to close", not any(t.startswith("project-r, ") for t in titles), titles)
        rc = next((i for i in w if i["title"] == "project-r"), None)
        ck("its Ready to close names the milestones not accepted", rc and "M1 and M2 are not accepted yet." in rc["detail"], rc)

        print("== bad input ==")
        r = await call("POST", f"/api/projects/{px}/milestones/{m_crew}/checks/9", {"result": "pass"}, crew)
        ck("a check that does not exist: 404", r.status_code == 404, r.text)
        r = await call("POST", f"/api/projects/{px}/milestones/{m_crew}/checks/1", {"result": "maybe"}, crew)
        ck("a result that is not pass or fail: 400", r.status_code == 400, r.text)
        r = await call("GET", f"/api/projects/{px}/milestones")
        ms = {m["name"]: m for m in r.json()["milestones"]}
        ck("list_milestones says what the crew still has to run", ms["M5 still building"]["crew_to_run"] == [1]
           and ms["M2 crew only"]["crew_to_run"] == [] and ms["M2 crew only"]["check_results"][0]["for"] == "crew"
           and ms["M2 crew only"]["checks"] == [{"check": "pytest", "expect": "0 failed"}], ms["M2 crew only"])


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
