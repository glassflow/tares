"""GitHub App webhooks through the ci_github integration (Tares 2.0, TR-1805, TR-1807).

The App's webhook key lives in `integration_keys`; /ingest/<key> finds it there, checks the delivery
with the App credential's webhook secret, keeps a `pull_request` event in `pr_events`, and tells core's
GitHub hub. `factory_branch_prs` reads `pr_events`, so a ticket in the work gets its pull request by
its factory/<ref> branch from a delivery alone. Run against a real daemon:

  * connecting an App credential writes its key to integration_keys, the key the answer names, its
    events under `github` (a second App's under `github_<credential>`);
  * a signed pull_request delivery answers 202, lands in pr_events and is told to the hub;
  * a redelivery (same delivery id) adds no row; an unsigned or wrongly signed one answers 401 and
    adds nothing;
  * factory_branch_prs names the pull request for the ticket ref of its branch;
  * the settings row shows the key and the deliveries; a key that is no App's answers 404;
  * rows older than 30 days go when new ones are kept.

Run: python tests/test_github_webhooks.py
"""
import asyncio, json, os, sys, tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-gh-hooks-")
AUTH = "root-token-gh-hooks"
os.environ.update({"TARES_HOME": os.path.join(TMP, "gh"), "TARES_AUTH_TOKEN": AUTH,
                   "TARES_EDITION": "factory"})

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


SECRET = "hook-secret"


def pr_payload(number: int, branch: str, action: str = "opened") -> dict:
    return {"action": action, "number": number, "installation": {"id": 101},
            "repository": {"full_name": "acme/app", "default_branch": "main"},
            "pull_request": {"number": number, "title": f"PR {number}", "state": "open",
                             "user": {"login": "kim"}, "head": {"ref": branch, "sha": "abc"},
                             "base": {"ref": "main"}, "html_url": f"https://github.com/acme/app/pull/{number}",
                             "created_at": "2026-11-27T10:00:00Z", "updated_at": "2026-11-27T10:00:00Z"}}


async def main():
    from tares.daemon import make_app
    from tares.github_webhook import HUB
    from tares.webhook_verify import sign
    from tares.envelope import now_utc
    app = make_app()
    heard = []
    hear = lambda source, envs: heard.extend((source, e.event_type) for e in envs)   # noqa: E731
    HUB.subscribe(hear)
    async with app.router.lifespan_context(app):
        store = app.state.store
        H = {"Authorization": f"Bearer {AUTH}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        print("== connect ==")
        r = await cx.post("/api/integrations/github", json={
            "name": "cloud", "kind": "app_broker", "token_url": "https://broker.test/t",
            "broker_secret": "broker-secret", "installation_id": 101, "account": "acme",
            "webhook_secret": SECRET})
        key = r.json().get("ingest_key") if r.status_code == 201 else None
        ck("connecting the App answers 201 with its ingest key", bool(key), r.text[:200])
        row = store.integration_key(key or "")
        ck("the key is in integration_keys, the ci_github integration's, for the credential",
           bool(row) and row["integration"] == "ci_github" and row["credential"] == "cloud", str(row))
        ck("its events go under `github`, as the answer says", r.json().get("source") == "github"
           and (row or {}).get("source") == "github" and key.startswith("github_app-")
           and len(key) == len("github_app-") + 16, r.text[:200])

        async def deliver(payload, delivery, secret=SECRET, event="pull_request"):
            raw = json.dumps(payload).encode()
            h = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "content-type": "application/json"}
            if secret is not None:
                h["X-Hub-Signature-256"] = sign("github", secret, raw)
            return await anon.post(f"/ingest/{key}", content=raw, headers=h)

        def pr_count():
            from sqlalchemy import func, select
            from tares.session_store.tables import pr_events
            with store.sessions_db.read() as conn:
                return conn.execute(select(func.count()).select_from(pr_events)).scalar()

        print("== a pull request ==")
        r = await deliver(pr_payload(7, "factory/T12"), "d-1")
        ck("a signed pull_request delivery answers 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
        ck("it is kept in pr_events", pr_count() == 1, pr_count())
        ck("the GitHub hub heard it", ("github", "pull_request") in heard, str(heard))
        r = await deliver(pr_payload(7, "factory/T12"), "d-1")
        ck("a redelivery adds no row", r.status_code == 202 and pr_count() == 1, f"{r.status_code} {pr_count()}")
        r = await deliver(pr_payload(8, "factory/T13"), "d-2", secret=None)
        ck("an unsigned delivery answers 401 and adds nothing", r.status_code == 401 and pr_count() == 1,
           f"{r.status_code} {pr_count()}")
        r = await deliver(pr_payload(8, "factory/T13"), "d-3", secret="not-the-secret")
        ck("a wrongly signed delivery answers 401 and adds nothing", r.status_code == 401 and pr_count() == 1,
           f"{r.status_code} {pr_count()}")
        r = await deliver(pr_payload(9, "feature/x"), "d-4")
        seen = store.factory_branch_prs({"acme/app"})
        ck("factory_branch_prs names the pull request of a factory/<ref> branch, and no other",
           seen == {("acme/app", "T12"): ("acme/app", 7)}, str(seen))

        print("== the settings row, a second App, an unknown key ==")
        r = await cx.get("/api/integrations/github")
        cred = next((c for c in r.json()["credentials"] if c["name"] == "cloud"), {})
        d = cred.get("deliveries") or {}
        ck("the settings row names the key, the source and the deliveries", cred.get("ingest_key") == key
           and cred.get("source") == "github" and d.get("received") == 3 and d.get("stored") == 2 and d.get("rejected_signature") == 2
           and cred.get("sources") == [] and cred.get("mcp_servers") == [], str({k: cred.get(k) for k in ("ingest_key", "source", "deliveries", "sources", "mcp_servers")}))
        r = await cx.post("/api/integrations/github", json={
            "name": "second", "kind": "app_broker", "token_url": "https://broker.test/t",
            "broker_secret": "broker-secret", "installation_id": 102, "account": "beta",
            "webhook_secret": SECRET})
        ck("a second App's events go under github_<credential>", r.status_code == 201
           and r.json().get("source") == "github_second" and r.json().get("ingest_key") != key, r.text[:200])
        r = await cx.post("/ingest/github_app-0000000000000000", json={})
        ck("a key that is no App's answers 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        r = await anon.post("/ingest/github_app-0000000000000000", json={})
        ck("and with no credential, 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
        store.put_integration_key("github_app-new", "ci_github", "cloud", "github")
        ck("a new key for the same credential replaces its old one",
           store.integration_key(key) is None and store.integration_key("github_app-new") is not None)
        r = await cx.delete("/api/integrations/github/cloud")
        ck("deleting the credential removes its webhook key", r.status_code == 200
           and store.integration_key("github_app-new") is None, r.text[:200])

        print("== 30 days ==")
        old = now_utc() - timedelta(days=40)
        store.record_pr_events([{"delivery_id": "old-1", "repo": "acme/app", "number": 1,
                                 "branch": "factory/T1", "action": "opened", "at": old}])
        store.record_pr_events([{"delivery_id": "new-1", "repo": "acme/app", "number": 2,
                                 "branch": "factory/T2", "action": "opened", "at": now_utc()}])
        seen = store.factory_branch_prs({"acme/app"})
        ck("a row older than 30 days goes when new ones are kept", ("acme/app", "T1") not in seen
           and ("acme/app", "T2") in seen, str(seen))
        await cx.aclose()
        await anon.aclose()
    HUB.unsubscribe(hear)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
