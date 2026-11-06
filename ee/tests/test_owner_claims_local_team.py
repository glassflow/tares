# Tares enterprise licence, see ee/LICENSE
"""The cell's owner takes over `local`, the half with a second person. Once `local` is claimed, a
second owner is a new person; a teammate is never made `local`, and neither is anyone an admin key
names; the real owner, arriving after them, still takes `local`. Runs on a licensed daemon; the
one-person half is tests/test_owner_claims_local.py.
"""
import asyncio, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # puts the repo and tests/ on sys.path
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
R = {"Authorization": f"Bearer {AUTH}"}


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


async def _wait(url):
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def start(db, port):
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH, "TARES_LICENSE": licensed.test_licence()}
    return subprocess.Popen(licensed.daemon_argv(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def key(cx, B, name, person=None):
    body = {"name": name, "scopes": ["admin"]}
    if person:
        body["person"] = person
    r = await cx.post(f"{B}/api/keys", headers=R, json=body)
    r.raise_for_status()
    return r.json()


async def people(cx, B):
    return (await cx.get(f"{B}/api/people", headers=R)).json()["people"]


async def who(cx, B, tok):
    return (await cx.get(f"{B}/api/whoami", headers=H(tok))).json().get("person") or {}


async def second_owner():
    """Once `local` is claimed, a second owner is a new person."""
    port = "8871"
    proc = start("/tmp/owner_second.duckdb", port)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            await key(cx, B, "cli: new machine",
                      {"email": "Ashish@Example.com", "name": "Ashish", "owner": True})
            r = await cx.post(f"{B}/api/people", headers=R,
                              json={"name": "Carol", "email": "carol@example.com", "owner": True})
            ck("a second owner once local is claimed: a new person (201)",
               r.status_code == 201 and r.json()["id"] != "local", r.text[:200])
            ck("... local keeps the first owner",
               next(p for p in await people(cx, B) if p["id"] == "local")["email"] == "ashish@example.com")
    finally:
        proc.terminate(); proc.wait()


async def teammate_first():
    """A teammate, or a person an admin key names as owner, never becomes local; the real owner,
    arriving after them, still does."""
    port = "8872"
    proc = start("/tmp/owner_teammate.duckdb", port)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            old = await key(cx, B, "console: owner")
            admin = (await key(cx, B, "admin key"))["secret"]
            r = await cx.post(f"{B}/api/people", headers=H(admin),
                              json={"name": "Eve", "email": "eve@example.com", "owner": True})
            ck("owner: true from an admin key is ignored: a new person (201)",
               r.status_code == 201 and r.json()["id"] != "local", r.text[:200])
            r = await cx.post(f"{B}/api/keys", headers=H(admin),
                              json={"name": "k", "scopes": ["read"],
                                    "person": {"email": "mallory@example.com", "owner": True}})
            ck("... and on a key an admin key mints", r.status_code == 201
               and (await who(cx, B, r.json()["secret"])).get("id") != "local", r.text[:200])
            r = await cx.post(f"{B}/api/people", headers=R,
                              json={"name": "Bob", "email": "bob@example.com"})
            ck("a teammate pushed by the control plane: a new person", r.status_code == 201
               and r.json()["id"] != "local", r.text[:200])
            local = next(p for p in await people(cx, B) if p["id"] == "local")
            ck("local is still unclaimed", local["email"] is None, str(local))

            r = await cx.put(f"{B}/api/keys/{old['id']}/person", headers=R,
                             json={"person": {"email": "ashish@example.com", "name": "Ashish",
                                              "owner": True}})
            ck("the backfill binds the owner's old key", r.status_code == 200, r.text[:200])
            p = await who(cx, B, old["secret"])
            ck("... to local, now the owner", p.get("id") == "local"
               and p.get("email") == "ashish@example.com", str(p))
            r = await cx.post(f"{B}/api/people", headers=R,
                              json={"name": "Ashish", "email": "ashish@example.com", "owner": True})
            ck("the member push for the owner after that: 200, local", r.status_code == 200
               and r.json()["id"] == "local", r.text[:200])
    finally:
        proc.terminate(); proc.wait()


asyncio.run(second_owner())
asyncio.run(teammate_first())
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
