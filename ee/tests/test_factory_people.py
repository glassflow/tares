# Tares enterprise licence, see ee/LICENSE
"""The factory People page (TR-1011): GET and POST /api/factory/people.

Who is on the cell, and adding a teammate. Checked here: every person is listed with their email,
admin flag, whether they have signed in (a key bound to them was used), the open projects they lead
and the repos they own; the caller comes first and is marked; a private project the caller is not on
is not named as someone's lead; an admin key adds a person on a self-hosted cell, a second add of
the same email is refused, a bad email is refused; a read key may look but not add; and on a Tares
Cloud cell (TARES_WORKSPACE_API_URL) the cell does not add people itself, because the console
invites through the workspace.

Run: .venv/bin/python ee/tests/test_factory_people.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-people-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-people"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

from tares_ee import areas as areas_mod
from tares import words

P = F = 0


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
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys, pid = {}, {}

    async def call(who, method, path, body=None):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        for who, scopes in (("alex", ["admin"]), ("kim", ["admin"]), ("reader", ["read"])):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": scopes,
                "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
            assert r.status_code < 300, r.text
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        # Alex leads Atlas (open) and Vault (private); Kim owns a repo; Kim has never used a key
        r = await call("alex", "POST", "/api/projects", {"template": "custom", "name": "Atlas",
                                                         "objects": [], "kind": "software_factory"})
        atlas = r.json()["id"]
        r = await call("alex", "POST", "/api/projects", {"template": "custom", "name": "Zephyrvault",
                                                         "objects": [], "kind": "software_factory"})
        vault = r.json()["id"]
        r = await call("alex", "PATCH", f"/api/projects/{vault}", {"scope": "private"})
        assert r.status_code < 300, r.text
        areas_mod.save(store, [{"repo": "example-org/service-a", "person_id": pid["kim"]}])
        with store._swrite:     # Kim was added, never signed in
            store.sc.execute("UPDATE api_keys SET last_used_at = NULL WHERE person_id = ?", [pid["kim"]])

        print("== the list ==")
        r = await call("alex", "GET", "/api/factory/people")
        ck("200", r.status_code == 200, r.text)
        a = r.json()
        ck("the answer's keys", set(a) == {"people", "can_add", "add_text", "cloud", "workspace_href", "now"}, sorted(a))
        by = {p["name"]: p for p in a["people"]}
        ck("every person is listed", {"Alex", "Kim", "Reader"} <= set(by), sorted(by))
        ck("a row's keys", all(set(p) == {"id", "name", "email", "admin", "picture", "you", "state", "state_words", "leads", "owns"}
                               for p in a["people"]), sorted(a["people"][0]))
        ck("the caller comes first and is marked", a["people"][0]["name"] == "Alex" and a["people"][0]["you"]
           and not by["Kim"]["you"], [p["name"] for p in a["people"]])
        ck("the lead's projects, private ones too for the lead", sorted(x["name"] for x in by["Alex"]["leads"]) == ["Atlas", "Zephyrvault"],
           by["Alex"]["leads"])
        ck("the owner's repos", by["Kim"]["owns"] == ["example-org/service-a"], by["Kim"]["owns"])
        ck("signed in: last active", by["Alex"]["state"] == "active" and by["Alex"]["state_words"].startswith("Last active"),
           by["Alex"]["state_words"])
        ck("never signed in: invited", by["Kim"]["state"] == "invited"
           and by["Kim"]["state_words"] == "Invited, not signed in yet", by["Kim"])
        ck("self-hosted: an admin may add, the cell adds", a["can_add"] and not a["cloud"]
           and a["add_text"].startswith("Add a teammate by email"), a)
        bad = words.offenders_in(a, skip_keys=("id", "name", "email", "owns", "now", "workspace_href"))
        ck("the cell's words keep the words list", bad == [], bad)

        r = await call("kim", "GET", "/api/factory/people")
        kim_view = {p["name"]: p for p in r.json()["people"]}
        ck("a private project is not named to someone not on it",
           [x["name"] for x in kim_view["Alex"]["leads"]] == ["Atlas"], kim_view["Alex"]["leads"])

        r = await call("reader", "GET", "/api/factory/people")
        ck("a read key may look, and is told it cannot add", r.status_code == 200 and not r.json()["can_add"]
           and r.json()["add_text"] == "Only an admin can add people to this cell.", r.text)

        print("== adding a person on a self-hosted cell ==")
        r = await call("alex", "POST", "/api/factory/people", {"email": "Sam@Example.com", "name": "  Sam  Doe "})
        ck("201 with the person, email lowercased, name tidied", r.status_code == 201
           and r.json()["email"] == "sam@example.com" and r.json()["name"] == "Sam Doe", r.text)
        r = await call("alex", "GET", "/api/factory/people")
        sam = next((p for p in r.json()["people"] if p["email"] == "sam@example.com"), None)
        ck("listed as invited", sam and sam["state"] == "invited", sam)
        r = await call("alex", "POST", "/api/factory/people", {"email": "sam@example.com"})
        ck("the same email twice: 409", r.status_code == 409 and "already on this cell" in r.text, r.text)
        r = await call("alex", "POST", "/api/factory/people", {"email": "not-an-email"})
        ck("a bad email: 400", r.status_code == 400, r.text)
        r = await call("alex", "POST", "/api/factory/people", {"email": ""})
        ck("no email: 400", r.status_code == 400, r.text)
        r = await call("reader", "POST", "/api/factory/people", {"email": "lou@example.com"})
        ck("a read key cannot add", r.status_code == 403 and store.find_person_by_email("lou@example.com") is None, r.text)
        n = len(store.list_people())
        ck("a cell of more than one person now", n >= 4 and not store.only_local_person(), n)

        print("== on Tares Cloud the workspace invites ==")
        os.environ["TARES_WORKSPACE_API_URL"] = "https://console.example.com/api/workspaces/demo"
        try:
            r = await call("reader", "GET", "/api/factory/people")
            ck("cloud: the page says the workspace sends the invite, and leaves the owner check to it",
               r.json()["cloud"] and r.json()["can_add"] and "Tares Cloud sends them an invite" in r.json()["add_text"], r.json())
            r = await call("alex", "POST", "/api/factory/people", {"email": "max@example.com"})
            ck("cloud: the cell does not add people itself", r.status_code == 409
               and store.find_person_by_email("max@example.com") is None, r.text)
        finally:
            os.environ.pop("TARES_WORKSPACE_API_URL", None)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
