"""GitHub credentials (a token stored once) and MCP server extra headers / credential references.

Run: .venv/bin/python tests/test_github_credentials.py   (no network: GitHub is faked in-process)

Covers: CRUD with the token never returned and blank-to-keep, the test endpoint, repo listing
with pagination and query filter, a `github` source resolving `credential:` at poll time (and
picking up a rotation), discover through a credential, MCP server headers stored/merged, an MCP
`credential:github/<name>` reference resolved to a bearer header, and YAML export/import of both.
"""
import asyncio
import json
import os

os.environ["TARES_DB"] = "/tmp/tares-ghcred-test.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/does-not-exist.yaml"

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
    for p in (os.environ["TARES_DB"], os.environ["TARES_DB"] + ".wal"):
        if os.path.exists(p):
            os.remove(p)
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

            print("== github source through a credential ==")
            r = await cx.post("/api/sources", json={
                "connector": "github", "name": "app_commits", "poll": "1h",
                "config": {"repo": "acme/app", "credential": "gh",
                           "labels": [{"name": "repo", "field": "repo", "primary": True}]}})
            check("source created with credential (no token)", r.status_code in (200, 201), r.text)
            r = await cx.get("/api/sources/app_commits")
            cfg = r.json().get("config") or r.json().get("source", {}).get("config") or {}
            check("source config keeps the credential name, no token",
                  cfg.get("credential") == "gh" and not cfg.get("token"), json.dumps(cfg))
            r = await cx.get("/api/integrations/github")
            check("credential usage lists the source",
                  r.json()["credentials"][0]["sources"] == ["app_commits"], r.text)

            # poll once through the runtime object; the token must come from the credential
            from tares.connectors import REGISTRY
            from tares.config import _source_from_dict
            store = app.state.store
            src_cfg = _source_from_dict({"name": "app_commits", "connector": "github", "poll": "1h",
                                         "config": cfg})
            conn = REGISTRY["github"](src_cfg, store)
            STATE["calls"].clear()
            envs = await conn.poll()
            check("poll ingests commits with the credential token",
                  len(envs) == 2 and all(t == "tok-1" for _, t in STATE["calls"]), str(STATE["calls"]))
            check("api_url from the credential (GHE) is used",
                  all(p.startswith("/repos/acme/app") for p, _ in STATE["calls"]), str(STATE["calls"]))

            print("== rotation ==")
            r = await cx.put("/api/integrations/github/gh", json={"name": "gh", "token": "tok-2"})
            check("rotate -> ok", r.status_code == 200, r.text)
            STATE["calls"].clear()
            conn._etags = {}    # a 304 would hide which token was sent
            await conn.poll()
            check("next poll uses the rotated token",
                  STATE["calls"] and all(t == "tok-2" for _, t in STATE["calls"]), str(STATE["calls"]))
            r = await cx.put("/api/integrations/github/gh", json={"name": "gh", "token": ""})
            r2 = await cx.post("/api/integrations/github/gh/test")
            check("blank token on update keeps the stored one", r2.json().get("ok"), r2.text)

            print("== discover through a credential ==")
            r = await cx.post("/api/sources/discover", json={"connector": "github",
                                                             "config": {"repo": "acme/app", "credential": "gh"}})
            check("discover ok, proposal keeps credential and no token",
                  r.status_code == 200 and r.json()["proposed_config"].get("credential") == "gh"
                  and "token" not in r.json()["proposed_config"], r.text[:300])
            r = await cx.post("/api/sources/discover", json={"connector": "github",
                                                             "config": {"repo": "acme/app", "credential": "nope"}})
            check("discover with unknown credential -> 404", r.status_code == 404, r.text)

            print("== MCP servers: headers + credential reference ==")
            r = await cx.post("/api/mcp-servers", json={
                "name": "github", "url": "https://mcp.github.test/mcp",
                "auth_value": "credential:github/gh",
                "headers": {"X-MCP-Toolsets": "repos,pull_requests", "X-MCP-Readonly": "true"}})
            check("mcp server with credential ref + headers -> 201", r.status_code == 201, r.text)
            r = await cx.post("/api/mcp-servers", json={
                "name": "bad", "url": "https://x/mcp", "auth_value": "credential:github/nope"})
            check("unknown credential ref -> 404", r.status_code == 404, r.text)
            r = await cx.post("/api/mcp-servers", json={
                "name": "bad2", "url": "https://x/mcp", "headers": {"not a header": "v"}})
            check("bad header name -> 400", r.status_code == 400, r.text)
            r = await cx.get("/api/mcp-servers")
            row = r.json()["servers"][0]
            check("row exposes headers and the credential name, not the token",
                  row["headers"].get("X-MCP-Toolsets") == "repos,pull_requests"
                  and row["auth_credential"] == "gh" and row["auth_value_configured"], r.text)
            r = await cx.get("/api/integrations/github")
            check("credential usage lists the MCP server",
                  r.json()["credentials"][0]["mcp_servers"] == ["github"], r.text)

            from tares.mcp_client import _headers, resolve_servers
            resolved = await resolve_servers(store, store.list_mcp_servers())
            hdrs = _headers(resolved[0])
            check("resolved headers: bearer from credential + extra headers",
                  hdrs.get("Authorization") == "Bearer tok-2"
                  and hdrs.get("X-MCP-Toolsets") == "repos,pull_requests"
                  and hdrs.get("X-MCP-Readonly") == "true", str(hdrs))
            store.upsert_mcp_server("orphan", "https://x/mcp", "", "credential:github/gone", {})
            bad = [s for s in await resolve_servers(store, store.list_mcp_servers())
                   if s["name"] == "orphan"][0]
            check("missing credential -> named auth error, no header",
                  bad.get("_auth_error") and "gone" in bad["_auth_error"] and not _headers(bad),
                  str(bad))
            store.delete_mcp_server("orphan")
            r = await cx.put("/api/mcp-servers/github", json={
                "name": "github", "url": "https://mcp.github.test/mcp", "auth_value": "",
                "headers": {"X-MCP-Toolsets": "repos"}})
            r = await cx.get("/api/mcp-servers")
            row = r.json()["servers"][0]
            check("update: blank auth keeps the credential ref, headers replaced",
                  row["auth_credential"] == "gh" and row["headers"] == {"X-MCP-Toolsets": "repos"}, r.text)

            print("== catalog export / import ==")
            r = await cx.get("/api/catalog/export")
            text = r.text
            check("export carries the credential ref (not a secret) and headers",
                  "credential:github/gh" in text and "X-MCP-Toolsets" in text, text[-600:])
            check("export never carries the token", "tok-2" not in text)
            import yaml
            doc = yaml.safe_load(text)
            src = [s for s in doc.get("sources", []) if s["name"] == "app_commits"][0]
            check("exported source keeps credential name",
                  src["config"].get("credential") == "gh" and "token" not in src["config"], str(src))
            from tares.config import import_yaml_to_db
            store.delete_mcp_server("github")
            import_yaml_to_db(store, text)
            m = store.get_mcp_server("github")
            check("re-import restores headers and credential ref",
                  m and m["headers"] == {"X-MCP-Toolsets": "repos"}
                  and m["auth_value"] == "credential:github/gh", str(m))

            print("== delete ==")
            r = await cx.delete("/api/integrations/github/gh")
            check("delete reports what referenced it",
                  r.status_code == 200 and r.json()["sources"] == ["app_commits"]
                  and r.json()["mcp_servers"] == ["github"], r.text)
            try:
                await conn.poll()
                check("poll after delete raises a named error", False)
            except ValueError as e:
                check("poll after delete raises a named error", "gh" in str(e), str(e))

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
