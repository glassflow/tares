"""What waits for you on the factory home (TR-769): the `waiting_for_you` of GET /api/home.

Six kinds (and the rule proposals of TR-1275), built from what the cell stores, oldest first: pull requests the reviewer passed,
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
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-waiting"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx
from fastapi import FastAPI
from sqlalchemy import text

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
    fx = factory_api.register(FastAPI(), SimpleNamespace(store=store, cc_source=lambda: "claude_code", hidden_ids=lambda: set()))
    cx_ = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                    headers={"Authorization": f"Bearer {TOKEN}"})

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    def back(table, key, col, **kw):
        sql = f"UPDATE {table} SET {col} = :at WHERE id = :key"
        if table == "tickets":                   # in the session store (TR-1612)
            from sqlalchemy import update
            from tares.session_store.tables import tickets as tickets_t
            with store.sessions_db.begin() as conn:
                conn.execute(update(tickets_t).where(tickets_t.c.id == key).values({col: ago(**kw)}))
            return
        with store.sessions_db.begin() as conn:  # every other table is in the session store too
            conn.execute(text(sql), {"at": ago(**kw), "key": key})

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
        ck("the builders are registered once", [f.__name__ for f in fh.WAITING_BUILDERS] == [
            "prs_to_review", "ready_to_start", "calls_to_confirm", "desk_items", "milestones_to_check",
            "projects_to_close", "rules_to_decide", "stations_waiting"],
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
        # TR-1021: what holds the crew up first (the refused commands, the PRs waiting for your
        # merge), then the rest, oldest first in each
        ck("nine items, what holds the crew up first, six kinds", got == [
            ("permission", "project-x"), ("prs_to_review", "project-x"), ("prs_to_review", "project-x"),
            ("permission", None),
            ("ready_to_start", "project-y"), ("calls_to_confirm", "project-z"), ("question", "project-w"),
            ("milestone_to_check", "project-w"), ("question", None)], got)
        ck("waiting_total counts them", h["waiting_total"] == 9, h["waiting_total"])
        ck("every item has the shape of the frame", all(set(i) == {
            "id", "kind", "label", "title", "detail", "meta", "project", "repo", "from", "since",
            "waited", "actions", "tickets", "blocking", "older", "question", "lines", "ask", "holds_up",
            "checks", "for_people", "for_admin", "no_lead", "desk_labels"} for i in w))
        ck("their ids are all different", len({i["id"] for i in w}) == 9, [i["id"] for i in w])
        ck("waited is in words, oldest first within each group", [i["waited"] for i in w] == [
            "4 days", "3 days", "5 hours", "40 minutes", "2 days", "1 day", "10 hours", "6 hours", "20 minutes"],
           [i["waited"] for i in w])
        ck("the headline says how many and for how long",
           h["headline"].endswith("9 things wait for you, the oldest for 4 days."), h["headline"])
        k = by_kind(h)

        print("== pull requests to review ==")
        pr, pr2 = k["prs_to_review"]
        ck("one item per pull request, only the passed ones on their current commit (TR-1450)",
           len(k["prs_to_review"]) == 2 and pr["label"] == "PR to review"
           and pr["title"] == "project-x, Add the footer"
           and pr["detail"] == "T1, Add the footer. It holds up a milestone."
           and pr2["detail"] == "T2, Add the sitemap.", k["prs_to_review"])
        ck("it says how long and where", pr["meta"] == "Waiting 3 days · example-org/docs-site"
           and pr["repo"] == "example-org/docs-site" and pr["id"] == f"prs_to_review:{px}:T1"
           and pr["tickets"] == ["T1"], pr)
        ck("checks never read say so (TR-1449)", pr["checks"] == {
            "ci": None, "behind_by": None, "line": "Checks not read yet."}, pr["checks"])
        ck("no other item carries checks", all(i["checks"] is None for i in w if i["kind"] != "prs_to_review"))
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
        ck("three actions: read the spec, keep it for later, copy the command", rs["actions"] == [
            {"label": "Read the spec", "primary": False, "href": f"/projects/{py}"},
            {"label": "Later", "primary": False, "do": "later", "project": py},
            {"label": "Copy the hand-over command", "primary": True, "do": "copy",
             "text": "/tares:ship project-y"}], rs["actions"])

        print("== crew answers to confirm ==")
        cc = k["calls_to_confirm"][0]
        ck("the open calls of a project, the kept one left out", len(k["calls_to_confirm"]) == 1
           and cc["label"] == "2 crew answers to confirm" and cc["title"] == "project-z"
           and cc["detail"] == "The crew decided without you:" and cc["lines"] == ["ISO dates", "the standard one"]
           and cc["meta"] == "Made by the builder and the reviewer" and cc["id"] == f"calls_to_confirm:{pz}", cc)
        ck("read them, or keep them all in one click", cc["actions"] == [
            {"label": "Read them", "primary": False, "href": f"/projects/{pz}/calls"},
            {"label": "Keep both", "primary": True, "do": "keep_assumptions", "project": pz,
             "refs": ["A1", "A2"]}], cc["actions"])
        ck("the desk batch of the same calls is not a second item",
           all(i["id"] != f"question:{batch['label']}" for i in w))

        print("== the desk ==")
        qb = fw.desk_question({"label": "D9", "options": ["a", "b"], "recommendation": "a",
                                   "why": "it is cheaper", "blocks": "T1"})
        ck("a question with options sends its options, the crew's answer, why, and what it holds up",
           qb == {"desk": "D9", "options": ["a", "b"], "recommendation": "a", "why": "it is cheaper", "blocks": "T1"}, qb)
        ck("a question without a why sends none", fw.desk_question({"label": "D9", "options": ["a"]})["why"] is None)
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
            {"label": "Allow", "primary": True, "do": "allow", "desk": pq["label"], "station": "project-x-build-m1",
             "may_limit": True}],
           px_perm["actions"])
        cp = perms[None]
        ck("a crew station's request is the reviewer's, for every project", cp["title"] == "Every project"
           and cp["detail"] == "The reviewer wants to use: `gh pr comment 12 --body ok`", cp)
        ck("a crew station's grant cannot be limited to one project, a builder's can",
           cp["actions"][-1].get("may_limit") is False and px_perm["actions"][-1]["may_limit"] is True)
        ck("the answered question is not there", all("Which font?" not in i["detail"] for i in w))

        print("== milestones to check ==")
        mc = k["milestone_to_check"][0]
        ck("every ticket merged (the canceled one does not count), its unrecorded crew checks counted (TR-1017)",
           len(k["milestone_to_check"]) == 1 and mc["label"] == "Milestone to check"
           and mc["title"] == "project-w, M1: styles"
           and mc["detail"] == "Every ticket of this milestone merged, but the crew has not recorded its 2 checks. Accept it as it is, or ask the crew."
           and mc["meta"] == "Merged 6 hours ago" and mc["id"] == f"milestone_to_check:{pw}:{mws['M1']}", mc)
        ck("accept in one tap (TR-1450), or check it on the milestone", mc["actions"] == [
            {"label": "Accept", "primary": True, "do": "accept_milestone", "project": pw,
             "milestone": mws["M1"]},
            {"label": "Check it", "primary": False, "href": f"/projects/{pw}#milestone-{mws['M1']}"}],
           mc["actions"])
        ck("a milestone with an open ticket, and one with only canceled tickets, show nothing",
           [i["title"] for i in k["milestone_to_check"]] == ["project-w, M1: styles"])

        print("== the ask: the decision, verb first (TR-1447) ==")
        asks = {i["id"]: i["ask"] for i in w}
        ck("every item has an ask", all(isinstance(x, str) and x for x in asks.values()), asks)
        ck("a PR item merges its pull request", pr["ask"] == "Merge Add the footer", pr["ask"])
        ck("one PR is merged by its number, else by its ticket's title",
           fw.ask_prs([{"number": 336, "title": "Add the footer"}], "x") == "Merge PR 336"
           and fw.ask_prs([{"number": None, "title": "Add the footer"}], "x") == "Merge Add the footer")
        ck("a project ready to start is handed over", rs["ask"] == "Hand project-y to the crew", rs["ask"])
        ck("calls are kept, by count", cc["ask"] == "Keep 2 calls on project-z"
           and fw.ask_calls(1, "atlas") == "Keep the crew's call on atlas"
           and fw.ask_calls(0, "atlas") == "Keep the crew's calls on atlas", cc["ask"])
        ck("a question's ask is the question", q["ask"] == "Which colour for the links?", q["ask"])
        ck("a permission's ask is what the crew wants to run",
           px_perm["ask"] == "Allow `gh api repos/example-org/docs-site/statuses/abc`", px_perm["ask"])
        ck("a milestone is checked by its number", mc["ask"] == "Check M1 of project-w", mc["ask"])

        print("== what it holds up (TR-1448) ==")
        ck("a PR that closes its milestone holds the milestone up", pr["holds_up"] == "M1: pages", pr["holds_up"])
        ck("a project not started holds its tickets", rs["holds_up"] == "2 tickets", rs["holds_up"])
        ck("a milestone holds the next one", mc["holds_up"] == "M2: search", mc["holds_up"])
        # M2 accepted out of order: M1 holds up the next one still waiting, M3
        store.accept_milestone(pw, mws["M2"], "Vladimir", None, now_utc())
        mc2 = next(i for i in (await get_home())["waiting_for_you"] if i["kind"] == "milestone_to_check")
        ck("a milestone accepted already is not the one held up", mc2["holds_up"] == "M3: dropped", mc2["holds_up"])
        with store.sessions_db.begin() as conn:
            conn.execute(text("DELETE FROM milestone_acceptances WHERE milestone = :m"),
                         {"m": mws["M2"]})
        ck("a refused command holds its station's work",
           px_perm["holds_up"] == "the builder's work", px_perm["holds_up"])
        ck("calls and a question that names nothing hold nothing",
           cc["holds_up"] is None and q["holds_up"] is None, (cc["holds_up"], q["holds_up"]))
        ck("a desk item says what it blocks, else the tickets it names",
           fw.holds_desk({"blocks": "the login flow", "tickets": ["T4"]}) == "the login flow"
           and fw.holds_desk({"blocks": "", "tickets": ["T4", "T5"]}) == "T4 and T5"
           and fw.holds_desk({"blocks": None, "tickets": [], "command": None}) is None)
        ck("many tickets are counted", fw.holds_tickets(["T4", "T5", "T6"]) == "T4 and 2 more tickets")
        dep = [{"id": "a", "identifier": "T1", "status": "in_progress", "depends_on": []},
               {"id": "b", "identifier": "T2", "status": "todo", "depends_on": ["a"]},
               {"id": "c", "identifier": "T3", "status": "done", "depends_on": ["a"]}]
        ck("a PR holds the open tickets that depend on its own, before its milestone",
           fw.holds_prs([{"ticket": "T1", "blocks_milestone": True, "milestone": "m"}], dep,
                        [{"id": "m", "name": "M1: pages"}]) == "T2")

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
        ck("five calls: three shown, one line each, the rest counted, keep all", cc["label"] == "5 crew answers to confirm"
           and len(cc["lines"]) == 4 and cc["lines"][-1] == "2 more" and cc["lines"][2].endswith("choice a…")
           and cc["actions"][1]["label"] == "Keep all" and len(cc["actions"][1]["refs"]) == 5, cc)
        ck("the long choice is cut", len(cc["lines"][2]) <= 140, len(cc["lines"][2]))

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
               len(by_kind(await get_home())["prs_to_review"]) == 1 and pr["detail"] == "T2, Add the sitemap."
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

        print("== projects to close ==")
        pv = project("project-v")
        v1 = ticket(pv, "Build it", stage="merged", status="done")
        v2 = ticket(pv, "Test it", stage="merged", status="done")
        v3 = ticket(pv, "Dropped idea", status="canceled")
        back("tickets", v1, "updated_at", hours=5)
        back("tickets", v2, "updated_at", hours=27)
        back("tickets", v3, "updated_at", hours=49)
        pu = project("project-u")
        ticket(pu, "Build it", stage="merged", status="done")
        ticket(pu, "Still open")
        pt = project("project-t")
        ticket(pt, "Dropped idea", status="canceled")
        pq1 = project("project-q")
        ticket(pq1, "Only one", stage="merged", status="done")
        k = by_kind(await get_home())
        sv = {i["project"]["name"]: i for i in k.get("signoff", [])}
        ck("only a project with every ticket finished and one delivered offers Close",
           sorted(sv) == ["project-q", "project-v"], sorted(sv))
        so = sv["project-v"]
        ck("the row: label, title, count without the canceled ticket, wait from the last ticket to finish",
           so["label"] == "Close the project?" and so["title"] == "project-v" and so["id"] == f"signoff:{pv}"
           and so["detail"] == "All 2 tickets are merged. Close the project when you have checked the result."
           and so["meta"] == "Ready for 5 hours" and so["waited"] == "5 hours", so)
        ck("a project ready to close sorts after what holds the crew up, however long it has waited",
           [i["kind"] for i in (await get_home())["waiting_for_you"]][-2:].count("signoff") == 2
           and [i["kind"] for i in (await get_home())["waiting_for_you"]][0] == "permission",
           [i["kind"] for i in (await get_home())["waiting_for_you"]])
        ck("the headline age is the oldest thing that holds the crew up, not a sign-off",
           (await get_home())["headline"].endswith("3 things wait for you, the oldest for 40 minutes."),
           (await get_home())["headline"])
        ck("a headline with only sign-offs gives no age", fh._headline(
            {"running": True, "desk_open": 0}, [{}], 0, 0, [so, sv["project-q"]], None).endswith(
            "2 things wait for you.") and fh._headline(
            {"running": True, "desk_open": 0}, [{}], 0, 0, [so], None).endswith("1 thing waits for you."))
        ps = project("project-s")
        s1 = ticket(ps, "Built", stage="merged", status="done")
        s2 = ticket(ps, "Dropped later", status="canceled")
        back("tickets", s1, "updated_at", hours=30)
        back("tickets", s2, "updated_at", hours=2)
        ss = {i["project"]["name"]: i for i in by_kind(await get_home()).get("signoff", [])}["project-s"]
        ck("the wait counts from the last ticket to finish, a canceled one too",
           ss["waited"] == "2 hours" and ss["meta"] == "Ready for 2 hours", ss)
        ck("one ticket reads in the singular", sv["project-q"]["detail"]
           == "Its ticket is merged. Close the project when you have checked the result.", sv["project-q"])
        ck("open the project, or close it", so["actions"] == [
            {"label": "Open project", "primary": False, "href": f"/projects/{pv}"},
            {"label": "Close project", "primary": True, "do": "close"}], so["actions"])
        ck("a project ready to close is closed (TR-1447)", so["ask"] == "Close project-v", so["ask"])
        ck("the project row says so too", {p["name"]: p for p in (await get_home())["projects"]}
           ["project-v"]["can_close"] is True)
        async with cx_() as cx:
            r = await cx.post(f"/api/projects/{pv}/finish", json={"words": "Closed from the home page"})
        ck("closing it ends the item", r.status_code == 200
           and "project-v" not in {i["project"]["name"] for i in by_kind(await get_home()).get("signoff", [])},
           r.text)

        store.add_desk_item(pu, "crew-reviewer was refused a command. Allow it?", "", [],
                            "crew-reviewer may comment on a pull request", "it posts its verdict",
                            "", [], False, "crew-orchestrator", [], ago(minutes=5),
                            station="crew-reviewer", command="gh pr comment 12 --body ok")
        cu = [i for i in by_kind(await get_home())["permission"] if (i["project"] or {}).get("name") == "project-u"]
        ck("a crew station's request inside a project still cannot be limited to it",
           len(cu) == 1 and cu[0]["actions"][-1]["may_limit"] is False, cu)

        print("== rule proposals (TR-1275) ==")
        pr_ = project("project-r")
        rp1 = store.upsert_rule_proposal("example-org/docs-site", "missing_test", pr_, "repo",
                                         "Every change to the parser comes with a test.", ["t1"], ago(hours=3))
        rp2 = store.upsert_rule_proposal("example-org/api", "no_log", None, "all",
                                         "A fallback logs once.", [], ago(hours=1))
        rp3 = store.upsert_rule_proposal("example-org/api", "decided", None, "all", "Already decided.", [], ago(hours=2))
        store.decide_rule_proposal(rp3["id"], "accepted", None, now_utc())
        rules = {i["id"]: i for i in by_kind(await get_home()).get("rule", [])}
        ck("each open proposal is a rule item, a decided one is not",
           sorted(rules) == sorted([f"rule:{rp1['id']}", f"rule:{rp2['id']}"]), sorted(rules))
        r1 = rules.get(f"rule:{rp1['id']}", {})
        ck("the row: label, the project as title, the rule as detail, who proposed it and when",
           r1.get("label") == "Rule proposed" and r1.get("title") == "project-r"
           and r1.get("detail") == "Every change to the parser comes with a test."
           and r1.get("meta") == "Proposed by the crew, 3 hours ago" and r1.get("project", {}).get("id") == pr_
           and r1.get("repo") == "example-org/docs-site" and r1.get("blocking") is False, r1)
        ck("a proposal for every project says so and has no project",
           rules.get(f"rule:{rp2['id']}", {}).get("title") == "Every project"
           and rules.get(f"rule:{rp2['id']}", {}).get("project") is None, rules.get(f"rule:{rp2['id']}"))
        ck("its one action is a primary link to the rules on Crew", all(i["actions"] == [
            {"label": "Accept or reject", "primary": True, "href": "/crew#rules"}] for i in rules.values()),
           [i["actions"] for i in rules.values()])
        ck("a proposal about a project the person may not see is left out",
           f"rule:{rp1['id']}" not in {i["id"] for i in fh.waiting_items(store, fx, now_utc(), {pr_})}
           and f"rule:{rp2['id']}" in {i["id"] for i in fh.waiting_items(store, fx, now_utc(), {pr_})})
        ck("rule is a kind the home knows", "rule" in fh.KINDS)
        ck("the rule rows pass the words check", words.offenders_in(list(rules.values())) == [],
           words.offenders_in(list(rules.values())))
        store.decide_rule_proposal(rp1["id"], "rejected", None, now_utc())
        store.decide_rule_proposal(rp2["id"], "accepted", None, now_utc())
        ck("deciding them ends the items", "rule" not in by_kind(await get_home()))

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
        # 500 ms, not 300 (TR-1467): a laptop takes 100 to 250 ms, a GitHub runner anywhere from 180 to
        # 315 ms for the same code. This catches a slowdown of a few ms per project or more (5 ms per
        # project reaches 500 ms on a laptop), not one more query per project (about 20 ms over these
        # 55). What catches that is a count of the statements Home runs, not a stopwatch (TR-1501,
        # ee/tests/test_factory_query_budget.py, on its way to main with the factory-perf work).
        ck("p95 under 500 ms", p95 < 500, f"{p95:.0f} ms")
        h2 = fh.waiting_items(store, fx, now_utc())
        rest = [i for i in h2 if i["kind"] != "signoff"]
        key = lambda i: (not i["blocking"], i["older"], i["kind"] == "signoff", i["since"])
        ck("a time written with another offset is given in UTC",
           fh._iso("2026-11-25T21:28:44+01:00") == "2026-11-25T20:28:44+00:00", fh._iso("2026-11-25T21:28:44+01:00"))
        ck("every since is in UTC, wherever it was read from",
           all(i["since"].endswith("+00:00") for i in h2), [i["since"] for i in h2 if not i["since"].endswith("+00:00")][:3])
        ck("the list is still in order: what holds the crew up, the rest, ready-to-close rows last",
           [key(i) for i in h2] == sorted(key(i) for i in h2), [(i["kind"], i["blocking"]) for i in h2])


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
