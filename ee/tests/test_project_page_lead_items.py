# Tares enterprise licence, see ee/LICENSE
"""On a team cell, what a project asks of its lead waits for the lead only, on every screen.

Seen on the factory cell: Kiran leads tares-desktop. Ashish (the cell's owner) is not on it, yet its
page showed him four "Milestone to check" items and "calls to confirm" under "Waiting for you", and
the project as "Waiting on you". His home showed none of them. The project page read the
single-person list, which asks nobody who an item is for. Now every screen reads the team home's
rule:

  - for Ashish the lead's items are under `waiting_others`, named for Kiran, with nothing to press;
    his `waiting` is empty and the project is not "Waiting on you" on its page, on Projects, or in
    the Tickets order;
  - for Kiran they are his, and the project is "Waiting on you";
  - the home and the project page list the same items for each person;
  - a project Ashish leads keeps its items for him.

Run: .venv/bin/python ee/tests/test_project_page_lead_items.py
"""
import asyncio
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-project-page-lead-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-project-page-lead"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from datetime import timedelta
    from tares import factory_home as fh
    from tares.daemon import make_app
    from tares.envelope import now_utc
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    mk = lambda h=None: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                          headers=h or root)

    def project(name, lead):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        store.set_project_lead(uid, lead)
        return uid

    async def lead_items(uid):
        """A milestone whose tickets all merged and whose checks nobody recorded, a call the crew
        made, and an open ticket so the project is not ready to close."""
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{uid}/milestones", json={
                "name": "M1 The app shows the crew", "checks": [{"check": "open the app", "expect": "the crew shows"}]})
            ck(f"{uid}: milestone made", r.status_code < 300, r.text)
            await cx.post(f"/api/projects/{uid}/milestones", json={"name": "M2 Tray"})
        ms = {m["name"][:2]: m["id"] for m in store.milestones_by_project([uid])[uid]}
        tid = store.create_ticket(uid, "tares", "Crew list", milestone=ms["M1"])
        store.update_ticket(tid, stage="merged", status="done")
        store.create_ticket(uid, "tares", "Tray icon", milestone=ms["M2"])
        store.add_assumption(uid, "Which tray library?", "the built-in one", "no new package", "", [],
                             "crew-orchestrator", "orchestrator", now_utc() - timedelta(hours=2))

    async with app.router.lifespan_context(app):
        # the owner is `local`, claimed, as on a cloud cell; Kiran and Adam are two more people
        store.claim_local("ashish@example.com", "Ashish")
        keys, pid = {}, {}
        async with mk() as cx:
            for who in ("ashish", "kiran", "adam"):
                r = await cx.post("/api/keys", json={"name": f"{who}-laptop", "scopes": ["admin"],
                                                     "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
                assert r.status_code < 300, r.text
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]
        ck("Ashish is the claimed local", pid["ashish"] == "local", pid)

        desktop = project("tares-desktop", pid["kiran"])
        store.add_project_member(desktop, pid["adam"], added_by=pid["kiran"])
        own = project("ashish-own", pid["ashish"])
        await lead_items(desktop)
        await lead_items(own)

        async def get(who, path):
            fh.clear_cache()
            async with mk(keys[who]) as cx:
                r = await cx.get(path)
                assert r.status_code == 200, (path, r.text)
                return r.json()

        page = lambda who, uid: get(who, f"/api/factory/projects/{uid}")
        home_ids = lambda h, uid: sorted(w["id"] for w in h["waiting_for_you"]
                                         if (w.get("project") or {}).get("id") == uid)

        print("== Ashish, not on tares-desktop ==")
        a = await page("ashish", desktop)
        others = {w["label"]: w for w in a["waiting_others"]}
        ck("nothing waits for him there", a["waiting"] == [], [w["label"] for w in a["waiting"]])
        ck("the project is not Waiting on you", a["state"] != "waiting_on_you", a["state"])
        ck("the milestone waits for Kiran, nothing to press",
           "Milestone to check for Kiran" in others and others["Milestone to check for Kiran"]["actions"] == []
           and others["Milestone to check for Kiran"]["for_person"]["id"] == pid["kiran"], list(others))
        ck("the call waits for Kiran", "1 call to confirm for Kiran" in others, list(others))
        ck("no item is named for anyone but Kiran", all(w["label"].endswith("for Kiran") for w in a["waiting_others"]),
           list(others))
        h = await get("ashish", "/api/home")
        ck("his home agrees with the page", home_ids(h, desktop) == [], home_ids(h, desktop))
        ps = {p["id"]: p for p in (await get("ashish", "/api/factory/projects"))["projects"]}
        ck("Projects does not say Waiting on you", ps[desktop]["state"] != "waiting_on_you", ps[desktop]["state"])

        print("== Ashish, lead of his own project ==")
        o = await page("ashish", own)
        kinds = sorted(w["kind"] for w in o["waiting"])
        ck("its milestone and its call are his", kinds == ["calls_to_confirm", "milestone_to_check"], kinds)
        ck("the project is Waiting on you", o["state"] == "waiting_on_you", o["state"])
        ck("nothing there waits for others", o["waiting_others"] == [], o["waiting_others"])
        ck("his home lists the same items", home_ids(h, own) == sorted(w["id"] for w in o["waiting"]),
           (home_ids(h, own), [w["id"] for w in o["waiting"]]))
        ck("Projects says Waiting on you", ps[own]["state"] == "waiting_on_you", ps[own]["state"])
        t = await get("ashish", "/api/factory/tickets")
        order = [p["id"] for p in t["projects"]]
        ck("Tickets lists the project that waits on him before Kiran's",
           own in order and desktop in order and order.index(own) < order.index(desktop), order)

        print("== Kiran, the lead ==")
        k = await page("kiran", desktop)
        kinds = sorted(w["kind"] for w in k["waiting"])
        ck("the milestone and the call are his", kinds == ["calls_to_confirm", "milestone_to_check"], kinds)
        ck("tares-desktop is Waiting on you for him", k["state"] == "waiting_on_you", k["state"])
        ck("nothing of his is under others", not any("Kiran" in w["label"] for w in k["waiting_others"]),
           [w["label"] for w in k["waiting_others"]])
        hk = await get("kiran", "/api/home")
        ck("his home lists the same items as the page", home_ids(hk, desktop) == sorted(w["id"] for w in k["waiting"]),
           (home_ids(hk, desktop), [w["id"] for w in k["waiting"]]))
        ck("Ashish's project is not on Kiran's page lists", home_ids(hk, own) == [], home_ids(hk, own))

        print("== Adam, a member who does not lead ==")
        d = await page("adam", desktop)
        ck("nothing waits for Adam", d["waiting"] == [] and d["state"] != "waiting_on_you",
           ([w["label"] for w in d["waiting"]], d["state"]))
        ck("he sees what waits for Kiran", any(w["label"] == "Milestone to check for Kiran" for w in d["waiting_others"]),
           [w["label"] for w in d["waiting_others"]])

        print("== a passed pull request waits for Kiran by his how-far, not the viewer's ==")
        from tares import factory as factory_mod
        store.update_project(desktop, status="active")
        pr = store.create_ticket(desktop, "tares", "Tray menu", repo="example-org/desktop")
        store.update_ticket(pr, stage="review", pr={
            "url": "https://github.com/example-org/desktop/pull/9", "number": 9, "repo": "example-org/desktop",
            "head": "dddd444", "ci": "success", "title": "Tray menu"})
        store.add_review(desktop, pr, None, "dddd444", "pass", "ran the tests", None, "crew-reviewer",
                         [], {}, factory_mod.finding_kind, now_utc())
        # Adam, not Ashish: the person `local` (Ashish) writes the cell's default, not their own
        async with mk(keys["adam"]) as cx:
            r = await cx.put("/api/crew/settings", json={"autonomy": "L4-ship"})
            ck("Adam saves L4-ship for his own crew", r.status_code == 200, r.text)
        a = await page("adam", desktop)
        ck("the PR waits for Kiran, who merges at L3-review",
           any(w["label"] == "1 PR to review for Kiran" for w in a["waiting_others"]),
           [w["label"] for w in a["waiting_others"]])
        k = await page("kiran", desktop)
        ck("and it is Kiran's own", any(w["kind"] == "prs_to_review" for w in k["waiting"]),
           [w["kind"] for w in k["waiting"]])

        print("== the milestone's checks say whom they wait for ==")
        a = await page("ashish", desktop)
        m1 = next(m for m in a["milestones"] if m["name"].startswith("M1"))
        ck("for Ashish the checks are Kiran's, not his to answer",
           m1["yours"] is False and [w["name"] for w in m1["checks_for"]] == ["Kiran"] and m1["can_accept"] is False, m1)
        k = await page("kiran", desktop)
        ck("for Kiran they are his", next(m for m in k["milestones"] if m["name"].startswith("M1"))["yours"] is True)

        print("== Reviews: another person's pull request is under Waiting for others ==")
        rv = await get("ashish", "/api/factory/reviews")
        ck("not in Ashish's Waiting for you", not any(r["ticket"]["title"] == "Tray menu" for r in rv["waiting_for_you"]),
           [r["ticket"]["title"] for r in rv["waiting_for_you"]])
        ck("under waiting_for_others, named for Kiran",
           any(r["ticket"]["title"] == "Tray menu" and [w["name"] for w in r["for_people"]] == ["Kiran"]
               for r in rv["waiting_for_others"]), rv["waiting_for_others"])
        rk = await get("kiran", "/api/factory/reviews")
        ck("Kiran's own", any(r["ticket"]["title"] == "Tray menu" for r in rk["waiting_for_you"]))

        print("== ready to close: Close is the lead's ==")
        done = project("all-merged", pid["kiran"])
        t1 = store.create_ticket(done, "tares", "Only ticket")
        store.update_ticket(t1, stage="merged", status="done")
        a, k = await page("ashish", done), await page("kiran", done)
        ck("Ashish cannot close Kiran's project and has no Ready to close of his own",
           a["can_close"] is False and not any(w["kind"] == "signoff" for w in a["waiting"])
           and any(w["label"] == "Ready to close for Kiran" for w in a["waiting_others"]), (a["can_close"], a["waiting"], a["waiting_others"]))
        ck("Kiran can", k["can_close"] is True and any(w["kind"] == "signoff" for w in k["waiting"]), k["can_close"])
        ps = {p["id"]: p for p in (await get("ashish", "/api/factory/projects"))["projects"]}
        ck("the Projects row offers Close to Kiran only", ps[done]["can_close"] is False
           and {p["id"]: p for p in (await get("kiran", "/api/factory/projects"))["projects"]}[done]["can_close"] is True)

        print("== a desk item of a project kept for later stays on its page ==")
        later = project("kept-later", pid["kiran"])
        store.update_project(later, status="later")
        async with mk() as cx:
            r = await cx.post("/api/desk", json={"question": "Which icon set?", "recommendation": "a", "why": "w",
                                                 "project": later, "to": "kiran@example.com"})
            ck("the desk takes it", r.status_code < 300, r.text)
        k = await page("kiran", later)
        ck("Kiran sees it on the project's page", any("icon set" in w["detail"] for w in k["waiting"]),
           [w["detail"] for w in k["waiting"]])
        a = await page("ashish", later)
        ck("Ashish sees it for Kiran", any("icon set" in w["detail"] and w["label"] == "Question for Kiran"
                                           for w in a["waiting_others"]), [w["label"] for w in a["waiting_others"]])

        print("== the lead leaves: the work falls to the admins, marked no lead ==")
        gone = project("left-behind", pid["adam"])
        await lead_items(gone)
        async with mk() as cx:
            r = await cx.delete(f"/api/people/{pid['adam']}")
            ck("Adam is removed", r.status_code < 300, r.text)
        ck("the project no longer names him as lead", store.get_project(gone).get("lead_person_id") is None)
        a = await page("ashish", gone)
        ck("Ashish, an admin, has its items, each saying it has no lead",
           sorted(w["kind"] for w in a["waiting"]) == ["calls_to_confirm", "milestone_to_check"]
           and all(w["no_lead"] and w["meta"].startswith("No lead, set one") for w in a["waiting"]), a["waiting"])
        h = await get("ashish", "/api/home")
        ck("and so has his home", sorted(w["kind"] for w in h["waiting_for_you"] if (w.get("project") or {}).get("id") == gone)
           == ["calls_to_confirm", "milestone_to_check"])
        k = await page("kiran", gone)
        ck("for Kiran they wait for Ashish", k["waiting"] == []
           and sorted(w["label"] for w in k["waiting_others"]) == ["1 call to confirm for Ashish", "Milestone to check for Ashish"],
           [w["label"] for w in k["waiting_others"]])

        print("== no admin on the cell: an orphan waits for no one, never for everyone ==")
        store.update_person("local", is_admin=False)
        a, k = await page("ashish", gone), await page("kiran", gone)
        ck("on nobody's list", a["waiting"] == [] and k["waiting"] == [], (a["waiting"], k["waiting"]))
        ck("on the page it is for no one, with No lead, set one",
           all(w["label"].endswith("for no one") and w["meta"].startswith("No lead, set one") for w in a["waiting_others"])
           and len(a["waiting_others"]) == 2, [(w["label"], w["meta"]) for w in a["waiting_others"]])
        async with mk() as cx:
            fh.clear_cache()
            rh = (await cx.get("/api/home")).json()
        ck("the cell's own credential still sees it", sum(1 for w in rh["waiting_for_you"]
                                                          if (w.get("project") or {}).get("id") == gone) == 2)
        store.update_person("local", is_admin=True)

        print("== a key with no person on a team cell: nothing is addressed to it ==")
        async with mk() as cx:
            r = await cx.post("/api/keys", json={"name": "no-person", "scopes": ["admin"]})
            nobody = {"Authorization": f"Bearer {r.json()['secret']}"}
        async with mk(nobody) as cx:
            fh.clear_cache()
            r = await cx.get(f"/api/factory/projects/{desktop}")
        ck("its project page lists nothing as its own", r.status_code == 200 and r.json()["waiting"] == []
           and r.json()["state"] != "waiting_on_you", r.text[:300])

        print("== without a licence the rule holds: each person sees their own ==")
        async with mk() as cx:
            r = await cx.delete("/api/licence")
            ck("the licence is removed", r.status_code < 300, r.text)
        a, k = await page("ashish", desktop), await page("kiran", desktop)
        ck("Ashish: nothing of Kiran's as his, and named for Kiran",
           a["waiting"] == [] and a["state"] != "waiting_on_you"
           and any(w["label"] == "Milestone to check for Kiran" for w in a["waiting_others"]),
           ([w["label"] for w in a["waiting"]], [w["label"] for w in a["waiting_others"]]))
        ck("Kiran: his", any(w["kind"] == "milestone_to_check" for w in k["waiting"]) and k["state"] == "waiting_on_you")
        h = await get("ashish", "/api/home")
        ck("Ashish's home agrees", home_ids(h, desktop) == [], home_ids(h, desktop))

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
