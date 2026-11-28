# Tares enterprise licence, see ee/LICENSE
"""A private project stays out of the raw routes (TR-903).

Lee leads Zephyrwatch, a private project that holds a doc. Kim is a teammate with an admin key who is
not on it. A use of its doc is a 404 for Kim, and Lee and the cell's own key still use it. The usage
route names no source (the source catalog went, TR-1811; the AI SRE routes went with the edition,
TR-1809, the skill routes in TR-1810, the catalog dependents with the catalog).

Run: .venv/bin/python ee/tests/test_private_raw_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-private-raw-routes-")
os.environ["TARES_HOME"] = os.path.join(TMP, "t")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-raw"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx


P = F = 0
UTC = timezone.utc
SECRET = ("zephyr", "ZEPHYR")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def mentions(body) -> bool:
    return "zephyr" in json.dumps(body, default=str).lower()


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
        return await call(who, "GET", path, **params)

    async with app.router.lifespan_context(app):
        for who in ("kim", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-rr@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        open_uid, vault = "prj-edgewatch", "prj-zephyrwatch"
        store.create_project(open_uid, "custom", "Edgewatch", {}, goal="Keep edge-proxy healthy")
        store.create_project(vault, "custom", "Zephyrwatch", {}, goal="Keep zephyr-api healthy",
                             lead_person_id=pid["lee"])
        store.set_project_scope(vault, "private")
        doc = store.create_doc(vault, "notes", "Zephyr notes", "The zephyr notes.")

        print("== usage ==")
        r = await get("kim", "/api/usage")
        ck("usage names no source", r.status_code == 200 and r.json()["sources"] == []
           and not mentions(r.json()), r.text[:200])

        print("== use ==")
        r = await call("kim", "POST", f"/api/projects/{open_uid}/docs/{doc}/use")
        ck("using the private doc is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("and the open project does not get it", doc not in [d["id"] for d in store.list_docs(open_uid)])

        print("== the people who can see Zephyrwatch ==")
        r = await call("lee", "POST", f"/api/projects/{open_uid}/docs/{doc}/use")
        ck("lee uses it in the open project", r.status_code == 200
           and doc in [d["id"] for d in store.list_docs(open_uid)], f"{r.status_code} {r.text[:120]}")

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
