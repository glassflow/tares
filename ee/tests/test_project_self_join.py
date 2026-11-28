# Tares enterprise licence, see ee/LICENSE
"""A person puts themself on a team project (TR-1720): `POST /api/projects/{uid}/join`.

A team project: the caller is added, once, as added by themself, and gets the project back. Someone
already on it (a member or the lead) gets the project and nothing changes. A private project: 403
`ask_lead`, naming its lead, and no row is written; its members and its lead get 200. Only a person
joins: a request from a station, the system credential, a key with no person, a read key and a
project key are all refused. Without a licence the answer is 402 `licence_required`. The lead-only
members route is as it was.

Joining by yourself does not give you the project's secrets: list, set, delete and use are refused
until the lead adds the person (or they hold a ticket of the project once it is private). It does not
make them a member of a project later made private either; the row stays and counts again if the
project goes back to team.

`GET /api/factory/projects/{uid}` says in `people` which of the two the console shows: `can_join`
(a team project this caller is not on and may join) or `ask_lead` (a private one they are not on).

In process, no ports: the licence is stored through PUT /api/licence.

Run: .venv/bin/python ee/tests/test_project_self_join.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-project-self-join-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-project-self-join"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"
os.environ["TARES_SECRETS_KEY"] = "a-long-random-string-for-this-cell-only"

import httpx
from sqlalchemy import select

from tares.session_store.tables import project_members as members_t
from tares.store import Store

# under the Postgres harness the data file's connection and the session store are one schema, so
# there is no second project_members table to look at
HARNESS = Store.__init__.__module__ != "tares.store"

P = F = 0
VALUE = "self-join-value-0123456789"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def client(app, headers):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers)


def own_folder(name):
    """A database path in a folder of its own: the session store is a file beside it, so two cells in
    one folder would share their people, keys and members."""
    os.makedirs(os.path.join(_TMP, name))
    return os.path.join(_TMP, name, "api")


async def licensed_cell():
    from tares.daemon import make_app
    app = make_app()
    keys = {"root": {"Authorization": f"Bearer {TOKEN}"}}
    ids = {}

    async def call(who, method, path, body=None, headers=None):
        async with client(app, {**keys[who], **(headers or {})}) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        store = app.state.store
        r = await call("root", "PUT", "/api/licence", {"key": licensed.test_licence()})
        assert r.status_code == 200, r.text
        for who, name, scopes in (("lead", "Lee", ["admin"]), ("kim", "Kim", ["read", "ingest"]),
                                  ("reader", "Rae", ["read"]), ("ana", "Ana", ["admin"])):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": scopes,
                "person": {"email": f"{who}@example.com", "name": name}})
            assert r.status_code < 300, r.text
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            ids[who] = r.json()["person"]["id"]
        r = await call("root", "POST", "/api/keys", {"name": "nobody", "scopes": ["admin"]})
        assert r.status_code < 300, r.text
        keys["nobody"] = {"Authorization": f"Bearer {r.json()['secret']}"}

        async def project(name):
            r = await call("lead", "POST", "/api/projects", {"template": "custom", "name": name,
                                                             "objects": [], "kind": "software_factory"})
            assert r.status_code < 300, r.text
            return r.json()["id"]

        def rows(uid):
            return [(m["person_id"], m["added_by"], str(m["added_at"])) for m in store.list_project_members(uid)]

        def detail(r):
            try:
                return r.json().get("detail")
            except ValueError:
                return r.text[:200]

        async def flags(who, uid):
            r = await call(who, "GET", f"/api/factory/projects/{uid}")
            assert r.status_code == 200, (who, r.status_code, r.text[:200])
            p = r.json()["people"]
            return p["can_join"], p["ask_lead"]

        async def home_members(who, uid):
            r = await call(who, "GET", "/api/home?projects=team")
            assert r.status_code == 200, (who, r.status_code, r.text[:200])
            return [m["id"] for p in r.json()["projects"] if p.get("id") == uid for m in p["members"]]

        team = await project("Atlas")
        join = f"/api/projects/{team}/join"

        def landed(uid):
            with store.sessions_db.read() as conn:
                return [tuple(r) for r in conn.execute(select(members_t.c.person_id, members_t.c.reason)
                        .where(members_t.c.project == uid).order_by(members_t.c.person_id))]

        stale = (team, ids["reader"], "manual")
        if not HARNESS:
            # a cell from before the move still has DuckDB's table, which nothing may read or write
            store.sc.execute("CREATE TABLE project_members (project VARCHAR, person_id VARCHAR, "
                             "added_by VARCHAR, added_at TIMESTAMP, reason VARCHAR, "
                             "PRIMARY KEY (project, person_id))")
            store.sc.execute("INSERT INTO project_members (project, person_id, reason) VALUES (?, ?, ?)",
                             list(stale))

        def left_behind():
            return [tuple(r) for r in store.sc.execute(
                "SELECT project, person_id, reason FROM project_members").fetchall()]

        print("== a team project ==")
        ck("someone not on it is offered to join", await flags("kim", team) == (True, False),
           str(await flags("kim", team)))
        ck("its lead is offered nothing", await flags("lead", team) == (False, False))
        ck("the system credential, no person, is offered nothing", await flags("root", team) == (False, False))
        ck("a key with no person is offered nothing", await flags("nobody", team) == (False, False))
        ck("a read key is offered no button", await flags("reader", team) == (False, False))
        # a ticket holder is listed with the members on the page, and is still not one
        store.create_ticket(team, "tares", "Add the header", assignee_person_id=ids["kim"])
        ck("someone holding a ticket in it, not added, is still offered to join",
           await flags("kim", team) == (True, False), str(await flags("kim", team)))

        print("== who may not join ==")
        for label, who, headers in (
                ("a station is refused", "kim", {"X-Tares-Station": "atlas-build-m1"}),
                ("the system credential is refused", "root", None),
                ("a key with no person is refused", "nobody", None)):
            r = await call(who, "POST", join, headers=headers)
            ck(label, r.status_code == 403 and "only a person can join" in str(detail(r)),
               f"{r.status_code} {r.text[:200]}")
        r = await call("reader", "POST", join)
        ck("a read key is refused", r.status_code == 403 and "'ingest' scope" in str(detail(r)),
           f"{r.status_code} {r.text[:200]}")
        r = await call("root", "POST", "/api/keys", {"name": "atlas-key", "project": team})
        assert r.status_code < 300, r.text
        keys["project"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("project", "POST", join)
        ck("a project key is refused", r.status_code == 403, f"{r.status_code} {r.text[:200]}")
        ck("nobody was added by any of those", rows(team) == [], str(rows(team)))

        print("== a member joins a team project ==")
        before = await home_members("kim", team)   # Home keeps this answer for a few seconds
        ck("Home lists nobody beside the lead yet", before == [], str(before))
        r = await call("kim", "POST", join)
        ck("join answers 200 with the project", r.status_code == 200 and r.json().get("id") == team,
           f"{r.status_code} {r.text[:200]}")
        after = await home_members("kim", team)
        ck("and their next read of Home lists them", after == [ids["kim"]], str(after))
        ck("the project answer lists them", ids["kim"] in [m["id"] for m in r.json().get("members", [])],
           str(r.json().get("members")))
        first = rows(team)
        ck("they are on it, added by themself",
           [(a, b) for a, b, _ in first] == [(ids["kim"], ids["kim"])], str(first))
        ck("and are offered nothing now", await flags("kim", team) == (False, False))
        ck("the row is in the session store's table", landed(team) == [(ids["kim"], "self")],
           str(landed(team)))
        if not HARNESS:
            ck("the table left in the data file is not written", left_behind() == [stale], str(left_behind()))
            ck("nor read: a person only in it is not on the project",
               not store.is_project_member(team, ids["reader"])
               and store.members_by_project([team])[team][0]["person_id"] == ids["kim"]
               and len(store.members_by_project([team])[team]) == 1, str(rows(team)))
        r = await call("kim", "POST", join)
        ck("joining again answers 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        ck("and changes nothing", rows(team) == first, str(rows(team)))
        r = await call("lead", "POST", join)
        ck("the lead joining their own project answers 200", r.status_code == 200, r.text[:200])
        ck("and writes no member row", rows(team) == first, str(rows(team)))
        r = await call("lead", "POST", "/api/projects/uc_nothing/join")
        ck("an unknown project is a 404", r.status_code == 404 and "unknown project" in str(detail(r)),
           f"{r.status_code} {r.text[:200]}")

        print("== the members route is still the lead's ==")
        # an admin passes the scope guard, so this refusal is the route's own
        r = await call("ana", "POST", join)
        assert r.status_code == 200, r.text
        r = await call("ana", "POST", f"/api/projects/{team}/members", {"person_id": ids["reader"]})
        ck("an admin who joined and is not the lead cannot add a person",
           r.status_code == 403 and detail(r) == "only the project lead can change this",
           f"{r.status_code} {r.text[:200]}")
        r = await call("ana", "DELETE", f"/api/projects/{team}/members/{ids['kim']}")
        ck("nor take one off",
           r.status_code == 403 and detail(r) == "only the project lead can change this"
           and ids["kim"] in [a for a, _, _ in rows(team)], f"{r.status_code} {r.text[:200]}")
        store.remove_project_member(team, ids["ana"])
        r = await call("lead", "DELETE", f"/api/projects/{team}/members/{ids['kim']}")
        ck("the lead still takes a person off", r.status_code == 200 and rows(team) == [], r.text[:200])

        print("== joining by yourself and the project's secrets ==")
        comet = await project("Comet")
        base = f"/api/projects/{comet}/secrets"
        r = await call("lead", "PUT", f"{base}/CELL_KEY", {"value": VALUE})
        assert r.status_code == 200, r.text

        def stored():
            return [(s["name"], s["value"]) for s in store.secret_rows(comet)]

        def reasons(uid):
            return {m["person_id"]: (m["reason"], m["added_by"]) for m in store.list_project_members(uid)}

        async def page(who, uid):
            r = await call(who, "GET", f"/api/factory/projects/{uid}")
            assert r.status_code == 200, (who, r.status_code, r.text[:200])
            return r.json()

        async def joined_mark(uid, pid):
            return [m.get("joined", False) for m in (await page("lead", uid))["people"]["members"]
                    if m["id"] == pid]

        async def refused_all(label, who, headers=None):
            """The four things a person does with secrets, each refused with the route's own words."""
            was = stored()
            for what, method, path, body in (("list", "GET", base, None),
                                             ("set", "PUT", f"{base}/CELL_KEY", {"value": "other-value-9876"}),
                                             ("delete", "DELETE", f"{base}/CELL_KEY", None),
                                             ("use", "POST", f"{base}/use", {})):
                r = await call(who, method, path, body, headers=headers)
                ck(f"{label}: {what} is refused",
                   r.status_code == 403 and "joined this project yourself" in str(detail(r))
                   and VALUE not in r.text and "CELL_KEY" not in r.text, f"{r.status_code} {r.text[:200]}")
            ck(f"{label}: the secret is as it was", stored() == was, "changed")

        r = await call("ana", "POST", f"/api/projects/{comet}/join")
        assert r.status_code == 200, r.text
        ck("the row says they joined by themselves", reasons(comet) == {ids["ana"]: ("self", ids["ana"])},
           str(reasons(comet)))
        ck("the project answer says the same",
           [m["reason"] for m in r.json()["members"] if m["id"] == ids["ana"]] == ["self"],
           str(r.json()["members"]))
        await refused_all("someone who joined", "ana")
        r = await call("ana", "POST", f"{base}/use", {}, headers={"X-Tares-Station": "comet-build-m1"})
        ck("their station gets no value either", r.status_code == 403 and VALUE not in r.text,
           f"{r.status_code} {r.text[:200]}")
        d = await page("ana", comet)
        ck("the page has no secrets for them", d.get("secrets") is None and "CELL_KEY" not in str(d),
           str(d.get("secrets")))
        ck("and marks them as joined", await joined_mark(comet, ids["ana"]) == [True])
        r = await call("ana", "POST", f"/api/projects/{comet}/tickets",
                       {"title": "Mine", "assignee": ids["ana"]})
        assert r.status_code == 201, r.text
        r = await call("ana", "GET", base)
        ck("taking a ticket of a team project does not give them the secrets", r.status_code == 403,
           f"{r.status_code} {r.text[:200]}")

        r = await call("lead", "POST", f"/api/projects/{comet}/members", {"person_id": ids["ana"]})
        assert r.status_code == 200, r.text
        ck("the lead adds them, and the row is the lead's now",
           reasons(comet) == {ids["ana"]: ("manual", ids["lead"])}, str(reasons(comet)))
        r = await call("ana", "GET", base)
        ck("then they list the secrets", r.status_code == 200
           and [s["name"] for s in r.json()["secrets"]] == ["CELL_KEY"], f"{r.status_code} {r.text[:200]}")
        r = await call("ana", "PUT", f"{base}/SECOND_KEY", {"value": "second-value-4567"})
        ck("set one", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        r = await call("ana", "POST", f"{base}/use", {"names": ["CELL_KEY"]})
        ck("use one", r.status_code == 200 and r.json().get("values") == {"CELL_KEY": VALUE},
           f"{r.status_code} {r.text[:200]}")
        r = await call("ana", "DELETE", f"{base}/SECOND_KEY")
        ck("and delete one", r.status_code == 200 and [n for n, _ in stored()] == ["CELL_KEY"],
           f"{r.status_code} {r.text[:200]}")
        d = await page("ana", comet)
        ck("the page has them, and no joined mark",
           d.get("secrets") is not None and await joined_mark(comet, ids["ana"]) == [False],
           str(d.get("secrets")))
        was = reasons(comet)
        r = await call("ana", "POST", f"/api/projects/{comet}/join")
        ck("joining once added changes nothing", r.status_code == 200 and reasons(comet) == was,
           str(reasons(comet)))
        ck("and the store keeps an added person added",
           store.add_project_member(comet, ids["ana"], ids["ana"], reason="self") is False
           and reasons(comet) == was, str(reasons(comet)))

        print("== joining by yourself, then the project goes private ==")
        # Kim again, with an admin key, so a refusal here is about membership and not about the
        # key's scope
        r = await call("root", "POST", "/api/keys", {
            "name": "kim-admin", "scopes": ["admin"], "person": {"email": "kim@example.com", "name": "Kim"}})
        assert r.status_code < 300 and r.json()["person"]["id"] == ids["kim"], r.text
        keys["kim_admin"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("kim_admin", "POST", f"/api/projects/{comet}/join")
        assert r.status_code == 200, r.text
        r = await call("lead", "PATCH", f"/api/projects/{comet}", {"scope": "private"})
        assert r.status_code == 200, r.text

        async def outsider(label):
            for what, path in (("the project", f"/api/projects/{comet}"),
                               ("its page", f"/api/factory/projects/{comet}"),
                               ("its tickets", f"/api/projects/{comet}/tickets"),
                               ("its secrets", base)):
                r = await call("kim_admin", "GET", path)
                ck(f"{label}: {what} is not there", r.status_code == 404 and VALUE not in r.text,
                   f"{r.status_code} {r.text[:200]}")
            r = await call("kim_admin", "POST", f"{base}/use", {})
            ck(f"{label}: no value to use", r.status_code == 404 and VALUE not in r.text,
               f"{r.status_code} {r.text[:200]}")
            ck(f"{label}: not a member to any read",
               not store.is_project_member(comet, ids["kim"]) and ids["kim"] not in reasons(comet)
               and ids["kim"] not in [m["person_id"] for m in
                                      store.members_by_project([comet]).get(comet, [])],
               str(reasons(comet)))
            r = await call("kim_admin", "POST", f"/api/projects/{comet}/join")
            ck(f"{label}: joining says to ask the lead",
               r.status_code == 403 and isinstance(detail(r), dict) and detail(r).get("code") == "ask_lead",
               f"{r.status_code} {r.text[:200]}")

        await outsider("after it went private")
        ck("the lead's list of members leaves them out",
           await joined_mark(comet, ids["kim"]) == [] and ids["ana"] in reasons(comet))
        r = await call("ana", "POST", f"/api/projects/{comet}/tickets",
                       {"title": "For Kim", "assignee": ids["kim"]})
        ck("another member cannot bring them in with a ticket", r.status_code == 403
           and ids["kim"] not in reasons(comet), f"{r.status_code} {r.text[:200]}")
        r = await call("lead", "PATCH", f"/api/projects/{comet}", {"scope": "team"})
        assert r.status_code == 200, r.text
        ck("back to team, they are on it again, still as joined",
           reasons(comet).get(ids["kim"]) == ("self", ids["kim"]), str(reasons(comet)))
        r = await call("kim_admin", "GET", base)
        ck("and still without the secrets", r.status_code == 403 and VALUE not in r.text,
           f"{r.status_code} {r.text[:200]}")
        r = await call("lead", "PATCH", f"/api/projects/{comet}", {"scope": "private"})
        assert r.status_code == 200, r.text
        r = await call("lead", "POST", f"/api/projects/{comet}/tickets",
                       {"title": "For Kim", "assignee": ids["kim"]})
        assert r.status_code == 201, r.text
        ck("the lead assigning them a ticket of it adds them",
           reasons(comet).get(ids["kim"]) == ("ticket", ids["lead"]), str(reasons(comet)))
        r = await call("kim_admin", "POST", f"{base}/use", {})
        ck("and then they use the secrets",
           r.status_code == 200 and r.json().get("values") == {"CELL_KEY": VALUE},
           f"{r.status_code} {r.text[:200]}")
        r = await call("lead", "DELETE", f"/api/projects/{comet}/members/{ids['kim']}")
        ck("the lead takes them off again", r.status_code == 200 and ids["kim"] not in reasons(comet),
           r.text[:200])

        print("== a private project ==")
        vault = await project("Vault")
        r = await call("lead", "POST", f"/api/projects/{vault}/members", {"person_id": ids["ana"]})
        assert r.status_code == 200, r.text
        r = await call("lead", "PATCH", f"/api/projects/{vault}", {"scope": "private"})
        assert r.status_code == 200, r.text
        join = f"/api/projects/{vault}/join"
        before = rows(vault)
        r = await call("kim", "POST", join)
        d = detail(r)
        ck("someone not on it gets 403 ask_lead",
           r.status_code == 403 and isinstance(d, dict) and d.get("code") == "ask_lead",
           f"{r.status_code} {r.text[:200]}")
        ck("which names the lead",
           isinstance(d, dict) and d.get("detail") == "This project is private. Ask its lead, Lee, to add you.",
           str(d))
        ck("and says nothing else about the project", "Vault" not in r.text and vault not in r.text
           and "Ana" not in r.text and "lee@example.com" not in r.text, r.text[:300])
        r = await call("kim", "GET", f"/api/projects/{vault}")
        ck("they still cannot open it", r.status_code == 404, f"{r.status_code} {r.text[:200]}")
        for label, who, headers in (
                ("a station is told nothing about it", "kim", {"X-Tares-Station": "atlas-build-m1"}),
                ("a key with no person is told nothing about it", "nobody", None)):
            r = await call(who, "POST", join, headers=headers)
            ck(label, r.status_code == 403 and "Lee" not in r.text and "ask_lead" not in r.text,
               f"{r.status_code} {r.text[:200]}")
        ck("nobody was added", rows(vault) == before, str(rows(vault)))
        r = await call("ana", "POST", join)
        ck("a member of it gets 200", r.status_code == 200 and r.json().get("id") == vault, r.text[:200])
        ck("and nothing changes", rows(vault) == before, str(rows(vault)))
        r = await call("lead", "POST", join)
        ck("its lead gets 200", r.status_code == 200 and rows(vault) == before, r.text[:200])
        store.set_project_lead(vault, None)
        r = await call("kim", "POST", join)
        d = detail(r)
        ck("with no lead, it says an admin has to give it one",
           r.status_code == 403 and isinstance(d, dict) and d.get("code") == "ask_lead"
           and d.get("detail") == "This project is private. It has no lead: ask an admin to give it one.",
           f"{r.status_code} {r.text[:200]}")

        print("== a joiner's project step ==")
        r = await call("root", "PUT", "/api/crew/settings", {"autonomy": "L3-review"})
        assert r.status_code == 200, r.text
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines = "".join(json.dumps(
            {"type": "session_station", "sessionId": f"s-lee-{role}", "cwd": "/w/crew", "timestamp": now,
             "station": f"lee-{role}", "role": role, "parent": "", "factory_station": f"lee-{role}",
             "factory_crew": "lee", "crew": "lee"}) + "\n" for role in ("orchestrator", "reviewer"))
        async with client(app, {**keys["lead"], "Content-Type": "application/x-ndjson"}) as cx:
            r = await cx.post("/ingest/claude_code", content=lines)
        assert r.status_code < 300, r.text
        dune = await project("Dune")

        async def project_step():
            r = await call("kim", "GET", "/api/home?projects=team")
            assert r.status_code == 200, (r.status_code, r.text[:200])
            return {s["key"]: s["done"] for s in r.json()["start"]["steps"]}.get("project")

        from tares import factory_home
        factory_home.clear_cache()
        first_read = await project_step()   # Home keeps this answer for a few seconds
        ck("someone joining a factory, on no project: the step is open", first_read is False,
           str(first_read))
        r = await call("kim", "POST", f"/api/projects/{dune}/join")
        assert r.status_code == 200, r.text
        ck("joining ticks it on their next read", await project_step() is True)
        ck("from a row in the session store's table", landed(dune) == [(ids["kim"], "self")],
           str(landed(dune)))
        r = await call("lead", "PATCH", f"/api/projects/{dune}", {"scope": "private"})
        assert r.status_code == 200, r.text
        factory_home.clear_cache()
        ck("once the project is private, joining by yourself no longer ticks it",
           await project_step() is False)

        print("== no licence ==")
        r = await call("root", "DELETE", "/api/licence")
        assert r.status_code in (200, 204), r.text
        r = await call("kim", "POST", f"/api/projects/{team}/join")
        ck("join answers 402 licence_required",
           r.status_code == 402 and r.json().get("code") == "licence_required",
           f"{r.status_code} {r.text[:200]}")
        ck("and adds nobody", rows(team) == [], str(rows(team)))
        ck("the page offers nothing", await flags("kim", team) == (False, False))


async def open_cell():
    """Auth off: whoever looks is the person `local`, who sees every project, so this is where the
    project page of a private project someone is not on can be read."""
    from tares import daemon
    daemon.AUTH_TOKEN = ""
    daemon.DATA_DIR = own_folder("open")
    app = daemon.make_app()

    async def call(method, path, body=None):
        async with client(app, {}) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        store = app.state.store
        r = await call("PUT", "/api/licence", {"key": licensed.test_licence()})
        assert r.status_code == 200, r.text
        r = await call("POST", "/api/people", {"name": "Lee", "email": "lee@example.com"})
        assert r.status_code < 300, r.text
        lee = r.json()["id"]
        r = await call("POST", "/api/projects", {"template": "custom", "name": "Atlas", "objects": [],
                                                 "kind": "software_factory"})
        assert r.status_code < 300, r.text
        uid = r.json()["id"]
        r = await call("PATCH", f"/api/projects/{uid}", {"lead_person_id": lee})
        assert r.status_code == 200, r.text
        store.remove_project_member(uid, "local")   # the one who handed it over stays on it

        async def flags():
            r = await call("GET", f"/api/factory/projects/{uid}")
            assert r.status_code == 200, (r.status_code, r.text[:200])
            return r.json()["people"]["can_join"], r.json()["people"]["ask_lead"]

        print("== auth off ==")
        ck("a team project the person is not on offers to join", await flags() == (True, False))
        store.set_project_scope(uid, "private")
        ck("a private one says to ask the lead", await flags() == (False, True), str(await flags()))
        r = await call("POST", f"/api/projects/{uid}/join")
        ck("and the route says the same", r.status_code == 403 and r.json()["detail"]["code"] == "ask_lead",
           f"{r.status_code} {r.text[:200]}")
        store.set_project_scope(uid, "team")
        r = await call("POST", f"/api/projects/{uid}/join")
        ck("the person `local` joins a team project", r.status_code == 200
           and store.is_project_member(uid, "local"), f"{r.status_code} {r.text[:200]}")
        block = (await call("GET", f"/api/factory/projects/{uid}")).json()
        mine = [m for m in block["people"]["members"] if m["id"] == "local"]
        ck("the page does not mark `local` as waiting for the lead", len(mine) == 1
           and "joined" not in mine[0], str(block["people"]["members"]))
        ck("`local` has the project's secrets", block["secrets"] is not None
           and (await call("GET", f"/api/projects/{uid}/secrets")).status_code == 200,
           str(block["secrets"]))


async def single_cell():
    """Auth on, and the only person is `local`: a key nobody has bound acts as them. They have a
    project's secrets however they came to be on it, so the page doesn't mark them as waiting."""
    from tares import daemon
    daemon.AUTH_TOKEN = TOKEN
    daemon.DATA_DIR = own_folder("single")
    app = daemon.make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}

    async def call(headers, method, path, body=None):
        async with client(app, headers) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        r = await call(root, "PUT", "/api/licence", {"key": licensed.test_licence()})
        assert r.status_code == 200, r.text
        r = await call(root, "POST", "/api/projects", {"template": "custom", "name": "Atlas", "objects": [],
                                                       "kind": "software_factory"})
        assert r.status_code < 300, r.text
        uid = r.json()["id"]
        r = await call(root, "POST", "/api/keys", {"name": "mine", "scopes": ["admin"]})
        assert r.status_code < 300, r.text
        mine = {"Authorization": f"Bearer {r.json()['secret']}"}

        print("== auth on, one person ==")
        r = await call(mine, "POST", f"/api/projects/{uid}/join")
        ck("`local` joins with a key nobody has bound", r.status_code == 200
           and app.state.store.is_project_member(uid, "local"), f"{r.status_code} {r.text[:200]}")
        r = await call(mine, "GET", f"/api/projects/{uid}/secrets")
        ck("and has its secrets with that key", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        people = (await call(mine, "GET", f"/api/factory/projects/{uid}")).json()["people"]
        row = [m for m in people["members"] if m["id"] == "local"]
        ck("and the page does not mark them as waiting for the lead",
           len(row) == 1 and "joined" not in row[0], str(people["members"]))


async def main():
    await licensed_cell()
    await open_cell()
    await single_cell()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
