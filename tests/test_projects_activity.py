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


rules()
asyncio.run(api())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
