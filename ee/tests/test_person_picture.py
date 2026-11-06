# Tares enterprise licence, see ee/LICENSE
"""A person's picture (TR-1016): the https address the console shows instead of their initials.

Checked here: the people API and whoami carry `picture`; only one https address of at most 2048
characters is taken (http, javascript:, data:, spaces, quotes and markup are refused); a person sets
or removes their own, an admin anyone's, and nobody else; the control plane (the system credential)
sets it on a new person, on an existing one, and when it makes the boot person the workspace owner,
while any other caller's POST leaves an existing person's picture alone; the People page rows carry
it. The cell never fetches the address.

Run: .venv/bin/python ee/tests/test_person_picture.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-person-picture-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-picture"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

P = F = 0
PIC = "https://lh3.googleusercontent.com/a/ACg8ocK-example=s96-c"


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


async def main():
    from tares.daemon import make_app
    from tares.store import Store
    app = make_app()
    store = app.state.store
    keys, pid = {}, {}

    async def call(who, method, path, body=None):
        hdr = {"Authorization": f"Bearer {TOKEN if who == 'root' else keys[who]}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    print("== the address ==")
    ok = [PIC, "https://example.com/me.png", "  https://example.com/me.png  "]
    bad = ["http://example.com/me.png", "javascript:alert(1)", "data:image/png;base64,AAAA", "https://",
           "https://example.com/a b.png", 'https://example.com/"x', "https://example.com/<x>", "ftp://x/y.png",
           "https://" + "a" * 2050, 12]
    ck("an https address is taken, trimmed", [Store.check_picture(x) for x in ok]
       == [PIC, "https://example.com/me.png", "https://example.com/me.png"])
    refused = []
    for x in bad:
        try:
            Store.check_picture(x)
        except ValueError:
            refused.append(x)
    ck("anything else is refused", len(refused) == len(bad), [x for x in bad if x not in refused])
    ck("none or empty is none", Store.check_picture(None) is None and Store.check_picture("  ") is None)

    async with app.router.lifespan_context(app):
        for who, scopes in (("alex", ["admin"]), ("kim", ["read"]), ("lee", ["read"])):
            r = await call("root", "POST", "/api/keys", {"name": f"{who}-laptop", "scopes": scopes,
                                                          "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
            assert r.status_code < 300, r.text
            keys[who], pid[who] = r.json()["secret"], r.json()["person"]["id"]

        print("== shape ==")
        r = await call("root", "GET", "/api/people")
        ck("every person carries picture, null by default", all("picture" in p and p["picture"] is None
                                                                 for p in r.json()["people"]), r.json())
        r = await call("kim", "GET", "/api/whoami")
        ck("whoami's person carries picture", r.json()["person"] == {"id": pid["kim"], "name": "Kim",
                                                                    "email": "kim@example.com", "picture": None}, r.json())

        print("== who sets it ==")
        r = await call("kim", "PATCH", f"/api/people/{pid['kim']}", {"picture": PIC})
        ck("a person sets their own", r.status_code == 200 and r.json()["picture"] == PIC, r.text)
        r = await call("kim", "GET", "/api/whoami")
        ck("and whoami shows it", r.json()["person"]["picture"] == PIC, r.json())
        r = await call("kim", "PATCH", f"/api/people/{pid['lee']}", {"picture": PIC})
        ck("not someone else's", r.status_code == 403 and store.get_person(pid["lee"])["picture"] is None, r.text)
        r = await call("kim", "PATCH", f"/api/people/{pid['kim']}", {"picture": "http://example.com/x.png"})
        ck("a bad address is refused with 400, the old one kept", r.status_code == 400
           and store.get_person(pid["kim"])["picture"] == PIC, r.text)
        r = await call("alex", "PATCH", f"/api/people/{pid['lee']}", {"picture": "https://example.com/lee.png"})
        ck("an admin sets anyone's", r.status_code == 200 and r.json()["picture"] == "https://example.com/lee.png", r.text)
        r = await call("kim", "PATCH", f"/api/people/{pid['kim']}", {"picture": None})
        ck("a person removes their own", r.status_code == 200 and r.json()["picture"] is None, r.text)
        r = await call("kim", "PATCH", f"/api/people/{pid['kim']}", {"picture": PIC, "is_admin": True})
        ck("a person still cannot change their admin flag", r.status_code == 403, r.text)

        print("== the control plane ==")
        r = await call("root", "POST", "/api/people", {"email": "kim@example.com", "name": "Kim", "picture": PIC})
        ck("the system credential sets an existing person's picture", r.status_code == 200 and r.json()["picture"] == PIC
           and store.get_person(pid["kim"])["picture"] == PIC, r.text)
        r = await call("alex", "POST", "/api/people", {"email": "kim@example.com", "picture": "https://example.com/other.png"})
        ck("an admin key's POST does not change an existing person's picture", r.json()["picture"] == PIC
           and store.get_person(pid["kim"])["picture"] == PIC, r.text)
        r = await call("root", "POST", "/api/people", {"email": "sam@example.com", "name": "Sam", "picture": PIC})
        ck("a new person gets it", r.status_code == 201 and r.json()["picture"] == PIC, r.text)
        r = await call("root", "POST", "/api/people", {"email": "sam2@example.com", "name": "Sam", "picture": "javascript:x"})
        ck("a bad address on POST is refused, nobody made", r.status_code == 400
           and store.find_person_by_email("sam2@example.com") is None, r.text)
        r = await call("root", "POST", "/api/people", {"email": "owner@example.com", "name": "Owner", "owner": True,
                                                       "picture": "https://example.com/owner.png"})
        ck("the owner claims the boot person with their picture", r.json()["id"] == "local"
           and r.json()["picture"] == "https://example.com/owner.png", r.text)

        print("== the People page ==")
        r = await call("alex", "GET", "/api/factory/people")
        rows = {p["name"]: p for p in r.json()["people"]}
        ck("rows carry the picture", rows["Kim"]["picture"] == PIC and rows["Alex"]["picture"] is None, rows)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
