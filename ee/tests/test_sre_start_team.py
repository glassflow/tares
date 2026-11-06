# Tares enterprise licence, see ee/LICENSE
"""The Start checklist of the AI SRE home (TR-765), the part that needs a second person: hiding it
with a key bound to a person, the hide following the person and not the name, and another person
of the same name. Runs on a licensed cell, with the demo project and a running watcher so the
checklist is not all done; the one-person part is tests/test_sre_start.py.

Run: .venv/bin/python ee/tests/test_sre_start_team.py
     (the Postgres variant: PYTHONPATH=tests/pg_harness TARES_TEST_PG=<dsn> the same way)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-sre-start-team-")
ROOT_TOKEN = "root-token-sre-start-team"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"

import httpx

from fixtures.sre_cell import build
from tares import sre_home
from tares import start_steps as ss
from tares.envelope import now_utc

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
        auth = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth)
        store, runtime = app.state.store, app.state.runtime

        # the cell the one-person file leaves behind at this point: a model, signals, services and a
        # running watcher, the demo project, no Slack: the list is not all done, so the block shows
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
        now = now_utc().replace(second=0, microsecond=0)
        build(store, now)
        from fixtures.sre_cell import _agents
        _agents(store, now, True, 0)
        runtime.reload_catalog()
        store.create_project("prj-demo", "ai_sre_demo", "Demo", {}, goal="Try it")
        sre_home.clear_cache()

        print("== hiding with a key bound to a person ==")
        # the home reads under the person's name, so the hide must be kept under it too
        r = await cx.post("/api/keys", json={"name": "kim-laptop", "scopes": ["admin"],
                                             "person": {"email": "kim@glassflow.dev", "name": "Kim"}})
        kim = {"Authorization": f"Bearer {r.json()['secret']}"}
        sre_home.clear_cache()
        eq("it shows before", (await cx.get("/api/home", headers=kim)).json()["start"]["show"], True)
        r = await cx.post("/api/home/start/hide", headers=kim)
        eq("hide answers hidden", (r.status_code, r.json()["show"]), (200, False))
        eq("and the next read is hidden", (await cx.get("/api/home", headers=kim)).json()["start"]["show"], False)
        eq("the root key still sees it", (await cx.get("/api/home")).json()["start"]["show"], True)

        print("== the hide follows the person, not the name ==")
        r = await cx.get("/api/keys")
        kim_key = next(k for k in r.json()["keys"] if k["name"] == "kim-laptop")
        kim_id = kim_key["person"]["id"]
        eq("the hide is kept under the person's id", ss.hidden(store, kim_id), True)
        r = await cx.patch(f"/api/people/{kim_id}", json={"name": "Dana Roe"})
        eq("rename answers", r.status_code, 200)
        sre_home.clear_cache()
        eq("after a rename it is still hidden", (await cx.get("/api/home", headers=kim)).json()["start"]["show"], False)
        r = await cx.post("/api/home/actions/raise_budget", json={"amount_usd": 5}, headers=kim)
        eq("the budget answer keeps the hide too", (r.status_code, r.json()["home"]["start"]["show"]), (200, False))
        r = await cx.post("/api/keys", json={"name": "other-laptop", "scopes": ["admin"],
                                             "person": {"email": "other@glassflow.dev", "name": "Dana Roe"}})
        other = {"Authorization": f"Bearer {r.json()['secret']}"}
        sre_home.clear_cache()
        eq("another person of the same name still sees it", (await cx.get("/api/home", headers=other)).json()["start"]["show"], True)
        r = await cx.post("/api/home/start/hide", headers=other)
        eq("their hide answers hidden", (r.status_code, r.json()["show"]), (200, False))
        r = await cx.request("DELETE", "/api/home/start/hide", headers=other)
        eq("their show answers shown", (r.status_code, r.json()["show"]), (200, True))
        eq("and it does not show Kim's", (await cx.get("/api/home", headers=kim)).json()["start"]["show"], False)
        r = await cx.post("/api/keys", json={"name": "bare-key", "scopes": ["admin"]})
        bare = {"Authorization": f"Bearer {r.json()['secret']}"}
        sre_home.clear_cache()
        await cx.post("/api/home/start/hide", headers=bare)
        sre_home.clear_cache()
        eq("a key with no person hides under its own name", (ss.hidden(store, "bare-key"),
           (await cx.get("/api/home", headers=bare)).json()["start"]["show"]), (True, False))

        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
