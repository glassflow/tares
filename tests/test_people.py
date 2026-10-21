"""People on the cell (TR-618): a list of people, keys that belong to one, and a credential that
names the person calling. Root token = the system, no person; a key = its person (an unbound key is
`local` while `local` is alone on the cell); auth off = `local`. Also the upgrade of a database that
has keys but no people.
"""
import asyncio, os, subprocess, sys
import duckdb
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"


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


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def start(db, port, auth):
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off"}
    if auth:
        env["TARES_AUTH_TOKEN"] = AUTH
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def auth_on():
    port = "8861"
    proc = start("/tmp/people_on.duckdb", port, True)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient() as cx:
            # ── the system credential is not a person ──
            w = (await cx.get(f"{B}/api/whoami", headers=H(AUTH))).json()
            ck("root token: kind system, person null", w.get("kind") == "system"
               and w.get("person") is None, str(w))
            people = (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
            ck("a fresh cell has only `local`", [p["id"] for p in people] == ["local"], str(people))

            # ── a key with no person acts as `local` while `local` is alone ──
            r = await cx.post(f"{B}/api/keys", json={"name": "old", "scopes": ["admin"]},
                              headers=H(AUTH))
            ck("create unbound key", r.status_code == 201 and r.json()["person"] is None, r.text)
            old_key, old_id = r.json()["secret"], r.json()["id"]
            w = (await cx.get(f"{B}/api/whoami", headers=H(old_key))).json()
            ck("unbound key, only `local`: person local", w["kind"] == "key"
               and (w["person"] or {}).get("id") == "local", str(w))

            # ── a key bound to a person: find or create by email ──
            body = {"name": "cli: mbp (kiran@glassflow.dev)", "scopes": ["admin"],
                    "person": {"email": "kiran@glassflow.dev", "name": "Kiran N"}}
            r = await cx.post(f"{B}/api/keys", json=body, headers=H(AUTH))
            ck("create key with person {email, name}", r.status_code == 201
               and r.json()["person"]["name"] == "Kiran N", r.text)
            kiran_key, kiran_kid = r.json()["secret"], r.json()["id"]
            pid = r.json()["person"]["id"]
            ck("person id is p_ plus 10 hex", pid.startswith("p_") and len(pid) == 12, pid)
            r2 = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                **body, "name": "console", "person": {"email": "KIRAN@glassflow.dev", "name": "Other"}})
            ck("same email twice (any case): one person", r2.json()["person"]["id"] == pid, r2.text)
            people = (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
            ck("people: local and one Kiran", sorted(p["id"] for p in people) == sorted(["local", pid]),
               str(people))
            w = (await cx.get(f"{B}/api/whoami", headers=H(kiran_key))).json()
            ck("whoami with a bound key returns the person",
               (w["person"] or {}).get("email") == "kiran@glassflow.dev"
               and w["person"]["id"] == pid, str(w))

            # ── once a second person exists, an unbound key is nobody ──
            w = (await cx.get(f"{B}/api/whoami", headers=H(old_key))).json()
            ck("unbound key, second person exists: person null", w["person"] is None, str(w))
            ck("... and it still works as before",
               (await cx.get(f"{B}/api/keys", headers=H(old_key))).status_code == 200)

            # ── keys list their person; an admin binds a key ──
            keys = {k["id"]: k for k in (await cx.get(f"{B}/api/keys", headers=H(AUTH))).json()["keys"]}
            ck("GET /api/keys: person {id, name} or null",
               keys[kiran_kid]["person"] == {"id": pid, "name": "Kiran N"}
               and keys[old_id]["person"] is None, str(keys))
            r = await cx.put(f"{B}/api/keys/{old_id}/person", headers=H(AUTH),
                             json={"person": {"email": "asha@example.com", "name": "Asha"}})
            ck("PUT /api/keys/{kid}/person binds and returns the key", r.status_code == 200
               and r.json()["person"]["name"] == "Asha", r.text)
            asha_id = r.json()["person"]["id"]
            w = (await cx.get(f"{B}/api/whoami", headers=H(old_key))).json()
            ck("the rebound key now acts as Asha", (w["person"] or {}).get("id") == asha_id, str(w))
            r = await cx.put(f"{B}/api/keys/{old_id}/person", headers=H(AUTH), json={"person_id": pid})
            ck("PUT with person_id", r.status_code == 200 and r.json()["person"]["id"] == pid, r.text)
            ck("PUT with an unknown person_id -> 404",
               (await cx.put(f"{B}/api/keys/{old_id}/person", headers=H(AUTH),
                             json={"person_id": "p_nobody"})).status_code == 404)
            ck("PUT on an unknown key -> 404",
               (await cx.put(f"{B}/api/keys/zzzz/person", headers=H(AUTH),
                             json={"person_id": pid})).status_code == 404)
            ck("PUT with no person -> 400",
               (await cx.put(f"{B}/api/keys/{old_id}/person", headers=H(AUTH), json={})).status_code == 400)
            ck("POST /api/keys with an unknown person_id -> 404",
               (await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                   "name": "x", "scopes": ["read"], "person_id": "p_nobody"})).status_code == 404)

            # ── POST /api/people: 201, then 200 with the same person ──
            r = await cx.post(f"{B}/api/people", headers=H(AUTH),
                              json={"name": "Lena", "email": "lena@example.com", "github": "@LenaGH"})
            ck("POST /api/people -> 201", r.status_code == 201 and r.json()["github"] == "lenagh"
               and r.json()["email"] == "lena@example.com" and r.json()["id"].startswith("p_"), r.text)
            lena_id = r.json()["id"]
            r = await cx.post(f"{B}/api/people", headers=H(AUTH),
                              json={"name": "Lena again", "email": "Lena@Example.com"})
            ck("POST /api/people, email exists -> 200 with the existing person",
               r.status_code == 200 and r.json()["id"] == lena_id and r.json()["name"] == "Lena", r.text)
            ck("an extra email of someone else -> 409",
               (await cx.post(f"{B}/api/people", headers=H(AUTH), json={
                   "name": "Z", "email": "z@example.com", "emails": ["lena@example.com"]})).status_code == 409)
            ck("POST /api/people without a name -> 400",
               (await cx.post(f"{B}/api/people", headers=H(AUTH), json={"email": "q@x.com"})).status_code == 400)

            # ── who may change a person ──
            r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                "name": "lena-read", "scopes": ["read"], "person_id": lena_id})
            lena_read = r.json()["secret"]
            ck("a read key lists people", (await cx.get(f"{B}/api/people", headers=H(lena_read))).status_code == 200)
            ck("a read key cannot create a person -> 403",
               (await cx.post(f"{B}/api/people", headers=H(lena_read),
                              json={"name": "N"})).status_code == 403)
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_read),
                               json={"name": "Lena K", "github": "lk", "emails": ["L2@example.com"]})
            ck("a person changes their own name, github and emails", r.status_code == 200
               and r.json()["name"] == "Lena K" and r.json()["github"] == "lk"
               and r.json()["emails"] == ["l2@example.com"], r.text)
            ck("... but not their admin flag -> 403",
               (await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_read),
                               json={"is_admin": True})).status_code == 403)
            ck("... nor their main email -> 403",
               (await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_read),
                               json={"email": "x@example.com"})).status_code == 403)
            ck("... nor someone else -> 403",
               (await cx.patch(f"{B}/api/people/{pid}", headers=H(lena_read),
                               json={"name": "Hacked"})).status_code == 403)
            ck("a key with no person cannot patch anyone -> 403",
               (await cx.patch(f"{B}/api/people/{pid}", headers=H(await _unbound(cx, B)),
                               json={"name": "Hacked"})).status_code == 403)
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH), json={"is_admin": True})
            ck("an admin changes the admin flag", r.status_code == 200 and r.json()["is_admin"] is True, r.text)
            ck("patch an unknown person -> 404",
               (await cx.patch(f"{B}/api/people/p_nobody", headers=H(AUTH), json={"name": "x"})).status_code == 404)
            ck("patch to another person's email -> 409",
               (await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"emails": ["kiran@glassflow.dev"]})).status_code == 409)

            # ── a project key carries its person, and reaches nothing of the people routes ──
            pr = (await cx.post(f"{B}/api/projects", headers=H(AUTH), json={"template": "custom", "name": "pp", "objects": []})).json()
            r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                "name": "pk", "project": pr["id"], "person_id": pid})
            pk = r.json()["secret"]
            w = (await cx.get(f"{B}/api/whoami", headers=H(pk))).json()
            ck("project key: kind project, with its person", w["kind"] == "project"
               and w["person"]["id"] == pid, str(w))
            ck("project key cannot list people -> 403",
               (await cx.get(f"{B}/api/people", headers=H(pk))).status_code == 403)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def _unbound(cx, B):
    r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={"name": "nobody", "scopes": ["read"]})
    return r.json()["secret"]


