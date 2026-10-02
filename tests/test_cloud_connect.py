"""The workspace side of connecting GitHub and Slack from Tares Cloud (P-TR-222, OSS 1.41.0).

Run: .venv/bin/python tests/test_cloud_connect.py   (no network)

Covers:
* /health: `slack_connect_url` and `workspaces_url` only when TARES_SLACK_CONNECT_URL and
  TARES_WORKSPACES_URL are set, next to `github_connect_url`.
* app_broker `repositories`: [] by default, stored on create, replaced wholesale by a PUT that
  carries it, kept by a PUT that does not, trimmed and deduplicated, a bad name refused, and
  returned by the credential row. A token credential never shows the field.
* PUT/DELETE /api/settings/slack-team and `team` on GET /api/settings/slack-bot-token.
* Both slack-team routes need admin: a read key gets 403, no key gets 401.
"""
import asyncio
import os

os.environ["TARES_DB"] = "/tmp/tares-cloud-connect-test.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/does-not-exist.yaml"
os.environ["TARES_AUTH_TOKEN"] = "root-secret"
for _k in ("TARES_SLACK_CONNECT_URL", "TARES_WORKSPACES_URL", "TARES_GITHUB_CONNECT_URL"):
    os.environ.pop(_k, None)

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


CP = "https://console.tares-glassflow.com"
BROKER = {"name": "cloud", "kind": "app_broker", "token_url": f"{CP}/api/github/token",
          "broker_secret": "broker-secret", "installation_id": 101, "account": "glassflow",
          "webhook_secret": "whsec"}


