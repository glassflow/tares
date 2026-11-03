"""The Crew page standup, "In the last day" (TR-826): it counts what happened in the last 24 hours,
read from the times of the ticket changes and of the questions, not what merely stands now.

Seeds a project with tickets that went to review, were blocked, merged or started a few days ago and
tickets that did the same just now, plus an old and a new question on the desk, and checks that only
the new ones are counted. Then makes everything old and checks the standup is empty.

Run: .venv/bin/python tests/test_factory_crew_standup.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-crew-standup-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-standup"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                   headers={"Authorization": f"Bearer {TOKEN}"})
    m._cx = mf._cx = mk
    store = app.state.store
    long_ago = datetime.now(timezone.utc) - timedelta(days=3)

    def backdate(tickets, desk_questions=()):
        from tares import factory as factory_mod
        ids = {factory_mod.label(t): t["id"] for uid in store.handed_over_projects()
               for t in store.list_tickets(uid)}
        with store._swrite:
            for t in tickets:
                store.sc.execute("UPDATE ticket_history SET changed_at = ? WHERE ticket = ?",
                                 [long_ago, ids[t]])
            for q in desk_questions:
                store.sc.execute("UPDATE desk_items SET created_at = ? WHERE question = ?", [long_ago, q])

    async def standup(cx):
        r = await cx.get("/api/factory/crew")
        assert r.status_code == 200, r.text
        return r.json()["standup"]

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            px = json.loads(await m.create_project("project-x", "Ship the docs site"))["id"]
            for t in ("Old review", "New review", "Old blocked", "New blocked", "Old merge", "New merge",
                      "New start", "Old start"):
                await m.write_ticket(t, project="project-x", repo="example-org/docs-site")
            await cx.post(f"/api/projects/{px}/handover",
                          json={"repos": [{"path": "/w/docs-site", "name": "example-org/docs-site"}]})

            async def stage(n, *stages):
                for s in stages:
                    body = {"stage": s, **({"reason": "waiting for the key"} if s == "blocked" else {})}
                    r = await cx.post(f"/api/projects/{px}/tickets/T{n}/stage", json=body)
                    assert r.status_code == 200, r.text

            await stage(1, "doing", "review")
            await stage(3, "doing", "blocked")
            await stage(5, "doing", "merged")
            await stage(8, "doing")
            backdate(["T1", "T3", "T5", "T8"])
            for q in ("An old question?", "A new question?"):
                r = await cx.post("/api/desk", json={"question": q, "recommendation": "Yes", "why": "ok",
                                                     "project": px, "blocking": False})
                assert r.status_code == 201, r.text
            backdate([], ["An old question?"])
            await stage(2, "doing", "review")
            await stage(4, "doing", "blocked")
            await stage(6, "doing", "merged")
            await stage(7, "doing")

            print("== events of the last day count, what stands from before does not ==")
            s = await standup(cx)
            ck("old and new in each group: only the new ones are counted",
               s is not None and s["text"] == "In the last day: 1 ticket done, 1 started, 1 sent to review, "
               "1 blocked, 1 question for you. Projects that moved: project-x.", s)
            ck("the window is named", s is not None and s["when"] == "The last 24 hours", s)

            print("== a project where only old things stand ==")
            backdate(["T2", "T4", "T6", "T7"], ["A new question?"])
            s = await standup(cx)
            ck("nothing happened in the last day, so there is no standup line", s is None, s)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
