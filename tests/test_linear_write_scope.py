"""A Linear connection that can only read says so in Settings (TR-1243).

Runs against a fake Linear (httpx.MockTransport), never the live API. Tares writes to Linear
(status moves, new issues), so a connection Linear refuses a write on for its scope ("Invalid
scope") must show that on the connection, with the way to fix it for its kind, and a new
connection must clear it. A refusal for any other reason, and a call made with a credential that
is not the saved one, must not mark the connection.

Run: .venv/bin/python tests/test_linear_write_scope.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-linear-scope-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-linear-scope"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import linear

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


GOOD_KEY = "lin_api_good"
MUTATION = "mutation($id: String!) { issueUpdate(id: $id, input: {}) { success } }"
STATE = {"write_error": "Invalid scope: `write` required"}


def handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != linear.API_URL:
        return httpx.Response(404)
    if request.headers.get("authorization") not in (GOOD_KEY, "Bearer cloud-access-1"):
        return httpx.Response(400, json={"errors": [{"message": "Authentication required, not authenticated"}]})
    q = json.loads(request.content)["query"]
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": {
            "id": "u1", "name": "Ada", "email": "ada@acme.dev",
            "organization": {"name": "Acme", "urlKey": "acme"}}}})
    if q.startswith("mutation"):
        return httpx.Response(200, json={"errors": [{"message": STATE["write_error"]}]})
    return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


linear._transport = httpx.MockTransport(handler)


async def write(store, **kw):
    try:
        await linear.query(store, MUTATION, {"id": "i1"}, **kw)
        return None
    except linear.LinearError as e:
        return str(e)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})
        acct = {"name": "Ada", "email": "ada@acme.dev", "org": "Acme", "url_key": "acme"}

        print("== a key that can only read ==")
        r = await cx.post("/api/linear", json={"api_key": GOOD_KEY})
        ck("connects", r.status_code == 200 and r.json()["connected"], r.text)
        ck("nothing to say while no write was refused", "write_error" not in r.json(), r.text)
        err = await write(store)
        ck("the refused write still raises Linear's words", err is not None and "Invalid scope" in err, err)
        r = await cx.get("/api/linear")
        ck("Settings now says it can only read, and what to do for a key",
           r.json()["connected"] and "only read" in r.json().get("write_error", "")
           and "key that has write access" in r.json()["write_error"], r.text)
        ck("no token in it", GOOD_KEY not in r.text)
        first = linear.get_connection(store)["write_refused_at"]
        await write(store)
        ck("a second refusal keeps the first time", linear.get_connection(store)["write_refused_at"] == first)

        print("== a new connection clears it ==")
        r = await cx.post("/api/linear", json={"api_key": GOOD_KEY})
        ck("saving the key again clears the reason", "write_error" not in r.json()
           and "write_refused_at" not in linear.get_connection(store), r.text)

        print("== the Tares Cloud connection ==")
        r = await cx.put("/api/settings/linear-connection",
                         json={"token": "cloud-access-1", "expires_at": 4102444800, "account": acct})
        ck("connected as cloud", r.json()["kind"] == "cloud" and "write_error" not in r.json(), r.text)
        await write(store)
        r = await cx.get("/api/linear")
        ck("a refused write says to connect Linear again",
           "Connect Linear again" in r.json().get("write_error", ""), r.text)
        r = await cx.put("/api/settings/linear-connection",
                         json={"token": "cloud-access-1", "expires_at": 4102444800, "account": acct})
        ck("handing the connection over again clears it", "write_error" not in r.json(), r.text)

        print("== what does not mark the connection ==")
        STATE["write_error"] = "Entity not found: Issue"
        err = await write(store)
        ck("another refusal raises", err is not None and "Entity not found" in err, err)
        ck("and leaves the connection alone", "write_error" not in (await cx.get("/api/linear")).json())
        STATE["write_error"] = "Invalid scope: `write` required"
        other = {"kind": "api_key", "token": GOOD_KEY}
        err = await write(store, conn=other)
        ck("a write made with a credential that is not the saved one raises",
           err is not None and "Invalid scope" in err, err)
        ck("and does not mark the saved connection", "write_error" not in (await cx.get("/api/linear")).json())
        await cx.delete("/api/linear")
        err = await write(store)
        ck("with no connection there is nothing to mark", err is not None and "not connected" in err, err)
        r = await cx.get("/api/linear")
        ck("and Settings shows none", r.json()["connected"] is False and "write_error" not in r.json(), r.text)

        print("== old console words are gone ==")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        text = open(os.path.join(here, "ui", "src", "components", "LinearSettings.tsx")).read().lower()
        ck("Settings no longer says Tares only reads", "never writes" not in text
           and "only reads" not in text and "read access is enough" not in text)
        await cx.aclose()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
