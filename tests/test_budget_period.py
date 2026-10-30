"""Monthly budgets (TR-770): an agent's `budget_period` is "lifetime" (the default, every dollar it
ever spent counts) or "month" (only this calendar month's, starting again on the 1st at 00:00 UTC).

Covers the one rule (Store.agent_budget_spend and budget_resets_on), the run path (capped with the
right words, free again on the 1st), project health's words, the catalog (validation, export, the
API round trip, an update that leaves the field out).

The model loop is a stand-in, so the real run path runs without a model. Time moves through
ba._clock (the runner) and project_health(now=); old spend is a run row with an old started_at.

Run: .venv/bin/python tests/test_budget_period.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
# a provider must resolve for a run to start; the stand-in loop never calls it
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx
import yaml

import tares.builtin_agents as ba
from tares import goal as G
from tares.builtin_agents import AgentRunner
from tares.config import CatalogError, export_db_to_yaml, validate_agent_dict
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    concluded.update({"outcome": "finding", "summary": "looked", "verdict": None, "key": None,
                      "label": None, "headline": None, "next_step": None, "produced": []})
    return "looked", 1, 1, [], False, ""


AgentRunner._loop = fake_loop

UTC = timezone.utc
LAST_MONTH = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
THIS_MONTH = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LAST_MINUTE = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
FIRST = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)

LIFETIME_WORDS = "budget of $0.05 for this agent reached; raise or clear it under Configuration, Advanced"
MONTH_WORDS = ("budget of $0.05 for October reached; it starts again on 1 Nov, or raise it under "
               "Configuration, Advanced")


def spend(store, agent, usd, when):
    """One finished run of `agent` that cost `usd` and started at `when`."""
    rid = "run_" + uuid.uuid4().hex[:12]
    store.start_agent_run(rid, agent, "spike", "d", "old", "h")
    store.finish_agent_run(rid, "ok")
    store.sc.execute("UPDATE agent_runs SET cost_usd = ?, started_at = ? WHERE id = ?",
                     [usd, when, rid])


async def wait_for(fn, tries=300):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        runtime = app.state.runtime
        runner = app.state.agents
        runner.attach_loop()
        real_clock = ba._clock

        async def mk(kind, body, status=201):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == status, (kind, r.status_code, r.text)
            return r.json()

        r = await cx.post("/api/projects", json={"template": "custom", "name": "Budgets",
                                                 "objects": []})
        pid = r.json()["id"]
        await mk("sources", {"connector": "webhook", "poll": "5s", "name": "errors", "project": pid,
                             "config": {"event_type": "log", "text_template": "{msg}",
                                        "labels": [{"name": "service", "field": "service",
                                                    "primary": True}]}})
        await mk("triggers", {"name": "spike", "project": pid, "sources": ["errors"],
                              "key_field": "service", "cooldown": "5m",
                              "condition": {"aggregate": "count", "predicate": "> 5",
                                            "window": "5m"}})
        # one agent with no budget_period (today's behaviour), one with "month"
        await mk("agents/builtin", {"name": "forever", "trigger": "spike", "prompt": "look",
                                    "project": pid, "budget_usd": 0.05})
        await mk("agents/builtin", {"name": "monthly", "trigger": "spike", "prompt": "look",
                                    "project": pid, "budget_usd": 0.05, "budget_period": "month"})
        for n in ("forever", "monthly"):
            await cx.post(f"/api/agents/builtin/{n}/enable")
        forever, monthly = store.get_catalog_agent("forever"), store.get_catalog_agent("monthly")
        eq("the default period is lifetime", forever["budget_period"], "lifetime")
        eq("a month agent is stored as month", monthly["budget_period"], "month")

        # 0.06 each: 0.03 last month and 0.03 this month
        for n in ("forever", "monthly"):
            spend(store, n, 0.03, LAST_MONTH)
            spend(store, n, 0.03, THIS_MONTH)

        print("== the one rule ==")
        eq("lifetime counts all of it", round(store.agent_budget_spend(forever, NOW), 6), 0.06)
        eq("lifetime counts all of it after the 1st", round(store.agent_budget_spend(forever, FIRST), 6), 0.06)
        eq("month counts this month only", round(store.agent_budget_spend(monthly, NOW), 6), 0.03)
        eq("month, the last minute of the month",
           round(store.agent_budget_spend(monthly, LAST_MINUTE), 6), 0.03)
        eq("month, at 00:00 UTC on the 1st it starts again",
           store.agent_budget_spend(monthly, FIRST), 0)
        eq("month_start is the 1st at 00:00 UTC", Store.month_start(NOW), datetime(2026, 10, 1, tzinfo=UTC))
        eq("month_start reads a naive time as UTC",
           Store.month_start(datetime(2026, 10, 31, 23, 59)), datetime(2026, 10, 1, tzinfo=UTC))
        eq("a lifetime budget never starts again", store.budget_resets_on(forever, NOW), None)
        eq("a month budget starts on the 1st of next month",
           store.budget_resets_on(monthly, NOW), date(2026, 11, 1))
        eq("December rolls into January",
           store.budget_resets_on(monthly, datetime(2026, 12, 15, tzinfo=UTC)), date(2027, 1, 1))

        print("== the run path ==")

        async def run(name, at):
            ba._clock = lambda: at
            rid = runner.run_now(name, "spike", "k" + uuid.uuid4().hex[:6], "")
            assert rid, name

            def done():
                rs = [x for x in store.list_agent_runs(name, limit=100) if x["id"] == rid]
                return rs[0] if rs and rs[0]["status"] != "running" else None
            return await wait_for(done)

        try:
            r1 = await run("forever", NOW)
            eq("lifetime: capped by last month's and this month's spend together",
               (r1["status"], r1["error"]), ("capped", LIFETIME_WORDS))
            r2 = await run("forever", FIRST)
            eq("lifetime: still capped on the 1st, with today's words",
               (r2["status"], r2["error"]), ("capped", LIFETIME_WORDS))
            r3 = await run("monthly", NOW)
            eq("month: last month's spend does not count", r3["status"], "ok")
            spend(store, "monthly", 0.03, THIS_MONTH)   # this month's spend is now 0.06
            r4 = await run("monthly", NOW)
            eq("month: capped once this month's spend reaches the budget, with the month's words",
               (r4["status"], r4["error"]), ("capped", MONTH_WORDS))
            r5 = await run("monthly", LAST_MINUTE)
            eq("month: still capped at 23:59 on the last day",
               (r5["status"], r5["error"]), ("capped", MONTH_WORDS))
            r6 = await run("monthly", FIRST)
            eq("month: free again at 00:00 UTC on the 1st", r6["status"], "ok")
            ck("no em dash in a budget message", "—" not in (r1["error"] + r4["error"]))
        finally:
            ba._clock = real_clock

        print("== project health ==")
        catalog = runtime.catalog
        project = store.get_project(pid)
        h = G.project_health(store, catalog, pid, project, runtime.health_snapshot(), now=NOW)
        msgs = {i["message"]: i for i in h["issues"]}
        want_forever = ("forever used its budget of $0.05, so it does not run until the budget is "
                        "raised.")
        want_monthly = ("monthly used its $0.05 budget for this month, so it does not run again "
                        "until 1 Nov or until the budget is raised.")
        ck("the lifetime agent keeps today's words", want_forever in msgs, str(list(msgs)))
        ck("the month agent says it starts again on the 1st", want_monthly in msgs, str(list(msgs)))
        ck("only the month agent says 'this month'",
           [m for m in msgs if "this month" in m] == [want_monthly], str(list(msgs)))
        ck("both point at the budget", all(msgs.get(m, {}).get("fix") == "Raise the budget"
                                           and msgs.get(m, {}).get("view") == f"agent:{n}"
                                           for m, n in ((want_forever, "forever"),
                                                        (want_monthly, "monthly"))), str(msgs))
        h = G.project_health(store, catalog, pid, project, runtime.health_snapshot(), now=FIRST)
        msgs = [i["message"] for i in h["issues"]]
        ck("on the 1st the month agent is no longer capped, the lifetime one still is",
           want_forever in msgs and not any(m.startswith("monthly used") for m in msgs), str(msgs))
        ck("no em dash in health", "—" not in " ".join(msgs))

        print("== the catalog ==")
        triggers = {"spike"}
        base = {"name": "x", "prompt": "p", "trigger": "spike"}
        for bad in ("weekly", "x", "Month"):
            try:
                validate_agent_dict({**base, "budget_period": bad}, triggers)
                ck(f"budget_period {bad!r} is refused", False, "no error")
            except CatalogError as e:
                ck(f"budget_period {bad!r} is refused", "budget_period" in str(e), str(e))
        for good in (None, "", "lifetime", "month"):
            try:
                validate_agent_dict({**base, "budget_period": good}, triggers)
                ck(f"budget_period {good!r} is accepted", True)
            except CatalogError as e:
                ck(f"budget_period {good!r} is accepted", False, str(e))
        out = yaml.safe_load(export_db_to_yaml(store))
        agents = {a["name"]: a for a in out["agents"]}
        ck("export: a lifetime agent has no budget_period key", "budget_period" not in agents["forever"],
           str(agents["forever"]))
        eq("export: a month agent has it", agents["monthly"].get("budget_period"), "month")
        eq("export: the budget itself is still there", agents["forever"].get("budget_usd"), 0.05)
        def doc(period):
            return {"yaml": yaml.safe_dump({"agents": [
                {"name": "imported", "trigger": "spike", "prompt": "p", "budget_usd": 1,
                 "budget_period": period}]})}
        r = await cx.post("/api/catalog/import", json=doc("weekly"))
        ck("an import with a weekly budget is refused", r.status_code == 400
           and "budget_period" in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.post("/api/catalog/import", json=doc("month"))
        ck("an import with a month budget is stored as month", r.status_code == 200
           and store.get_catalog_agent("imported")["budget_period"] == "month",
           f"{r.status_code} {r.text[:200]}")

        async def period_of(name):
            rows = (await cx.get("/api/agents/builtin")).json()["agents"]
            return next(a["budget_period"] for a in rows if a["name"] == name)

        body = {"name": "forever", "trigger": "spike", "prompt": "look", "project": pid,
                "budget_usd": 0.05}
        eq("the list says lifetime for an agent that never set it", await period_of("forever"), "lifetime")
        r = await cx.put("/api/agents/builtin/forever", json={**body, "budget_period": "month"})
        ck("PUT budget_period month", r.status_code == 200, r.text)
        eq("it comes back from GET", await period_of("forever"), "month")
        r = await cx.put("/api/agents/builtin/forever", json={**body, "budget_period": "x"})
        ck("PUT budget_period x is a 400", r.status_code == 400 and "budget_period" in r.text,
           f"{r.status_code} {r.text}")
        eq("and the stored value is untouched", await period_of("forever"), "month")
        r = await cx.put("/api/agents/builtin/forever", json={**body, "prompt": "look again"})
        ck("an update without the field", r.status_code == 200, r.text)
        eq("keeps the stored period", await period_of("forever"), "month")
        r = await cx.put("/api/agents/builtin/forever", json={**body, "budget_period": "lifetime"})
        eq("lifetime can be set back", (r.status_code, await period_of("forever")), (200, "lifetime"))
        r = await cx.post("/api/agents/builtin", json={**body, "name": "bad-period",
                                                       "budget_period": "weekly"})
        ck("create with budget_period weekly is a 400", r.status_code == 400 and "budget_period" in r.text,
           f"{r.status_code} {r.text}")
        ck("and nothing was created", store.get_catalog_agent("bad-period") is None)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
