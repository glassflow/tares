"""Runs list paging and status filter (TR-267): GET /api/agents/builtin/{name}/runs takes
`status` and `offset`, so a console can show only the successful runs of an agent whose list is
mostly capped rows, and keep paging past the first page.

Run: .venv/bin/python tests/test_agent_runs_filter.py
"""
import asyncio
import os

DB = "/tmp/tares-runs-filter.duckdb"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = "/tmp/tares-runs-filter.catalog.yaml"
for _p in (DB, DB + ".wal", os.environ["TARES_CATALOG"]):
    if os.path.exists(_p):
        os.remove(_p)

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
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store

        r = await cx.post("/api/sources", json={
            "name": "evt", "connector": "webhook", "poll": "5s",
            "config": {"event_type": "log", "text_template": "{msg}",
                       "labels": [{"name": "service", "field": "service", "primary": True}]}})
        ck("source", r.status_code == 201, r.text)
        r = await cx.post("/api/triggers", json={
            "name": "t", "sources": ["evt"], "key_field": "service",
            "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"},
            "emit": {"kind": "x"}, "cooldown": "1m"})
        ck("trigger", r.status_code == 201, r.text)
        default = next(p["id"] for p in (await cx.get("/api/projects")).json()["projects"] if p["default"])
        ck("trigger lands in the default project", r.json().get("project") == default, r.text)
        r = await cx.post("/api/agents/builtin", json={"name": "a", "trigger": "t", "prompt": "look"})
        ck("agent", r.status_code == 201, r.text)

        # 60 capped, 7 ok, 3 failed, newest last so ordering is observable
        statuses = ["capped"] * 60 + ["ok"] * 7 + ["failed"] * 3
        for i, st in enumerate(statuses):
            rid = f"run{i:03d}"
            store.start_agent_run(rid, "a", "t", f"d{i}", f"svc{i}", "h", 6)
            store.finish_agent_run(rid, st, rounds=1 if st == "ok" else 0,
                                   finding="found" if st == "ok" else None,
                                   error="boom" if st == "failed" else None)

        print("== default page ==")
        page = (await cx.get("/api/agents/builtin/a/runs")).json()
        ck("default limit is 50, newest first", len(page) == 50 and page[0]["id"] == "run069", str(len(page)))

        print("== status filter ==")
        ok = (await cx.get("/api/agents/builtin/a/runs?status=ok")).json()
        ck("status=ok returns only the ok runs", len(ok) == 7 and all(x["status"] == "ok" for x in ok), str(len(ok)))
        failed = (await cx.get("/api/agents/builtin/a/runs?status=failed")).json()
        ck("status=failed returns only the failed runs", len(failed) == 3 and all(x["status"] == "failed" for x in failed))
        r = await cx.get("/api/agents/builtin/a/runs?status=nope")
        ck("unknown status -> 400", r.status_code == 400, r.text)

        print("== paging ==")
        first = (await cx.get("/api/agents/builtin/a/runs?limit=50&offset=0")).json()
        second = (await cx.get("/api/agents/builtin/a/runs?limit=50&offset=50")).json()
        ids = [x["id"] for x in first] + [x["id"] for x in second]
        ck("offset pages through all 70 with no overlap", len(second) == 20 and len(set(ids)) == 70, str(len(second)))
        ok2 = (await cx.get("/api/agents/builtin/a/runs?status=ok&limit=5&offset=5")).json()
        ck("filter and offset combine", len(ok2) == 2 and all(x["status"] == "ok" for x in ok2), str(ok2))
        big = (await cx.get("/api/agents/builtin/a/runs?limit=1000")).json()
        ck("limit is capped at 200 (here: everything)", len(big) == 70)

        r = await cx.get("/api/agents/builtin/ghost/runs")
        ck("unknown agent -> 404", r.status_code == 404)
        await cx.aclose()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