async def main():
    for p in (os.environ["TARES_DB"], os.environ["TARES_DB"] + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    from tares import daemon
    app = daemon.make_app()
    root = {"Authorization": "Bearer root-secret"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers=root) as cx, \
                httpx.AsyncClient(transport=transport, base_url="http://test") as anon:

            print("== /health ==")
            h = (await anon.get("/health")).json()
            check("self-host: no slack_connect_url, workspaces_url or github_connect_url",
                  not ({"slack_connect_url", "workspaces_url", "github_connect_url"} & set(h)),
                  str(h))
            daemon.SLACK_CONNECT_URL = f"{CP}/slack/install?workspace=acme"
            daemon.WORKSPACES_URL = f"{CP}/api/me/workspaces"
            daemon.GITHUB_CONNECT_URL = f"{CP}/api/github/connect?workspace=acme"
            h = (await anon.get("/health")).json()
            check("cloud: slack_connect_url on the public /health",
                  h.get("slack_connect_url") == f"{CP}/slack/install?workspace=acme", str(h))
            check("cloud: workspaces_url on the public /health",
                  h.get("workspaces_url") == f"{CP}/api/me/workspaces", str(h))
            check("cloud: github_connect_url still there",
                  h.get("github_connect_url") == f"{CP}/api/github/connect?workspace=acme")
            daemon.SLACK_CONNECT_URL = ""
            h = (await anon.get("/health")).json()
            check("each field follows its own variable",
                  "slack_connect_url" not in h and "workspaces_url" in h, str(h))
            daemon.WORKSPACES_URL = daemon.GITHUB_CONNECT_URL = ""

            async def row(name):
                r = await cx.get("/api/integrations/github")
                return next((c for c in r.json()["credentials"] if c["name"] == name), None)

            print("== app_broker repositories ==")
            r = await cx.post("/api/integrations/github", json=BROKER)
            check("create without repositories -> 201", r.status_code == 201, r.text)
            check("no repositories picked -> []", (await row("cloud"))["repositories"] == [])

            r = await cx.put("/api/integrations/github/cloud",
                             json={"name": "cloud", "repositories": [
                                 " glassflow/api ", "glassflow/web", "GlassFlow/API"]})
            check("PUT with repositories -> 200", r.status_code == 200, r.text)
            check("stored trimmed, in order, without repeats",
                  (await row("cloud"))["repositories"] == ["glassflow/api", "glassflow/web"],
                  str((await row("cloud"))["repositories"]))

            r = await cx.put("/api/integrations/github/cloud",
                             json={"name": "cloud", "account": "glassflow"})
            check("PUT without repositories -> 200", r.status_code == 200, r.text)
            check("PUT without repositories keeps them",
                  (await row("cloud"))["repositories"] == ["glassflow/api", "glassflow/web"])

            r = await cx.put("/api/integrations/github/cloud",
                             json={"name": "cloud", "repositories": ["glassflow/docs"]})
            check("PUT replaces the list wholesale",
                  (await row("cloud"))["repositories"] == ["glassflow/docs"],
                  str((await row("cloud"))["repositories"]))

            r = await cx.put("/api/integrations/github/cloud",
                             json={"name": "cloud", "repositories": []})
            check("PUT with [] clears the list", (await row("cloud"))["repositories"] == [])

            r = await cx.put("/api/integrations/github/cloud",
                             json={"name": "cloud", "repositories": ["not-a-repo"]})
            check("a name that is not owner/repo -> 400", r.status_code == 400, r.text)
            check("a refused PUT changes nothing", (await row("cloud"))["repositories"] == [])

            r = await cx.post("/api/integrations/github", json={
                **BROKER, "name": "cloud2", "repositories": ["glassflow/api", "glassflow/api"]})
            check("create with repositories -> 201", r.status_code == 201, r.text)
            check("create stores them, deduplicated",
                  (await row("cloud2"))["repositories"] == ["glassflow/api"])
            check("the row carries no broker secret",
                  "broker-secret" not in str(await row("cloud2")))

            await cx.post("/api/integrations/github", json={"name": "pat", "token": "ghp_x",
                                                            "api_url": "http://127.0.0.1:9"})
            check("a token credential has no repositories field",
                  "repositories" not in (await row("pat")))

            print("== Slack team ==")
            r = await cx.get("/api/settings/slack-bot-token")
            check("no team stored -> team null", r.status_code == 200
                  and r.json().get("team") is None and "team" in r.json(), r.text)
            r = await cx.put("/api/settings/slack-team",
                             json={"team_id": " T0123ABC ", "team_name": "GlassFlow"})
            check("PUT slack-team -> 200", r.status_code == 200, r.text)
            r = await cx.get("/api/settings/slack-bot-token")
            check("GET slack-bot-token returns the team",
                  r.json().get("team") == {"id": "T0123ABC", "name": "GlassFlow"}, r.text)
            r = await cx.put("/api/settings/slack-team", json={"team_id": "", "team_name": "x"})
            check("PUT without team_id -> 400", r.status_code == 400, r.text)
            r = await cx.delete("/api/settings/slack-team")
            check("DELETE slack-team -> 200", r.status_code == 200, r.text)
            r = await cx.get("/api/settings/slack-bot-token")
            check("after DELETE the team is null", r.json().get("team") is None, r.text)

            print("== admin scope ==")
            rk = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
            reader = {"Authorization": "Bearer "
                      + (rk.json().get("secret") or rk.json().get("key") or "")}
            r = await anon.put("/api/settings/slack-team", headers=reader,
                               json={"team_id": "T1", "team_name": "x"})
            check("a read key cannot set the team (403)", r.status_code == 403,
                  f"{rk.status_code} {r.status_code}")
            r = await anon.delete("/api/settings/slack-team", headers=reader)
            check("a read key cannot clear the team (403)", r.status_code == 403, str(r.status_code))
            r = await anon.put("/api/settings/slack-team", json={"team_id": "T1"})
            check("no key -> 401", r.status_code == 401, str(r.status_code))
            r = await anon.put("/api/integrations/github/cloud", headers=reader,
                               json={"name": "cloud", "repositories": ["a/b"]})
            check("a read key cannot change repositories (403)", r.status_code == 403,
                  str(r.status_code))

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
