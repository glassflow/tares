"""What waits for you on the factory home (TR-769): the `waiting_for_you` of GET /api/home.

Six kinds, built from what the cell stores, oldest first: pull requests the reviewer passed,
projects ready to hand to the crew, calls the crew made without you, questions and permission
requests on the desk, and milestones whose tickets all merged. Each ends when the person
answers it (or the work moves on), and nothing is kept for them. The same texts, action shapes
and ids are checked as the page reads them, and the whole body against the words list.

Run: .venv/bin/python tests/test_factory_home_waiting.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-home-waiting-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-waiting"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx
from fastapi import FastAPI

from tares import factory as factory_mod
from tares import factory_api
from tares import factory_home as fh
from tares import factory_waiting as fw
from tares import words
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    fx = factory_api.register(FastAPI(), store, SimpleNamespace(cc_source=lambda: "claude_code"))
    cx_ = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                    headers={"Authorization": f"Bearer {TOKEN}"})

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    def back(table, key, col, **kw):
        with store._swrite:
            store.sc.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", [ago(**kw), key])

    def project(name, factory=True, goal="A project for the test"):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal=goal)
        if factory:
            store.set_project_kind(uid, "software_factory")
        return uid

    def ticket(uid, title, owner="tares", milestone=None, repo=None, **fields):
        tid = store.create_ticket(uid, owner, title, milestone=milestone, repo=repo)
        if fields:
            store.update_ticket(tid, **fields)
        return tid

    def spec(uid, **kw):
        did = store.create_doc(uid, "spec", "Spec", "What to build.")
        back("docs", did, "created_at", **kw)

    def in_review(uid, title, head, verdict, **kw):
        tid = ticket(uid, title, repo="example-org/docs-site", stage="review",
                     pr={"url": f"https://github.com/example-org/docs-site/pull/{len(title)}",
                         "repo": "example-org/docs-site", "head": head}, **kw.pop("fields", {}))
        store.add_review(uid, tid, None, "aaaaaaa1111", verdict, "ran the tests", None,
                         "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc())
        return tid

    async def get_home():
        fh._cache.clear()
        async with cx_() as cx:
            r = await cx.get("/api/home")
        assert r.status_code == 200, r.text
        return r.json()

    def by_kind(h):
        out = {}
        for i in h["waiting_for_you"]:
            out.setdefault(i["kind"], []).append(i)
        return out

    async with app.router.lifespan_context(app):
        print("== an empty cell ==")
        h = await get_home()
        ck("nothing waits", h["waiting_for_you"] == [] and h["waiting_total"] == 0, h["waiting_for_you"])
        ck("the five builders are registered once", [f.__name__ for f in fh.WAITING_BUILDERS] == [
            "prs_to_review", "ready_to_start", "calls_to_confirm", "desk_items", "milestones_to_check"],
           [f.__name__ for f in fh.WAITING_BUILDERS])

        # project-x: two passed pull requests (one holds up a milestone), two that do not wait for
        # the person, a permission request from its builder
        px = project("project-x")
        store.set_handover(px, [{"path": "/w/docs-site", "name": "example-org/docs-site"}], None, None,
                           now_utc())
        async with cx_() as cx:
            r = await cx.post(f"/api/projects/{px}/milestones", json={"name": "M1: pages"})
        mx = store.milestones_by_project([px]).get(px, [{}])[0].get("id") if r.status_code < 300 else None
        t1 = in_review(px, "Add the footer", "aaaaaaa1111", "pass", fields={"milestone": mx})
        t2 = in_review(px, "Add the sitemap", "aaaaaaa1111", "pass")
        t3 = in_review(px, "Add the search box", "aaaaaaa1111", "changes")
        t4 = in_review(px, "Add the news page", "bbbbbbb2222", "pass")
        ticket(px, "Add the header", milestone=mx, stage="merged", status="done")
        back("tickets", t1, "updated_at", days=3)
        back("tickets", t2, "updated_at", hours=5)
        back("tickets", t3, "updated_at", days=6)
        back("tickets", t4, "updated_at", days=6)
        pq = store.add_desk_item(
            px, "project-x-build-m1 was refused a command. Allow it?", "", [],
            "project-x-build-m1 may read the status of a commit", "it needs it to wait for CI", "",
            [], False, "crew-orchestrator", [], ago(days=4),
            station="project-x-build-m1", command="gh api repos/example-org/docs-site/statuses/abc")

        # project-y: a spec and a plan, not handed over
        py = project("project-y")
        spec(py, days=2)
        ticket(py, "Export the posts", owner="github")
        ticket(py, "Import the posts", owner="github")

        # project-z: the crew decided two things without the person; one more is already kept
        pz = project("project-z")
        a1 = store.add_assumption(pz, "Which date format?", "ISO dates", "it sorts", "", [],
                                  "z-build-m1", "builder", ago(hours=26))
        a2 = store.add_assumption(pz, "Which logger?", "the standard one", "no new package", "",
                                  [], "crew-reviewer", "reviewer", ago(hours=20))
        a3 = store.add_assumption(pz, "Which port?", "8080", "the default", "", [], "z-build-m1",
                                  "builder", ago(days=9))
        store.decide_assumption(a3["id"], "kept", None, None, now_utc())
        batch = store.add_desk_item(pz, "Keep these assumptions?", "", [], "keep both",
                                    "they are defaults", "", [], False, "crew-orchestrator",
                                    ["A1", "A2"], ago(hours=1))

        # project-w: a question on the desk, a milestone whose tickets all merged
        pw = project("project-w")
        async with cx_() as cx:
            await cx.post(f"/api/projects/{pw}/milestones", json={
                "name": "M1: styles", "checks": [{"check": "open the page", "expect": "it looks right"},
                                                 {"check": "resize it", "expect": "it fits"}]})
            await cx.post(f"/api/projects/{pw}/milestones", json={"name": "M2: search"})
            await cx.post(f"/api/projects/{pw}/milestones", json={"name": "M3: dropped"})
        mws = {m["name"].split(":")[0]: m["id"] for m in store.milestones_by_project([pw])[pw]}
        w1 = ticket(pw, "Tokens", milestone=mws["M1"], stage="merged", status="done")
        w2 = ticket(pw, "Spacing", milestone=mws["M1"], stage="merged", status="done")
        w3 = ticket(pw, "Dropped idea", milestone=mws["M1"], status="canceled")
        ticket(pw, "Search box", milestone=mws["M2"])
        ticket(pw, "Old idea", milestone=mws["M3"], status="canceled")
        back("tickets", w1, "updated_at", hours=8)
        back("tickets", w2, "updated_at", hours=6)
        back("tickets", w3, "updated_at", hours=1)
        store.add_desk_item(pw, "Which colour for the links?", "", ["blue", "green"], "blue",
                            "it reads best", "", [], False, "crew-orchestrator", [], ago(hours=10))
        # no project: a permission request of a crew station and a question for every project
        store.add_desk_item(None, "crew-reviewer was refused a command. Allow it?", "", [],
                            "crew-reviewer may comment on a pull request", "it posts its verdict",
                            "", [], False, "crew-orchestrator", [], ago(minutes=40),
                            station="crew-reviewer", command="gh pr comment 12 --body ok")
        store.add_desk_item(None, "Where do the release notes go?", "", [], "the changelog",
                            "it is there already", "", [], False, "crew-orchestrator", [],
                            ago(minutes=20))
        # an answered question never shows
        gone = store.add_desk_item(pw, "Which font?", "", [], "serif", "it reads well", "", [], False,
                                   "crew-orchestrator", [], ago(days=7))
        store.close_desk_item(gone["id"], "answered", None, now_utc())

        # what must show nothing: a closed project and a project that is not a factory one
        pd = project("project-d")
        spec(pd, days=30)
        ticket(pd, "Old work", stage="merged", status="done")
        store.add_assumption(pd, "Old question?", "old choice", "old", "", [], "d-build",
                             "builder", ago(days=30))
        store.set_handover(pd, [{"path": "/w/d", "name": "example-org/d"}], None, None, now_utc())
        async with cx_() as cx:
            r = await cx.post(f"/api/projects/{pd}/finish", json={"words": "done"})
        ck("project-d is closed", r.status_code == 200, r.text)
        pp = project("plain-project", factory=False)
        spec(pp, days=30)
        ticket(pp, "A task")
        store.add_assumption(pp, "Which?", "this one", "why", "", [], "x", "builder", ago(days=30))
        ck("plain-project is not a factory project", store.get_project(pp).get("kind") != "software_factory")

        print("== the whole list ==")
        h = await get_home()
        w = h["waiting_for_you"]
        got = [(i["kind"], (i["project"] or {}).get("name")) for i in w]
        ck("eight items, oldest first, six kinds", got == [
            ("permission", "project-x"), ("prs_to_review", "project-x"), ("ready_to_start", "project-y"),
            ("calls_to_confirm", "project-z"), ("question", "project-w"),
            ("milestone_to_check", "project-w"), ("permission", None), ("question", None)], got)
        ck("waiting_total counts them", h["waiting_total"] == 8, h["waiting_total"])
        ck("every item has the shape of the frame", all(set(i) == {
            "id", "kind", "label", "title", "detail", "meta", "project", "repo", "from", "since",
            "waited", "actions"} for i in w))
        ck("their ids are all different", len({i["id"] for i in w}) == 8, [i["id"] for i in w])
        ck("waited is in words, oldest first", [i["waited"] for i in w] == [
            "4 days", "3 days", "2 days", "1 day", "10 hours", "6 hours", "40 minutes", "20 minutes"],
           [i["waited"] for i in w])
        ck("the headline says how many and for how long",
           h["headline"].endswith("8 things wait for you, the oldest for 4 days."), h["headline"])
        k = by_kind(h)

        print("== pull requests to review ==")
        pr = k["prs_to_review"][0]
        ck("one item per project, only the passed pull requests on their current commit",
           len(k["prs_to_review"]) == 1 and pr["label"] == "2 PRs to review" and pr["title"] == "project-x"
           and pr["detail"] == "Add the footer, Add the sitemap. One holds up a milestone.", pr)
        ck("it says how long and where", pr["meta"] == "Waiting 3 days · example-org/docs-site"
           and pr["repo"] == "example-org/docs-site" and pr["id"] == f"prs_to_review:{px}", pr)
        ck("one action: review, a link", pr["actions"] == [
            {"label": "Review", "primary": True, "href": f"/reviews?project={px}"}], pr["actions"])
        ck("the changes and the older-commit verdicts are not in it",
           "search box" not in pr["detail"] and "news page" not in pr["detail"])

        print("== ready to start ==")
        rs = k["ready_to_start"][0]
        ck("a spec and tickets, not handed over", len(k["ready_to_start"]) == 1 and rs["label"] == "Ready to start"
           and rs["title"] == "project-y" and rs["detail"] ==
           "Spec and plan written, 2 tickets in GitHub. The crew has not picked it up."
           and rs["meta"] == "Spec written 2 days ago" and rs["id"] == f"ready_to_start:{py}", rs)
        ck("two actions: read the spec, copy the command", rs["actions"] == [
            {"label": "Read the spec", "primary": False, "href": f"/projects/{py}"},
            {"label": "Copy the hand-over command", "primary": True, "do": "copy",
             "text": "/tares:ship project-y"}], rs["actions"])

        print("== calls to confirm ==")
        cc = k["calls_to_confirm"][0]
        ck("the open calls of a project, the kept one left out", len(k["calls_to_confirm"]) == 1
           and cc["label"] == "2 calls to confirm" and cc["title"] == "project-z"
           and cc["detail"] == "The crew decided without you: ISO dates, and the standard one. Keep both?"
           and cc["meta"] == "Made by the builder and the reviewer" and cc["id"] == f"calls_to_confirm:{pz}", cc)
        ck("read them, or keep them all in one click", cc["actions"] == [
            {"label": "Read them", "primary": False, "href": f"/projects/{pz}#calls"},
            {"label": "Keep both", "primary": True, "do": "keep_assumptions", "project": pz,
             "refs": ["A1", "A2"]}], cc["actions"])
        ck("the desk batch of the same calls is not a second item",
           all(i["id"] != f"question:{batch['label']}" for i in w))

        print("== the desk ==")
        qs = {i["id"]: i for i in k["question"]}
        q = qs[next(i for i in qs if i.startswith("question:D") and qs[i]["project"])]
        ck("a question for a project: its text, who asked, one primary link", q["label"] == "Question for you"
           and q["title"] == "project-w" and q["detail"] == "Which colour for the links?"
           and q["meta"] == "Asked by the crew, 10 hours ago" and q["project"]["id"] == pw
           and q["actions"] == [{"label": "Answer", "primary": True,
                                 "href": f"/projects/{pw}#desk-{q['id'].split(':')[1]}"}], q)
        qn = next(i for i in qs.values() if not i["project"])
        ck("a question for every project says so, and opens on the crew page",
           qn["title"] == "Every project" and qn["detail"] == "Where do the release notes go?"
           and qn["actions"][0]["href"].startswith("/crew#desk-D"), qn)
        perms = {i["project"]["name"] if i["project"] else None: i for i in k["permission"]}
        px_perm = perms["project-x"]
        ck("a permission request names who wants what, in a monospace span", px_perm["label"] == "Permission"
           and px_perm["title"] == "project-x"
           and px_perm["detail"] == "The builder wants to use: `gh api repos/example-org/docs-site/statuses/abc`"
           and px_perm["meta"] == "Asked by the crew, 4 days ago", px_perm)
        ck("answer, and allow in one click", px_perm["actions"] == [
            {"label": "Answer", "primary": False, "href": f"/projects/{px}#desk-{px_perm['id'].split(':')[1]}"},
            {"label": "Allow", "primary": True, "do": "allow", "desk": pq["label"]}], px_perm["actions"])
        cp = perms[None]
        ck("a crew station's request is the reviewer's, for every project", cp["title"] == "Every project"
           and cp["detail"] == "The reviewer wants to use: `gh pr comment 12 --body ok`", cp)
        ck("the answered question is not there", all("Which font?" not in i["detail"] for i in w))

        print("== milestones to check ==")
        mc = k["milestone_to_check"][0]
        ck("every ticket merged (the canceled one does not count), its checks counted",
           len(k["milestone_to_check"]) == 1 and mc["label"] == "Milestone to check"
           and mc["title"] == "project-w, M1: styles"
           and mc["detail"] == "Every ticket of this milestone merged. Try its 2 checks."
           and mc["meta"] == "Merged 6 hours ago" and mc["id"] == f"milestone_to_check:{pw}:{mws['M1']}", mc)
        ck("check it, a link to the milestone", mc["actions"] == [
            {"label": "Check it", "primary": True, "href": f"/projects/{pw}#milestone-{mws['M1']}"}],
           mc["actions"])
        ck("a milestone with an open ticket, and one with only canceled tickets, show nothing",
           [i["title"] for i in k["milestone_to_check"]] == ["project-w, M1: styles"])

        print("== who it is not for ==")
        names = {(i["project"] or {}).get("name") for i in w}
        ck("a closed project and a project that is not a factory one show nothing",
           "project-d" not in names and "plain-project" not in names, names)

        print("== the project's state ==")
        by = {p["name"]: p for p in h["projects"]}
        ck("a question on the desk puts project-w first: waiting on you",
           by["project-w"]["state"] == "waiting_on_you" and by["project-x"]["state"] == "waiting_on_you", by)

        print("== the words ==")
        ck("no forbidden word or dash in any item", words.offenders_in(w) == [], words.offenders_in(w))
        ck("the whole body is clean", words.offenders_in(h) == [], words.offenders_in(h))
        ck("no scheduled kind: only the six", {i["kind"] for i in w} == {
            "prs_to_review", "ready_to_start", "calls_to_confirm", "question", "permission",
            "milestone_to_check"})

        print("== a long list of calls ==")
        for n, c in enumerate(("a very long choice " * 12, "choice three", "choice four")):
            store.add_assumption(pz, f"Q{n}?", c, "why", "", [], "z-build-m1", "builder", ago(hours=2))
        cc = by_kind(await get_home())["calls_to_confirm"][0]
        ck("five calls: three shown, the rest counted, keep all", cc["label"] == "5 calls to confirm"
           and cc["detail"].endswith("..., and 2 more. Keep them?")
           and "a very long choice a very long choice" in cc["detail"] and "..." in cc["detail"]
           and cc["actions"][1]["label"] == "Keep all" and len(cc["actions"][1]["refs"]) == 5, cc)
        ck("the long choice is cut", len(cc["detail"]) < 300, len(cc["detail"]))

        print("== what the person does ends it ==")
        async with cx_() as cx:
            for ref in ("A1", "A2", "A4", "A5", "A6"):
                r = await cx.post(f"/api/projects/{pz}/assumptions/{ref}/state", json={"state": "kept"})
                assert r.status_code == 200, r.text
            k = by_kind(await get_home())
            ck("keeping the calls ends the item; the desk batch stays open", "calls_to_confirm" not in k
               and store.get_desk_item(batch["id"])["state"] == "open")
            r = await cx.post(f"/api/desk/{pq['label']}/allow", json={
                "words": "project-x-build-m1 may read the status of a commit", "scope": "project"})
            ck("allowing the permission", r.status_code == 200, r.text)
            k = by_kind(await get_home())
            ck("allowed, the permission is gone; the crew's stays",
               [i["project"] for i in k["permission"]] == [None], k.get("permission"))
            for i in k["question"]:
                d = store.get_desk_item(i["id"].split(":")[1])
                store.close_desk_item(d["id"], "answered", None, now_utc())
            k = by_kind(await get_home())
            ck("answered questions are gone", "question" not in k)
            store.set_handover(py, [{"path": "/w/y", "name": "example-org/y"}], None, None, now_utc())
            k = by_kind(await get_home())
            ck("handed over, the project is no longer ready to start", "ready_to_start" not in k)
            store.update_ticket(t1, pr={"url": "https://github.com/example-org/docs-site/pull/14",
                                        "repo": "example-org/docs-site", "head": "ccccccc3333"})
            pr = by_kind(await get_home())["prs_to_review"][0]
            ck("a new commit makes the old pass stale: one pull request left",
               pr["label"] == "1 PR to review" and pr["detail"] == "Add the sitemap."
               and pr["meta"].startswith("Waiting 5 hours"), pr)
            store.update_ticket(t2, stage="merged", status="done")
            ck("merged, it is gone", "prs_to_review" not in by_kind(await get_home()))
            for tw in store.tickets_by_project([pw])[pw]:
                if tw["status"] == "todo":
                    store.update_ticket(tw["id"], status="canceled")
            r = await cx.post(f"/api/projects/{pw}/finish", json={"words": "done"})
            ck("closing the project ends its milestone item", r.status_code == 200
               and "milestone_to_check" not in by_kind(await get_home()), r.text)
        h = await get_home()
        ck("one permission is left, for every project", [i["kind"] for i in h["waiting_for_you"]] == ["permission"]
           and h["headline"].endswith("1 thing waits for you, for 40 minutes."), h["headline"])

        print("== the cost of a read ==")
        for i in range(50):
            uid = project(f"load-{i:02d}")
            for j in range(4):
                ticket(uid, f"Task {j}")
            if i % 3 == 0:
                spec(uid, days=1)
            if i % 2 == 0:
                store.add_assumption(uid, "Which?", "this one", "why", "", [], "b", "builder",
                                     ago(hours=3))
            if i % 5 == 0:
                in_review(uid, f"Change {i}", "aaaaaaa1111", "pass")
            if i % 4 == 0:
                store.add_desk_item(uid, f"Question {i}?", "", [], "yes", "why", "", [], False,
                                    "crew-orchestrator", [], ago(hours=4))
        n = len(fh.waiting_items(store, fx, now_utc()))
        ck("many items are waiting while it is timed", n >= 40, n)
        took = []
        for _ in range(20):
            t0 = time.perf_counter()
            fh.home(store, fx, now_utc())
            took.append((time.perf_counter() - t0) * 1000)
        p95 = sorted(took)[18]
        print(f"       home() over 55 projects, {n} waiting: p95 {p95:.0f} ms, slowest {max(took):.0f} ms")
        ck("p95 under 300 ms", p95 < 300, f"{p95:.0f} ms")
        h2 = fh.waiting_items(store, fx, now_utc())
        ck("the list is still oldest first", [i["since"] for i in h2] == sorted(i["since"] for i in h2))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
