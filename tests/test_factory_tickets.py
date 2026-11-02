"""The factory Tickets list (TR-806): GET /api/factory/tickets.

One read answers the page: a page of tickets across every factory project, the count of each state
for the filters chosen, the projects and milestones to choose from, and how many canceled tickets
are hidden. The filters (project, milestone, state, text), the three sorts and the paging are the
server's. Nothing in the answer may name a station, a session or a folder of the crew's machine,
and every shown word passes the edition words list.

Run: .venv/bin/python tests/test_factory_tickets.py
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-tickets-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-tickets"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import tares
from tares import factory as factory_mod
from tares import words
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def strings(v, path="$", key=""):
    """Every (path, key, string) in a JSON value, `key` being the name of the field holding it."""
    if isinstance(v, str):
        yield path, key, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from strings(x, f"{path}.{k}", k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from strings(x, f"{path}[{i}]", key)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    cx_ = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    def project(name, factory=True):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        if factory:
            store.set_project_kind(uid, "software_factory")
        return uid

    def ticket(uid, title, **fields):
        ms, deps = fields.pop("milestone", None), fields.pop("depends_on", None)
        tid = store.create_ticket(uid, "tares", title, milestone=ms, depends_on=deps)
        if fields:
            store.update_ticket(tid, **fields)
        return tid

    PATH = "/Users/alex/code/project-x"
    REPO = "example-org/docs-site"
    STATIONS = ["project-x-build-m1", "project-x-build-m2", "crew-orchestrator", "crew-reviewer"]
    SESSIONS = ["sess-b1", "sess-b2", "sess-lead", "sess-rev"]

    async with app.router.lifespan_context(app):
        # ── project-x: a ticket in every state across two milestones ─────────────
        px = project("project-x")
        store.set_handover(px, [{"path": PATH, "name": REPO}], None, None, now_utc())
        for sid, station, role in zip(SESSIONS, STATIONS, ("builder", "builder", "orchestrator", "reviewer")):
            store.set_session_station(sid, station, role, "" if role == "orchestrator" else "crew-orchestrator",
                                      px if role == "builder" else None, now_utc())
        m1 = store.create_milestone(px, "M1: pages", "The pages are live.", [])
        m2 = store.create_milestone(px, "M2: search", "Search works.", [])
        pr = lambda n, **kw: {"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO, "number": n,
                              "head": "aaaaaaa1111", **kw}
        t1 = ticket(px, "Add the header", milestone=m1, stage="merged", status="done",
                    pr=pr(11, state="closed", merged=True))
        t2 = ticket(px, "Add the footer", milestone=m1, stage="review", status="in_progress",
                    holder=STATIONS[3], pr=pr(12, state="open", ci="success"))
        t3 = ticket(px, "Add the sitemap", milestone=m1, stage="changes", status="in_progress",
                    holder=STATIONS[0], pr=pr(13, state="open", ci="failure"))
        t4 = ticket(px, "Add the search box", milestone=m2, stage="doing", status="in_progress",
                    holder=STATIONS[1])
        t5 = ticket(px, "Add the results page", milestone=m2, depends_on=[t4])
        t6 = ticket(px, f"Fix links under {PATH}/docs", milestone=m2)
        t7 = ticket(px, "Drop the old theme", milestone=m2, status="canceled")
        t8 = ticket(px, "Write the release notes", stage="blocked", status="in_progress", holder=STATIONS[2])
        t9 = ticket(px, "Tidy the readme")
        store.add_review(px, t3, None, "aaaaaaa1111", "changes", "ran the tests", None, STATIONS[3],
                         [], {}, factory_mod.finding_kind, now_utc())
        # the last change of each ticket, so the sort is known
        for tid, h in ((t1, 300), (t2, 1), (t3, 2), (t4, 3), (t5, 4), (t6, 5), (t7, 50), (t8, 6), (t9, 7)):
            with store._swrite:
                store.sc.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", [ago(hours=h), tid])

        # ── project-y: two tickets; project-z: only a canceled one; a draft; a platform project
        py = project("project-y")
        ty1 = ticket(py, "Export the posts")
        ty2 = ticket(py, "Import the posts", depends_on=[ty1])
        for tid, h in ((ty1, 20), (ty2, 21)):
            with store._swrite:
                store.sc.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", [ago(hours=h), tid])
        pz = project("project-z")
        ticket(pz, "Old idea", status="canceled")
        pd = project("project-d")
        ticket(pd, "In a draft")
        store.update_project(pd, status="draft")
        pw = project("project-w", factory=False)
        ticket(pw, "In a project of another kind")

        async with cx_() as cx:
            async def get(path):
                return await cx.get("/api/factory/tickets" + path)

            def refs(r):
                return [t["ref"] for t in r.json()["tickets"]]

            by_title = {t["title"]: t for t in (await get("?limit=200")).json()["tickets"]}

            print("== the whole list ==")
            r = await get("")
            a = r.json()
            ck("200 and the shape", r.status_code == 200 and all(
                k in a for k in ("tickets", "total", "offset", "limit", "more", "counts", "projects",
                                 "milestones", "hidden_canceled", "now")), list(a))
            ck("canceled tickets are hidden by default",
               all(t["state"] != "canceled" for t in a["tickets"]) and "Drop the old theme" not in by_title
               and "Old idea" not in by_title)
            ck("the total counts the tickets shown (8 in project-x, 2 in project-y)", a["total"] == 10, a["total"])
            ck("a draft and a project of another kind have no tickets here",
               "In a draft" not in by_title and "In a project of another kind" not in by_title)
            ck("two canceled tickets are hidden", a["hidden_canceled"] == 2, a["hidden_canceled"])
            ck("newest change first", [t["title"] for t in a["tickets"]][:3]
               == ["Add the footer", "Add the sitemap", "Add the search box"],
               [t["title"] for t in a["tickets"]][:3])
            ck("limit is 25 and offset 0 by default", (a["limit"], a["offset"], a["more"]) == (25, 0, False))

            print("== a ticket in words ==")
            foot = by_title["Add the footer"]
            ck("a ticket carries its ref, title, project, milestone, who, state, pull request, review, change, link",
               foot["state"] == "in review" and foot["state_key"] == "in_review"
               and foot["project"] == {"id": px, "name": "project-x"}
               and foot["milestone"]["number"] == "M1" and foot["milestone"]["name"] == "M1: pages"
               and foot["who"] == "Reviewer"
               and foot["pr"] == {"number": 12, "url": f"https://github.com/{REPO}/pull/12",
                                  "state_text": "open, CI green"}
               and foot["changed"] == "1 hour ago" and foot["changed_at"]
               and foot["href"] == f"/projects/{px}/tickets/{foot['ref']}", foot)
            site = by_title["Add the sitemap"]
            ck("a builder is the builder for its ticket's milestone", site["who"] == "Builder for M1", site["who"])
            ck("a review in words", site["review"] == "round 1: changes asked"
               and foot["review"] is None, (site["review"], foot["review"]))
            ck("changes asked is its own state", site["state"] == "changes asked", site["state"])
            ck("a ticket in the work, and its builder", by_title["Add the search box"]["state"] == "in progress"
               and by_title["Add the search box"]["who"] == "Builder for M2")
            res = by_title["Add the results page"]
            ck("a ticket that waits on another is blocked and says by what",
               res["state"] == "blocked" and res["blocked_by"] == [factory_mod.label(store.get_ticket(px, t4))],
               res)
            ck("a ticket nobody holds says so", by_title["Tidy the readme"]["who"] == "Not picked up yet"
               and by_title["Tidy the readme"]["state"] == "to do")
            ck("a merged ticket has no one working on it",
               by_title["Add the header"]["state"] == "merged" and by_title["Add the header"]["who"] is None)
            ck("the lead is the lead", by_title["Write the release notes"]["who"] == "Lead")
            ck("a ticket without a pull request has none", by_title["Tidy the readme"]["pr"] is None)
            ck("a folder of the crew's machine shows the repo",
               PATH not in json.dumps(a) and f"Fix links under {REPO}/docs" in by_title, list(by_title))

            print("== counts ==")
            c = a["counts"]
            ck("counts by state, whatever the state asked for",
               c == {"total": 10, "moving": 3, "to_do": 3, "in_progress": 1, "in_review": 1,
                     "changes_asked": 1, "blocked": 3, "merged": 1, "canceled": 2}, c)
            c2 = (await get("?state=merged")).json()["counts"]
            ck("the state does not change the counts", c2 == c, c2)
            c3 = (await get(f"?project={py}")).json()["counts"]
            ck("a project narrows the counts", c3["total"] == 2 and c3["to_do"] == 1 and c3["blocked"] == 1
               and c3["canceled"] == 0, c3)
            c4 = (await get(f"?project={px}&milestone={m2}")).json()["counts"]
            ck("a milestone narrows them again (canceled and blocked count)",
               c4["total"] == 3 and c4["in_progress"] == 1 and c4["blocked"] == 1 and c4["canceled"] == 1, c4)

            print("== filters ==")
            ck("a project", set(t["project"]["name"] for t in (await get(f"?project={py}")).json()["tickets"])
               == {"project-y"})
            ck("a milestone", sorted(t["title"] for t in (await get(f"?project={px}&milestone={m1}")).json()["tickets"])
               == ["Add the footer", "Add the header", "Add the sitemap"])
            for spelled in ("in review", "in-review", "in_review", "IN REVIEW"):
                ck(f"state {spelled!r}", refs(await get(f"?state={spelled.replace(' ', '%20')}")) == [foot["ref"]])
            mv = (await get("?state=moving")).json()
            ck("moving is in progress, in review and changes asked", mv["total"] == 3 and
               {t["state"] for t in mv["tickets"]} == {"in progress", "in review", "changes asked"}, mv["total"])
            can = (await get("?state=canceled")).json()
            ck("canceled shows only when asked for, and nothing is then hidden",
               sorted(t["title"] for t in can["tickets"]) == ["Drop the old theme", "Old idea"]
               and can["hidden_canceled"] == 0, can["total"])
            ck("text matches the title, any case", refs(await get("?q=SITEMAP")) == [site["ref"]])
            tidy = by_title["Tidy the readme"]
            ck("text matches the ref", refs(await get(f"?q={tidy['ref']}")) == [tidy["ref"]], tidy["ref"])
            ck("text with nothing matching gives an empty page and keeps the lists",
               (await get("?q=zzzz")).json()["tickets"] == [] and len((await get("?q=zzzz")).json()["projects"]) == 3)
            ck("filters combine", refs(await get(f"?project={px}&milestone={m1}&state=merged")) == [by_title["Add the header"]["ref"]])
            ck("blank filters are no filter", refs(await get("?state=&q=&project=&milestone=")) == refs(await get("")))

            print("== sorts and paging ==")
            old = (await get("?sort=old")).json()["tickets"]
            ck("oldest change first", old[0]["title"] == "Add the header", old[0]["title"])
            byref = (await get("?sort=ref")).json()["tickets"]
            ck("by project, then the number in the ref",
               [t["project"]["name"] for t in byref] == sorted(t["project"]["name"] for t in byref))
            p1 = (await get("?limit=4")).json()
            p2 = (await get("?limit=4&offset=4")).json()
            p3 = (await get("?limit=4&offset=8")).json()
            ck("a page is the limit, with more to come", len(p1["tickets"]) == 4 and p1["more"] and p1["total"] == 10)
            ck("pages do not overlap and the last says there is no more",
               not set(refs_ := [t["ref"] + t["project"]["id"] for t in p1["tickets"]]) &
               set(t["ref"] + t["project"]["id"] for t in p2["tickets"]) and len(p3["tickets"]) == 2 and not p3["more"])

            print("== the lists to choose from ==")
            ck("the projects, the ones with tickets, with their counts",
               {p["name"]: p["count"] for p in a["projects"]} == {"project-x": 8, "project-y": 2, "project-z": 0},
               a["projects"])
            ck("the milestones of every project, numbered", [(m["number"], m["name"]) for m in a["milestones"]]
               == [("M1", "M1: pages"), ("M2", "M2: search")], a["milestones"])
            only = (await get(f"?project={py}")).json()
            ck("a project with no milestone has none to choose", only["milestones"] == [])

            print("== bad requests ==")
            for q_, want in (("?state=nonsense", 400), ("?sort=random", 400), ("?limit=0", 400), ("?limit=201", 400),
                             ("?offset=-1", 400), ("?project=uc_nothing", 404), (f"?project={pd}", 404),
                             (f"?project={pw}", 404)):
                ck(f"{q_} is {want}", (await get(q_)).status_code == want, (await get(q_)).status_code)

            print("== no internal names ==")
            r = await get("?limit=200&state=")
            text = r.text
            a = r.json()
            hits = [n for n in STATIONS + SESSIONS if n in text]
            ck("no station name and no session id", hits == [], hits)
            bad = [(p, k, s) for p, k, s in strings(a) if s[:1] in ("/", "~") and k not in ("url", "href")]
            ck("no value starts with / or ~ except a link", bad == [], bad)
            raw = [(p, s) for p, k, s in strings(a) if k not in ("state", "state_key", "id", "kind")
                   and re.search(r"\b(in_progress|ready_to_close|waiting_on_you|not_started|software_factory)\b", s)]
            ck("no raw stage or state key in a shown string", raw == [], raw)
            found = words.offenders_in(a, ("name", "title"))
            ck("the words list finds nothing", found == [], found)
            run = subprocess.run([sys.executable, "-m", "tares.words", "--skip", "name,title"],
                                 input=r.text, capture_output=True, text=True,
                                 env={**os.environ, "PYTHONPATH": ROOT})
            ck("python -m tares.words says 0 problems", run.stdout.strip() == "0 problems" and run.returncode == 0,
               run.stdout + run.stderr)
            ck("no em dash", "\u2014" not in text)

            print("== no projects, and a platform cell ==")
            for uid in (px, py, pz, pd):
                store.update_project(uid, status="paused")
                store.set_project_kind(uid, None)
            e = (await get("")).json()
            ck("no factory project: an empty list, nothing to choose",
               e["tickets"] == [] and e["projects"] == [] and e["total"] == 0 and e["counts"]["total"] == 0, e)
            for uid in (px, py, pz):
                store.set_project_kind(uid, "software_factory")
            r = await cx.put("/api/settings/edition", json={"edition": "platform"})
            ck("the edition switches to platform", r.status_code == 200, r.text)
            ck("the route is served on a platform cell too", (await get("")).status_code == 200)
            await cx.put("/api/settings/edition", json={"edition": "factory"})

            print("== auth ==")
            async with cx_("wrong-token") as bad_cx:
                ck("a wrong token is refused", (await bad_cx.get("/api/factory/tickets")).status_code in (401, 403))


print("== the code under test ==")
ck("the module comes from this checkout", os.path.abspath(tares.__file__).startswith(ROOT), tares.__file__)
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
