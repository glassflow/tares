"""All resources (TR-351): GET /api/resources lists every part on the cell with the projects
that use it.

Covers: a source in two projects lists both; a trigger and an agent list the project they are
in; a part in the default project lists it; a revoked key and a draft project are left out; the
GitHub source is titled by its repository.

Run: .venv/bin/python tests/test_resources.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


COND = {"aggregate": "count", "predicate": "> 3", "window": "5m"}


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        store = app.state.store
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        default = next(p for p in (await cx.get("/api/projects")).json()["projects"] if p["default"])

        r = await cx.post("/api/projects", json={"template": "custom", "name": "Checkout", "objects": []})
        uid = r.json()["id"]
        await cx.post("/api/sources", json={"name": "checkout_logs", "connector": "webhook", "project": uid,
                                            "config": {"labels": [{"name": "app", "field": "app", "primary": True}]}})
        await cx.post("/api/sources", json={"name": "repo_commits", "connector": "github", "poll": "5m",
                                            "config": {"repo": "acme/shop"}})
        r = await cx.post("/api/triggers", json={"name": "checkout_spike", "project": uid,
                                                 "sources": ["checkout_logs", "repo_commits"],
                                                 "condition": COND})
        ck("trigger made", r.status_code == 201, r.text)
        await cx.post("/api/triggers", json={"name": "default_watch", "sources": ["repo_commits"],
                                             "condition": COND})
        r = await cx.post("/api/agents/builtin", json={"name": "checkout-look", "trigger": "checkout_spike",
                                                       "prompt": "Look.", "project": uid})
        ck("agent made", r.status_code in (200, 201), r.text)
        k = (await cx.post(f"/api/projects/{uid}/keys", json={"name": "claude-code"})).json()
        gone = (await cx.post(f"/api/projects/{uid}/keys", json={"name": "old-key"})).json()
        await cx.delete(f"/api/keys/{gone['id']}")
        store.create_project("uc_draft00001", "custom", "A draft", {"objects": []}, status="draft")

        res = (await cx.get("/api/resources")).json()
        ck("every kind is listed", sorted(res) == sorted(["sources", "triggers", "agents", "tools",
                                                           "skills", "keys"]), sorted(res))
        src = {s["name"]: s for s in res["sources"]}
        used = lambda row: sorted(u["name"] for u in row["used_by"])  # noqa: E731
        ck("a source two projects read lists both",
           used(src["repo_commits"]) == sorted([default["name"], "Checkout"]), src["repo_commits"])
        ck("a GitHub source says its repository", src["repo_commits"]["title"] == "acme/shop",
           src["repo_commits"])
        trig = {t["name"]: t for t in res["triggers"]}
        ck("a trigger lists its project", used(trig["checkout_spike"]) == ["Checkout"],
           trig["checkout_spike"])
        ag = {a["name"]: a for a in res["agents"]}
        ck("an agent lists its project (it follows its trigger)",
           used(ag["checkout-look"]) == ["Checkout"], ag["checkout-look"])
        keys = {x["name"]: x for x in res["keys"]}
        ck("a project key lists its project, never used yet",
           used(keys["claude-code"]) == ["Checkout"] and keys["claude-code"]["state"] == "never used",
           keys.get("claude-code"))
        ck("a revoked key is left out", "old-key" not in keys, list(keys))
        ck("a draft uses nothing", not any(u["name"] == "A draft" for rows in res.values()
                                           for r_ in rows for u in r_["used_by"]))
        ck("the secret of a key is never in it", k["secret"] not in str(res))
        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
