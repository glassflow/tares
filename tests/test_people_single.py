"""Core with one person and no licence: who can be added, and what the owner's key still does.

Making a person row other than the owner is an ee action counted against the licence seats. Without
a licence no request makes a second person row (402 `licence_required`, nothing written). The
owner keeps their own path: claiming `local`, editing themselves, and, arriving under another email
once `local` is claimed, being `local` again (never refused, never a second row). Finding a person
who is already on the cell by email works in every licence state, and so does their key.

The owner's key also carries two one-person behaviours that core must keep on its own: a project set
up with a person's key is led by that person, and the Start hide is kept under the person's id.
Both are checked under an owner key (`owner: true`), the way a one-person cell has it.

The seats and the licensed paths are ee/tests/test_people_seats.py.

Run: .venv/bin/python tests/test_people_single.py
"""
import asyncio
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

TMP = tempfile.mkdtemp(prefix="tares-people-single-")
ROOT = "root-token-people-single"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_EDITION"] = "sre"   # the edition whose Start hide follows the person
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"

import httpx

import _people
from tares import start_steps as ss

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


class Failing:
    """A model provider that fails at once: a draft is made (and led) before any planning runs."""
    kind = "anthropic"

    async def complete(self, **kw):
        raise RuntimeError("no model in this test")


PLAN = {
    "goal": "Tell me about every new lead",
    "name": "Lead one",
    "summary": "derived",
    "watches": [{
        "key": "w1", "existing": False, "name": "lead-events", "connector": "webhook",
        "config": {"event_type": "log", "text_template": "{service}: {msg}",
                   "labels": [{"name": "service", "field": "service", "primary": True}]},
        "sentence": "New leads, as they happen", "needs": "send",
        "sample": {"service": "crm", "msg": "new lead", "level": "info"}}],
    "wakes": [{
        "key": "k1", "name": "lead-new", "sources": ["lead-events"], "filters": [],
        "key_field": "service",
        "condition": {"aggregate": "count", "predicate": "> 5", "window": "5m"},
        "cooldown": "10m", "window": "15m", "sentence": "A new lead",
        "knobs": [{"id": "threshold", "label": "leads", "value": 5, "min": 1, "max": 1000},
                  {"id": "window_minutes", "label": "minutes", "value": 5, "min": 1, "max": 1440}]}],
    "who": "own", "agents": [],
    "own_agent": {"name": "claude-code", "wake": "webhook", "sentence": "Your agent is woken"},
    "tools": [], "skills": [], "notes": [],
}


