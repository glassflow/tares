"""The cell's owner takes over `local`. A cell used by one person before people existed keeps all of
it under `local`: keys bound to no one, projects without a lead, desk items addressed to no one.
When the control plane names the owner (`owner: true`, system credential only), `local` gets their
name and email instead of a new person being made, so all of that stays theirs and their old keys
keep acting as them. This file is the one-person half; a teammate is never made `local`, and neither
is anyone an admin key names: that half is ee/tests/test_owner_claims_local_team.py.
"""
import asyncio, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
           "TARES_AUTH_TOKEN": AUTH}
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


async def owner_alone():
    """A one-person cell: the owner's new key takes over `local`."""
    port = "8871"
    proc = start("/tmp/owner_alone.duckdb", port)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            old = (await key(cx, B, "cli: old machine"))["secret"]
            ck("an old key bound to no one acts as local", (await who(cx, B, old)).get("id") == "local")
            r = await cx.post(f"{B}/api/projects", headers=H(old),
                              json={"template": "custom", "name": "before", "kind": "software_factory"})
            ck("the old key makes a project", r.status_code == 201, r.text[:200])

            new = await key(cx, B, "cli: new machine",
                            {"email": "Ashish@Example.com", "name": "Ashish", "owner": True})
            ps = await people(cx, B)
            ck("the owner's new key: still one person", len(ps) == 1, str(ps))
            ck("... it is local, with the owner's name and email",
               ps[0]["id"] == "local" and ps[0]["name"] == "Ashish"
               and ps[0]["email"] == "ashish@example.com", str(ps))
            ck("the new key acts as local", (await who(cx, B, new["secret"])).get("id") == "local")
            p_old = await who(cx, B, old)
            ck("the old key still acts as the owner", p_old.get("id") == "local"
               and p_old.get("email") == "ashish@example.com", str(p_old))

            again = await key(cx, B, "cli: third machine",
                              {"email": "ashish@example.com", "name": "Ashish", "owner": True})
            ck("the owner again: the same person, nothing new",
               (await who(cx, B, again["secret"])).get("id") == "local"
               and len(await people(cx, B)) == 1)
    finally:
        proc.terminate(); proc.wait()


asyncio.run(owner_alone())
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
