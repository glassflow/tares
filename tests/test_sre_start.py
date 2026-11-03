"""The Start checklist of the AI SRE home (TR-765): tares/start_steps.sre_steps, its place in
GET /api/home as `start`, and hiding it per credential (POST and DELETE /api/home/start/hide).

A new cell has five rows in a fixed order; a connected cell ticks the rows its data answers; the
demo project adds a sixth. Each row is done from one read of the store, its actions are empty once
done, the block hides when everything is done or when the caller hid it (for that caller only),
and the words pass tares/words.py.

Run: .venv/bin/python tests/test_sre_start.py
     (the Postgres variant: PYTHONPATH=tests/pg_harness TARES_TEST_PG=<dsn> the same way)
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="tares-sre-start-")
ROOT_TOKEN = "root-token-sre-start"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"

import httpx

from fixtures.sre_cell import PROJECT, build
from tares import sre_home
from tares import start_steps as ss
from tares import words
from tares.envelope import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


KEYS = ["model", "signals", "services", "watching", "slack"]


def done_of(block):
    return {s["key"]: s["done"] for s in block["steps"]}


def start_of(store, runtime, who="console"):
    runtime.reload_catalog()
    sre_home.clear_cache()
    return sre_home.build(store, runtime.catalog, None, person=who)["start"]


def main_checks_unit():
    print("== the rows, from a context ==")
    steps = ss.sre_steps(ss.SreStartCtx())
    eq("a new cell has five rows in order", [s["key"] for s in steps], KEYS)
    ck("none is done", not any(s["done"] for s in steps))
    ck("an open row has an action", all(s["actions"] for s in steps))
    ck("every row has a title and a gets line", all(s["title"] and s["gets"] for s in steps))
    ck("the signals row offers the demo", any(a.get("do") == "start_demo"
                                              for a in steps[1]["actions"]))
    for key in KEYS:
        steps = ss.sre_steps(ss.SreStartCtx(**{key: True}))
        row = next(s for s in steps if s["key"] == key)
        eq(f"{key} alone: only it is done", [s["key"] for s in steps if s["done"]], [key])
        eq(f"{key} done: no action left", row["actions"], [])
    steps = ss.sre_steps(ss.SreStartCtx(demo=True))
    eq("the demo adds the test incident row", [s["key"] for s in steps], KEYS + ["test_incident"])
    eq("the demo ticks signals", done_of({"steps": steps})["signals"], True)
    eq("a demo without a finding: the test incident is open",
       done_of({"steps": steps})["test_incident"], False)
    steps = ss.sre_steps(ss.SreStartCtx(demo=True, demo_finding=True))
    eq("a demo with a finding: the test incident is done", done_of({"steps": steps})["test_incident"], True)
    eq("a test incident without a demo is not a row",
       [s["key"] for s in ss.sre_steps(ss.SreStartCtx(demo_finding=True))], KEYS)
    steps = ss.sre_steps({"model": True, "unknown": 1})
    eq("a dict context works and ignores keys it does not know", done_of({"steps": steps})["model"], True)
    for c in (ss.SreStartCtx(), ss.SreStartCtx(demo=True)):
        bad = words.offenders_in(ss.sre_steps(c))
        eq("the words pass the word list", bad, [])


def main_checks_slack_channel():
    """The demo's investigator posts to a demo channel the cell hosts itself, so a demo
    investigator with a channel ticks Slack without a Slack token. Any other project's does not."""
    print("== the Slack row and the demo channel ==")

    class Store:
        def __init__(self, projects, channels):
            self._projects, self._channels = projects, channels

        def list_projects(self):
            return self._projects

        def list_catalog_agents(self):
            return [{"name": n, "slack_channel": c} for n, c in self._channels.items()]

        def list_catalog_sources(self):
            return []

        def project_findings(self, pid, limit=1):
            return []

    saved = (sre_home.providers.default_id, sre_home.slack_mod.resolve_token)
    sre_home.providers.default_id = lambda store: None
    sre_home.slack_mod.resolve_token = lambda store: ("", "")   # no Slack token
    try:
        demo = {"id": "prj-demo", "template": sre_home.DEMO_TEMPLATE}
        other = {"id": "prj-x", "template": "ai_sre"}
        roles = lambda project: {"investigators": [{"agent": "inv", "project": project}]}  # noqa: E731

        def slack(projects, channels, project):
            return sre_home._start_ctx(Store(projects, channels), [], {}, roles(project)).slack

        eq("a demo investigator with a channel: Slack done without a token",
           slack([demo], {"inv": "C0DEMO"}, "prj-demo"), True)
        eq("a demo investigator without a channel: Slack open",
           slack([demo], {"inv": ""}, "prj-demo"), False)
        eq("another project's investigator with a channel and no token: open",
           slack([demo, other], {"inv": "C0X"}, "prj-x"), False)
        eq("a channel but no demo project at all: open",
           slack([other], {"inv": "C0X"}, "prj-x"), False)
    finally:
        sre_home.providers.default_id, sre_home.slack_mod.resolve_token = saved


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        auth = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth)
        store, runtime = app.state.store, app.state.runtime
        os.environ.pop("ANTHROPIC_API_KEY", None)

        print("== a new cell ==")
        r = await cx.get("/api/home")
        eq("the route answers", (r.status_code, r.json()["edition"]), (200, "sre"))
        block = r.json()["start"]
        eq("five rows", [s["key"] for s in block["steps"]], KEYS)
        eq("nothing done", (block["done"], block["total"], block["show"]), (0, 5, True))
        eq("the model row is open without a key", done_of(block)["model"], False)
        ck("no forbidden word, no em dash", not words.offenders_in(block), str(words.offenders_in(block)))

        print("== a model ==")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
        eq("a provider ticks the model row", done_of(start_of(store, runtime))["model"], True)

        print("== a connected cell ==")
        now = now_utc().replace(second=0, microsecond=0)
        build(store, now)
        block = start_of(store, runtime)
        d = done_of(block)
        eq("signals and services are done", (d["signals"], d["services"]), (True, True))
        eq("watching and Slack are still open", (d["watching"], d["slack"]), (False, False))
        eq("no test incident row without the demo", "test_incident" in d, False)
        eq("three of five", (block["done"], block["total"]), (3, 5))

        print("== watching and the Slack channel ==")
        from fixtures.sre_cell import _agents
        names = _agents(store, now, True, 0)
        runtime.reload_catalog()
        sre_home.clear_cache()
        d = done_of(start_of(store, runtime))
        eq("a running watcher ticks watching", d["watching"], True)
        eq("no channel yet: Slack is open", d["slack"], False)
        store.upsert_catalog_agent(
            "investigator", "watch-prod", "Find the cause of what the watcher flagged.",
            budget_usd=25.0, concludes=True, slack_channel="C0INVENTED")
        os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-invented-token"
        eq("a channel and a Slack token tick Slack", done_of(start_of(store, runtime))["slack"], True)
        os.environ.pop("TARES_SLACK_BOT_TOKEN")
        eq("a channel without a Slack token: open", done_of(start_of(store, runtime))["slack"], False)
        ck("names came back", names["project"].endswith(PROJECT))

        print("== the demo project ==")
        store.create_project("prj-demo", "ai_sre_demo", "Demo", {}, goal="Try it")
        block = start_of(store, runtime)
        eq("six rows with the demo", [s["key"] for s in block["steps"]], KEYS + ["test_incident"])
        eq("the test incident is open without a finding", done_of(block)["test_incident"], False)
        store.start_agent_run("run_demo1", "investigator", "watch-prod", "", "demo-service", "h",
                              woken_by="handoff", project="prj-demo")
        store.finish_agent_run("run_demo1", "ok", finding="The demo-service returns 500s.",
                               outcome="finding", verdict="rca")
        eq("a finding ticks it", done_of(start_of(store, runtime))["test_incident"], True)

        print("== a project made or deleted shows on the next read ==")
        store.delete_project("prj-demo")
        sre_home.clear_cache()
        eq("no demo yet: five rows", len((await cx.get("/api/home")).json()["start"]["steps"]), 5)
        r = await cx.post("/api/projects", json={"template": "ai_sre_demo", "params": {}})
        eq("the demo is created", r.status_code, 201)
        eq("the very next read has its row (no wait on the cache)",
           len((await cx.get("/api/home")).json()["start"]["steps"]), 6)
        r = await cx.delete(f"/api/projects/{r.json()['id']}")
        eq("the demo is deleted", r.status_code, 200)
        eq("the very next read is back to five rows",
           len((await cx.get("/api/home")).json()["start"]["steps"]), 5)
        store.create_project("prj-demo", "ai_sre_demo", "Demo", {}, goal="Try it")
        sre_home.clear_cache()

        print("== all done hides the block ==")
        os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-invented-token"
        block = start_of(store, runtime)
        eq("every row done", (block["done"], block["total"], block["show"]), (6, 6, False))
        eq("a finished list is not offered back", block.get("hidden"), False)
        os.environ.pop("TARES_SLACK_BOT_TOKEN")

        print("== hiding ==")
        block = start_of(store, runtime)
        eq("shown before", block["show"], True)
        sre_home.clear_cache()
        r = await cx.get("/api/home")
        eq("the route shows it, and its answer is now cached", r.json()["start"]["show"], True)
        r = await cx.post("/api/home/start/hide")
        eq("hide answers", r.status_code, 200)
        eq("and the answer says hidden", r.json()["show"], False)
        r = await cx.get("/api/home")
        eq("the very next read is hidden (no wait on the cache)", r.json()["start"]["show"], False)
        eq("and the block says this person hid it, so the page can offer it back",
           (r.json()["start"].get("hidden"), r.json()["start"]["done"] < r.json()["start"]["total"]), (True, True))
        eq("another credential still sees it", start_of(store, runtime, "someone-else")["show"], True)
        eq("and it is not marked hidden for them", start_of(store, runtime, "someone-else").get("hidden"), False)
        r = await cx.request("DELETE", "/api/home/start/hide")
        eq("unhide answers", r.status_code, 200)
        r = await cx.get("/api/home")
        eq("shown again at once", r.json()["start"]["show"], True)
        eq("and it is not marked hidden any more", r.json()["start"].get("hidden"), False)

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

        await cx.aclose()


main_checks_unit()
main_checks_slack_channel()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
