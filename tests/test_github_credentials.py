"""GitHub credentials (a token stored once) and MCP server extra headers / credential references.

Run: .venv/bin/python tests/test_github_credentials.py   (no network: GitHub is faked in-process)

Covers: CRUD with the token never returned and blank-to-keep, the test endpoint, repo listing
with pagination and query filter, the credential's token resolved where it is used (and picking up
a rotation), and the delete. Nothing in the cell names a credential since the source catalog went
(TR-1811): its row lists no sources and no MCP servers.
"""
import asyncio
import os
from _env import remove_db

os.environ["TARES_HOME"] = "/tmp/tares-ghcred-test"

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


# ── a fake GitHub, served through httpx's MockTransport for every AsyncClient the daemon opens ──
STATE = {"valid": {"tok-1", "tok-2"}, "login": {"tok-1": "alice", "tok-2": "alice"},
         "calls": [], "repos_pages": 0}
COMMITS = [{"sha": "bbb2222", "html_url": "u", "author": {"login": "alice"},
            "commit": {"message": "second", "author": {"name": "A", "date": "2026-08-18T10:01:00Z"}}},
           {"sha": "aaa1111", "html_url": "u", "author": {"login": "alice"},
            "commit": {"message": "first", "author": {"name": "A", "date": "2026-08-18T10:00:00Z"}}}]


def fake_github(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
    STATE["calls"].append((request.url.path, token))
    if request.url.host != "api.github.test":
        return httpx.Response(599, text="unexpected host " + request.url.host)
    if token not in STATE["valid"]:
        return httpx.Response(401, json={"message": "Bad credentials"})
    path = request.url.path
    if path == "/user":
        return httpx.Response(200, json={"login": STATE["login"][token], "name": "Alice"},
                              headers={"x-oauth-scopes": "repo, read:org"})
    if path == "/user/repos":
        page = int(request.url.params.get("page", "1"))
        STATE["repos_pages"] += 1
        if page == 1:   # a full page, so the lister asks for page 2
            repos = [{"full_name": f"acme/repo-{i:03d}", "default_branch": "main",
                      "private": i % 2 == 0, "pushed_at": "2026-08-18T00:00:00Z"} for i in range(100)]
        elif page == 2:
            repos = [{"full_name": "acme/last-one", "default_branch": "develop",
                      "private": False, "pushed_at": "2026-08-17T00:00:00Z"}]
        else:
            repos = []
        return httpx.Response(200, json=repos)
    if path == "/repos/acme/app":
        return httpx.Response(200, json={"default_branch": "main", "private": True})
    if path == "/repos/acme/app/commits":
        return httpx.Response(200, json=COMMITS)
    return httpx.Response(404, json={"message": "Not Found"})


_RealClient = httpx.AsyncClient


class PatchedClient(_RealClient):
    """Every AsyncClient the daemon opens without an explicit transport talks to the fake."""
    def __init__(self, *a, **kw):
        if "transport" not in kw:
            kw["transport"] = httpx.MockTransport(fake_github)
        super().__init__(*a, **kw)


httpx.AsyncClient = PatchedClient
API = "http://api.github.test"


async def main():
    remove_db(os.environ["TARES_HOME"])
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with _RealClient(transport=transport, base_url="http://test") as cx:

            print("== credentials CRUD ==")
            r = await cx.post("/api/integrations/github", json={"name": "gh", "token": "tok-1",
                                                                "api_url": API})
            check("create -> 201", r.status_code == 201, r.text)
            check("create discovers the account", r.json().get("account") == "alice", r.text)
            r = await cx.post("/api/integrations/github", json={"name": "gh", "token": "x"})
            check("duplicate -> 409", r.status_code == 409, r.text)
            r = await cx.post("/api/integrations/github", json={"name": "bad name!", "token": "x"})
            check("bad name -> 400", r.status_code == 400, r.text)
            r = await cx.get("/api/integrations/github")
            creds = r.json()["credentials"]
            check("list has one, token never returned",
                  len(creds) == 1 and "token" not in creds[0] and creds[0]["token_configured"],
                  r.text)
            check("list shows account and empty usage",
                  creds[0]["account"] == "alice" and creds[0]["sources"] == [], r.text)

            print("== test endpoint ==")
            r = await cx.post("/api/integrations/github/gh/test")
            check("test ok with login + scopes",
                  r.json().get("ok") and r.json().get("login") == "alice"
                  and "repo" in r.json().get("scopes", []), r.text)
            r = await cx.post("/api/integrations/github/nope/test")
            check("test unknown -> 404", r.status_code == 404, r.text)

            print("== repo listing ==")
            r = await cx.get("/api/integrations/github/gh/repos")
            repos = r.json()["repos"]
            check("paginates: 101 repos", len(repos) == 101, str(len(repos)))
            check("shape: full_name/default_branch/private/pushed_at",
                  set(repos[0]) == {"full_name", "default_branch", "private", "pushed_at"}, str(repos[0]))
            pages_before = STATE["repos_pages"]
            r = await cx.get("/api/integrations/github/gh/repos", params={"query": "last"})
            check("query filters", [x["full_name"] for x in r.json()["repos"]] == ["acme/last-one"], r.text)
            check("second listing served from cache", STATE["repos_pages"] == pages_before)

            print("== the token where it is used ==")
            from tares.github_credentials import get_token
            store = app.state.store
            check("the credential's token resolves", await get_token(store, "gh", "acme/app") == "tok-1")
            r = await cx.get("/api/integrations/github")
            row = r.json()["credentials"][0]
            check("its row lists no sources and no MCP servers",
                  row["sources"] == [] and row["mcp_servers"] == [], r.text[:200])

            print("== rotation ==")
            r = await cx.put("/api/integrations/github/gh", json={"name": "gh", "token": "tok-2"})
            check("rotate -> ok", r.status_code == 200, r.text)
            check("the rotated token resolves", await get_token(store, "gh", "acme/app") == "tok-2")
            r = await cx.put("/api/integrations/github/gh", json={"name": "gh", "token": ""})
            r2 = await cx.post("/api/integrations/github/gh/test")
            check("blank token on update keeps the stored one", r2.json().get("ok")
                  and await get_token(store, "gh", "acme/app") == "tok-2", r2.text)

            print("== delete ==")
            r = await cx.delete("/api/integrations/github/gh")
            check("delete reports nothing that referenced it",
                  r.status_code == 200 and r.json()["sources"] == [] and r.json()["mcp_servers"] == [],
                  r.text)
            try:
                await get_token(store, "gh", "acme/app")
                check("the token after delete raises a named error", False)
            except ValueError as e:
                check("the token after delete raises a named error", "gh" in str(e), str(e))

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
