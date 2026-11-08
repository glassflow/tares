# Tares enterprise licence, see ee/LICENSE
"""People on the cell, the part that needs a second person (TR-618): a key bound to a person (found
or made by email), keys rebound, POST /api/people, who may change whom, the checks on emails, two
creations of one person at once, and a project key that carries its person. Runs on a licensed
daemon; the one-person part is tests/test_people.py.

Run: .venv/bin/python ee/tests/test_people_team.py
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


def start(db, port):
    for p in (db, db + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH, "TARES_LICENSE": licensed.test_licence(seats=50)}
    return subprocess.Popen(licensed.daemon_argv(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def auth_on():
    port = "8861"
    proc = start("/tmp/people_team_on.duckdb", port)
    B = f"http://127.0.0.1:{port}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth on)", False); return
        async with httpx.AsyncClient() as cx:
            # an unbound key, made while `local` is alone (the one-person test checks what it acts as)
            r = await cx.post(f"{B}/api/keys", json={"name": "old", "scopes": ["admin"]}, headers=H(AUTH))
            old_key, old_id = r.json()["secret"], r.json()["id"]

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
            ck("a person made through the root token is created_via cloud",
               next(p for p in people if p["id"] == pid)["created_via"] == "cloud", str(people))
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
                               json={"name": "Lena K"})
            ck("a person changes their own name", r.status_code == 200 and r.json()["name"] == "Lena K", r.text)
            before = (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
            for field, val in (("emails", ["pre-claim@example.com"]), ("github", "lk"),
                               ("email", "x@example.com"), ("is_admin", True)):
                r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_read), json={field: val})
                ck(f"... but not their {field} -> 403 with a plain message", r.status_code == 403
                   and "only an admin" in r.text and "\u2014" not in r.text, r.text)
            ck("... and a refused self edit changes nothing",
               (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"] == before)
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"github": "lk", "emails": ["L2@example.com"]})
            ck("an admin sets a person's github and extra emails", r.status_code == 200
               and r.json()["github"] == "lk" and r.json()["emails"] == ["l2@example.com"], r.text)
            ck("... nor someone else -> 403",
               (await cx.patch(f"{B}/api/people/{pid}", headers=H(lena_read),
                               json={"name": "Hacked"})).status_code == 403)
            ck("a key with no person cannot patch anyone -> 403",
               (await cx.patch(f"{B}/api/people/{pid}", headers=H(await _unbound(cx, B)),
                               json={"name": "Hacked"})).status_code == 403)
            ck("an admin's emails must be a list, not a string -> 400",
               (await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"emails": "@"})).status_code == 400)
            ck("... and each entry needs an @ -> 400",
               (await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"emails": ["nope"]})).status_code == 400)
            r = await cx.post(f"{B}/api/keys", headers=H(kiran_key), json={
                "name": "by-key", "scopes": ["read"], "person": {"email": "l2@EXAMPLE.com", "name": "Dup"}})
            ck("a key's person {email} matching an extra email finds that person",
               r.status_code == 201 and r.json()["person"]["id"] == lena_id, r.text)
            r = await cx.post(f"{B}/api/keys", headers=H(kiran_key), json={
                "name": "by-key2", "scopes": ["read"], "person": {"email": "mia@example.com", "name": "Mia"}})
            mia_id = r.json()["person"]["id"]
            ck("a person made through a key is created_via key",
               next(p for p in (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
                    if p["id"] == mia_id)["created_via"] == "key")
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH), json={"is_admin": True})
            ck("an admin changes the admin flag", r.status_code == 200 and r.json()["is_admin"] is True, r.text)
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH), json={"emails": []})
            ck("an admin removes the extra emails", r.status_code == 200 and r.json()["emails"] == [], r.text)
            ck("... and the person is not found by the removed address",
               next(p for p in (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
                    if p["id"] == lena_id)["emails"] == [])
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"emails": ["typo@example.com", "keep@example.com"]})
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"emails": ["keep@example.com"]})
            ck("an admin removes one extra email and keeps the other", r.status_code == 200
               and r.json()["emails"] == ["keep@example.com"], r.text)
            r = await cx.post(f"{B}/api/people", headers=H(AUTH), json={
                "name": "Owner", "email": "typo@example.com"})
            ck("a removed extra email is free for its real owner", r.status_code == 201, r.text)
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_read),
                               json={"name": "Lena K", "emails": []})
            ck("a self edit cannot send emails, even with a name -> 403, nothing changes",
               r.status_code == 403 and next(p for p in (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
                        if p["id"] == lena_id)["emails"] == ["keep@example.com"], r.text)
            ck("patch an unknown person -> 404",
               (await cx.patch(f"{B}/api/people/p_nobody", headers=H(AUTH), json={"name": "x"})).status_code == 404)
            ck("patch to another person's email -> 409",
               (await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH),
                               json={"emails": ["kiran@glassflow.dev"]})).status_code == 409)

            # ── the main email is checked like the extra ones (TR-618 follow-up) ──
            for bad in ("foo", ["a"], ["a@b.com"], 5, {"x": 1}):
                r = await cx.post(f"{B}/api/people", headers=H(AUTH), json={"name": "Bad", "email": bad})
                ck(f"POST /api/people with email {bad!r} -> 400", r.status_code == 400
                   and "\u2014" not in r.text, r.text)
                r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH), json={"email": bad})
                ck(f"PATCH /api/people with email {bad!r} -> 400", r.status_code == 400
                   and "\u2014" not in r.text, r.text)
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(AUTH), json={"email": "Lena2@Example.com"})
            ck("a good main email is kept lowercased", r.status_code == 200
               and r.json()["email"] == "lena2@example.com", r.text)
            r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                "name": "badp", "scopes": ["read"], "person": {"email": "nope", "name": "N"}})
            ck("a key's person with a bad email -> 400", r.status_code == 400, r.text)

            # ── who may rename themselves: the guard needs the read scope for PATCH /api/people ──
            r = await cx.post(f"{B}/api/keys", headers=H(AUTH), json={
                "name": "lena-ingest", "scopes": ["ingest"], "person_id": lena_id})
            lena_ingest = r.json()["secret"]
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_ingest), json={"name": "Ingest Rename"})
            ck("a bound ingest-only key cannot rename its person -> 403", r.status_code == 403, r.text)
            ck("... and the name is untouched",
               next(p for p in (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
                    if p["id"] == lena_id)["name"] == "Lena K")
            r = await cx.patch(f"{B}/api/people/{lena_id}", headers=H(lena_read), json={"name": "Lena Read"})
            ck("a bound read key renames its person", r.status_code == 200 and r.json()["name"] == "Lena Read", r.text)

            # ── two creations of one person at once: one wins, the others are told ──
            rs = await asyncio.gather(*[cx.post(f"{B}/api/people", headers=H(AUTH), json={
                "name": f"Race {i}", "github": "racer", "email": f"race{i}@example.com"})
                for i in range(8)])
            codes = sorted(r.status_code for r in rs)
            ck("8 creations of one GitHub login at once: one 201, the rest 409",
               codes == [201] + [409] * 7, str(codes))
            people = (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
            ck("... and one row", sum(1 for p in people if p["github"] == "racer") == 1, str(people))
            rs = await asyncio.gather(*[cx.post(f"{B}/api/people", headers=H(AUTH), json={
                "name": f"Same {i}", "email": "same@example.com"}) for i in range(8)])
            codes = sorted(r.status_code for r in rs)
            ck("8 creations of one email at once: one 201, the rest answer with that person (200)",
               codes == [200] * 7 + [201] and len({r.json()["id"] for r in rs}) == 1, str(codes))
            people = (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
            ck("... and one row", sum(1 for p in people if p["email"] == "same@example.com") == 1)
            rs = await asyncio.gather(*[cx.post(f"{B}/api/people", headers=H(AUTH), json={
                "name": f"Extra {i}", "emails": ["extra@example.com"]}) for i in range(8)])
            codes = sorted(r.status_code for r in rs)
            ck("8 creations of one extra email at once: one 201, the rest 409",
               codes == [201] + [409] * 7, str(codes))
            people = (await cx.get(f"{B}/api/people", headers=H(AUTH))).json()["people"]
            ck("... and one row", sum(1 for p in people if "extra@example.com" in p["emails"]) == 1)

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


asyncio.run(auth_on())
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
