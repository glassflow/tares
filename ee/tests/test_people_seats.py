# Tares enterprise licence, see ee/LICENSE
"""People beyond the owner, with a licence: a new person row is made while the licence has a seat for
one more, then refused (402 `licence_required`, nothing written). Seats are every row in `people`,
the owner included. An expired licence refuses adding only; a licence with fewer seats than people
(over seats) refuses adding only. Finding someone who is already on the cell, their keys and editing
other people work in every state, and the owner arriving under another email is `local` again.
The no-licence half is tests/test_people_single.py.

In process, no ports: the key is stored through PUT /api/licence, which checks again at once.

Run: .venv/bin/python ee/tests/test_people_seats.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-people-seats-")
ROOT = "root-token-people-seats"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod
from tares_ee.team import Team

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def refused(r):
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return r.status_code == 402 and body.get("code") == "licence_required"


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store

        async def licence(text):
            r = await cx.put("/api/licence", json={"key": text})
            ck("the licence is stored", r.status_code == 200, r.text[:200])

        async def people():
            return (await cx.get("/api/people")).json()["people"]

        async def add(email, name="X", **extra):
            return await cx.post("/api/people", json={"name": name, "email": email, **extra})

        print("== no seat left for a second person without a licence: the entry point is there ==")
        eq("no licence: the single-person team", type(team_mod.get()).__name__, "SingleTeam")
        r = await add("early@example.com")
        ck("so a second person is refused", refused(r), f"{r.status_code} {r.text[:200]}")

        print("== three seats: local and two more ==")
        await licence(licensed.test_licence(seats=3))
        from tares_ee.team import Team
        ck("team_mod.get() is the ee Team", type(team_mod.get()) is Team, type(team_mod.get()).__name__)
        eq("the owner row counts: one seat used", len(await people()), 1)
        eq("room for one more", team_mod.get().can_add_person(store), (True, None))
        r = await add("ann@example.com", "Ann")
        ck("the second person (201)", r.status_code == 201, r.text[:200])
        r = await add("ben@example.com", "Ben")
        ck("the third person (201)", r.status_code == 201, r.text[:200])
        ben = r.json()["id"]
        r = await add("cy@example.com", "Cy")
        ck("the fourth is refused", refused(r), f"{r.status_code} {r.text[:200]}")
        ck("... and says the seats are used",
           "seat" in r.json().get("message", ""), r.text[:200])
        eq("nothing was written", len(await people()), 3)
        ok, why = team_mod.get().can_add_person(store)
        ck("can_add_person says no, with a reason", ok is False and "seat" in why, f"{ok} {why}")
        r = await cx.post("/api/keys", json={"name": "cy", "scopes": ["admin"],
                                             "person": {"email": "cy@example.com", "name": "Cy"}})
        ck("a key naming a new person is refused too", refused(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/factory/people", json={"email": "cy@example.com"})
        ck("POST /api/factory/people is refused", refused(r), f"{r.status_code} {r.text[:200]}")
        page = (await cx.get("/api/factory/people")).json()
        ck("the People page cannot add and says why",
           page["can_add"] is False and "seat" in page["add_text"], str(page)[:300])

        os.environ["TARES_WORKSPACE_API_URL"] = "http://workspace.invalid"
        try:
            page = (await cx.get("/api/factory/people")).json()
        finally:
            del os.environ["TARES_WORKSPACE_API_URL"]
        ck("on Tares Cloud the page leaves adding to the workspace, seats or not",
           page["cloud"] is True and page["can_add"] is True, str(page)[:300])

        print("== everyone already there still works at full seats ==")
        r = await add("ANN@example.com", "Ann")
        ck("an existing email finds the person (200)", r.status_code == 200, r.text[:200])
        r = await cx.post("/api/keys", json={"name": "ann", "scopes": ["admin"],
                                             "person": {"email": "ann@example.com"}})
        ck("a key for an existing person (201)", r.status_code == 201, r.text[:200])
        ann_key = r.json()["secret"]
        eq("it acts as Ann", (await cx.get("/api/whoami", headers=H(ann_key))).json()["person"]["email"],
           "ann@example.com")
        r = await cx.patch(f"/api/people/{ben}", json={"name": "Benjamin"})
        ck("changing another person works with the licence", r.status_code == 200, r.text[:200])
        r = await cx.patch(f"/api/people/{ben}", headers=H(ann_key), json={"name": "Ben B"})
        ck("an admin key changes another person too", r.status_code == 200, r.text[:200])

        print("== the owner under another email is local ==")
        r = await cx.post("/api/keys", json={"name": "owner", "scopes": ["admin"], "person": {
            "email": "owner@example.com", "name": "Owner", "owner": True}})
        ck("the owner claims local (201)", r.status_code == 201, r.text[:200])
        eq("as local", (await cx.get("/api/whoami", headers=H(r.json()["secret"]))).json()["person"]["id"], "local")
        r = await cx.post("/api/keys", json={"name": "owner2", "scopes": ["admin"], "person": {
            "email": "owner@other.example.com", "name": "Owner", "owner": True}})
        ck("under another email at full seats: made, not refused", r.status_code == 201, r.text[:200])
        eq("as local again", (await cx.get("/api/whoami", headers=H(r.json()["secret"]))).json()["person"]["id"], "local")
        r = await add("owner@third.example.com", "Owner", owner=True)
        ck("POST /api/people from the control plane: local (200)",
           r.status_code == 200 and r.json()["id"] == "local", f"{r.status_code} {r.text[:200]}")
        eq("no row was added", len(await people()), 3)

        print("== more seats, the fourth fits ==")
        await licence(licensed.test_licence(seats=4))
        r = await add("cy@example.com", "Cy")
        ck("the fourth person (201)", r.status_code == 201, r.text[:200])
        r = await add("di@example.com", "Di")
        ck("the fifth is refused", refused(r), f"{r.status_code} {r.text[:200]}")

        print("== over seats: adding is refused, nothing else changes ==")
        await licence(licensed.test_licence(seats=2))
        st = (await cx.get("/api/licence")).json()
        ck("four people on two seats", st["over_seats"] is True and st["used"] == 4, str(st))
        r = await add("ed@example.com", "Ed")
        ck("a new person is refused", refused(r), f"{r.status_code} {r.text[:200]}")
        r = await add("cy@example.com", "Cy")
        ck("an existing person is found (200)", r.status_code == 200, r.text[:200])
        r = await cx.patch(f"/api/people/{ben}", json={"name": "Ben Again"})
        ck("changing a person still works", r.status_code == 200, r.text[:200])
        eq("Ann's key still acts as Ann", (await cx.get("/api/whoami", headers=H(ann_key))).json()["person"]["email"],
           "ann@example.com")

        print("== expired: adding is refused, nothing else changes ==")
        gone = (date.today() - timedelta(days=3)).isoformat()
        await licence(licensed.test_licence(seats=10, expires=gone))
        st = (await cx.get("/api/licence")).json()
        ck("expired but valid, with seats to spare", st["valid"] and st["expired"] and st["seats"] == 10, str(st))
        ck("the ee team is still the team", type(team_mod.get()) is Team, type(team_mod.get()).__name__)
        r = await add("fay@example.com", "Fay")
        ck("a new person is refused", refused(r), f"{r.status_code} {r.text[:200]}")
        ck("... and says it expired", "expired" in r.json().get("message", ""), r.text[:200])
        r = await add("ann@example.com", "Ann")
        ck("an existing person is found (200)", r.status_code == 200, r.text[:200])
        r = await cx.patch(f"/api/people/{ben}", json={"name": "Ben Last"})
        ck("changing a person still works", r.status_code == 200, r.text[:200])
        r = await cx.post("/api/keys", json={"name": "ann3", "scopes": ["admin"],
                                             "person": {"email": "ann@example.com"}})
        ck("a key for an existing person (201)", r.status_code == 201, r.text[:200])

        print("== a licence without the enterprise feature is no licence for this ==")
        await licence(licensed.test_licence(seats=10, features=("other",)))
        eq("the single-person team again", type(team_mod.get()).__name__, "SingleTeam")
        r = await add("gus@example.com", "Gus")
        ck("a new person is refused", refused(r), f"{r.status_code} {r.text[:200]}")

        print("== many adds at once never pass the seats ==")
        await licence(licensed.test_licence(seats=6))
        eq("four people, two seats left", len(await people()), 4)
        real = Team.can_add_person

        def slow(self, st):
            answer = real(self, st)
            time.sleep(0.05)   # widen the window between the check and the insert
            return answer

        Team.can_add_person = slow
        try:
            async def via(i):
                if i % 3 == 0:
                    return await add(f"race{i}@example.com", f"Race {i}")
                if i % 3 == 1:
                    return await cx.post("/api/keys", json={
                        "name": f"race{i}", "scopes": ["admin"],
                        "person": {"email": f"race{i}@example.com", "name": f"Race {i}"}})
                return await cx.post("/api/factory/people", json={"email": f"race{i}@example.com"})
            rs = await asyncio.gather(*[via(i) for i in range(9)])
        finally:
            Team.can_add_person = real
        made = [r for r in rs if r.status_code == 201]
        eq("two of nine were made", len(made), 2)
        ck("the rest were refused for the licence", all(refused(r) for r in rs if r.status_code != 201),
           str([r.status_code for r in rs]))
        eq("six people, never more than the seats", len(await people()), 6)

        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
