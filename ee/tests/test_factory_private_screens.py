# Tares enterprise licence, see ee/LICENSE
"""A private project stays out of the factory screens (TR-892).

Lee leads Vault, a private factory project. Kim and Alex are teammates with admin keys who are not
on it. The screens answer for the person asking: the two single-project routes (the project page and
the ticket page) answer 404 for Vault exactly as they do for a project that does not exist, and
every list the screens return (projects, tickets, reviews, docs, crew, settings, home) leaves Vault
out, its tickets, its pull requests, its documents, its builder and its people's grants. Lee, the
cell's own key and a key for Vault still see all of it. With auth off everything shows.

Run: .venv/bin/python ee/tests/test_factory_private_screens.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-factory-private-screens-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-screens"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory as factory_mod
from tares import factory_home as fh
from tares.store import now_utc

P = F = 0

SECRET = "Zephyrvault"          # a word only the private project carries
LEAKS = (SECRET, "Rotate the zephyrvault keys", "Zephyrvault launch plan")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def leaks(body, *extra):
    """Every place a body names the private project: its words, and the ids given."""
    text = json.dumps(body, default=str).lower()
    return [w for w in (*LEAKS, *extra) if w.lower() in text]


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys, pid = {}, {}

    async def call(who, method, path, body=None, **params):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body, params=params or None)

    async def get(who, path, **params):
        fh._cache.clear()
        return await call(who, "GET", path, **params)

    async with app.router.lifespan_context(app):
        for who in ("alex", "kim", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-ps@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        # an open project everyone sees, and Vault, private, led by Lee
        r = await call("kim", "POST", "/api/projects", {"template": "custom", "name": "Atlas",
                                                        "objects": [], "kind": "software_factory"})
        atlas = r.json()["id"]
        r = await call("lee", "POST", "/api/projects", {
            "template": "custom", "name": "Zephyrvault project", "objects": [],
            "kind": "software_factory", "goal": "Zephyrvault goal"})
        assert r.status_code in (200, 201), r.text
        vault = r.json()["id"]
        r = await call("lee", "PATCH", f"/api/projects/{vault}", {"scope": "private"})
        assert r.status_code < 300 and store.get_project(vault)["scope"] == "private", r.text
        store.set_project_status(vault, "active") if hasattr(store, "set_project_status") else None

        t_atlas = store.create_ticket(atlas, "tares", "Atlas page", repo="example-org/service-a")
        store.update_ticket(t_atlas, stage="review", pr={
            "url": "https://github.com/example-org/service-a/pull/11", "number": 11,
            "repo": "example-org/service-a", "head": "aaaa111", "ci": "success", "title": "PR: Atlas page"})
        t_vault = store.create_ticket(vault, "tares", "Rotate the zephyrvault keys",
                                      repo="example-org/service-a")
        store.update_ticket(t_vault, stage="review", pr={
            "url": "https://github.com/example-org/service-a/pull/12", "number": 12,
            "repo": "example-org/service-a", "head": "bbbb222", "ci": "success",
            "title": "PR: Rotate the zephyrvault keys"})
        store.add_review(vault, t_vault, None, "bbbb222", "pass", "ran the tests", None, "crew-reviewer",
                         [], {}, factory_mod.finding_kind, now_utc())
        for uid in (atlas, vault):
            store.create_doc(uid, "memory", "Memory", "# Memory\n\n- a line\n" if uid == atlas
                             else "# Memory\n\n- Zephyrvault launch plan\n")
        dk_vault = store.add_desk_item(vault, "Zephyrvault launch plan, go?", "", [], "yes", "it is ready", "", [],
                            False, "crew-orchestrator", [], now_utc() - timedelta(hours=1),
                            to_person_id=pid["kim"])
        dk_atlas = store.add_desk_item(atlas, "Which colour for Atlas?", "", ["blue"], "blue", "it reads", "", [],
                            False, "crew-orchestrator", [], now_utc() - timedelta(hours=1),
                            to_person_id=pid["kim"])

        # both projects were handed to the crew, so the crew page's standup line names them; and the
        # reviewer offered a rule from each
        for uid in (atlas, vault):
            store.set_handover(uid, [{"path": "/w/crew", "name": "example-org/service-a"}], None, None,
                               now_utc())
        store.upsert_rule_proposal("example-org/service-a", "missing-tests", vault, "project",
                                   "Zephyrvault: always rotate keys", [], now_utc())
        store.upsert_rule_proposal("example-org/service-a", "no-docs", atlas, "project",
                                   "Atlas: write the docs page", [], now_utc())

        # Lee's crew has a builder on Vault
        r = await call("root", "POST", "/api/sources", {"name": "claude_code", "connector": "claude_code",
                                                         "poll": "10s", "config": {"push": True}})
        stamp = lambda m: (now_utc() - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        lines = [{"type": "session_station", "sessionId": "l1", "cwd": "/w/crew", "timestamp": stamp(3),
                  "station": "zephyrvault-build-m1", "role": "builder", "parent": "",
                  "factory_station": "zephyrvault-build-m1", "factory_crew": "l", "crew": "l",
                  "factory_machine": "m2", "machine": "m2", "tares_project": vault},
                 {"type": "session_state", "sessionId": "l1", "cwd": "/w/crew", "state": "working",
                  "reason": "", "timestamp": stamp(3)},
                 # a builder on Atlas that also holds the ticket of Vault, in review
                 {"type": "session_station", "sessionId": "l2", "cwd": "/w/crew", "timestamp": stamp(2),
                  "station": "atlas-build-m1", "role": "builder", "parent": "",
                  "factory_station": "atlas-build-m1", "factory_crew": "l", "crew": "l",
                  "factory_machine": "m2", "machine": "m2", "tares_project": atlas},
                 {"type": "session_state", "sessionId": "l2", "cwd": "/w/crew", "state": "idle",
                  "reason": "", "timestamp": stamp(2)},
                 # the crew's orchestrator, idle
                 {"type": "session_station", "sessionId": "l3", "cwd": "/w/crew", "timestamp": stamp(1),
                  "station": "crew-orchestrator", "role": "orchestrator", "parent": "",
                  "factory_station": "crew-orchestrator", "factory_crew": "l", "crew": "l",
                  "factory_machine": "m2", "machine": "m2"},
                 {"type": "session_state", "sessionId": "l3", "cwd": "/w/crew", "state": "idle",
                  "reason": "", "timestamp": stamp(1)}]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={**keys["lee"], "Content-Type": "application/x-ndjson"}) as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n")

        store.update_ticket(t_vault, holder="atlas-build-m1")
        # a grant given on Vault, in the project's own grants document
        store.create_doc(vault, "grants", "Grants", '# Grants\n\n- "Zephyrvault may read the status of a '
                         'commit" (2026-11-01)\n')
        # Vault's desk question was answered once; a line in the grants of Atlas points at that answer
        d_label = f"D{dk_vault['number']} A"
        store.add_decision(vault, dk_vault["id"], "Zephyrvault launch plan, go?", d_label, "yes", False,
                           "project", False, None, True, [], now_utc())
        store.create_doc(atlas, "grants", "Grants", '# Grants\n\n- "Atlas may read the status of a '
                         f'commit" (2026-11-01)\n- "{d_label}" (2026-11-02)\n')

        lists = ["/api/factory/projects", "/api/factory/tickets", "/api/factory/reviews",
                 "/api/factory/docs", "/api/factory/crew", "/api/factory/settings", "/api/home"]
        singles = [f"/api/factory/projects/{vault}", f"/api/factory/projects/{vault}/tickets/{t_vault}",
                   f"/api/factory/projects/{vault}/tickets/T1"]

        print("== a teammate who is not on Vault ==")
        for who in ("kim", "alex"):
            missing = await get(who, "/api/factory/projects/uc_nothere")
            for path in singles[:2]:
                r = await get(who, path)
                ck(f"{who.capitalize()}: {path.split('/api/factory/')[1]} answers 404", r.status_code == 404,
                   (r.status_code, r.text[:120]))
                ck(f"{who.capitalize()}: the same words as for a project that is not there",
                   r.status_code == 404 and missing.status_code == 404
                   and r.json().get("detail", "").replace(vault, "X") == missing.json().get(
                       "detail", "").replace("uc_nothere", "X") and not leaks(r.json()), r.text[:160])
            for path in lists:
                r = await get(who, path)
                ck(f"{who.capitalize()}: {path} answers", r.status_code == 200, (r.status_code, r.text[:120]))
                found = leaks(r.json(), vault, t_vault, "zephyrvault-build-m1") if r.status_code == 200 else []
                ck(f"{who.capitalize()}: {path} leaves Vault out", not found, found)
            r = await get(who, "/api/factory/projects")
            names = [p.get("name") for p in r.json().get("projects", [])] if r.status_code == 200 else []
            ck(f"{who.capitalize()}: the project list still holds Atlas", "Atlas" in names, names)
            r = await get(who, f"/api/factory/projects/{atlas}")
            ck(f"{who.capitalize()}: Atlas opens", r.status_code == 200, r.text[:120])
            r = await get(who, "/api/factory/tickets", project=vault)
            ck(f"{who.capitalize()}: the tickets list filtered to Vault gives nothing of it",
               not leaks(r.json(), t_vault) and not [t for t in r.json().get("tickets", [])], r.text[:160])
            r = await get(who, "/api/factory/reviews", project=vault)
            ck(f"{who.capitalize()}: the reviews list filtered to Vault gives nothing of it",
               not leaks(r.json(), t_vault) and r.json().get("project") is None
               and not r.json().get("waiting_for_you") and not r.json().get("with_the_crew"), r.text[:160])
            # the doc of Vault, asked by id
            vdoc = next(d["id"] for d in store.list_docs(vault) if d["kind"] == "memory")
            r = await get(who, f"/api/factory/docs/{vdoc}")
            ck(f"{who.capitalize()}: Vault's memory by id answers 404", r.status_code == 404,
               (r.status_code, r.text[:120]))

        print("== the pieces under the screens ==")
        fx = app.state.factory if hasattr(app.state, "factory") else None
        items = fh.waiting_items(store, fx, now_utc())
        ck("the waiting items hold the item of Vault, as the cell's own key reads it",
           any((i.get("project") or {}).get("id") == vault for i in items), [i["title"] for i in items])
        ck("and leave it out when told Vault is hidden", not any(
            (i.get("project") or {}).get("id") == vault for i in fh.waiting_items(store, fx, now_utc(), {vault})))
        r = await get("lee", "/api/factory/settings")
        ck("Lee reads the grant of Vault on the settings page", "zephyrvault" in json.dumps(r.json()).lower(),
           r.text[:200])
        ck("Lee reads the question behind the grant that points at the answer on Vault",
           "launch plan" in json.dumps(r.json()).lower(), r.text[:300])
        r = await get("kim", "/api/factory/settings")
        g = next((g for g in r.json().get("crew", {}).get("grants", []) if str(g.get("text")).startswith("D")), None)
        ck("Kim sees the grant that points at the answer on Vault, without the question", bool(g)
           and not g.get("question") and not leaks(r.json()), g)
        r = await get("lee", "/api/factory/crew")
        ck("Lee sees the builder of Vault on the crew page", "zephyrvault-build-m1" in json.dumps(r.json()),
           r.text[:300])
        ck("and what the Atlas builder waits for is the ticket of Vault", "zephyrvault" in json.dumps(r.json()).lower(),
           r.text[:300])

        print("== the people who can see Vault ==")
        vdoc = next(d["id"] for d in store.list_docs(vault) if d["kind"] == "memory")
        for who in ("lee", "root"):
            for path in singles[:2]:
                r = await get(who, path)
                ck(f"{who}: {path.split('/api/factory/')[1]} answers", r.status_code == 200,
                   (r.status_code, r.text[:120]))
            r = await get(who, "/api/factory/projects")
            ck(f"{who}: the project list holds Vault", SECRET.lower() in json.dumps(r.json()).lower(), r.text[:120])
            r = await get(who, "/api/factory/tickets")
            ck(f"{who}: the ticket list holds its ticket", "zephyrvault" in json.dumps(r.json()).lower(), r.text[:120])
            r = await get(who, "/api/factory/reviews")
            ck(f"{who}: the reviews list holds its pull request", "zephyrvault" in json.dumps(r.json()).lower(),
               r.text[:120])
            r = await get(who, f"/api/factory/docs/{vdoc}")
            ck(f"{who}: its memory opens", r.status_code == 200, (r.status_code, r.text[:120]))

        print("== a member added to Vault ==")
        store.add_project_member(vault, pid["alex"])
        r = await get("alex", f"/api/factory/projects/{vault}")
        ck("Alex, once a member, opens Vault", r.status_code == 200, r.text[:120])
        r = await get("alex", "/api/factory/tickets")
        ck("and sees its tickets", "zephyrvault" in json.dumps(r.json()).lower(), r.text[:120])
        store.remove_project_member(vault, pid["alex"])
        r = await get("alex", f"/api/factory/projects/{vault}")
        ck("removed again, Alex gets 404", r.status_code == 404, r.status_code)

        print("== an open project ==")
        store.set_project_scope(vault, "team")
        r = await get("kim", f"/api/factory/projects/{vault}")
        ck("made a team project, Kim opens it", r.status_code == 200, (r.status_code, r.text[:120]))
        store.set_project_scope(vault, "private")

        print("== only the question of Vault is open ==")
        store.close_desk_item(dk_atlas["id"], "answered", None, now_utc())

        def find(body, key):
            if isinstance(body, dict):
                if key in body:
                    return body[key]
                for v in body.values():
                    got = find(v, key)
                    if got is not None:
                        return got
            elif isinstance(body, list):
                for v in body:
                    got = find(v, key)
                    if got is not None:
                        return got
            return None

        r = await get("kim", "/api/factory/crew")
        m = r.json().get("talk_to")
        ck("Kim: the orchestrator reports an empty desk, the question of Vault is not hers",
           bool(m) and m.get("doing") == "Desk empty.", m)
        r = await get("kim", "/api/home")
        ck("Kim: the home page counts no question on the desk", find(r.json(), "desk_open") == 0,
           find(r.json(), "desk_open"))
        r = await get("lee", "/api/factory/crew")
        m = r.json().get("talk_to")
        ck("Lee: the orchestrator does not report an empty desk, the question of Vault is open",
           bool(m) and m.get("doing") != "Desk empty.", m)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
