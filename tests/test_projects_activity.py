"""The Projects list's Updated column (TR-689): GET /api/projects gives every project an
`activity_at`. An ordinary project's is its own update; a software factory project's is the newer of
its own update and its newest ticket's, so a project the crew is working on reads as fresh
without anyone editing the project itself.

Run: .venv/bin/python tests/test_projects_activity.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-projects-activity-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-activity"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
          "TARES_PLATFORM_PROVIDER_KEY", "TARES_SLACK_BOT_TOKEN"):
    os.environ.pop(k, None)

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory as factory_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def when(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def rules():
    print("== the rule ==")
    a, b = datetime(2026, 1, 1), datetime(2026, 1, 5)
    ck("no tickets: the project's own update", factory_mod.activity_at(a, []) == a)
    ck("a newer ticket wins", factory_mod.activity_at(a, [{"updated_at": b}, {"updated_at": a}]) == b)
    ck("an older ticket does not", factory_mod.activity_at(b, [{"updated_at": a}]) == b)
    ck("a ticket with no stamp is ignored", factory_mod.activity_at(a, [{"updated_at": None}]) == a)
    ck("nothing at all is None", factory_mod.activity_at(None, []) is None)


async def api():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t",
                                              headers={"Authorization": f"Bearer {TOKEN}"})
    m._cx = mf._cx = mk

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        async with mk() as cx:
            blog = (await cx.post("/api/projects", json={"template": "custom", "name": "Blog",
                                                          "objects": []})).json()["id"]
        await m.write_ticket("Catalog", project="Shop")
        await mf.hand_over(project="Shop", repo="/w/shop")
        old = datetime.now(timezone.utc) - timedelta(days=9)
        store.con.execute("UPDATE usecases SET updated_at = ? WHERE id IN (?, ?)", [old, shop, blog])
        store.con.execute("UPDATE tickets SET updated_at = ? WHERE project = ?",
                          [old + timedelta(days=1), shop])
        newest = old + timedelta(days=3)
        store.con.execute("UPDATE tickets SET updated_at = ? WHERE project = ? AND number = 1",
                          [newest, shop])

        print("== GET /api/projects ==")
        async with mk() as cx:
            rows = {p["id"]: p for p in (await cx.get("/api/projects")).json()["projects"]}
        ck("an ordinary project reports its own update", when(rows[blog]["activity_at"]) == old,
           rows[blog])
        ck("a software factory project reports its newest ticket's update",
           when(rows[shop]["activity_at"]) == newest, rows[shop])
        ck("the project's own updated_at is left as it was", when(rows[shop]["updated_at"]) == old,
           rows[shop])
        ck("only the factory project carries progress",
           "progress" in rows[shop] and "progress" not in rows[blog], [list(rows[shop]), list(rows[blog])])


async def reread():
    """A PR read again with nothing new is not activity (the reviewer's probe on TR-689):
    `stale()` re-reads every unmerged PR each minute, which stamps a new `checked_at`."""
    from tares import factory_api, factory_github
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = lambda timeout=10: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {TOKEN}"})
    print("== a re-read of an unchanged pull request ==")

    async with app.router.lifespan_context(app):
        uid = json.loads(await m.create_project("Reread", "Keep the date honest"))["id"]
        await m.write_ticket("Catalog", project="Reread")
        await mf.hand_over(project="Reread", repo="/w/reread")
        tid = store.list_tickets(uid)[0]["id"]
        old = datetime.now(timezone.utc) - timedelta(days=9)
        pr = {"repo": "o/r", "number": 5, "url": "https://github.com/o/r/pull/5", "state": "open",
              "merged": False, "head": "abc", "ci": "pending", "verdict": None,
              "checked_at": old.isoformat(), "error": None}
        store.update_ticket(tid, pr=pr)
        store.con.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", [old, tid])
        store.con.execute("UPDATE usecases SET updated_at = ? WHERE id = ?", [old, uid])
        reads = []

        async def same(_store, repo, number, prev=None, **changes):
            reads.append(1)
            return {**prev, "checked_at": datetime.now(timezone.utc).isoformat(), **changes}

        real = factory_github.read_pr
        factory_github.read_pr = same
        try:
            await factory_api._refresh(store, uid)
            ck("the pull request was read again", len(reads) == 1, reads)
            t = store.list_tickets(uid)[0]
            ck("and its new checked_at was kept", when(t["pr"]["checked_at"]) > old + timedelta(days=8), t["pr"])
            ck("the ticket did not move", when(t["updated_at"]) == old, t["updated_at"])
            ck("so the project did not either",
               when(factory_mod.activity_at(old, store.list_tickets(uid))) == old)

            reads.clear()
            store.update_ticket(tid, pr={**t["pr"], "checked_at": old.isoformat()}, touch=False)
            factory_github.read_pr = lambda *a, **k: same(*a, error="GitHub answered 404", **k)
            await factory_api._refresh(store, uid)
            t = store.list_tickets(uid)[0]
            ck("a read that failed is not activity either",
               t["pr"]["error"] == "GitHub answered 404" and when(t["updated_at"]) == old, t)

            reads.clear()
            store.update_ticket(tid, pr={**t["pr"], "checked_at": old.isoformat(), "error": None}, touch=False)
            factory_github.read_pr = lambda *a, **k: same(*a, ci="success", **k)
            await factory_api._refresh(store, uid)
            t = store.list_tickets(uid)[0]
            ck("a pull request that changed is", t["pr"]["ci"] == "success"
               and when(t["updated_at"]) > old + timedelta(days=8), t["updated_at"])
        finally:
            factory_github.read_pr = real


rules()
asyncio.run(api())
asyncio.run(reread())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
