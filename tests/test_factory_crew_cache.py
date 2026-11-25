"""The Crew page is kept for a few seconds per caller (TR-1630), in the same cache as Home
(factory_home.cached).

A repeat read by one caller builds the page once and answers the same body; another credential, a
window gone by or a clear builds it again; Home and Crew never answer with each other's body; a
caller who must not see a private project never gets the body of one who does; and a write a person
makes in the console shows on the very next read, while the crew's machine writes wait for the
window.

Run: .venv/bin/python tests/test_factory_crew_cache.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-crew-cache-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-cache"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory_home as fh
from tares.screens import factory_crew as fc

P = F = 0
BUILDS = {"crew": 0, "home": 0}
OVERTAKE = [False]      # a save lands while the next Crew page is still building


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def count_builds():
    real_plan, real_home = fc._plan, fh.home

    def plan(*a, **k):                  # the Crew page calls it once per build
        BUILDS["crew"] += 1
        if OVERTAKE[0]:
            fh.clear_cache()
        return real_plan(*a, **k)

    def home(*a, **k):
        BUILDS["home"] += 1
        return real_home(*a, **k)

    # the Home page calls factory_home._plan, which is not the name patched here
    fc._plan, fh.home = plan, home


def age_cache():
    """Make every kept body older than the window."""
    for k, (t, v) in list(fh._cache.items()):
        fh._cache[k] = (t - fh.CACHE_SECONDS - 1, v)


def ts(seconds_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station(sid, name, role, project="", state="working"):
    first = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(60),
             "station": name, "role": role, "parent": "", "factory_station": name}
    if project:
        first["tares_project"] = project
    return [first, {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(5),
                    "state": state, "reason": ""}]


async def main():
    import tares.daemon as daemon_mod
    count_builds()
    app = daemon_mod.make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}

    def mk(headers=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=headers or root)

    async def get(path, headers=None):
        async with mk(headers) as cx:
            return await cx.get(path)

    async def page(headers=None):
        r = await get("/api/factory/crew", headers)
        assert r.status_code == 200, r.text
        return r.json()

    async def built(headers=None):
        """(the page, how many times it was built for this read)"""
        n = BUILDS["crew"]
        body = await page(headers)
        return body, BUILDS["crew"] - n

    async def send(method, path, body=None, headers=None):
        async with mk(headers) as cx:
            r = await cx.request(method, path, json=body)
        assert r.status_code < 300, f"{method} {path}: {r.status_code} {r.text}"
        return r.json()

    async def ingest(lines):
        async with mk() as cx:
            r = await cx.post("/ingest/claude_code",
                              content="\n".join(json.dumps(x) for x in lines) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
        assert r.status_code < 300, r.text

    async with app.router.lifespan_context(app):
        print("== one caller, inside the window ==")
        fh.clear_cache()
        a, n1 = await built()
        b, n2 = await built()
        ck("a cell with no crew: the first read builds, the second does not", (n1, n2) == (1, 0), (n1, n2))
        ck("and both answer the same body", a == b and a["crews"] == [] and a["summary"] == "Not started", a)

        print("== another caller ==")
        k1 = {"Authorization": "Bearer " + (await send("POST", "/api/keys", {"name": "laptop", "scopes": ["read"]}))["secret"]}
        k2 = {"Authorization": "Bearer " + (await send("POST", "/api/keys", {"name": "laptop", "scopes": ["read"]}))["secret"]}
        _, n1 = await built(k1)
        _, n2 = await built(k1)
        _, n3 = await built(k2)
        ck("a key builds its own page once, though the cell's credential has one kept",
           (n1, n2) == (1, 0), (n1, n2))
        ck("a second key of the same name builds its own", n3 == 1, n3)

        print("== the window, a clear, a clear during a build ==")
        await page()
        age_cache()
        _, n1 = await built()
        ck("after the window the page is built again", n1 == 1, n1)
        fh.clear_cache()
        _, n1 = await built()
        ck("after a clear the page is built again", n1 == 1, n1)
        fh.clear_cache()
        OVERTAKE[0] = True
        try:
            await page()
        finally:
            OVERTAKE[0] = False
        _, n1 = await built()
        ck("a build that a clear overtook is not kept", n1 == 1, n1)

        print("== Home and Crew ==")
        fh.clear_cache()
        homes = BUILDS["home"]
        h1 = (await get("/api/home")).json()
        c1 = await page()
        h2 = (await get("/api/home")).json()
        c2 = await page()
        ck("each answers its own body, the kept one too",
           h1 == h2 and c1 == c2 and "waiting_total" in h2 and "crews" not in h2
           and "crews" in c2 and "waiting_total" not in c2, (sorted(h2), sorted(c2)))
        ck("and Home was built once", BUILDS["home"] - homes == 1, BUILDS["home"] - homes)
        fh.clear_cache()
        c1 = await page()
        h1 = (await get("/api/home")).json()
        ck("Crew first, then Home: the same", "crews" in c1 and "crews" not in h1, sorted(h1))

        print("== who may see a private project ==")
        secret = (await send("POST", "/api/projects", {"template": "custom", "name": "project-secret"}))["id"]
        store.set_project_kind(secret, "software_factory")
        store.set_project_scope(secret, "private")
        await ingest(station("s1", "secret-build", "builder", secret))
        fh.clear_cache()
        seen = json.dumps(await page())
        public = {**root, "x-tares-viewer": "public"}
        narrow, n1 = await built(public)
        ck("the cell's credential reading as the public never gets the full body",
           "project-secret" in seen and "project-secret" not in json.dumps(narrow) and n1 == 1, (n1, narrow["builders"]))
        like = {"Authorization": "Bearer " + (await send("POST", "/api/keys", {"name": "auth token (env)",
                                                                               "scopes": ["read"]}))["secret"]}
        # an API key is not the cell's own credential, so it does not see what that one sees
        await page()
        theirs, n1 = await built(like)
        ck("a key named like the cell's credential never gets that credential's body",
           "project-secret" not in json.dumps(theirs) and n1 == 1, (n1, theirs["builders"]))

        def asked(scopes, person="p1"):
            cred = {"id": "key:1", "name": "k", "person": {"id": person}}
            return SimpleNamespace(state=SimpleNamespace(credential=cred, scopes=scopes),
                                   headers={}, query_params={})
        ck("the same credential as admin and not as admin has two kept bodies",
           fh._cache_key(store, asked(["read"]), "crew") != fh._cache_key(store, asked(["admin", "read"]), "crew"))
        ck("and one credential bound to another person has its own",
           fh._cache_key(store, asked(["read"]), "crew") != fh._cache_key(store, asked(["read"], "p2"), "crew"))
        pk = await send("POST", "/api/keys", {"name": "one project", "project": secret})
        await page()
        r = await get("/api/factory/crew", {"Authorization": f"Bearer {pk['secret']}"})
        ck("a project key may not read the page, kept or not", r.status_code == 403, r.status_code)
        r = await get("/api/factory/crew", {"Authorization": "Bearer nope"})
        ck("nor may an unknown token", r.status_code == 401, r.status_code)

        print("== what a person changes in the console shows on the next read ==")

        async def after(label, write, shows):
            fh.clear_cache()
            before = await page()
            await write()
            now, n = await built()
            ck(label, n == 1 and shows(before, now), (n, now.get("how_far"), now.get("builders"),
                                                      now.get("rule_proposals")))

        store.set_project_scope(secret, "team")
        px = (await send("POST", "/api/projects", {"template": "custom", "name": "project-x"}))["id"]
        store.set_project_kind(px, "software_factory")
        await ingest(station("o1", "crew-orchestrator", "orchestrator") + station("b1", "x-build", "builder", px))
        for i, text in enumerate(("Check the first thing.", "Check the second thing.")):
            store.upsert_rule_proposal("example-org/docs-site", f"finding {i}", px, "project", text,
                                       ["T1"], datetime.now(timezone.utc) - timedelta(hours=1))
        fh.clear_cache()
        first, second = [p["id"] for p in (await page())["rule_proposals"]]

        await after("the crew's settings", lambda: send("PUT", "/api/crew/settings", {"autonomy": "L4-ship"}),
                    lambda a, b: a["how_far"]["text"] != b["how_far"]["text"])
        await after("a rule rejected", lambda: send("POST", f"/api/rule-proposals/{first}/reject", {}),
                    lambda a, b: len(a["rule_proposals"]) == 2 and [p["id"] for p in b["rule_proposals"]] == [second])
        await after("a rule accepted", lambda: send("POST", f"/api/rule-proposals/{second}/accept", {}),
                    lambda a, b: b["rule_proposals"] == [])
        await after("a station marked ended", lambda: send("POST", "/api/crew/crew-orchestrator/end", {}),
                    lambda a, b: a["talk_to"]["state"] == "working" and b["talk_to"]["state"] == "ended")

        def group_names(body):
            return [g["project"]["name"] for g in body["builders"]]

        await after("a project renamed", lambda: send("PUT", f"/api/projects/{px}", {"name": "project-renamed"}),
                    lambda a, b: "project-x" in group_names(a) and "project-renamed" in group_names(b))
        await after("a person renamed", lambda: send("PATCH", "/api/people/local", {"name": "Robin"}),
                    lambda a, b: "Robin" not in json.dumps(a["crews"]) and "Robin" in json.dumps(b["crews"]))
        # keeping for later does not change the Crew page; starting it again is read on Home, which
        # shares the cache
        store.update_project(px, status="later")
        fh.clear_cache()
        h1 = (await get("/api/home")).json()
        await send("POST", f"/api/projects/{px}/later", {"later": False})
        h2 = (await get("/api/home")).json()
        ck("a project started again after being kept for later, on Home",
           (h1["later_count"], h2["later_count"]) == (1, 0), (h1["later_count"], h2["later_count"]))
        await after("a project deleted", lambda: send("DELETE", f"/api/projects/{px}"),
                    lambda a, b: "project-renamed" in group_names(a) and "project-renamed" not in group_names(b))

        print("== the crew's own writes wait for the window ==")
        fh.clear_cache()
        await page()
        await ingest(station("r1", "crew-reviewer", "reviewer", state="idle"))
        kept, n1 = await built()
        age_cache()
        fresh, n2 = await built()
        ck("lines taken in do not clear the page; it shows them once the window has gone by",
           n1 == 0 and kept["reviewer"] is None and n2 == 1 and fresh["reviewer"] is not None, (n1, n2))

    print("== an open cell ==")
    daemon_mod.AUTH_TOKEN = ""
    open_dir = os.path.join(_TMP, "open")
    os.makedirs(open_dir, exist_ok=True)
    daemon_mod.DB_PATH = os.path.join(open_dir, "open.duckdb")
    app = daemon_mod.make_app()
    async with app.router.lifespan_context(app):
        fh.clear_cache()
        n = BUILDS["crew"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            a = await cx.get("/api/factory/crew")
            b = await cx.get("/api/factory/crew")
        ck("no sign-in: built once, the same body twice",
           a.status_code == 200 and a.json() == b.json() and BUILDS["crew"] - n == 1,
           (a.status_code, BUILDS["crew"] - n))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
