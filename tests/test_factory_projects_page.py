"""The factory Projects list (TR-733): GET /api/factory/projects.

Every software-factory project is one row, from one read: the state of each in words, the counts of
each state, a Close that is offered only for a project whose tickets are all merged, a finished
project included as Done, the cell's other projects left out, the list sorted by what needs you,
and the tickets read in one query whatever the number of projects.

Run: .venv/bin/python tests/test_factory_projects_page.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-projects-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-projects"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory_home as fh
from tares import words
from tares.screens import factory_projects as fp

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def units():
    print("== the helpers ==")
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    ck("plain_date: day and month, the year only for another year",
       [fp.plain_date(datetime(2026, 10, 20, 9, tzinfo=timezone.utc), now),
        fp.plain_date("2026-01-05T00:00:00Z", now), fp.plain_date("2025-12-31T23:00:00Z", now),
        fp.plain_date(None, now)] == ["20 Oct", "5 Jan", "31 Dec 2025", ""],
       [fp.plain_date("2025-12-31T23:00:00Z", now)])
    ck("the order: waiting on you, building, not started, ready to close, later, done",
       sorted(fp.ORDER, key=fp.ORDER.get) == ["waiting_on_you", "building", "not_started",
                                              "ready_to_close", "later", "done"], fp.ORDER)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})
    user_text = ("name", "goal", "repos", "repo_names")

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            print("== a cell with no factory project ==")
            r = await cx.get("/api/factory/projects")
            a = r.json()
            ck("the list answers, empty", r.status_code == 200 and a["projects"] == []
               and a["counts"] == {"all": 0, "building": 0, "waiting_on_you": 0, "not_started": 0,
                                   "ready_to_close": 0, "later": 0, "done": 0} and "now" in a, r.text)

            async def make(name, goal, kind="software_factory"):
                r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": [],
                                                         "goal": goal, **({"kind": kind} if kind else {})})
                assert r.status_code < 300, r.text
                time.sleep(0.02)   # a later project is newer
                return r.json()["id"]

            async def stage(uid, n, stg):
                r = await cx.post(f"/api/projects/{uid}/tickets/T{n}/stage", json={"stage": stg})
                assert r.status_code == 200, r.text
                time.sleep(0.02)

            async def hand(uid, repo):
                r = await cx.post(f"/api/projects/{uid}/handover",
                                  json={"repos": [{"path": "/w/" + repo.split("/")[1], "name": repo}]})
                assert r.status_code == 200, r.text

            def tickets(uid, n):
                for i in range(n):
                    store.create_ticket(uid, "test", f"Task {i + 1}")

            # oldest first, so the newest activity is the project made last
            pnot = await make("project-n", "Not a factory project", kind=None)
            tickets(pnot, 2)
            pdone_b = await make("project-e", "Closed by hand, never handed to the crew")
            tickets(pdone_b, 1)
            await stage(pdone_b, 1, "merged")
            await cx.post(f"/api/projects/{pdone_b}/finish", json={"words": "done"})
            pdone_a = await make("project-d", "Old work, finished")
            tickets(pdone_a, 2)
            await hand(pdone_a, "example-org/service-a")
            await stage(pdone_a, 1, "merged")
            await stage(pdone_a, 2, "merged")
            r = await cx.post(f"/api/projects/{pdone_a}/finish", json={"words": "done"})
            assert r.status_code == 200, r.text
            pnew1 = await make("project-y", "Move the blog")
            tickets(pnew1, 3)
            pnew2 = await make("project-v", "Start something else")
            tickets(pnew2, 1)
            pbuild1 = await make("project-b1", "Ship the docs site")
            tickets(pbuild1, 4)
            await hand(pbuild1, "example-org/docs-site")
            await stage(pbuild1, 1, "merged")
            await stage(pbuild1, 2, "doing")
            pbuild2 = await make("project-b2", "Tidy the styles")
            tickets(pbuild2, 2)
            await hand(pbuild2, "example-org/service-a")
            await stage(pbuild2, 1, "merged")
            await stage(pbuild2, 2, "doing")
            pwait = await make("project-w", "Wait for an answer")
            tickets(pwait, 2)
            await hand(pwait, "example-org/service-b")
            await stage(pwait, 1, "merged")
            r = await cx.post("/api/desk", json={"question": "Which gateway?", "recommendation": "a",
                                                 "why": "w", "project": "project-w"},
                              headers={"X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})
            ck("a question waits on project-w", r.status_code < 300, r.text)
            pready = await make("project-z", "Everything merged")
            tickets(pready, 2)
            await hand(pready, "example-org/service-a")
            await stage(pready, 1, "merged")
            await stage(pready, 2, "merged")

            print("== one read ==")
            calls = {"tickets_by_project": 0, "list_tickets": 0}
            real_by, real_list = store.tickets_by_project, store.list_tickets

            def counted_by(*a, **k):
                calls["tickets_by_project"] += 1
                return real_by(*a, **k)

            def counted_list(*a, **k):
                calls["list_tickets"] += 1
                return real_list(*a, **k)

            async def counted_get():
                calls.update(tickets_by_project=0, list_tickets=0)
                store.tickets_by_project, store.list_tickets = counted_by, counted_list
                try:
                    return await cx.get("/api/factory/projects")
                finally:
                    store.tickets_by_project, store.list_tickets = real_by, real_list

            saved = list(fh.WAITING_BUILDERS)
            fh.WAITING_BUILDERS.clear()   # the waiting readers have their own reads
            r = await counted_get()
            fh.WAITING_BUILDERS[:] = saved
            ck("the page's own read of tickets is one query for all projects, none one by one",
               r.status_code == 200 and calls == {"tickets_by_project": 1, "list_tickets": 0}, calls)
            r = await counted_get()
            a = r.json()
            ck("one call answers", r.status_code == 200, r.text)
            before = dict(calls)
            for i in range(20):
                uid = f"uc_more{i:02d}"
                store.create_project(uid, "custom", f"more-{i:02d}", {}, goal="Another")
                store.set_project_kind(uid, "software_factory")
                tickets(uid, 3)
            r = await counted_get()
            ck("20 more projects cost the same number of queries", r.status_code == 200
               and calls == before and len(r.json()["projects"]) == 28, (before, calls))
            for i in range(20):
                store.update_project(f"uc_more{i:02d}", status="done")
                store.set_project_kind(f"uc_more{i:02d}", None)
            r = await cx.get("/api/factory/projects")
            a = r.json()
            by = {p["name"]: p for p in a["projects"]}
            names = [p["name"] for p in a["projects"]]

            print("== which projects ==")
            ck("a project of another kind is left out, the finished ones are in",
               "project-n" not in by and {"project-d", "project-e"} <= set(by) and len(by) == 8, names)
            ck("the cell's Default project is left out too",
               all(p["name"] != "Default" for p in a["projects"]), names)
            ck("every key of a row", all(set(p) == {"id", "name", "icon", "goal", "state", "state_label", "milestone",
               "tickets", "repos", "builders", "last_activity_at", "last_activity", "meta", "can_close",
               "repo_names"} for p in a["projects"]), sorted(a["projects"][0]))
            ck("the answer's keys", set(a) == {"projects", "counts", "now"}, sorted(a))

            print("== the state of each ==")
            states = {n: p["state"] for n, p in by.items()}
            ck("each project's state", states == {
                "project-w": "waiting_on_you", "project-b1": "building", "project-b2": "building",
                "project-y": "not_started", "project-v": "not_started", "project-z": "ready_to_close",
                "project-d": "done", "project-e": "done"}, states)
            ck("the state in words", {n: p["state_label"] for n, p in by.items()} == {
                "project-w": "Waiting on you", "project-b1": "Building", "project-b2": "Building",
                "project-y": "Not started", "project-v": "Not started", "project-z": "Ready to close",
                "project-d": "Done", "project-e": "Done"}, {n: p["state_label"] for n, p in by.items()})
            ck("the counts are by state and add up", a["counts"] == {
                "all": 8, "building": 2, "waiting_on_you": 1, "not_started": 2, "ready_to_close": 1,
                "later": 0, "done": 2}, a["counts"])
            ck("tickets merged and total", by["project-b1"]["tickets"] == {"merged": 1, "blocked": 0, "total": 4}
               and by["project-z"]["tickets"] == {"merged": 2, "blocked": 0, "total": 2}, by["project-b1"])

            print("== close ==")
            ck("only the fully merged project can be closed",
               [n for n, p in by.items() if p["can_close"]] == ["project-z"],
               [n for n, p in by.items() if p["can_close"]])
            ck("a finished project cannot be closed again",
               by["project-d"]["can_close"] is False and by["project-e"]["can_close"] is False)

            print("== a row in words ==")
            d, e = by["project-d"], by["project-e"]
            today = datetime.now(timezone.utc)
            when = fp.plain_date(today, today)
            ck("a finished project says when, from the day it was finished",
               d["milestone"] == "Finished " + when and e["milestone"] == "Finished " + when
               and d["meta"] == "" and d["repo_names"] == ["example-org/service-a"], (d, e))
            b1 = by["project-b1"]
            ck("a building row: repo kept apart, no builders working, how long ago",
               b1["repo_names"] == ["example-org/docs-site"] and b1["repos"] == ["docs-site"]
               and b1["meta"] == "No builders working · Active just now"
               and b1["goal"] == "Ship the docs site", b1)
            ck("a not started row says it was written, with no builders yet",
               by["project-y"]["meta"] == "No builders yet · Written just now", by["project-y"]["meta"])
            z = by["project-z"]
            ck("a ready row says merged, with its milestone line",
               z["meta"] == "No builders working · Merged just now" and z["milestone"] == "All milestones done"
               and z["goal"] == "Everything merged", z)
            ck("a project with something waiting is ready for you, not building",
               by["project-w"]["state"] == "waiting_on_you"
               and by["project-w"]["meta"].startswith("No builders working · Active"), by["project-w"]["meta"])
            clean = words.offenders_in(a, user_text)
            ck("no forbidden word or dash outside the person's own names", clean == [], clean)

            print("== the order ==")
            ck("waiting on you, building, not started, ready to close, then done",
               [p["state"] for p in a["projects"]] == ["waiting_on_you", "building", "building", "not_started",
                                                      "not_started", "ready_to_close", "done", "done"],
               [p["state"] for p in a["projects"]])
            ck("newest activity first within a state", names == [
                "project-w", "project-b2", "project-b1", "project-v", "project-y", "project-z",
                "project-d", "project-e"], names)

            print("== closing moves it to done ==")
            r = await cx.post(f"/api/projects/{pready}/finish", json={"words": "Closed from the Projects list"})
            ck("the close goes through the route that exists", r.status_code == 200, r.text)
            a2 = (await cx.get("/api/factory/projects")).json()
            by2 = {p["name"]: p for p in a2["projects"]}
            ck("project-z is done now, last in its state's place",
               by2["project-z"]["state"] == "done" and by2["project-z"]["can_close"] is False
               and by2["project-z"]["milestone"] == "Finished " + when, by2["project-z"])
            ck("the counts follow", a2["counts"]["ready_to_close"] == 0 and a2["counts"]["done"] == 3
               and a2["counts"]["all"] == 8, a2["counts"])
            r = await cx.post(f"/api/projects/{pready}/finish", json={"words": "again"})
            ck("closing again is a 409 with a sentence", r.status_code == 409 and "already finished" in r.text,
               (r.status_code, r.text))
            r = await cx.post(f"/api/projects/{pbuild1}/finish", json={"words": "too soon"})
            ck("closing a project with open tickets is a 409 with a sentence",
               r.status_code == 409 and "open tickets" in r.text, (r.status_code, r.text))

            print("== the token ==")
            ck("the list is behind the token", (await mk(None).get("/api/factory/projects")).status_code == 401)


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
