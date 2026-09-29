"""Two sources of one trigger ingesting within the trigger debounce interval must both fire (per key).
Regression for the shared code context cookbook: billing fired, orders (ingested one second later)
never did, because the debounced evaluation was dropped and the 2m window slid past the commits.
Run: .venv/bin/python tests/test_trigger_debounce.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-trigger-debounce-test.duckdb"
CATALOG = "/tmp/tares-trigger-debounce-test.catalog.yaml"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = CATALOG
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "3"

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


CATALOG_YAML = """
sources:
  - name: a_repo
    connector: webhook
    config:
      event_type: commit
      labels: [{name: repo, field: repo, primary: true}]
  - name: b_repo
    connector: webhook
    config:
      event_type: commit
      labels: [{name: repo, field: repo, primary: true}]
triggers:
  - name: changes
    sources: [a_repo, b_repo]
    key_field: repo
    condition: {aggregate: count, predicate: "> 0", window: 2m, group_by: [key_value]}
    emit: {kind: change, context_window: 15m}
    cooldown: 5m
"""


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    open(CATALOG, "w").write(CATALOG_YAML)
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:
            srcs = (await cx.get("/api/sources")).json()
            paths = {s["name"]: f"/ingest/{s.get('ingest_key') or s['name']}" for s in srcs}
            r1 = await cx.post(paths["a_repo"], json={"repo": "org/a", "sha": "1"})
            check("a ingested", r1.status_code == 202, r1.text[:200])
            await asyncio.sleep(0.5)          # inside the 3s debounce
            r2 = await cx.post(paths["b_repo"], json={"repo": "org/b", "sha": "2"})
            check("b ingested one second later", r2.status_code == 202, r2.text[:200])

            async def fired_keys():
                rows = (await cx.get("/api/activity/dispatches")).json()
                return sorted({str(x.get("key")) for x in rows if x.get("trigger") == "changes"})

            k = await fired_keys()
            check("first key fired immediately", "org/a" in k, str(k))
            # the second key must fire once the debounce interval passes, not never
            for _ in range(20):
                k = await fired_keys()
                if "org/b" in k:
                    break
                await asyncio.sleep(0.5)
            check("second key fires after the debounce catch-up", "org/b" in k, str(k))
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
