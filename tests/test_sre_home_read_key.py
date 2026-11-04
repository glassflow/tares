"""A read key on the AI SRE home (TR-895): the page is told it cannot press its buttons.

GET /api/home carries `can_act`: true for a key that may change things, false for one that can
only read. The console hides the buttons and says which key it is when the flag is false. The
flag is added after the cache, because the cache is shared by every key of one name. The action
routes still refuse a read key (403), so the flag is for the screen, not the lock.

Run: .venv/bin/python tests/test_sre_home_read_key.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="tares-sre-home-read-key-")
ROOT_TOKEN = "root-token-sre-home-read-key"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from tares import sre_home

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        root = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root)

        async def key(name, scopes):
            r = await cx.post("/api/keys", json={"name": name, "scopes": scopes})
            return {"Authorization": f"Bearer {r.json()['secret']}"}

        async def can_act(headers):
            r = await cx.get("/api/home", headers=headers)
            return r.status_code, r.json().get("can_act")

        print("== who may press the buttons ==")
        sre_home.clear_cache()
        eq("the cell's own token", await can_act(root), (200, True))
        eq("an admin key", await can_act(await key("ops", ["admin"])), (200, True))
        eq("a read key", await can_act(await key("viewer", ["read"])), (200, False))
        eq("a key that reads and ingests", await can_act(await key("shipper", ["read", "ingest"])), (200, False))

        print("== the flag is not in the cache ==")
        read = await key("same-name", ["read"])
        sre_home.clear_cache()
        eq("a read key reads first", await can_act(read), (200, False))
        # an admin key of the same name is served from the same cached page; it must still be true
        admin = await key("same-name", ["admin"])
        eq("then an admin key of that name, inside the cache window", await can_act(admin), (200, True))
        eq("and the read key again", await can_act(read), (200, False))

        print("== the routes still refuse a read key ==")
        r = await cx.post("/api/home/actions/raise_budget", headers=read, json={"amount_usd": 5})
        eq("raise the budget", r.status_code, 403)
        ck("and the refusal names the scope, not the person", "scope" in r.text, r.text)
        r = await cx.post("/api/incidents/inc-x/on_it", headers=read)
        eq("pick an incident up", r.status_code, 403)
        r = await cx.post("/api/incidents/inc-x/resolve", headers=read)
        eq("resolve an incident", r.status_code, 403)
        r = await cx.put("/api/findings/run-x/worth", headers=read, json={"worth": True})
        eq("answer whether a finding was worth raising", r.status_code, 403)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
