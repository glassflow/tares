"""A pull request's checks and its merge in one tap (TR-1449, TR-1450).

TR-1449: the PR item carries `checks`, its CI in a word, how many commits its base has that the
branch lacks, and the two in one sentence. The sync measures that drift with a compare when it
reads a pull request in review again (GitHub stubbed here), and keeps it on the snapshot.

TR-1450: when the merge queue asks the same person to approve the pull request, the PR item's
primary action is Merge (approve_merge with the queue row), and the queue's own desk question
shows no more, so the decision shows once. Approved, the PR leaves Home (the queue has it) and the
answer is recorded under the PR item's id, from the surface that sent it. A milestone to check
is accepted in one tap (accept_milestone), and that is recorded too.

Run: .venv/bin/python tests/test_pr_one_tap.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-pr-one-tap-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-one-tap"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory as factory_mod
from tares import factory_api
from tares import factory_github
from tares import factory_home as fh
from tares import factory_waiting as fw
from tares import merge_queue as mq
from tares.store import now_utc

P = F = 0
REPO = "example-org/shop"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    cx_ = lambda **h: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                        headers={"Authorization": f"Bearer {TOKEN}", **h})

    async def home():
        fh._cache.clear()
        async with cx_() as cx:
            r = await cx.get("/api/home")
        assert r.status_code == 200, r.text
        return r.json()["waiting_for_you"]

    async def answers():
        async with cx_() as cx:
            return {a["id"]: a for a in (await cx.get("/api/answers")).json()["answers"]}

    print("== the checks sentence ==")
    c = fw.pr_checks
    ck("passing, behind", c({"ci": "success", "behind_by": 2, "base": "main"}) == {
        "ci": "passing", "behind_by": 2, "line": "Checks pass. main has 2 commits this branch lacks."})
    ck("failing, one commit", c({"ci": "failure", "behind_by": 1, "base": "dev"})["line"]
       == "Checks fail. dev has 1 commit this branch lacks.")
    ck("running, up to date", c({"ci": "pending", "behind_by": 0})["line"]
       == "Checks are still running. Up to date with main.")
    ck("no checks, drift unknown", c({"ci": "none", "behind_by": None}) == {
        "ci": "none", "behind_by": None, "line": "No checks ran."})
    ck("nothing read", c({}) == {"ci": None, "behind_by": None, "line": "Checks not read yet."})

    async with app.router.lifespan_context(app):
        uid = "uc_shop"
        store.create_project(uid, "custom", "shop", {}, goal="Sell things")
        store.set_project_kind(uid, "software_factory")
        store.set_handover(uid, [{"path": "/w/shop", "name": REPO}], None, None, now_utc())
        tid = store.create_ticket(uid, "tares", "Add the cart", repo=REPO)
        store.update_ticket(tid, stage="review", pr={
            "url": f"https://github.com/{REPO}/pull/12", "repo": REPO, "number": 12,
            "head": "aaaaaaa1111", "base": "main", "state": "open", "checked_at": None})
        store.add_review(uid, tid, None, "aaaaaaa1111", "pass", "ran the tests", None,
                         "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc())
        t = store.get_ticket(uid, tid)
        ref = factory_mod.label(t)
        pid = f"prs_to_review:{uid}:{ref}"

        print("== the sync measures the drift (TR-1449) ==")
        calls = []

        async def read_pr(store_, repo, number, prev=None):
            return {**(prev or {}), "repo": repo, "number": number, "head": "aaaaaaa1111",
                    "base": "main", "state": "open", "merged": False, "ci": "success",
                    "checked_at": now_utc().isoformat()}

        async def compare(store_, repo, base, head):
            calls.append((repo, base, head))
            return {"ok": True, "behind_by": 3, "ahead_by": 1, "files": [], "error": None}
        real = (factory_github.read_pr, factory_github.compare)
        factory_github.read_pr, factory_github.compare = read_pr, compare
        try:
            await factory_api._refresh(store, uid)
        finally:
            factory_github.read_pr, factory_github.compare = real
        snap = store.get_ticket(uid, tid)["pr"]
        ck("one compare, base against head", calls == [(REPO, "main", "aaaaaaa1111")], calls)
        ck("the snapshot keeps behind_by and CI", snap.get("behind_by") == 3 and snap.get("ci") == "success", snap)

        w = await home()
        pr = next((i for i in w if i["id"] == pid), None)
        ck("the PR item carries its checks", pr and pr["checks"] == {
            "ci": "passing", "behind_by": 3, "line": "Checks pass. main has 3 commits this branch lacks."},
           pr)
        ck("no merge asked yet: Review is its one action", pr and pr["actions"] == [
            {"label": "Review", "primary": True, "href": f"/reviews?project={uid}"}], pr and pr["actions"])

        async def fails(*a, **k):
            return {"ok": False, "behind_by": 0, "ahead_by": 0, "files": [], "error": "boom"}
        store.update_ticket(tid, pr={**snap, "checked_at": None})
        factory_github.read_pr, factory_github.compare = read_pr, fails
        try:
            await factory_api._refresh(store, uid)
        finally:
            factory_github.read_pr, factory_github.compare = real
        snap = store.get_ticket(uid, tid)["pr"]
        ck("a compare that fails leaves the drift unknown, not zero", snap.get("behind_by") is None, snap)

        print("== the merge in one tap (TR-1450) ==")
        row = store.merge_queue_add(REPO, "main", 12, "aaaaaaa1111", tid, uid)
        mq._ask(store, row, t)
        d = store.get_desk_item(row["desk_item"])
        w = await home()
        pr = next((i for i in w if i["id"] == pid), None)
        ck("Merge approves the queue row, Review stays as a link", pr and pr["actions"] == [
            {"label": "Merge", "primary": True, "do": "approve_merge", "queue": row["id"]},
            {"label": "Review", "primary": False, "href": f"/reviews?project={uid}"}], pr and pr["actions"])
        ck("the queue's own question shows no more: the decision shows once",
           [i["id"] for i in w] == [pid], [i["id"] for i in w])
        ck("absorbs is not part of the item", pr and "absorbs" not in pr)
        ck("the question is still open on the desk", store.get_desk_item(d["id"])["state"] == "open")

        async with cx_(**{"X-Tares-Via": "feed"}) as cx:
            r = await cx.post(f"/api/factory/merge-queue/{row['id']}/approve")
        ck("approved", r.status_code == 200 and store.merge_queue_get(row["id"])["state"] == "queued", r.text)
        ck("approved, the PR leaves Home: the queue has it", all(i["id"] != pid for i in await home()))
        a = (await answers()).get(pid) or {}
        ck("the answer is recorded under the PR item, from the feed", a.get("choice") == "yes"
           and a.get("said") == "Merge" and a.get("via") == "feed" and a.get("kind") == "prs_to_review"
           and a.get("ask") == "Merge PR 12", a)
        store.update_ticket(tid, pr={**snap, "head": "bbbbbbb2222"})
        store.add_review(uid, tid, None, "bbbbbbb2222", "pass", "ran the tests", None,
                         "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc())
        ck("a new commit waits again: the approval was for the old one",
           any(i["id"] == pid for i in await home()))

        print("== a milestone accepted in one tap (TR-1450) ==")
        async with cx_() as cx:
            await cx.post(f"/api/projects/{uid}/milestones", json={
                "name": "M1: cart", "checks": [{"check": "add a book", "expect": "the cart counts it", "for": "person"}]})
        mid = store.milestones_by_project([uid])[uid][0]["id"]
        done = store.create_ticket(uid, "tares", "Cart model", milestone=mid, repo=REPO)
        store.update_ticket(done, stage="merged", status="done")
        w = await home()
        mc = next((i for i in w if i["kind"] == "milestone_to_check"), None)
        ck("the milestone item offers Accept", mc and mc["actions"][0] == {
            "label": "Accept", "primary": True, "do": "accept_milestone", "project": uid, "milestone": mid},
           mc)
        async with cx_() as cx:
            r = await cx.post(f"/api/projects/{uid}/milestones/{mid}/accept", json={})
        ck("accepted", r.status_code == 200, r.text)
        ck("accepted, it leaves Home", all(i["kind"] != "milestone_to_check" for i in await home()))
        a = (await answers()).get(f"milestone_to_check:{uid}:{mid}") or {}
        ck("the acceptance is recorded", a.get("choice") == "yes" and a.get("said") == "Accepted"
           and a.get("via") == "console" and a.get("ask") == "Check M1: cart", a)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
