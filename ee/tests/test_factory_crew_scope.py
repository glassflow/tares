# Tares enterprise licence, see ee/LICENSE
"""Factory Settings, the Crew page and the home card read the setting the save writes (TR-893).

A signed-in owner (an admin key that names a person) saves the crew's settings as their own, not
as the cell's default. Every screen that shows those settings must read the same scope, or the
control snaps back after a reload. The owner changes how far the crew may go, the number of
builders, the challenger and pacing; each of the three screens shows the new value on the next
read. The cell's own credential still sets the default, which a second person who set nothing sees.

Run: .venv/bin/python ee/tests/test_factory_crew_scope.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-factory-crew-scope-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-scope"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

P = F = 0
MERGES = "The crew merges"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    keys, pids = {}, {}

    async def call(who, method, path, body=None):
        hdr = {"Authorization": f"Bearer {TOKEN}"} if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        for who in ("alex", "kim"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-cs@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pids[who] = r.json()["person"]["id"]
        store = app.state.store
        store.create_project("prj-x", "software_factory", "Project X", {}, goal="Build X")

        async def seen(who):
            """What the three screens show, for this person, right now."""
            s = (await call(who, "GET", "/api/factory/settings")).json()["crew"]
            c = (await call(who, "GET", "/api/factory/crew")).json()["how_far"]
            hm = (await call(who, "GET", "/api/home")).json()["crew"]["how_far"]
            rv = (await call(who, "GET", "/api/factory/reviews")).json()["autonomy_text"]
            return {"reviews": rv, "settings": (s["how_far"]["value"], s["builders_max"], s["challenger"], s["pace"]),
                    "crew": c, "home": hm}

        print("== before: the cell's default ==")
        before = await seen("alex")
        ck("the owner starts on the cell's default", before["settings"][0] != "L4-ship", str(before))

        print("== the owner saves their own ==")
        r = await call("alex", "PUT", "/api/crew/settings", {"autonomy": "L4-ship", "builders_max": 5,
                                                              "challenger": True, "pace": True})
        ck("the save answers", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        ck("and stores a personal setting, not the cell's default",
           (await call("root", "GET", "/api/crew/settings")).json()["autonomy"] != "L4-ship")
        now = await seen("alex")
        ck("Settings shows the saved value after a reload", now["settings"] == ("L4-ship", 5, True, True),
           str(now["settings"]))
        ck("the Crew page shows it", MERGES in now["crew"]["text"] and "5 builders" in now["crew"]["detail"],
           str(now["crew"]))
        ck("the home card shows it", now["home"]["autonomy"] == "L4-ship", str(now["home"]))
        ck("the Reviews page says who merges", MERGES in now["reviews"], now["reviews"])

        print("== another person and the cell are unchanged ==")
        other = await seen("kim")
        ck("a second person sees the cell's default", other["settings"][0] != "L4-ship", str(other))
        ck("on every screen", other["home"]["autonomy"] != "L4-ship"
           and MERGES not in other["crew"]["text"] and MERGES not in other["reviews"], str(other))

        print("== what waits on the person follows the saved how-far ==")
        from tares import claude_usage, factory as factory_mod
        from tares.envelope import now_utc
        store.set_project_kind("prj-x", "software_factory")
        store.update_project("prj-x", status="active")
        # a pull request waits for the owner of its ticket (factory_owners), at the level the merge
        # queue merges its project at: the hand-over's, else the crew of whoever handed it over
        # (merge_queue.autonomy_of). Never the viewer's own how-far.
        store.set_project_lead("prj-x", pids["kim"])
        t = store.create_ticket("prj-x", "tares", "Page", repo="example-org/svc")
        store.update_ticket(t, assignee_person_id=pids["kim"])
        store.update_ticket(t, stage="review", pr={
            "url": "https://github.com/example-org/svc/pull/7", "number": 7, "repo": "example-org/svc",
            "head": "cccc333", "ci": "success", "title": "PR: Page"})
        store.add_review("prj-x", t, None, "cccc333", "pass", "ran the tests", None, "crew-reviewer",
                         [], {}, factory_mod.finding_kind, now_utc())

        async def waits(who):
            """Does the pull request wait on this person: the Projects row, the project page?"""
            rows = (await call(who, "GET", "/api/factory/projects")).json()["projects"]
            row = [x for x in rows if x["id"] == "prj-x"][0]
            pg = (await call(who, "GET", "/api/factory/projects/prj-x")).json()
            return ("PR to review" in row["meta"],
                    any(w["kind"] == "prs_to_review" for w in pg["waiting"]))

        ck("not handed over, under the cell's L3-review default, a passed PR waits on its ticket's owner",
           await waits("kim") == (True, True), str(await waits("kim")))
        ck("not on Alex, whose ticket it is not, whatever Alex saved",
           await waits("alex") == (False, False), str(await waits("alex")))

        print("== the Tickets filter orders its projects by what waits on the person ==")
        # a project whose tickets are all out of the builders' hands but for a passed PR waits on
        # its owner; a busier project is newer. For someone it does not wait on, the newer leads.
        store.create_project("prj-y", "software_factory", "Project Y", {}, goal="Build Y")
        store.set_project_kind("prj-y", "software_factory")
        store.update_project("prj-y", status="active")
        # prj-x keeps its passed PR in review (a done ticket would leave review, TR-1008)
        store.create_ticket("prj-x", "tares", "Next", repo="example-org/svc")   # and one still to do
        busy = store.create_ticket("prj-y", "tares", "Busy", repo="example-org/svc2")
        store.update_ticket(busy, stage="doing")

        async def order(who):
            r = (await call(who, "GET", "/api/factory/tickets")).json()
            return [p["id"] for p in r["projects"]]

        ok_kim, ok_alex = await order("kim"), await order("alex")
        ck("the person who must merge sees the project with the passed PR first", ok_kim[0] == "prj-x", str(ok_kim))
        ck("someone it does not wait on sees the busier project first", ok_alex[0] == "prj-y", str(ok_alex))

        print("== handed over by the owner who saved L4-ship: the crew merges it ==")
        store.set_handover("prj-x", [], None, "alex", now_utc(), person_id=pids["alex"])
        ck("the hand-over person's L4-ship has the crew merge it: nothing waits on Kim",
           await waits("kim") == (False, False), str(await waits("kim")))

        print("== pacing follows the saved value ==")
        from datetime import timedelta
        soon = (now_utc() + timedelta(hours=1)).isoformat()
        store.set_setting(claude_usage.PLAN_SETTING, json.dumps({"at": now_utc().isoformat(), "plan": {"limits": [
            {"kind": "session", "label": "5-hour", "percent": 96, "resets_at": soon,
             "severity": "high", "active": True}]}}))
        fh_clear = __import__("tares.factory_home", fromlist=["clear_cache"]).clear_cache
        fh_clear()
        a = (await call("alex", "GET", "/api/factory/settings")).json()["crew"]
        k = (await call("kim", "GET", "/api/factory/settings")).json()["crew"]
        ck("Settings tells the owner the crew is slowed", "96%" in a["pace_text"], a["pace_text"])
        ck("and tells the second person pacing is off", "Pacing is off" in k["pace_text"], k["pace_text"])
        ca = (await call("alex", "GET", "/api/factory/crew")).json()
        ck("the Crew page says so for the owner only",
           bool(ca.get("pacing")) and not (await call("kim", "GET", "/api/factory/crew")).json().get("pacing"),
           str(ca.get("pacing")))

        print("== a save that lands while the home is being built is not lost to the kept copy ==")
        from tares import factory_home as fh
        real = fh.home
        def slow_home(*a, **k):
            out = real(*a, **k)
            fh.clear_cache()          # the owner's save lands while this read is still building
            return out
        fh.home = slow_home
        try:
            fh.clear_cache()
            await call("kim", "GET", "/api/home")
        finally:
            fh.home = real
        ck("the build that was overtaken by a clear is not kept", not fh._cache, str(list(fh._cache)))

        print("== the cell's own credential sets the default ==")
        r = await call("root", "PUT", "/api/crew/settings", {"autonomy": "L3-review"})
        ck("the default saves", r.status_code == 200, r.text[:200])
        root = await seen("root")
        ck("and shows on every screen for it", root["settings"][0] == "L3-review"
           and root["home"]["autonomy"] == "L3-review" and MERGES not in root["crew"]["text"], str(root))

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
