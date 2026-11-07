"""Core with one person and no licence: every multi-person point has its final answer (TR-925).

The audit is the Tares note "Audit: multi-person paths" (TR-916). Three kinds of row, one proof each:

- A place that would make a second person (or address one) answers 402 `licence_required` and writes
  nothing: a key or POST /api/people naming a new person, POST /api/factory/people, and a desk item
  addressed to someone else (`desk_target`). Addressing yourself, or `local` on a cell of one, is
  never refused. A stand-in team that is enabled is not asked to refuse.
- Binding a key to a person who is already on the cell has no licence check, whatever the licence
  says: PUT /api/keys/{kid}/person and POST /api/keys with `person_id` give 200/201 and /api/whoami
  names that person. A cloud cell must not lock out the people it already has.
- A "mine" filter on a cell of one is the same list as the unfiltered one: review queue, crews, desk,
  my work, projects. The licensed versions of these are under ee/tests.

The second person on the second cell is seeded before the daemon starts (tests/_people.py), not
added over HTTP. Nothing here needs ee: the `mit` suite deletes ee/.

Run: .venv/bin/python tests/test_core_single_person.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-core-single-person-")
os.environ["TARES_DB"] = os.path.join(TMP, "one.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT = "root-token-core-single-person"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import _people
from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares import daemon

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


def exact_402(r, text=None):
    """The licence contract: 402 `licence_required`, a message, the docs URL."""
    try:
        b = r.json()
    except Exception:
        return False
    return (r.status_code == 402 and b.get("code") == "licence_required"
            and b.get("url") == "https://docs.glassflow.ai/tares/enterprise"
            and (text is None or b.get("message") == text))


def ts(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station_lines(sid, station, role, say):
    base = {"sessionId": sid, "cwd": "/w/crew", "factory_station": station}
    return [{"type": "session_station", "timestamp": ts(30), "station": station, "role": role,
             "parent": "", **base},
            {"type": "assistant", "timestamp": ts(1), "message": {"role": "assistant", "content": say},
             **base}]


def nd(lines):
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class CountingTeam(team_mod.SingleTeam):
    """A stand-in team that is enabled (it is not the single team), to show the check is the
    team's to make: the single team refuses, an enabled one is not asked to."""


