# Tares enterprise licence, see ee/LICENSE
"""The admin routes leave a private project alone (TR-998).

Lee leads Zephyrwatch, a private project with a project key. Mia is on it as a member. Kim is a
teammate with an admin key who is not. A name Zephyrwatch owns answers Kim exactly like a name that
does not exist, on every route under Settings and Keys (the AI SRE routes this test also covered went
with the edition, TR-1809, the catalog export and import with the source catalog, TR-1811):

  - the key list, and the key count on the settings page, leave out the project key, and revoking
    it, rebinding it or making one for Zephyrwatch answers as for a key or project that is not there.

Mia, Lee and the cell's own key still see and change all of it.

Run: .venv/bin/python ee/tests/test_private_admin_routes.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-private-admin-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-admin"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

P = F = 0
UTC = timezone.utc


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def alike(a, b, name_a, name_b) -> bool:
    """Two answers that differ only by the name asked about."""
    return (a.status_code == b.status_code
            and a.text.replace(name_a, "X") == b.text.replace(name_b, "X"))


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

    async with app.router.lifespan_context(app):
        for who in ("kim", "mia", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-adm@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        vault = "prj-zephyrwatch"
        store.create_project(vault, "custom", "Zephyrwatch", {}, goal="Keep zephyr-api healthy",
                             lead_person_id=pid["lee"])
        store.set_project_scope(vault, "private")
        store.add_project_member(vault, pid["mia"])

        # a project key for Zephyrwatch, made by the one who sees it
        r = await call("lee", "POST", "/api/keys", {"name": "zephyr-reader", "project": vault})
        ck("Lee makes a key for her private project", r.status_code == 201, r.text[:200])
        zkey = r.json()["id"]

        r = await call("kim", "GET", "/api/catalog/export")
        ck("the catalog export is gone (TR-1811)", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

        print("== the keys ==")
        r = await call("kim", "GET", "/api/keys")
        ids = [k["id"] for k in r.json()["keys"]]
        ck("Kim's key list leaves out the project key", zkey not in ids and not any(
            k.get("project") == vault or "zephyr" in k["name"] for k in r.json()["keys"]), str(ids))
        ck("and still lists the others", keys["kim"]["Authorization"].split("_")[1] in ids,
           str(ids))
        for who in ("mia", "lee", "root"):
            r = await call(who, "GET", "/api/keys")
            ck(f"{who} sees the project key", zkey in [k["id"] for k in r.json()["keys"]], str(r.text[:200]))
        r = await call("kim", "DELETE", f"/api/keys/{zkey}")
        unk = await call("kim", "DELETE", "/api/keys/nokey000")
        ck("Kim revoking it answers as for a key that is not there",
           r.status_code == 404 and alike(r, unk, zkey, "nokey000"),
           f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and it is still active", (store.get_api_key(zkey) or {}).get("revoked_at") is None)
        r = await call("kim", "PUT", f"/api/keys/{zkey}/person", {"person_id": pid["kim"]})
        unk = await call("kim", "PUT", "/api/keys/nokey000/person", {"person_id": pid["kim"]})
        ck("binding it to herself answers as for a key that is not there",
           r.status_code == 404 and alike(r, unk, zkey, "nokey000"),
           f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and it is still bound to nobody", (store.get_api_key(zkey) or {}).get("person_id") is None)
        r = await call("kim", "POST", "/api/keys", {"name": "snoop", "project": vault})
        unk = await call("kim", "POST", "/api/keys", {"name": "snoop", "project": "prj-no-such"})
        ck("making a key for Zephyrwatch answers as for a project that is not there",
           r.status_code == unk.status_code and alike(r, unk, vault, "prj-no-such")
           and "secret" not in r.text, f"{r.status_code} {r.text[:120]} vs {unk.status_code} {unk.text[:120]}")
        ck("and none was made", not any(k["name"] == "snoop" for k in store.list_api_keys()))
        print("== the key count ==")
        live = [k for k in store.list_api_keys() if not k.get("revoked_at")]
        shared = [k for k in live if k.get("project") != vault]
        ck("the cell has a key for Zephyrwatch besides the others", len(live) == len(shared) + 1,
           f"{len(live)} {len(shared)}")
        r = await call("kim", "GET", "/api/factory/settings")
        ck("Kim's key count leaves out the project key",
           r.status_code == 200 and r.json()["people"]["keys"]["count"] == len(shared),
           f"{r.status_code} {r.text[:200]}")
        r = await call("lee", "GET", "/api/factory/settings")
        ck("Lee's count has it", r.status_code == 200 and r.json()["people"]["keys"]["count"] == len(live),
           f"{r.status_code} {r.text[:200]}")
        r = await call("mia", "PUT", f"/api/keys/{zkey}/person", {"person_id": pid["mia"]})
        ck("Mia, on the project, rebinds it", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        r = await call("lee", "DELETE", f"/api/keys/{zkey}")
        ck("Lee revokes it", r.status_code == 200 and (store.get_api_key(zkey) or {}).get("revoked_at"),
           f"{r.status_code} {r.text[:160]}")
        r = await call("lee", "DELETE", f"/api/keys/{zkey}")
        ck("and a second revoke is a plain 404", r.status_code == 404)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
