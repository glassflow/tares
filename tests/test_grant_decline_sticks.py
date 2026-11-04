"""A declined grant stays declined (TR-834).

A grant that waits for the project's lead is declined; a consent that comes after it, or that
reads the grant as waiting just before the decline lands, must not undo the decline or put the
grant in effect. The store's consent and activation only write a grant that still waits.

Covers: consent after a decline: 409 and the grant stays revoked with only the decline in its
consents; a decline landing between the consent's read and its write (the write is refused: 409,
the decline is kept); a decline landing between the consent's write and the grant taking effect
(409, the grant stays revoked and its line is not left in the grants doc); the store's
consent_waiting_grant and activate_waiting_grant refuse a grant that is no longer waiting.

TR-835 adds: a consent that does not finish the grant, arriving after a decline or meeting one between its
read and its write (409, still revoked, only the decline in its consents); the lead's own add of words that wait, with a decline
landing before it takes effect (409, still revoked, no line in the grants doc); two settles of
one grant, the second losing the race to the first (the grant is in effect, no 409, its line stays
in the grants doc); activate_waiting_grant writes areas and needs_lead only for a waiting grant.

Run: .venv/bin/python tests/test_grant_decline_sticks.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-grant-decline-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-grant-decline"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}

    async with app.router.lifespan_context(app):
        keys, pid = {}, {}

        async def call(who, method, path, body=None):
            hdr = dict(root if who == "root" else keys[who])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as cx:
                return await cx.request(method, path, json=body, headers=hdr)

        for who, scopes in (("alex", ["admin"]), ("kim", ["read", "ingest"]),
                            ("lee", ["read", "ingest"])):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": scopes,
                "person": {"email": f"{who}-gd@example.com", "name": who}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]
        r = await call("root", "PUT", "/api/areas/example-org/web", {"person_id": pid["kim"]})
        ck("example-org/web is kim's", r.status_code < 300, r.text)
        r = await call("root", "PUT", "/api/areas/example-org/api", {"person_id": pid["lee"]})
        ck("example-org/api is lee's", r.status_code < 300, r.text)
        r = await call("alex", "POST", "/api/projects", {"template": "custom", "name": "shop",
                                                         "objects": [], "kind": "software_factory"})
        uid = r.json()["id"]

        async def waiting(words):
            """A grant kim gives over her own repo: it waits for alex, who leads the project."""
            r = await call("kim", "POST", "/api/grants", {
                "kind": "project", "project": uid, "words": words, "areas": ["example-org/web"]})
            g = r.json().get("grant") or {}
            ck(f"'{words}' waits for alex", r.status_code < 300 and g.get("state") == "waiting"
               and [w["id"] for w in g.get("waiting_for", [])] == [pid["alex"]], r.text)
            return g["id"]

        def doc_has(words):
            d = next((x for x in store.list_docs(uid, "grants") if words in
                      (store.get_doc(uid, x["id"]) or {}).get("body", "")), None)
            return d is not None

        decline = [{"person_id": pid["alex"], "at": "2026-11-03T10:00:00+00:00", "declined": True}]

        print("== a consent after a decline ==")
        g1 = await waiting("Builders may push to web")
        r = await call("alex", "POST", f"/api/grants/{g1}/decline", {})
        ck("alex declines it", r.status_code == 200 and r.json()["grant"]["state"] == "revoked", r.text)
        r = await call("alex", "POST", f"/api/grants/{g1}/consent")
        row = store.get_grant_row(g1)
        ck("a consent after it: 409", r.status_code == 409, r.text)
        ck("it stays revoked, with only the decline in its consents", row["state"] == "revoked"
           and [c.get("declined") for c in row["consents"]] == [True], str(row))

        print("== a decline between the consent's read and its write ==")
        g2 = await waiting("Builders may edit the web footer")
        real = store.consent_waiting_grant

        def declined_first(gid, consents):
            store.decline_waiting_grant(gid, decline)
            return real(gid, consents)
        store.consent_waiting_grant = declined_first
        try:
            r = await call("alex", "POST", f"/api/grants/{g2}/consent")
        finally:
            store.consent_waiting_grant = real
        row = store.get_grant_row(g2)
        ck("the consent is refused: 409", r.status_code == 409, r.text)
        ck("the grant stays revoked and keeps the decline", row["state"] == "revoked"
           and row["consents"] == decline, str(row))

        print("== a decline between the consent's write and the grant taking effect ==")
        g3 = await waiting("Builders may rename web pages")
        real_act = store.activate_waiting_grant

        def declined_before_effect(gid):
            store.decline_waiting_grant(gid, decline)
            return real_act(gid)
        store.activate_waiting_grant = declined_before_effect
        try:
            r = await call("alex", "POST", f"/api/grants/{g3}/consent")
        finally:
            store.activate_waiting_grant = real_act
        row = store.get_grant_row(g3)
        ck("it is refused: 409", r.status_code == 409, r.text)
        ck("the grant stays revoked", row["state"] == "revoked", str(row))
        ck("and its line is not left in the grants doc", not doc_has("Builders may rename web pages"))

        print("== the store ==")
        ck("consent_waiting_grant refuses a revoked grant and leaves its row",
           store.consent_waiting_grant(g1, []) is False
           and [c.get("declined") for c in store.get_grant_row(g1)["consents"]] == [True])
        ck("activate_waiting_grant refuses a revoked grant",
           store.activate_waiting_grant(g1) is False and store.get_grant_row(g1)["state"] == "revoked")
        g4 = await waiting("Builders may add web tests")
        ck("a waiting one takes a consent", store.consent_waiting_grant(g4, decline[:0]) is True)
        ck("and takes effect", store.activate_waiting_grant(g4) is True
           and store.get_grant_row(g4)["state"] == "active")

        print("== a consent that does not finish the grant, after a decline (TR-835) ==")
        r = await call("alex", "POST", "/api/grants", {
            "kind": "project", "project": uid, "words": "Builders may deploy web and api",
            "areas": ["example-org/web", "example-org/api"]})
        g5 = r.json()["grant"]["id"]
        ck("it waits for kim and lee", sorted(w["id"] for w in r.json()["grant"]["waiting_for"])
           == sorted([pid["kim"], pid["lee"]]), r.text)
        r = await call("kim", "POST", f"/api/grants/{g5}/decline", {})
        ck("kim declines it", r.status_code == 200 and r.json()["grant"]["state"] == "revoked", r.text)
        r = await call("lee", "POST", f"/api/grants/{g5}/consent")
        row = store.get_grant_row(g5)
        ck("lee's consent after it: 409", r.status_code == 409, r.text)
        ck("it stays revoked, with only the decline in its consents", row["state"] == "revoked"
           and [c.get("declined") for c in row["consents"]] == [True], str(row))
        r = await call("alex", "POST", "/api/grants", {
            "kind": "project", "project": uid, "words": "Builders may deploy web and api again",
            "areas": ["example-org/web", "example-org/api"]})
        g5b = r.json()["grant"]["id"]
        real_consent = store.consent_waiting_grant

        def kim_declines_first(gid, consents):
            store.decline_waiting_grant(gid, decline)
            return real_consent(gid, consents)
        store.consent_waiting_grant = kim_declines_first
        try:
            r = await call("lee", "POST", f"/api/grants/{g5b}/consent")
        finally:
            store.consent_waiting_grant = real_consent
        row = store.get_grant_row(g5b)
        ck("lee's consent, with a decline landing before its write: 409", r.status_code == 409, r.text)
        ck("it stays revoked, with only the decline in its consents", row["state"] == "revoked"
           and row["consents"] == decline, str(row))

        print("== the lead's own add, with a decline before it takes effect (TR-835) ==")
        g6 = await waiting("Builders may fix web links")
        real_act = store.activate_waiting_grant

        def declined_before_add(gid, **kw):
            store.decline_waiting_grant(gid, decline)
            return real_act(gid, **kw)
        store.activate_waiting_grant = declined_before_add
        try:
            r = await call("alex", "POST", "/api/grants", {
                "kind": "project", "project": uid, "words": "Builders may fix web links"})
        finally:
            store.activate_waiting_grant = real_act
        row = store.get_grant_row(g6)
        ck("the add is refused: 409", r.status_code == 409, r.text)
        ck("the grant stays revoked", row["state"] == "revoked", str(row))
        ck("and its line is not in the grants doc", not doc_has("Builders may fix web links"))
        g7 = await waiting("Builders may fix web links again")
        r = await call("alex", "POST", "/api/grants", {
            "kind": "project", "project": uid, "words": "Builders may fix web links again"})
        ck("without a decline, the lead's add takes it into effect",
           r.status_code < 300 and store.get_grant_row(g7)["state"] == "active"
           and doc_has("Builders may fix web links again"), r.text)

        print("== two settles of one grant (TR-835) ==")
        g8 = await waiting("Builders may tidy web assets")
        ck("alex's consent is written, the grant not yet in effect",
           store.consent_waiting_grant(g8, [{"person_id": pid["alex"], "at": "2026-11-03T10:00:00+00:00"}])
           and store.get_grant_row(g8)["state"] == "waiting")

        def taken_first(gid, **kw):
            real_act(gid, **kw)   # the other settle gets there first
            return real_act(gid, **kw)
        store.activate_waiting_grant = taken_first
        try:
            r = await call("alex", "GET", f"/api/grants?project={uid}")
        finally:
            store.activate_waiting_grant = real_act
        ck("the read does not fail", r.status_code == 200, r.text)
        ck("the grant is in effect", store.get_grant_row(g8)["state"] == "active")
        ck("and its line stays in the grants doc", doc_has("Builders may tidy web assets"))

        print("== activate_waiting_grant and what it writes (TR-835) ==")
        g9 = await waiting("Builders may open web issues")
        ck("a declined grant keeps its areas and refuses",
           store.decline_waiting_grant(g9, decline) is True
           and store.activate_waiting_grant(g9, areas=[], needs_lead=False) is False
           and store.get_grant_row(g9)["areas"] == ["example-org/web"]
           and store.get_grant_row(g9)["state"] == "revoked")
        g10 = await waiting("Builders may close web issues")
        ck("a waiting one takes effect with the areas it is given",
           store.activate_waiting_grant(g10, areas=[], needs_lead=False) is True
           and store.get_grant_row(g10)["areas"] == []
           and store.get_grant_row(g10)["state"] == "active")

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