async def one_person_cell():
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        store = app.state.store

        async def people():
            return (await cx.get("/api/people")).json()["people"]

        ck("no licence on this cell", app.state.host.licence_state()["valid"] is False)
        ck("a cell of one: only local", store.only_local_person())

        print("== _body_person: a key naming a new person is refused, nothing written ==")
        r = await cx.post("/api/keys", json={"name": "k-new", "scopes": ["admin"],
                                             "person": {"email": "new@example.com", "name": "New"}})
        ck("POST /api/keys with a new person: 402 licence_required", exact_402(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Keyed", "objects": []})
        r = await cx.post("/api/keys", json={"name": "k-new2", "scopes": ["read"], "project": r.json()["id"],
                                             "person": {"email": "new2@example.com"}})
        ck("a project key naming a new person: 402 licence_required", exact_402(r),
           f"{r.status_code} {r.text[:200]}")
        eq("still one person", len(await people()), 1)
        ck("still a cell of one", store.only_local_person())
        ck("no key was left behind",
           not any(k["name"].startswith("k-new") for k in (await cx.get("/api/keys")).json()["keys"]))

        print("== create_person: POST /api/people ==")
        r = await cx.post("/api/people", json={"name": "Carol", "email": "carol@example.com"})
        ck("a new email: 402 licence_required", exact_402(r), f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/people", json={"name": "No Email"})
        ck("no email: 402 licence_required", exact_402(r), f"{r.status_code} {r.text[:200]}")
        eq("still one person", len(await people()), 1)

        print("== factory_people: POST /api/factory/people ==")
        r = await cx.post("/api/factory/people", json={"email": "dave@example.com", "name": "Dave"})
        ck("402 licence_required", exact_402(r), f"{r.status_code} {r.text[:200]}")
        eq("still one person", len(await people()), 1)

        print("== desk_target: addressing yourself is free, nobody else is reachable ==")
        q = {"question": "Ship it?", "recommendation": "yes", "why": "the tests pass"}
        r = await cx.post("/api/desk", json=q)
        ck("a desk item with no `to` (the caller's own): 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/desk", json={**q, "to": "local"})
        ck("`to: local` on a cell of one: 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/desk", json={**q, "to": "nobody@example.com"})
        eq("`to` a person who is not on the cell is 404, not 402", r.status_code, 404)
        eq("two desk items were added", len(store.list_desk(None, None)), 2)

        print("== the trivial rows: mine is the unfiltered list on a cell of one ==")
        r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
        ck("the claude_code source is made", r.status_code < 300, r.text[:200])
        r = await cx.post("/ingest/claude_code", content=nd(
            station_lines("o-1", "crew-orchestrator", "orchestrator", "ready")
            + station_lines("r-1", "crew-reviewer", "reviewer", "reviewing")),
            headers={"Content-Type": "application/x-ndjson"})
        ck("two stations are taken in", r.status_code < 300, r.text[:200])
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Shop", "objects": [],
                                                 "kind": "software_factory"})
        shop = r.json()["id"]
        for title in ("first", "second"):
            r = await cx.post(f"/api/projects/{shop}/tickets", json={"title": title})
            ck(f"ticket {title} is made", r.status_code == 201, r.text[:200])
            store.update_ticket(r.json()["id"], stage="review")
        r = await cx.post("/api/keys", json={"name": "owner-key", "scopes": ["admin"], "person_id": "local"})
        ck("a key bound to local is made", r.status_code == 201, r.text[:200])
        mine = H(r.json()["secret"])

        for label, path, mine_path, pick in (
                ("review queue", "/api/review-queue", "/api/review-queue?mine=1", lambda b: b["queue"]),
                ("crews", "/api/crew", "/api/crew?mine=1", lambda b: b["crews"]),
                ("desk", "/api/desk", "/api/desk?mine=1", lambda b: b["desk"]),
                ("projects", "/api/projects", "/api/projects", lambda b: b)):
            a = pick((await cx.get(path)).json())
            b = pick((await cx.get(mine_path, headers=mine)).json())
            ck(f"{label}: the owner's key sees what the root token sees ({len(a)} rows)",
               a == b and len(a) > 0, f"{len(a)} vs {len(b)}")
        a = (await cx.get("/api/crew/my-work")).json()
        b = (await cx.get("/api/crew/my-work", headers=mine)).json()
        ck("my work: the owner's key and the root token get the same answer", a == b, f"{a} vs {b}")
        r = await cx.get("/api/crew/my-work", headers=mine)
        ck("my work lists the two desk items addressed to local", len(r.json()["desk"]) == 2, r.text[:300])

        print("== the crew's settings: a person's own write is the cell's on a cell of one ==")
        r = await cx.put("/api/crew/settings", headers=mine, json={"autonomy": "L4-ship"})
        ck("the owner writes crew settings", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/crew")
        eq("the cell's default is the owner's", r.json()["settings"].get("autonomy"), "L4-ship")
        await cx.aclose()


async def two_person_cell():
    path = os.path.join(TMP, "two.duckdb")
    _people.seed(path, [("Pat", "pat@example.com")])   # a second person, with no licence behind them
    daemon.DB_PATH = path   # read once at import: point the new cell at its own file
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        store = app.state.store
        ck("two people on the cell", len(store.list_people()) == 2)
        pat = store.find_person_by_email("pat@example.com")["id"]

        async def whoami(secret):
            return ((await cx.get("/api/whoami", headers=H(secret))).json().get("person") or {})

        print("== desk_target: a desk item addressed to someone else needs the licence ==")
        q = {"question": "Ship it?", "recommendation": "yes", "why": "the tests pass"}
        before = len(store.list_desk(None, None))
        for to in (pat, "pat@example.com"):
            r = await cx.post("/api/desk", json={**q, "to": to})
            ck(f"`to: {to}`: 402 licence_required with the reason",
               exact_402(r, team_mod.ADDRESS_REASON), f"{r.status_code} {r.text[:200]}")
        eq("nothing was written", len(store.list_desk(None, None)), before)
        r = await cx.post("/api/desk", json=q)
        ck("no `to` (the caller's own): 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/desk", json={**q, "to": "local"})
        ck("`to: local`, the system credential's own person: 201", r.status_code == 201,
           f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/keys", json={"name": "pat-key", "scopes": ["admin"], "person_id": pat})
        pat_key = r.json()["secret"]
        r = await cx.post("/api/desk", headers=H(pat_key), json={**q, "to": pat})
        ck("a key bound to Pat addressing Pat: 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/desk", headers=H(pat_key), json={**q, "to": "local"})
        ck("... addressing someone else: 402", exact_402(r, team_mod.ADDRESS_REASON), f"{r.status_code} {r.text[:200]}")

        print("== desk_target: a team that is enabled is not asked to refuse ==")
        real, team = team_mod.get, CountingTeam()
        team_mod.get = lambda: team
        try:
            r = await cx.post("/api/desk", json={**q, "to": pat})
        finally:
            team_mod.get = real
        ck("an enabled team: the desk item addressed to Pat is made", r.status_code == 201,
           f"{r.status_code} {r.text[:200]}")
        eq("... and it is addressed to Pat", r.json().get("to_person_id"), pat)

        print("== set_key_person and create_key: binding to someone already here never needs the licence ==")
        r = await cx.post("/api/keys", json={"name": "unbound", "scopes": ["admin"]})
        ck("a key with no person is made", r.status_code == 201, r.text[:200])
        kid, secret = r.json()["id"], r.json()["secret"]
        ck("... it is not Pat", (await whoami(secret)).get("id") != pat)
        r = await cx.put(f"/api/keys/{kid}/person", json={"person_id": pat})
        ck("PUT /api/keys/{kid}/person with Pat's id, no licence: 200", r.status_code == 200,
           f"{r.status_code} {r.text[:200]}")
        eq("GET /api/whoami with that key names Pat", (await whoami(secret)).get("id"), pat)
        r = await cx.post("/api/keys", json={"name": "pat-2", "scopes": ["admin"], "person_id": pat})
        ck("POST /api/keys with Pat's person_id, no licence: 201", r.status_code == 201,
           f"{r.status_code} {r.text[:200]}")
        eq("... and that key is Pat", (await whoami(r.json()["secret"])).get("id"), pat)
        r = await cx.put(f"/api/keys/{kid}/person", json={"person_id": "local"})
        ck("rebinding it to local: 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        eq("... it is local", (await whoami(secret)).get("id"), "local")
        r = await cx.put(f"/api/keys/{kid}/person", json={"person_id": "nobody"})
        eq("a person who is not on the cell is 404, not 402", r.status_code, 404)
        r = await cx.put(f"/api/keys/{kid}/person", json={"person": {"email": "fresh@example.com"}})
        ck("naming a new person by email still needs the licence: 402", exact_402(r),
           f"{r.status_code} {r.text[:200]}")
        eq("... and the key is still local", (await whoami(secret)).get("id"), "local")
        eq("no row was added", len(store.list_people()), 2)
        await cx.aclose()


asyncio.run(one_person_cell())
asyncio.run(two_person_cell())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
