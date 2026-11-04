"""Showing the Start checklist again (TR-828): the block says when this person hid it
(`hidden`), so the home can offer "Show it again", and DELETE /api/home/start/hide brings it back.

`hidden` is true only for the caller who hid a list that still has open steps. It is false while
the list is shown, when everything is done, and for anyone else.

Run: .venv/bin/python tests/test_factory_start_show.py
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-start-show-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-start-show"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
for _k in ("TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
           "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE", "FACTORY_PROJECT",
           "TARES_LOGIN_URL", "TARES_GITHUB_CONNECT_URL", "TARES_LINEAR_CONNECT_URL"):
    os.environ.pop(_k, None)

import httpx
from fastapi import FastAPI

from tares import factory_api
from tares import factory_home as fh
from tares import start_steps as ss

fh.WAITING_BUILDERS.clear()

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


ALL = dict(key_used=True, github_connected=True, linear_connected=True, stations=2,
           spec_with_tickets=True)


class Store:
    """The two settings calls the block uses."""
    def __init__(self):
        self.d = {}

    def get_setting(self, k):
        return self.d.get(k)

    def set_setting(self, k, v):
        if v is None:
            self.d.pop(k, None)
        else:
            self.d[k] = v


def units():
    print("== the block ==")
    st = Store()
    some = ss.factory_steps({"key_used": True})
    c = ss.checklist(st, some, "ana")
    ck("shown: not hidden", c["show"] is True and c["hidden"] is False, c)
    ss.hide(st, "ana")
    c = ss.checklist(st, some, "ana")
    ck("hidden by the caller with open steps: hidden is true", c["show"] is False and c["hidden"] is True, c)
    ck("the steps and counts stay in the block so Show it again has nothing to fetch",
       c["total"] == 5 and c["done"] == 1 and len(c["steps"]) == 5)
    c = ss.checklist(st, some, "bo")
    ck("another person did not hide it: shown, not hidden", c["show"] is True and c["hidden"] is False, c)
    c = ss.checklist(st, ss.factory_steps(ALL), "ana")
    ck("everything done: neither shown nor hidden, whatever the setting says",
       c["show"] is False and c["hidden"] is False and c["done"] == 5, c)
    ss.unhide(st, "ana")
    c = ss.checklist(st, some, "ana")
    ck("shown again after unhide", c["show"] is True and c["hidden"] is False, c)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    factory_api.register(FastAPI(), store, SimpleNamespace(cc_source=lambda: "claude_code"))
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    async with app.router.lifespan_context(app):
        print("== hide, then show it again, over the API ==")
        async with mk() as cx:
            ka = (await cx.post("/api/keys", json={"name": "key-a", "scopes": ["read"]})).json()["secret"]
            kb = (await cx.post("/api/keys", json={"name": "key-b", "scopes": ["read"]})).json()["secret"]
        async with mk(ka) as ax, mk(kb) as bx:
            async def block(c):
                fh._cache.clear()
                return (await c.get("/api/home")).json()["start"]

            a = await block(ax)
            ck("shown at first", a["show"] is True and a["hidden"] is False, a)
            r = await ax.post("/api/home/start/hide")
            ck("hide answers with the new block, marked hidden",
               r.status_code == 200 and r.json()["show"] is False and r.json()["hidden"] is True, r.text[:200])
            a = await block(ax)
            ck("the home says hidden for the one who hid it", a["show"] is False and a["hidden"] is True, a)
            b = await block(bx)
            ck("the other key still sees the list", b["show"] is True and b["hidden"] is False, b)
            r = await ax.delete("/api/home/start/hide")
            ck("show again answers with the shown block",
               r.status_code == 200 and r.json()["show"] is True and r.json()["hidden"] is False, r.text[:200])
            a = await block(ax)
            ck("and the home shows it again", a["show"] is True and a["hidden"] is False, a)


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
