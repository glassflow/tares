# Tares enterprise licence, see ee/LICENSE
"""The 7-day usage by project is read once per request and kept for 60 seconds (TR-1627).

Home, Crew and Setup show "This week" from `usage_by_project`, a query that joins and sorts every
reply of the week. Crew used to run it twice in one request. A licensed factory cell with replies
from two projects, then the three pages are read and the statements of that query are counted:
Crew once (not twice), Home and Setup none while the kept answer is fresh, and again once the kept
answer is cleared or 60 seconds old. The share each page shows does not change.

Run: .venv/bin/python ee/tests/test_factory_usage_keep.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-usage-keep-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-usage-keep"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import claude_usage
from tares import factory_home as fh
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    hdr = {"Authorization": f"Bearer {TOKEN}"}

    async def get(path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr, timeout=60) as cx:
            r = await cx.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        return r.json()

    runs = [0]
    real = store.usage_by_project

    def counted(since=None):
        runs[0] += 1
        return real(since)

    def fresh():
        """Both kept answers forgotten, so the next page reads as the first one of a minute."""
        fh.clear_cache()
        claude_usage.clear_week_by_project()
        runs[0] = 0

    async with app.router.lifespan_context(app):
        for uid, name, cost in (("uc_keep_a", "Alpha", 3.0), ("uc_keep_b", "Beta", 1.0)):
            store.create_project(uid, "custom", name, {}, goal=f"Goal of {name}")
            store.set_project_kind(uid, "software_factory")
            store.record_reply_usage(f"sess-{uid}", f"m-{uid}", now_utc() - timedelta(hours=2),
                                     "claude-opus-5-5", uid, None,
                                     {"input_tokens": int(cost * 1000), "output_tokens": 10})
        store.usage_by_project = counted

        print("== Crew: once per request ==")
        fresh()
        crew = await get("/api/factory/crew")
        ck("Crew reads the week once, not twice", runs[0] == 1, runs[0])
        shown = [p["name"] for p in crew["plan"]["by_project"]]
        ck("the by-project block lists both projects", set(shown) == {"Alpha", "Beta"}, shown)
        ck("the plan line carries the share too", bool(crew["plan"].get("share_text")), crew["plan"])
        runs[0] = 0
        again = await get("/api/factory/crew")
        ck("a second Crew read within the minute reads it zero times", runs[0] == 0, runs[0])
        ck("the kept answer is the same answer", again["plan"] == crew["plan"], (again["plan"], crew["plan"]))

        print("== Home and Setup: at most once a minute ==")
        fh.clear_cache()
        runs[0] = 0
        await get("/api/home")
        ck("Home reads it zero times while the kept answer is fresh", runs[0] == 0, runs[0])
        await get("/api/factory/setup/progress")
        ck("Setup reads it zero times too", runs[0] == 0, runs[0])
        fresh()
        home = await get("/api/home")
        ck("Home on a cold start reads it once", runs[0] == 1, runs[0])
        ck("Home shows the same share Crew does", home["plan"]["share_text"] == crew["plan"]["share_text"],
           (home["plan"]["share_text"], crew["plan"]["share_text"]))
        fresh()
        await get("/api/factory/setup/progress")
        ck("Setup on a cold start reads it once", runs[0] == 1, runs[0])

        print("== the kept answer expires ==")
        fresh()
        await get("/api/factory/crew")
        key = id(store)
        at, ref, rows = claude_usage._week[key]
        claude_usage._week[key] = (at - claude_usage.WEEK_KEEP_SECONDS - 1, ref, rows)   # 61 seconds old
        runs[0] = 0
        fh.clear_cache()         # the kept home goes; the kept week stays
        await get("/api/factory/crew")
        ck("a 61 second old answer is read again", runs[0] == 1, runs[0])
        runs[0] = 0
        await get("/api/factory/crew")
        ck("and the new one is kept", runs[0] == 0, runs[0])

        print("== a kept week belongs to its store ==")
        rows = claude_usage.week_by_project(store, now_utc())
        rows[0]["share"] = -1
        ck("a caller changing the rows it got does not change the kept ones",
           all((r["share"] or 0) >= 0 for r in claude_usage.week_by_project(store, now_utc())))
        other = type(store).__new__(type(store))
        other.usage_by_project = lambda since=None: []
        other.list_projects = lambda *a, **kw: []
        ck("another store does not read this store's answer",
           claude_usage.week_by_project(other, now_utc()) == [])

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
