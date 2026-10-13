"""Linear (TR-408): the cell's connection and the sync that keeps a project's tickets current.

Runs against a fake Linear (httpx.MockTransport answering like Linear's GraphQL and OAuth
endpoints), never the live API. Covers connecting with an API key (refused key, account shown,
token never returned), sign-in with OAuth and PKCE (no client id, the authorize URL, the callback,
a forged state, the refresh before expiry), linking a project by URL (tickets, events on the
`linear` source, the source in the project), changes made in Linear (status, rename, new issue,
reorder, an issue leaving the project) picked up by the connector's poll, a revoked credential
leaving the tickets as they were, and unlinking.

Run: .venv/bin/python tests/test_linear.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-linear-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-linear"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ.pop("TARES_LINEAR_CLIENT_ID", None)

import httpx

from tares import linear
from tares.config import SourceCfg
from tares.connectors.linear import LinearConnector

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake Linear ──────────────────────────────────────────────────────────
GOOD_KEY = "lin_api_good"
STATE = {
    "valid_tokens": {GOOD_KEY, "Bearer oauth-access-1", "Bearer oauth-access-2"},
    "issues": [
        {"id": "i1", "identifier": "FAC-1", "title": "Parse invoices", "url": "https://linear.app/acme/issue/FAC-1",
         "sortOrder": 1.0, "updatedAt": "2026-10-07T10:00:00Z", "state": {"name": "Todo", "type": "unstarted"}},
        {"id": "i2", "identifier": "FAC-2", "title": "Store invoices", "url": "https://linear.app/acme/issue/FAC-2",
         "sortOrder": 2.0, "updatedAt": "2026-10-07T10:00:00Z", "state": {"name": "Backlog", "type": "backlog"}},
        {"id": "i3", "identifier": "FAC-3", "title": "Email invoices", "url": "https://linear.app/acme/issue/FAC-3",
         "sortOrder": 3.0, "updatedAt": "2026-10-07T10:00:00Z", "state": {"name": "Todo", "type": "unstarted"}},
    ],
    "token_requests": [],
}
PROJECT = {"id": "lp-uuid-1", "name": "Invoicing", "url": "https://linear.app/acme/project/invoicing-ab12cd34ef56",
           "slugId": "ab12cd34ef56"}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == linear.TOKEN_URL:
        form = parse_qs(request.content.decode())
        STATE["token_requests"].append({k: v[0] for k, v in form.items()})
        if form.get("grant_type") == ["authorization_code"]:
            return httpx.Response(200, json={"access_token": "oauth-access-1", "expires_in": 86399,
                                             "refresh_token": "oauth-refresh-1", "scope": "read"})
        return httpx.Response(200, json={"access_token": "oauth-access-2", "expires_in": 86399,
                                         "refresh_token": "oauth-refresh-2"})
    if url == linear.REVOKE_URL:
        STATE["revoked"] = parse_qs(request.content.decode()).get("token", [""])[0]
        return httpx.Response(200, json={})
    if url != linear.API_URL:
        return httpx.Response(404)
    if request.headers.get("authorization") not in STATE["valid_tokens"]:
        return httpx.Response(400, json={"errors": [{"message": "Authentication required, not authenticated"}]})
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": {
            "id": "u1", "name": "Ada", "email": "ada@acme.dev",
            "organization": {"name": "Acme", "urlKey": "acme"}}}})
    if "issues(" in q:
        if v["id"] != PROJECT["id"]:
            return httpx.Response(200, json={"data": {"project": None}})
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": STATE["issues"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "project(id" in q:
        hit = v["id"] in (PROJECT["id"], PROJECT["slugId"])
        if not hit:
            return httpx.Response(200, json={"errors": [{"message": "Entity not found: Project"}]})
        return httpx.Response(200, json={"data": {"project": PROJECT}})
    if "projects(" in q:
        n = (v.get("n") or v.get("q") or "").lower()
        nodes = [PROJECT] if not n or n in PROJECT["name"].lower() else []
        return httpx.Response(200, json={"data": {"projects": {"nodes": nodes}}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


linear._transport = httpx.MockTransport(handler)


async def main():
    from tares.daemon import make_app
    from tares import github_app as gh
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})
        r = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
        reader = {"Authorization": f"Bearer {r.json()['secret']}"}

        print("== connecting ==")
        r = await cx.get("/api/linear")
        ck("not connected, no sign-in app", r.json()["connected"] is False
           and r.json()["oauth_available"] is False, r.text)
        r = await cx.get("/api/linear", headers=reader)
        ck("a read key cannot see the connection -> 403", r.status_code == 403, r.text)
        r = await cx.post("/api/linear", json={"api_key": "lin_api_bad"})
        ck("a refused key -> 400 that says so", r.status_code == 400
           and "did not accept" in r.json()["detail"], r.text)
        ck("and nothing is saved", store.get_setting(linear.CONNECTION_SETTING) is None)
        r = await cx.post("/api/linear", json={"api_key": GOOD_KEY})
        ck("a good key connects, as whom", r.status_code == 200 and r.json()["connected"]
           and r.json()["kind"] == "api_key" and r.json()["account"]["org"] == "Acme", r.text)
        ck("the key never comes back", GOOD_KEY not in r.text
           and GOOD_KEY not in (await cx.get("/api/linear")).text)
        r = await cx.get("/api/linear/projects", params={"q": "invo"})
        ck("project picker", [p["name"] for p in r.json()["projects"]] == ["Invoicing"], r.text)

        print("== OAuth with PKCE ==")
        r = await cx.get("/api/linear/oauth/start")
        ck("no client id -> 400 that says what to do", r.status_code == 400
           and "OAuth application" in r.json()["detail"], r.text)
        r = await cx.put("/api/linear/oauth-app", json={"client_id": "cid-123"})
        ck("save the client id", r.json()["oauth_available"] and r.json()["client_id"] == "cid-123",
           r.text)
        r = await cx.get("/api/linear/oauth/start")
        u = urlsplit(r.json()["url"])
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        ck("authorize URL with PKCE and the callback", u.netloc == "linear.app"
           and qs["client_id"] == "cid-123" and qs["code_challenge_method"] == "S256"
           and qs["redirect_uri"] == "http://t/api/linear/oauth/callback"
           and r.json()["redirect_uri"] == qs["redirect_uri"], r.text)
        ck("the verifier is not in the URL", "code_verifier" not in r.json()["url"])
        noauth = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        r = await noauth.get("/api/linear/oauth/callback", params={"code": "c", "state": "forged.x"})
        ck("a forged state -> back to Settings with an error, nothing saved",
           r.status_code == 303 and "error=" in r.headers["location"]
           and linear.get_connection(store)["kind"] == "api_key", r.headers.get("location"))
        r = await noauth.get("/api/linear/oauth/callback", params={"code": "c1", "state": qs["state"]})
        ck("the callback needs no Tares key and connects", r.status_code == 303
           and "event=connected" in r.headers["location"]
           and linear.get_connection(store)["kind"] == "oauth", r.headers.get("location"))
        tr = STATE["token_requests"][-1]
        ck("the code exchange sent the verifier, no secret", tr.get("code_verifier")
           and "client_secret" not in tr and tr["client_id"] == "cid-123", tr)
        r = await noauth.get("/api/linear/oauth/callback", params={"code": "c1", "state": qs["state"]})
        ck("a used state is refused", "error=" in r.headers["location"], r.headers["location"])
        conn = linear.get_connection(store)
        linear._save_connection(store, {**conn, "expires_at": int(time.time()) + 60})
        await linear.query(store, "query { viewer { id name email organization { name urlKey } } }")
        ck("an access token about to expire is refreshed first",
           STATE["token_requests"][-1]["grant_type"] == "refresh_token"
           and linear.get_connection(store)["token"] == "oauth-access-2"
           and linear.get_connection(store)["refresh_token"] == "oauth-refresh-2")
        await noauth.aclose()

        print("== linking a project ==")
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Invoices",
                                                    "objects": []})).json()["id"]
        r = await cx.post(f"/api/projects/{uid}/linear", json={"project": "https://linear.app/acme/nope-000"})
        ck("an unknown Linear project -> 400", r.status_code == 400, r.text)
        r = await cx.post(f"/api/projects/{uid}/linear", json={"project": PROJECT["url"] + "/overview"})
        tickets = r.json()["tickets"]
        ck("link by URL: three tickets in Linear's order", r.status_code == 200
           and [t["identifier"] for t in tickets] == ["FAC-1", "FAC-2", "FAC-3"]
           and all(t["owner"] == "linear" for t in tickets), r.text)
        ck("statuses mapped from Linear's state types",
           [t["status"] for t in tickets] == ["todo", "todo", "todo"], tickets)
        ck("the link records the sync", r.json()["linear"]["name"] == "Invoicing"
           and r.json()["linear"]["synced_at"] and r.json()["linear"]["error"] is None, r.text)
        ck("the linear source exists and is in the project",
           "linear" in app.state.runtime.catalog.sources
           and "linear" in store.project_sources(uid))
        n_created = store.con.execute("SELECT count(*) FROM events WHERE source = 'linear' AND "
                                      "event_type = 'ticket_created'").fetchone()[0]
        ck("one event per ticket found", n_created == 3, n_created)
        r = await cx.post(f"/api/projects/{uid}/tickets", json={"title": "x"})
        ck("no Tares tickets on it", r.status_code == 409, r.text)

        print("== changes made in Linear ==")
        iss = STATE["issues"]
        iss[0] = {**iss[0], "state": {"name": "In Progress", "type": "started"}}
        iss[1] = {**iss[1], "title": "Store invoices in Postgres", "sortOrder": 0.5}
        removed = iss.pop(2)
        iss.append({"id": "i4", "identifier": "FAC-4", "title": "Retry failed emails",
                    "url": "https://linear.app/acme/issue/FAC-4", "sortOrder": 4.0,
                    "updatedAt": "2026-10-07T11:00:00Z", "state": {"name": "Todo", "type": "unstarted"}})
        conn_src = LinearConnector(SourceCfg(name="linear", type="event_stream", connector="linear",
                                             poll_seconds=60, config={}), store)
        envs = await conn_src.poll()
        kinds = sorted(e.event_type for e in envs)
        ck("the poll sees each change once", kinds == sorted(
            ["ticket_status", "ticket_renamed", "ticket_reordered", "ticket_removed",
             "ticket_created"]), kinds)
        ck("events keyed by the Linear identifier with the project label",
           all(e.labels["project"] == "Invoices" and e.key_value.startswith("FAC-") for e in envs))
        store.append(envs)
        t = {x["identifier"]: x for x in (await cx.get(f"/api/projects/{uid}/tickets")).json()["tickets"]}
        ck("status moved", t["FAC-1"]["status"] == "in_progress", t["FAC-1"])
        ck("renamed and reordered", t["FAC-2"]["title"] == "Store invoices in Postgres"
           and t["FAC-2"]["position"] == 0.5, t["FAC-2"])
        ck("the one that left Linear is canceled, not dropped", t["FAC-3"]["status"] == "canceled")
        ck("the new one is there, without a working doc", t["FAC-4"]["working_doc"] is None)
        ck("a second poll with nothing new emits nothing", await conn_src.poll() == [])
        r = await cx.post(f"/api/projects/{uid}/docs", json={"kind": "working", "title": "W",
                                                             "body": "steps"})
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-4", json={"working_doc": r.json()["id"]})
        ck("the new ticket can get a working doc", r.json()["working_doc_body"] == "steps", r.text)
        iss.append(removed)
        await conn_src.poll()
        t = {x["identifier"]: x for x in (await cx.get(f"/api/projects/{uid}/tickets")).json()["tickets"]}
        ck("an issue back in the project comes back to its state", t["FAC-3"]["status"] == "todo")
        r = await cx.post(f"/api/projects/{uid}/linear/sync")
        ck("sync now", r.status_code == 200 and len(r.json()["tickets"]) == 4, r.text)

        print("== a revoked credential ==")
        STATE["valid_tokens"].clear()
        before = store.list_tickets(uid)
        r = await cx.post(f"/api/projects/{uid}/linear/sync")
        ck("sync now says the credential is refused", r.status_code == 400
           and "did not accept" in r.json()["detail"], r.text)
        ck("the error is on the project", "did not accept" in
           ((await cx.get(f"/api/projects/{uid}/tickets")).json()["linear"]["error"] or ""))
        ck("the tickets stay as they were", store.list_tickets(uid) == before)
        try:
            await conn_src.poll()
            ck("the poll fails so the source's health shows it", False)
        except linear.LinearError:
            ck("the poll fails so the source's health shows it", True)

        print("== unlink and disconnect ==")
        STATE["valid_tokens"].add("Bearer oauth-access-2")
        r = await cx.delete(f"/api/projects/{uid}/linear")
        ck("unlink: tickets stay, now Tares's", r.status_code == 200 and r.json()["linear"] is None
           and all(x["owner"] == "tares" for x in r.json()["tickets"]), r.text)
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"status": "done"})
        ck("and can be edited in Tares", r.json()["status"] == "done", r.text)
        uid2 = (await cx.post("/api/projects", json={"template": "custom", "name": "Other",
                                                     "objects": []})).json()["id"]
        await cx.post(f"/api/projects/{uid2}/tickets", json={"title": "mine"})
        r = await cx.post(f"/api/projects/{uid2}/linear", json={"project": PROJECT["id"]})
        ck("a project with its own tickets cannot be linked", r.status_code == 409, r.text)
        r = await cx.delete("/api/linear")
        ck("disconnect", r.json()["connected"] is False and linear.get_connection(store) is None,
           r.text)
        ck("an OAuth token is revoked on disconnect", STATE.get("revoked") == "oauth-access-2",
           STATE.get("revoked"))
        await cx.aclose()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
