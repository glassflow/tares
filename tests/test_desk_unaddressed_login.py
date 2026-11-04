"""A person with a login can answer or allow a desk item that has no addressee (TR-898).

Items from before desk items were addressed carry no person. Only the person `local` had them in
their lists, so a signed-in person who is not `local` got a 403 ("not addressed to you") when
they allowed such a permission request or replied to such a question. An admin key, the
project's lead and `local` may act on such an item; a key that can only ingest and is not the
lead may not (it must not grant a permission). An item addressed to someone else stays theirs,
and a key with no person is still refused.

Run: .venv/bin/python tests/test_desk_unaddressed_login.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-desk-unaddressed-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-desk-unaddressed"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf

logging.getLogger("httpx").setLevel(logging.WARNING)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


STATION = {"X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"}
WORDS = "shop-build-m1 may run `git push` in shop"


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    async with app.router.lifespan_context(app):
        keys, pid = {}, {}

        async def call(who, method, path, body=None):
            hdr = dict(root if who == "root" else keys[who])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as cx:
                return await cx.request(method, path, json=body, headers=hdr)

        for who in ("kim", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-admin", "scopes": ["admin"],
                "person": {"email": f"{who}-ud@example.com", "name": who}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]
        r = await call("root", "POST", "/api/keys", {"name": "project-key", "scopes": ["admin"]})
        keys["nobody"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("kim", "POST", "/api/projects", {"template": "custom", "name": "shop",
                                                        "objects": [], "kind": "software_factory"})
        uid = r.json()["id"]

        headers.update(keys["kim"]); headers.update(STATION)

        async def add(question, to=None, **kw):
            out = await mf.desk_add(question, "yes", "because", project="shop", **kw)
            d = json.loads(out)
            with store._lock:
                store.sc.execute("UPDATE desk_items SET to_person_id = ? WHERE number = ?",
                                 [to, int(d["label"][1:])])
            return d

        def state(item):
            return store.get_desk_item(item["label"])["state"]

        print("== an item with no addressee ==")
        a1 = await add("Shop: run git push?", command="git push", station="shop-build-m1")
        ck("(it has no person)", store.get_desk_item(a1["label"])["to_person_id"] is None)
        r = await call("nobody", "POST", f"/api/desk/{a1['label']}/allow",
                       {"words": WORDS, "scope": "project"})
        ck("a key with no person is still refused: 403", r.status_code == 403
           and "no person" in r.text, f"{r.status_code} {r.text}")
        r = await call("lee", "POST", f"/api/desk/{a1['label']}/allow",
                       {"words": WORDS, "scope": "project"})
        ck("a signed-in person allows it", r.status_code == 200, f"{r.status_code} {r.text}")
        ck("and it is answered", state(a1) == "answered", state(a1))

        a2 = await add("Shop: one page or two?")
        r = await call("lee", "POST", f"/api/desk/{a2['label']}/reply", {"words": "Two pages"})
        ck("a signed-in person replies to a question with no addressee", r.status_code == 200,
           f"{r.status_code} {r.text}")
        ck("and it is answered", state(a2) == "answered", state(a2))

        print("== an item addressed to someone else stays theirs ==")
        a3 = await add("Shop: push again?", to=pid["kim"], command="git push",
                       station="shop-build-m1")
        r = await call("lee", "POST", f"/api/desk/{a3['label']}/allow",
                       {"words": WORDS, "scope": "project"})
        ck("lee cannot allow kim's: 403", r.status_code == 403 and "kim" in r.text,
           f"{r.status_code} {r.text}")
        a4 = await add("Shop: three pages?", to=pid["kim"])
        r = await call("lee", "POST", f"/api/desk/{a4['label']}/reply", {"words": "No"})
        ck("lee cannot reply to kim's: 403", r.status_code == 403, f"{r.status_code} {r.text}")
        ck("both are still open", state(a3) == "open" and state(a4) == "open")

        print("== lists keep the older rule ==")
        a5 = await add("Shop: old question?")
        async with mk() as cx:
            rows = (await cx.get("/api/crew/my-work", headers=keys["lee"])).json()["desk"]
        ck("an item with no addressee is still not in lee's my-work",
           a5["label"] not in [d["label"] for d in rows], rows)

        print("== a key that can only ingest ==")
        for who, email in (("joe", "joe-ud@example.com"), ("kimi", "kim-ud@example.com")):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-ingest", "scopes": ["ingest"],
                "person": {"email": email, "name": who}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]
        r = await call("kim", "POST", "/api/projects", {"template": "custom", "name": "lead-shop",
                                                        "objects": [], "kind": "software_factory"})
        lead_uid = r.json()["id"]
        r = await call("kim", "PATCH", f"/api/projects/{lead_uid}", {"lead_person_id": pid["kimi"]})
        lead_ok = r.status_code == 200 and (store.get_project(lead_uid) or {}).get("lead_person_id") == pid["kimi"]
        ck("(joe is no lead; the lead of lead-shop is an ingest key)", lead_ok, f"{r.status_code} {r.text}")
        b1 = await add("Shop: ingest question?")
        r = await call("joe", "POST", f"/api/desk/{b1['label']}/reply", {"words": "yes"})
        ck("a non-lead ingest key cannot reply to an unaddressed item: 403", r.status_code == 403,
           f"{r.status_code} {r.text}")
        ck("and it is still open", state(b1) == "open")
        b2 = await add("Shop: ingest push?", command="git push", station="shop-build-m1")
        r = await call("joe", "POST", f"/api/desk/{b2['label']}/allow",
                       {"words": WORDS, "scope": "project"})
        ck("a non-lead ingest key cannot allow one: 403", r.status_code == 403,
           f"{r.status_code} {r.text}")
        ck("and it is still open", state(b2) == "open")
        with store._lock:
            store.sc.execute("UPDATE desk_items SET project = ? WHERE number = ?",
                             [lead_uid, int(b1["label"][1:])])
        r = await call("kimi", "POST", f"/api/desk/{b1['label']}/reply", {"words": "yes"})
        ck("the project's lead (ingest key) replies: 200", r.status_code == 200,
           f"{r.status_code} {r.text}")

        print("== the recorded decision ==")
        b3 = await add("Shop: push for the record?", command="git push", station="shop-build-m1")
        r = await call("lee", "POST", f"/api/desk/{b3['label']}/allow",
                       {"words": WORDS, "scope": "project"})
        dec = [x for x in store.list_decisions(None, True) if x["question"] == b3["question"]] \
            if hasattr(store, "list_decisions") else []
        ck("allowed by lee", r.status_code == 200, f"{r.status_code} {r.text}")
        ck("the decision is the person's, made in the console",
           bool(dec) and dec[-1].get("via") == "console" and (dec[-1].get("person") or {}).get("id") == pid["lee"],
           dec[-1:] if dec else "no decision found")

        print("== local ==")
        b4 = await add("Shop: local push?", command="git push", station="shop-build-m1")
        r = await call("root", "POST", f"/api/desk/{b4['label']}/allow",
                       {"words": WORDS, "scope": "project"})
        ck("the cell's own credential (local) allows an unaddressed item", r.status_code == 200,
           f"{r.status_code} {r.text}")

        print("== a private project is not there for an outsider ==")
        r = await call("kim", "PATCH", f"/api/projects/{uid}", {"scope": "private"})
        ck("(shop is private)", r.status_code == 200, f"{r.status_code} {r.text}")
        b5 = await add("Shop: private question?")
        r = await call("joe", "POST", f"/api/desk/{b5['label']}/reply", {"words": "yes"})
        ck("a non-member gets 404, not 403", r.status_code == 404, f"{r.status_code} {r.text}")


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