async def auth_off():
    port = "8862"
    proc = start("/tmp/people_off.duckdb", port, False)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth off)", False); return
        async with httpx.AsyncClient() as cx:
            w = (await cx.get(f"{B}/api/whoami")).json()
            ck("auth off: kind open, person local", w.get("kind") == "open"
               and (w.get("person") or {}).get("id") == "local", str(w))
            people = (await cx.get(f"{B}/api/people")).json()["people"]
            ck("auth off: `local` is listed, an admin",
               people and people[0]["id"] == "local" and people[0]["is_admin"], str(people))
            r = await cx.patch(f"{B}/api/people/local", json={"name": "Ashish"})
            ck("auth off: anyone may rename `local`", r.status_code == 200 and r.json()["name"] == "Ashish", r.text)
            w = (await cx.get(f"{B}/api/whoami")).json()
            ck("whoami follows the rename", w["person"]["name"] == "Ashish", str(w))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def upgrade():
    """A database from before people: its keys survive, `local` is made, person_id is added."""
    from tares.store import Store
    db = "/tmp/people_old.duckdb"
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    con = duckdb.connect(db)
    con.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY, name TEXT, prefix TEXT, hash TEXT, "
                "scopes JSON, created_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ)")
    con.execute("INSERT INTO api_keys VALUES ('k1', 'legacy', 'nvf_k1', 'h', '[\"read\"]', now(), NULL, NULL)")
    con.close()
    s = Store(db)
    ck("upgrade: `local` is made", [p["id"] for p in s.list_people()] == ["local"])
    k = s.get_api_key("k1")
    ck("upgrade: the old key is kept, bound to nobody", k and k["person_id"] is None, str(k))
    s.con.close()
    s = Store(db)
    ck("upgrade: reopening adds no second `local`", len(s.list_people()) == 1)
    s.con.close()


asyncio.run(auth_on())
asyncio.run(auth_off())
upgrade()
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