async def main():
    from tares import daemon
    # one existing person before the cell starts: the cell has a person other than `local`
    _people.seed(os.environ["TARES_DB"], [("Pat", "pat@example.com")])
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        store, runtime = app.state.store, app.state.runtime

        async def people():
            return (await cx.get("/api/people")).json()["people"]

        async def key(person=None, name="k", **extra):
            body = {"name": name, "scopes": ["admin"], **extra}
            if person:
                body["person"] = person
            return await cx.post("/api/keys", json=body)

        async def whoami(secret):
            return ((await cx.get("/api/whoami", headers=H(secret))).json().get("person") or {})

        def refused(r):
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return r.status_code == 402 and body.get("code") == "licence_required"

        ck("no licence on this cell", app.state.host.licence_state()["valid"] is False)
        eq("two people on the cell: local and Pat", len(await people()), 2)

        print("== the owner claims local ==")
        r = await key({"email": "Owner@Example.com", "name": "Owner", "owner": True}, "owner-1")
        ck("the owner's key is made", r.status_code == 201, r.text[:200])
        owner_key = r.json()["secret"]
        me = await whoami(owner_key)
        eq("it acts as local", me.get("id"), "local")
        eq("local has the owner's email", me.get("email"), "owner@example.com")
        eq("no row was added", len(await people()), 2)

        print("== a new person is refused, nothing is written ==")
        r = await key({"email": "new@example.com", "name": "New"}, "newbie")
        ck("a key naming a new person: 402 licence_required", refused(r), f"{r.status_code} {r.text[:200]}")
        ck("... the message says what to do", "enterprise licence" in r.json().get("message", ""), r.text[:200])
        r = await cx.post("/api/people", json={"name": "Carol", "email": "carol@example.com"})
        ck("POST /api/people with a new email: 402", refused(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/people", json={"name": "No Email"})
        ck("POST /api/people with no email: 402", refused(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/factory/people", json={"email": "dave@example.com", "name": "Dave"})
        ck("POST /api/factory/people: 402", refused(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/factory/people")
        body = r.json()
        ck("the People page says it cannot add, and why",
           body.get("can_add") is False and "licence" in (body.get("add_text") or ""), str(body)[:300])
        eq("still two people", len(await people()), 2)
        ck("no key was left behind for the refused person",
           not any(k["name"] == "newbie" for k in (await cx.get("/api/keys")).json()["keys"]))

        print("== the owner under another email is local, never refused ==")
        r = await key({"email": "owner@work.example.com", "name": "Owner", "owner": True}, "owner-2")
        ck("a key for the owner under another email", r.status_code == 201, r.text[:200])
        eq("it acts as local", (await whoami(r.json()["secret"])).get("id"), "local")
        eq("no second row", len(await people()), 2)
        r = await cx.post("/api/people", json={"name": "Owner", "email": "owner@home.example.com",
                                               "owner": True})
        ck("POST /api/people from the control plane for the owner: local, 200",
           r.status_code == 200 and r.json()["id"] == "local", f"{r.status_code} {r.text[:200]}")
        eq("still two people", len(await people()), 2)
        plain_admin = (await key(name="plain-admin")).json()["secret"]
        r = await cx.post("/api/keys", headers=H(plain_admin), json={
            "name": "p2", "scopes": ["admin"],
            "person": {"email": "grab@example.com", "name": "Grab", "owner": True}})
        ck("an admin key claiming owner for a new email is refused, not made local",
           refused(r), f"{r.status_code} {r.text[:200]}")

        print("== people already on the cell are never refused ==")
        r = await key({"email": "PAT@example.com"}, "pat-laptop")
        ck("a key naming Pat by email", r.status_code == 201, r.text[:200])
        pat = await whoami(r.json()["secret"])
        eq("it acts as Pat", pat.get("email"), "pat@example.com")
        pat_id = pat["id"]
        r = await cx.post("/api/people", json={"name": "Pat", "email": "pat@example.com"})
        ck("POST /api/people with Pat's email finds Pat (200)",
           r.status_code == 200 and r.json()["id"] == pat_id, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/factory/people", json={"email": "pat@example.com"})
        eq("POST /api/factory/people for Pat is the same answer as ever (409)", r.status_code, 409)
        eq("still two people", len(await people()), 2)

        print("== editing: yourself yes, someone else needs the licence ==")
        r = await cx.patch("/api/people/local", headers=H(owner_key), json={"name": "Owner Renamed"})
        ck("the owner renames themselves", r.status_code == 200 and r.json()["name"] == "Owner Renamed",
           f"{r.status_code} {r.text[:200]}")
        r = await cx.patch(f"/api/people/{pat_id}", headers=H(owner_key), json={"name": "Patrick"})
        ck("an admin changing someone else: 402", refused(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.patch("/api/people/nobody-here", headers=H(owner_key), json={"name": "X"})
        eq("a person who is not there is still 404", r.status_code, 404)
        r = await cx.patch(f"/api/people/{pat_id}", json={"name": "Patrick"})
        ck("the system credential changes anyone", r.status_code == 200 and r.json()["name"] == "Patrick",
           f"{r.status_code} {r.text[:200]}")

        print("== the owner's key still leads what it sets up (kept in core) ==")
        daemon.resolve_provider = lambda store, pid=None: (Failing(), "test")
        r = await cx.post("/api/setup/drafts", headers=H(owner_key),
                          json={"goal": PLAN["goal"], "who": "own"})
        ck("a draft made with the owner's key (202)", r.status_code == 202, r.text[:300])
        draft = r.json()["project"]["id"]
        eq("the owner leads the draft", store.get_project(draft)["lead_person_id"], "local")
        for _ in range(100):   # let the failed planning finish before the cell stops
            if ((store.get_project_setup(draft) or {}).get("planning") or {}).get("state") != "running":
                break
            await asyncio.sleep(0.05)
        raw = copy.deepcopy(PLAN)
        from tares import setup_flow as S
        plan, errs = S.normalize(raw, store, runtime.catalog)
        eq("the plan is valid", errs, [])
        r = await cx.post("/api/setup/apply", headers=H(owner_key), json={"plan": plan})
        ck("set up with no draft with the owner's key (201)", r.status_code == 201, r.text[:300])
        eq("the owner leads that project",
           store.get_project(r.json()["project"]["id"])["lead_person_id"], "local")
        r = await cx.post("/api/projects", headers=H(owner_key), json={"template": "custom", "name": "mine"})
        ck("a project made with the owner's key (201 or 200)", r.status_code in (200, 201), r.text[:200])
        eq("the owner leads it", store.get_project(r.json()["id"])["lead_person_id"], "local")
        eq("a project made with the system credential has no lead",
           store.get_project((await cx.post("/api/projects", json={"template": "custom", "name": "sys"})).json()["id"])
           ["lead_person_id"], None)

        print("== the Start hide is kept under the person's id (kept in core) ==")
        r = await cx.post("/api/home/start/hide", headers=H(owner_key))
        eq("hide with the owner's key answers", r.status_code, 200)
        eq("the hide is kept under local, the person's id", ss.hidden(store, "local"), True)
        eq("and not under the key's name", ss.hidden(store, "owner-1"), False)
        bare = (await key(name="bare-key")).json()["secret"]
        await cx.post("/api/home/start/hide", headers=H(bare))
        eq("a key bound to nobody, on a cell with two people, hides under its own name",
           ss.hidden(store, "bare-key"), True)

        await cx.aclose()


async def one_person_cell():
    """A cell with only `local`: an admin key bound to no one is that person, so it renames them."""
    from tares import daemon
    daemon.DB_PATH = os.path.join(TMP, "one.duckdb")   # read once at import: point the new cell at its own file
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        print("== one person on the cell: an unbound admin key is that person ==")
        eq("one person", len((await cx.get("/api/people")).json()["people"]), 1)
        r = await cx.post("/api/keys", json={"name": "unbound", "scopes": ["admin"]})
        secret = r.json()["secret"]
        r = await cx.patch("/api/people/local", headers=H(secret), json={"name": "Solo"})
        ck("it renames local (200)", r.status_code == 200 and r.json()["name"] == "Solo",
           f"{r.status_code} {r.text[:200]}")
        r = await cx.patch("/api/people/nobody-here", headers=H(secret), json={"name": "X"})
        eq("a person who is not there is 404", r.status_code, 404)
        await cx.aclose()


asyncio.run(main())
asyncio.run(one_person_cell())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
