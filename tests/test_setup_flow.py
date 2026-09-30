"""The guided project setup: goal -> plan -> adjust -> apply -> connect -> try it.

Covers the plan (normalize: names, knobs, derived sentences, no em dashes), the model loop with a
stand-in provider (read tools, the forced propose_plan, one retry with the errors, 422 when the
retry is still wrong, 409 without a provider), adjust (a number the model changed moves its
knob), apply (goal, every object owned by the project, handoffs, on and off agents, skills, MCP
servers from enabled tools only, rollback naming the failing step), the own agent (a key, no
Tares agents), the Connect checks (waiting then receiving after a test event, tool test states,
the own agent joining after a key call), practice runs (flagged, handoffs too, left out of today
and health), the own agent's practice firing and finding, and auth (admin only, closed to
project keys). The check route: the plan normalized as apply does it, sentences and the
summary derived from the plan as edited, problems per item in plain words, a source swapped for
one already on the cell, a schedule, the average of a field, an MCP server already on the cell, a
copied skill, and apply taking a plan edited in the console.

Run: .venv/bin/python tests/test_setup_flow.py
"""
import asyncio
from datetime import datetime, timedelta, timezone
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
ROOT = "root-token-setup-flow"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ.pop("TARES_PUBLIC_URL", None)
os.environ.pop("TARES_MCP_URL", None)

import httpx

from tares import setup_flow as S
from tares.builtin_agents import AgentRunner
from tares.models import ModelReply, ToolCall

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


def no_em_dash(label, obj):
    text = json.dumps(obj, default=str)
    ck(f"no em dash in {label}", "—" not in text and "\\u2014" not in text)


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── the stand-in model loop for agent runs ───────────────────────────────────
SCRIPT: dict = {}


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    s = SCRIPT.get(agent["name"], {})
    summary = s.get("summary", f"{agent['name']} looked at {key}.")
    concluded.update({"outcome": s.get("outcome", "finding"), "summary": summary,
                      "verdict": s.get("verdict"), "key": s.get("key"), "label": s.get("label"),
                      "headline": s.get("headline"), "next_step": s.get("next_step"),
                      "produced": []})
    return summary, 1, 1, [], False, ""


AgentRunner._loop = fake_loop


# ── the stand-in model provider for planning ─────────────────────────────────
class Stub:
    kind = "anthropic"

    def __init__(self):
        self.script = []   # each: a list of (tool name, arguments)
        self.calls = []

    async def complete(self, *, model, system, tools, messages, max_tokens, tools_allowed=True,
                       tracer=None, tool_choice=None):
        self.calls.append({"tool_choice": tool_choice, "messages": copy.deepcopy(
            [{k: v for k, v in m.items() if k != "raw"} for m in messages], ),
                           "tools": [t["name"] for t in tools], "system": system})
        step = self.script.pop(0)
        calls = [ToolCall(id=f"tc_{len(self.calls)}_{i}", name=n, arguments=a)
                 for i, (n, a) in enumerate(step)]
        raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
               for c in calls]
        return ModelReply(text="", tool_calls=calls, usage={"input_tokens": 10,
                                                             "output_tokens": 5},
                          model="stub-model", stop_reason="tool_use", raw=raw)


STUB = Stub()

GOOD = {
    "goal": "Catch checkout outages early and find the root cause",
    "name": "Checkout outages",
    "summary": "anything, it is derived",
    "watches": [{
        "key": "w1", "existing": False, "name": "Checkout Errors", "connector": "webhook",
        "config": {"event_type": "log", "text_template": "{service}: {msg}",
                   "labels": [{"name": "service", "field": "service", "primary": True}]},
        "sentence": "Checkout errors your checkout service sends to Tares — as they happen",
        "needs": "send",
        "sample": {"service": "checkout", "msg": "payment failed", "level": "error"}}],
    "wakes": [{
        "key": "k1", "name": "checkout-error-spike", "sources": ["Checkout Errors"],
        "filters": [], "key_field": "service",
        "condition": {"aggregate": "count", "predicate": "> 5", "window": "5m"},
        "cooldown": "10m", "window": "15m", "sentence": "whatever",
        "knobs": [{"id": "threshold", "label": "errors", "value": 5, "min": 1, "max": 1000},
                  {"id": "window_minutes", "label": "minutes", "value": 5, "min": 1,
                   "max": 1440}]}],
    "who": "tares",
    "agents": [
        {"key": "a1", "name": "checkout-triage", "trigger": "checkout-error-spike",
         "on_trigger": True, "prompt": "Is this a real outage? Conclude investigate if so.",
         "handoffs": [{"verdict": "investigate", "agent": "checkout-rca"}],
         "mcp_servers": ["github", "pager"], "sentence": "Triage looks first: is it real?",
         "optional": False, "enabled": True},
        {"key": "a2", "name": "checkout-rca", "trigger": "checkout-error-spike",
         "on_trigger": False, "prompt": "Find the root cause.", "handoffs": [],
         "mcp_servers": [], "sentence": "Root cause digs in", "optional": True,
         "enabled": True}],
    "own_agent": None,
    "tools": [
        {"key": "t1", "name": "github", "url": "https://mcp.example.com/mcp",
         "why": "check recent deploys", "can_act": False, "enabled": True},
        {"key": "t2", "name": "pager", "url": "", "why": "page someone", "can_act": True,
         "enabled": False}],
    "skills": [
        {"key": "s1", "name": "checkout-error-codes", "description": "What the checkout "
         "error codes mean.", "body": "# Codes\n\nE42 is a declined card.", "enabled": True},
        {"key": "s2", "name": "house-rules", "description": "How we page.",
         "body": "Page the on-call.", "enabled": False}],
    "notes": ["Handing off from a Tares agent to your own agent is not supported yet."],
}


def plan_with(**kw):
    p = copy.deepcopy(GOOD)
    p.update(kw)
    return p


async def wait_for(fn, tries=300):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def unit(store, catalog):
    print("== normalize ==")
    plan, errors = S.normalize(GOOD, store, catalog)
    eq("a good plan has no errors", errors, [])
    eq("names are kebab-case, references follow", (plan["watches"][0]["name"],
                                                    plan["wakes"][0]["sources"]),
       ("checkout-errors", ["checkout-errors"]))
    eq("the threshold knob sets the condition", plan["wakes"][0]["condition"]["predicate"], "> 5")
    eq("a cooldown knob is derived from the cooldown",
       [k for k in plan["wakes"][0]["knobs"] if k["id"] == "cooldown_minutes"][0]["value"], 10)
    eq("the wake sentence is goal.py's phrasing with the knob's unit",
       plan["wakes"][0]["sentence"],
       "Checkout-errors gets more than 5 errors in 5 minutes for one service.")
    eq("the cooldown sentence", plan["wakes"][0]["cooldown_sentence"],
       "Then waits 10 minutes before waking again for the same service.")
    eq("the summary is derived", plan["summary"],
       "When checkout-errors gets more than 5 errors in 5 minutes for one service, "
       "checkout-triage looks first. If it concludes investigate, checkout-rca digs in.")
    eq("em dashes become commas", plan["watches"][0]["sentence"],
       "Checkout errors your checkout service sends to Tares, as they happen")
    no_em_dash("a normalized plan", plan)

    edited = copy.deepcopy(plan)
    edited["wakes"][0]["knobs"][0]["value"] = 12
    edited["wakes"][0]["knobs"][1]["value"] = 3
    p2, errors = S.normalize(edited, store, catalog)
    eq("an edited knob applies exactly", (errors, p2["wakes"][0]["condition"]["predicate"],
                                          p2["wakes"][0]["condition"]["window"]),
       ([], "> 12", "3m"))
    ck("and the sentences follow the knobs", "more than 12 errors in 3 minutes"
       in p2["wakes"][0]["sentence"] and "more than 12 errors in 3 minutes" in p2["summary"],
       p2["summary"])
    over = copy.deepcopy(plan)
    over["wakes"][0]["knobs"][0]["value"] = 5000
    eq("a knob is held to its range", S.normalize(over, store, catalog)[0]["wakes"][0]
       ["condition"]["predicate"], "> 1000")

    bad = plan_with(agents=[{**GOOD["agents"][0], "trigger": "nope", "handoffs": []}])
    _p, errors = S.normalize(bad, store, catalog)
    ck("an agent on an unknown trigger is an error", any("nope" in e for e in errors), errors)
    many = plan_with(agents=[{**GOOD["agents"][1], "name": f"x{i}", "key": f"a{i}",
                              "on_trigger": True} for i in range(4)])
    _p, errors = S.normalize(many, store, catalog)
    ck("more than 3 agents is an error", any("At most 3" in e for e in errors), errors)
    on_no_url = plan_with(tools=[{**GOOD["tools"][1], "enabled": True}])
    _p, errors = S.normalize(on_no_url, store, catalog)
    ck("a tool turned on without an address is an error",
       any("no address" in e for e in errors), errors)

    print("== knobs on adjust ==")
    moved = copy.deepcopy(plan)
    moved["wakes"][0]["condition"]["predicate"] = "> 20"
    p3, _e = S.normalize(moved, store, catalog, prev=plan)
    eq("a number the model changed moves its knob", (p3["wakes"][0]["knobs"][0]["value"],
                                                     p3["wakes"][0]["condition"]["predicate"]),
       (20, "> 20"))
    p4, _e = S.normalize(moved, store, catalog)
    eq("without a previous plan the knob wins", p4["wakes"][0]["condition"]["predicate"], "> 5")


async def main():
    from tares import daemon
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        runtime = app.state.runtime
        runner = app.state.agents
        runner.attach_loop()
        unit(store, runtime.catalog)

        print("== plan: no provider ==")
        real_resolve = daemon.resolve_provider
        daemon.resolve_provider = lambda store, pid=None: (None, "")
        r = await cx.post("/api/setup/plan", json={"goal": "Catch checkout outages"})
        eq("409 without a model provider", (r.status_code, r.json().get("detail")),
           (409, "Add a model provider under Settings to plan a project; or start from a "
                 "template."))
        daemon.resolve_provider = lambda store, pid=None: (STUB, "test")

        print("== plan: reads, one retry with the errors ==")
        wrong = plan_with(agents=[{**GOOD["agents"][0], "trigger": "no-such-trigger",
                                   "handoffs": []}])
        STUB.script = [[("list_connectors", {})], [("propose_plan", wrong)],
                       [("propose_plan", GOOD)]]
        STUB.calls.clear()
        r = await cx.post("/api/setup/plan", json={"goal": GOOD["goal"], "who": "tares"})
        ck("200 with the fixed plan", r.status_code == 200, r.text)
        plan = r.json().get("plan") or {}
        eq("three model calls", len(STUB.calls), 3)
        eq("reads allowed first, the retry forced to propose_plan",
           [c["tool_choice"] for c in STUB.calls], ["any", "any", "propose_plan"])
        ck("the read tools and propose_plan are offered",
           set(STUB.calls[0]["tools"]) == {"list_connectors", "list_sources", "source_fields",
                                           "list_templates", "propose_plan"},
           STUB.calls[0]["tools"])
        ck("the goal is in the first message", GOOD["goal"] in
           STUB.calls[0]["messages"][0]["content"])
        ck("the read tool ran against the daemon",
           any(m.get("role") == "tool" and "webhook" in m["results"][0]["content"]
               for m in STUB.calls[1]["messages"]), "no connectors in the tool result")
        last = STUB.calls[2]["messages"][-1]
        ck("the retry carries the errors", last.get("role") == "tool"
           and "no-such-trigger" in last["results"][0]["content"], str(last)[:300])
        eq("the plan comes back normalized", plan.get("watches", [{}])[0].get("name"),
           "checkout-errors")
        no_em_dash("the plan response", r.json())

        STUB.script = [[("propose_plan", wrong)], [("propose_plan", wrong)]]
        r = await cx.post("/api/setup/plan", json={"goal": GOOD["goal"]})
        # still wrong after the retry: the plan opens anyway, and check names the problem on
        # its card, for the person to fix in place
        ck("still wrong after the retry: the plan opens", r.status_code == 200
           and r.json()["plan"]["agents"][0]["name"] == "checkout-triage", r.text[:300])
        probs = (await cx.post("/api/setup/check", json={"plan": r.json()["plan"]})).json()["problems"]
        ck("and check names the problem on the agent", any(p["where"].startswith("agents.")
                                                           for p in probs), probs)
        r = await cx.post("/api/setup/plan", json={"goal": ""})
        eq("no goal: 400", r.status_code, 400)
        STUB.script = [[("propose_plan", GOOD)], [("propose_plan", GOOD)]]
        r = await cx.post("/api/setup/plan", json={"goal": GOOD["goal"], "who": "own"})
        ck("the plan keeps who the person chose", r.status_code == 422
           and "who own" in r.text, r.text)

        print("== adjust ==")
        revised = copy.deepcopy(plan)
        revised["wakes"][0]["condition"]["predicate"] = "> 10"
        STUB.script = [[("propose_plan", revised)]]
        STUB.calls.clear()
        r = await cx.post("/api/setup/adjust", json={"plan": plan,
                                                     "instruction": "wake at 10 errors"})
        ck("adjust answers a plan", r.status_code == 200, r.text)
        adj = r.json().get("plan") or {}
        eq("the knob follows the model's number", adj["wakes"][0]["knobs"][0]["value"], 10)
        ck("the sentence follows too", "more than 10 errors" in adj["wakes"][0]["sentence"])
        ck("the instruction and the current plan reach the model",
           "wake at 10 errors" in STUB.calls[0]["messages"][0]["content"]
           and "checkout-error-spike" in STUB.calls[0]["messages"][0]["content"])
        r = await cx.post("/api/setup/adjust", json={"plan": plan, "instruction": " "})
        eq("an empty instruction: 400", r.status_code, 400)

        print("== apply: a failing step rolls back ==")
        real_upsert = store.upsert_catalog_trigger

        def boom(*a, **k):
            raise RuntimeError("disk on fire")
        store.upsert_catalog_trigger = boom
        r = await cx.post("/api/setup/apply", json={"plan": plan})
        store.upsert_catalog_trigger = real_upsert
        ck("400 naming the step", r.status_code == 400 and "adding the triggers" in r.text
           and "Nothing was kept" in r.text, r.text)
        ck("no project is left", store.get_project_by_name("Checkout outages") is None)
        ck("no source, MCP server or skill is left",
           "checkout-errors" not in runtime.catalog.sources
           and store.get_mcp_server("github") is None)
        ck("no agent is left", store.get_catalog_agent("checkout-triage") is None)

        print("== apply ==")
        r = await cx.post("/api/setup/apply", json={"plan": plan})
        ck("201", r.status_code == 201, r.text)
        body = r.json()
        proj = body.get("project") or {}
        uid = proj.get("id")
        eq("the goal and name", (proj.get("goal"), proj.get("name")),
           (GOOD["goal"], "Checkout outages"))
        eq("the project carries its setup step", proj.get("setup"),
           {"step": "connect", "practice_run": None})
        src = {s["name"]: s for s in store.list_catalog_sources()}
        eq("the source is the project's", src["checkout-errors"].get("owned_by"), uid)
        trig = {t["name"]: t for t in store.list_catalog_triggers()}
        eq("the trigger is the project's, knobs applied",
           (trig["checkout-error-spike"].get("owned_by"),
            trig["checkout-error-spike"]["condition"]["predicate"],
            trig["checkout-error-spike"].get("cooldown")), (uid, "> 5", "10m"))
        triage = store.get_catalog_agent("checkout-triage")
        rca = store.get_catalog_agent("checkout-rca")
        eq("both agents are the project's", (triage.get("owned_by"), rca.get("owned_by")),
           (uid, uid))
        eq("triage hands off to root cause", [(h["verdict"], h["agent"])
                                              for h in triage.get("handoffs") or []],
           [("investigate", "checkout-rca")])
        ck("triage is on for the trigger, root cause is left off",
           store.subscription_by_url("tares://agent/checkout-triage") is not None
           and store.subscription_by_url("tares://agent/checkout-rca") is None)
        eq("MCP servers from enabled tools only", (store.get_mcp_server("github") or {}).get(
            "owned_by"), uid)
        ck("the tool left off is not created", store.get_mcp_server("pager") is None)
        eq("the agent uses only the enabled tool", triage.get("mcp_servers"), ["github"])
        eq("skills: the enabled one", [s["name"] for s in store.list_skills(uid)],
           ["checkout-error-codes"])
        objs = {(o["kind"], o["name"]) for o in (proj.get("objects") or [])}
        ck("the project lists its objects", {("source", "checkout-errors"),
                                             ("trigger", "checkout-error-spike"),
                                             ("agent", "checkout-triage"),
                                             ("agent", "checkout-rca"),
                                             ("mcp_server", "github")} <= objs, objs)
        conn = body.get("connect") or {}
        cfg = runtime.catalog.sources["checkout-errors"]
        eq("connect: the source with its ingest URL and sample", conn.get("sources"), [{
            "name": "checkout-errors", "needs": "send",
            "ingest_url": f"http://t/ingest/{cfg.ingest_key}",
            "sample": GOOD["watches"][0]["sample"], "credential_hint": None}])
        eq("connect: the enabled tool needs a token",
           conn.get("tools"), [{"name": "github", "url": "https://mcp.example.com/mcp",
                                "needs_token": True}])
        eq("connect: no own agent", conn.get("own_agent"), None)
        no_em_dash("the apply response", body)

        r = await cx.post("/api/setup/apply", json={"plan": plan})
        ck("the same plan again gets fresh names",
           r.status_code == 201 and r.json()["project"]["name"] == "Checkout outages 2"
           and store.get_catalog_agent("checkout-triage-2") is not None, r.text)
        bad = copy.deepcopy(plan)
        bad["agents"][0]["trigger"] = "nope"
        r = await cx.post("/api/setup/apply", json={"plan": bad})
        ck("an invalid plan: 422 with a plain message", r.status_code == 422
           and "cannot be set up" in r.text, r.text)

        print("== setup checks ==")
        r = await cx.get(f"/api/projects/{uid}/setup")
        ck("GET setup", r.status_code == 200, r.text)
        st = r.json()
        eq("step and plan", (st["step"], st["plan"]["name"]), ("connect", "Checkout outages"))
        s0 = st["checks"]["sources"][0]
        eq("the source waits", (s0["name"], s0["state"], s0["last_event_at"], s0["fields_seen"]),
           ("checkout-errors", "waiting", None, []))
        eq("the tool is untested", st["checks"]["tools"],
           [{"name": "github", "state": "untested", "detail": "Not tested yet"}])
        r = await cx.post(f"/api/projects/{uid}/setup/test-event",
                          json={"source": "checkout-errors"})
        ck("send a test event", r.status_code == 200 and r.json().get("ingested") == 1, r.text)
        s0 = (await cx.get(f"/api/projects/{uid}/setup")).json()["checks"]["sources"][0]
        eq("then it is receiving with the fields seen", (s0["state"], s0["fields_seen"]),
           ("receiving", ["level", "msg", "service"]))
        ev = store.recent_events(source="checkout-errors", limit=5)
        ck("the test event is stored", len(ev) == 1)
        runs = [r_ for a in ("checkout-triage", "checkout-rca")
                for r_ in store.list_agent_runs(a, limit=10)]
        eq("and it woke no real run", runs, [])
        r = await cx.post(f"/api/projects/{uid}/setup/test-event", json={"source": "nope"})
        eq("an unknown source: 404", r.status_code, 404)

        import tares.mcp_client as mc
        real_list = mc.list_remote_tools

        async def ok_tools(server):
            return [{"name": "deploys"}, {"name": "commits"}]

        async def bad_tools(server):
            raise ConnectionError("refused")
        mc.list_remote_tools = ok_tools
        await cx.post("/api/mcp-servers/github/test")
        t = (await cx.get(f"/api/projects/{uid}/setup")).json()["checks"]["tools"][0]
        eq("a passing test: ok", (t["state"], t["detail"]), ("ok", "Connected, 2 tools offered"))
        mc.list_remote_tools = bad_tools
        await cx.post("/api/mcp-servers/github/test")
        t = (await cx.get(f"/api/projects/{uid}/setup")).json()["checks"]["tools"][0]
        ck("a failing test: error", t["state"] == "error" and "refused" in t["detail"], t)
        mc.list_remote_tools = real_list

        r = await cx.put(f"/api/projects/{uid}/setup", json={"step": "try"})
        eq("move the step", (r.status_code, r.json().get("step")), (200, "try"))
        r = await cx.put(f"/api/projects/{uid}/setup", json={"step": "later"})
        eq("an unknown step: 400", r.status_code, 400)

        print("== practice run ==")
        SCRIPT["checkout-triage"] = {"verdict": "investigate", "headline": "Card declines",
                                     "next_step": "Call the payment provider.",
                                     "summary": "Real outage."}
        SCRIPT["checkout-rca"] = {"verdict": "rca", "headline": "Gateway timeout",
                                  "next_step": "Roll back the gateway config.",
                                  "summary": "The gateway timed out."}
        # both agents have a write-back webhook: a practice run must not call it
        store.con.execute("UPDATE catalog_agents SET webhook_url = 'http://127.0.0.1:9/hook' "
                          "WHERE name IN ('checkout-triage', 'checkout-rca')")
        r = await cx.post(f"/api/projects/{uid}/setup/practice")
        ck("practice answers a run id", r.status_code == 200 and r.json().get("run_id"), r.text)
        rid = r.json().get("run_id")
        done = await wait_for(lambda: [x for x in store.list_agent_runs("checkout-rca", limit=5)
                                       if x["status"] != "running"])
        tri = store.get_agent_run(rid)
        eq("the practice run is flagged and woken by practice",
           (tri["practice"], tri["woken_by"], tri["key"]), (True, "practice", "checkout"))
        ck("its handoff is practice too", bool(done) and done[0]["practice"], done)
        ck("a practice run delivers nowhere outside Tares (no write-back attempt)",
           tri["delivery"] is None and done and done[0]["delivery"] is None,
           (tri["delivery"], done and done[0]["delivery"]))
        res = (await cx.get(f"/api/projects/{uid}/results")).json()
        ck("the result is shown, marked practice",
           len(res["results"]) == 1 and res["results"][0]["practice"] is True, res)
        eq("today leaves it out", res["today"], {"looked_at": 0, "found": 0, "spent_usd": 0.0})
        eq("the daily cap in health leaves it out",
           store.agent_runs_today("checkout-triage", include_practice=False), 0)
        eq("the setup remembers the practice run",
           (await cx.get(f"/api/projects/{uid}/setup")).json()["practice_run"], rid)
        h = (await cx.get(f"/api/projects/{uid}/health")).json()
        ck("health does not count it as a capped agent",
           not any("limit" in i["message"] for i in h["issues"]), h)
        await wait_for(lambda: not runner._inflight)
        ag = {x["name"]: x for x in store.list_catalog_agents()}
        ck("a planned agent is told to finish with conclude (it carries the headline)",
           ag["checkout-triage"]["prompt"].endswith(
               "call conclude with what you found and a verdict (investigate when it needs a "
               "closer look)."), ag["checkout-triage"]["prompt"])
        eq("a prompt that names conclude is kept as it is",
           S.with_conclude("Look, then conclude.", []), "Look, then conclude.")

        print("== practice entity: the newest event the wake-up's filters let through ==")
        cfg = runtime.catalog.sources["checkout-errors"]
        for svc in ("payments", "checkout"):
            r = await cx.post(f"/ingest/{cfg.ingest_key}",
                              json={"service": svc, "msg": "x", "level": "error"})
            ck(f"ingest {svc}", r.status_code in (200, 202), r.text)
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        eq("unfiltered: the newest", (store.newest_key(["checkout-errors"], [], since) or [None])[0],
           "checkout")
        eq("filtered: the newest that matches",
           (store.newest_key(["checkout-errors"],
                             [{"field": "service", "op": "eq", "value": "payments"}], since)
            or [None])[0], "payments")
        eq("nothing matches: none", store.newest_key(
            ["checkout-errors"], [{"field": "service", "op": "eq", "value": "nope"}], since), None)

        print("== who = own ==")
        own_raw = plan_with(who="own", agents=[], name="Checkout own",
                            own_agent={"name": "claude-code", "wake": "webhook",
                                       "sentence": "Your agent is woken at its webhook"})
        own_raw["watches"][0]["name"] = "checkout-own"
        own_raw["wakes"][0]["sources"] = ["checkout-own"]
        own_raw["wakes"][0]["name"] = "checkout-own-spike"
        own_plan, errors = S.normalize(own_raw, store, runtime.catalog)
        eq("an own-agent plan is valid", errors, [])
        ck("its summary tells the own agent", "your agent claude-code is told"
           in own_plan["summary"], own_plan["summary"])
        n_agents = len(store.list_catalog_agents())
        r = await cx.post("/api/setup/apply", json={"plan": own_plan})
        ck("apply", r.status_code == 201, r.text)
        ob = r.json()
        ouid = ob["project"]["id"]
        eq("no Tares agents are created", len(store.list_catalog_agents()), n_agents)
        keys = store.list_api_keys(project=ouid)
        eq("a project key named after the agent", [(k["name"], sorted(k["scopes"]))
                                                   for k in keys],
           [("claude-code", ["findings", "read"])])
        own = ob["connect"]["own_agent"]
        secret = own["key"]
        eq("the MCP URL and the Claude Code command",
           (own["mcp_url"], own["claude_command"]),
           ("http://t/mcp", f"claude mcp add --transport http tares http://t/mcp --header "
                            f"\"Authorization: Bearer {secret}\""))
        ck("the subscribe hint names the project's subscribe route",
           f"/api/projects/{ouid}/subscribe" in own["subscribe_hint"])
        chk = (await cx.get(f"/api/projects/{ouid}/setup")).json()["checks"]["own_agent"]
        eq("waiting for the agent", chk, {"state": "waiting",
                                          "detail": "Waiting for your agent to use its key"})
        r = await anon.get(f"/api/projects/{ouid}/timeline", headers=H(secret))
        eq("the agent reads the project with its key", r.status_code, 200)
        chk = (await cx.get(f"/api/projects/{ouid}/setup")).json()["checks"]["own_agent"]
        ck("then it has joined", chk["state"] == "joined" and "used its key" in chk["detail"],
           chk)
        ck("an agent woken at its webhook is told it has not subscribed yet",
           "not subscribed" in chk["detail"], chk)
        st_ = store.get_project_setup(ouid)
        st_["plan"]["own_agent"]["wake"] = "poll"
        store.set_project_setup(ouid, st_)
        chk = (await cx.get(f"/api/projects/{ouid}/setup")).json()["checks"]["own_agent"]
        ck("an agent that checks in is never told to subscribe",
           chk["state"] == "joined" and "subscribe" not in chk["detail"], chk)
        st_["plan"]["own_agent"]["wake"] = "webhook"
        store.set_project_setup(ouid, st_)

        print("== own agent: practice firing and finding ==")
        from tares.dispatch import Dispatcher
        posted = []
        real_post = Dispatcher._post

        async def fake_post(self, url, body, attempts=5):
            posted.append((url, body))
            return True, None
        Dispatcher._post = fake_post
        r = await cx.post(f"/api/projects/{ouid}/subscribe",
                          json={"url": "https://agent.example.com/hook"})
        ck("admin subscribes a URL for the test", r.status_code == 200, r.text)
        r = await cx.post(f"/api/projects/{ouid}/setup/practice")
        Dispatcher._post = real_post
        ck("practice answers a dispatch id", r.status_code == 200 and r.json().get("dispatch_id"),
           r.text)
        did = r.json().get("dispatch_id")
        eq("the firing reached the subscription, marked practice",
           [(u, b.get("practice"), b.get("key")) for u, b in posted],
           [("https://agent.example.com/hook", True, "checkout")])
        r = await anon.post(f"/api/projects/{ouid}/findings", headers=H(secret),
                            json={"entity": "checkout", "finding": "Card declines spiked.",
                                  "verdict": "rca", "headline": "Card declines",
                                  "next_step": "Call the provider."})
        ck("the agent records a finding", r.status_code == 201, r.text)
        frun = store.get_agent_run(r.json()["run_id"])
        eq("its finding is flagged practice", frun["practice"], True)
        res = (await cx.get(f"/api/projects/{ouid}/results")).json()
        ck("the results show it as practice", [x["practice"] for x in res["results"]] == [True],
           res["results"])
        eq("today leaves the practice firing and finding out", res["today"],
           {"looked_at": 0, "found": 0, "spent_usd": 0.0})
        r = await anon.post(f"/api/projects/{ouid}/findings", headers=H(secret),
                            json={"entity": "checkout", "finding": "A second look."})
        eq("only the first finding answers the practice firing",
           store.get_agent_run(r.json()["run_id"])["practice"], False)
        eq("the setup remembers the firing",
           (await cx.get(f"/api/projects/{ouid}/setup")).json()["practice_run"], did)
        # the first key is shown once: a key made in its place answers a practice firing too
        st_ = store.get_project_setup(ouid)
        st_["practice_finding"] = None
        st_["practice_at"] = datetime.now(timezone.utc).isoformat()
        store.set_project_setup(ouid, st_)
        k2 = (await cx.post(f"/api/projects/{ouid}/keys", json={"name": "claude-code-2"})).json()
        r = await anon.post(f"/api/projects/{ouid}/findings", headers=H(k2["secret"]),
                            json={"entity": "checkout", "finding": "From the new key."})
        eq("a replacement key's finding answers the practice firing",
           store.get_agent_run(r.json()["run_id"])["practice"], True)

        print("== check: normalization, derived sentences and the summary ==")
        draft = copy.deepcopy(GOOD)
        draft["name"] = "Edited outages"
        draft["summary"] = "The model's words, which must not survive."
        draft["wakes"][0]["knobs"][0]["value"] = 8
        r = await cx.post("/api/setup/check", json={"plan": draft})
        ck("check answers 200", r.status_code == 200, r.text)
        chk = r.json()
        eq("a good plan has no problems", chk["problems"], [])
        cp = chk["plan"]
        eq("names come back the way apply makes them (unique against the cell)",
           (cp["watches"][0]["name"], cp["wakes"][0]["sources"]),
           ("checkout-errors-3", ["checkout-errors-3"]))
        eq("the summary follows the plan, not the model's text", cp["summary"],
           "When checkout-errors-3 gets more than 8 errors in 5 minutes for one service, "
           "checkout-triage-3 looks first. If it concludes investigate, checkout-rca-3 digs in.")
        eq("agent sentences are derived", [a["sentence"] for a in cp["agents"]],
           ["checkout-triage-3 looks first.",
            "checkout-rca-3 digs in when checkout-triage-3 concludes investigate, at most once "
            "every 30 minutes."])
        eq("the cooldown sentence is derived", cp["wakes"][0]["cooldown_sentence"],
           "Then waits 10 minutes before waking again for the same service.")
        no_em_dash("a checked plan", chk)
        r2 = await cx.post("/api/setup/check", json={"plan": cp})
        eq("checking a checked plan changes nothing", r2.json()["plan"], cp)
        r = await cx.post("/api/setup/check", json={"plan": "nope"})
        eq("no plan object: 400", r.status_code, 400)

        print("== check: problems per item ==")
        bad = copy.deepcopy(GOOD)
        bad["watches"].append({"key": "w9", "existing": True, "name": "no-such-source",
                               "sentence": "", "needs": "none"})
        bad["wakes"][0]["sources"] = []
        bad["wakes"][0]["filters"] = [{"field": "level", "op": "eq", "value": ""}]
        bad["agents"][0]["prompt"] = " "
        bad["agents"][1]["handoffs"] = [{"verdict": "escalate", "agent": "nobody"}]
        bad["tools"][1]["enabled"] = True
        bad["skills"][0]["body"] = ""
        r = await cx.post("/api/setup/check", json={"plan": bad})
        probs = r.json()["problems"]
        where = {}
        for p in probs:
            where.setdefault(p["where"], []).append(p["message"])
        ck("an unknown existing source is flagged on its watch",
           any("no source called no-such-source" in m for m in where.get("watches.w9", [])),
           probs)
        ck("a wake-up left with no source is flagged, not dropped",
           any("no source to watch" in m for m in where.get("wakes.k1", []))
           and len(r.json()["plan"]["wakes"]) == 1, probs)
        ck("an \"only when\" line without a value is flagged",
           any("level" in m and "no value" in m for m in where.get("wakes.k1", [])), probs)
        ck("empty instructions are flagged on the agent",
           any("instructions are empty" in m for m in where.get("agents.a1", [])), probs)
        ck("a handoff to an agent not in the plan is flagged",
           any("nobody" in m for m in where.get("agents.a2", [])), probs)
        ck("a tool turned on without an address is flagged on the tool",
           any("no address" in m for m in where.get("tools.t2", [])), probs)
        ck("an empty skill is flagged on the skill", "skills.s1" in where, probs)
        ck("messages are plain: no catalog prefixes",
           not any(m.startswith(("trigger '", "agent '", "source '", "skill '")) for m in
                   [p["message"] for p in probs]), probs)
        r = await cx.post("/api/setup/apply", json={"plan": bad})
        eq("apply refuses what check flags", r.status_code, 422)

        print("== check: who own, the summary names the agent once ==")
        own_draft = plan_with(who="own", agents=[],
                              own_agent={"name": "your own agent", "wake": "webhook",
                                         "sentence": "whatever"})
        cp = (await cx.post("/api/setup/check", json={"plan": own_draft})).json()["plan"]
        ck("\"your own agent\" reads \"your agent is told\"",
           cp["summary"].endswith(", your agent is told.")
           and "your agent your" not in cp["summary"], cp["summary"])
        eq("the own agent's sentence is derived", cp["own_agent"]["sentence"],
           "Your agent is woken at its webhook with what happened.")
        own_draft["own_agent"] = {"name": "claude-code", "wake": "poll"}
        cp = (await cx.post("/api/setup/check", json={"plan": own_draft})).json()["plan"]
        ck("a named agent: your agent claude-code is told",
           cp["summary"].endswith(", your agent claude-code is told."), cp["summary"])
        eq("and polls", cp["own_agent"]["sentence"],
           "Your agent claude-code checks the project for what happened.")

        print("== check: swap a source for one already on the cell ==")
        r = await cx.post("/api/sources", json={
            "name": "payments-events", "connector": "webhook",
            "config": {"text_template": "{service}: {msg}",
                       "labels": [{"name": "service", "field": "service", "primary": True},
                                  {"name": "latency_ms", "field": "latency_ms",
                                   "type": "number"}]}})
        ck("an existing source to swap in", r.status_code in (200, 201), r.text)
        swap = copy.deepcopy(GOOD)
        swap["name"] = "Swapped outages"
        swap["watches"][0] = {"key": "w1", "existing": True, "name": "payments-events",
                              "sentence": "", "needs": "none"}
        swap["wakes"][0]["sources"] = ["payments-events"]   # the console renames the reference
        chk = (await cx.post("/api/setup/check", json={"plan": swap})).json()
        eq("the swapped plan has no problems", chk["problems"], [])
        w0 = chk["plan"]["watches"][0]
        eq("the swapped watch is existing, needs nothing, has a derived sentence",
           (w0["existing"], w0["needs"], w0["connector"], w0["sentence"]),
           (True, "none", "webhook", "payments-events, a source already on Tares"))
        ck("the wake-up counts the swapped source",
           chk["plan"]["wakes"][0]["sentence"].startswith("Payments-events gets more than 5"),
           chk["plan"]["wakes"][0]["sentence"])
        stale = copy.deepcopy(swap)
        stale["wakes"][0]["sources"] = ["Checkout Errors"]
        probs = (await cx.post("/api/setup/check", json={"plan": stale})).json()["problems"]
        ck("a wake-up still naming the removed source is flagged",
           any(p["where"] == "wakes.k1" and "Checkout Errors" in p["message"] for p in probs),
           probs)

        print("== check: a schedule, and the average of a field ==")
        sched = copy.deepcopy(swap)
        sched["wakes"][0].update({"condition": {"every": "60m"}, "key_field": "",
                                  "knobs": [{"id": "cooldown_minutes", "label": "", "value": 10}]})
        chk = (await cx.post("/api/setup/check", json={"plan": sched})).json()
        eq("a schedule has no problems", chk["problems"], [])
        wk = chk["plan"]["wakes"][0]
        eq("its knobs: every N minutes, derived from the condition",
           [(k["id"], k["value"]) for k in wk["knobs"]],
           [("every_minutes", 60), ("cooldown_minutes", 10)])
        eq("its sentence is in the knob's unit", wk["sentence"], "Every 60 minutes.")
        ck("the summary leads with the schedule",
           chk["plan"]["summary"].startswith("Every 60 minutes, checkout-triage"),
           chk["plan"]["summary"])
        avg = copy.deepcopy(swap)
        avg["wakes"][0].update({
            "condition": {"aggregate": "avg", "field": "latency_ms", "predicate": "> 500",
                          "window": "5m"},
            "filters": [{"field": "level", "op": "eq", "value": "error"}],
            "knobs": [{"id": "threshold", "label": "", "value": 500},
                      {"id": "window_minutes", "label": "", "value": 5}]})
        chk = (await cx.post("/api/setup/check", json={"plan": avg})).json()
        eq("an average has no problems", chk["problems"], [])
        eq("its sentence says the average of the field, with the filter",
           chk["plan"]["wakes"][0]["sentence"],
           "The average latency_ms of events matching level = error on payments-events goes "
           "above 500 in 5 minutes for one service.")
        eq("the threshold knob is labelled with the field",
           chk["plan"]["wakes"][0]["knobs"][0]["label"], "latency_ms")
        nofield = copy.deepcopy(avg)
        nofield["wakes"][0]["condition"]["field"] = ""
        probs = (await cx.post("/api/setup/check", json={"plan": nofield})).json()["problems"]
        ck("an average without a field is flagged in plain words",
           any(p["where"] == "wakes.k1" and "Pick the number to take the average of" in
               p["message"] for p in probs), probs)

        print("== check and apply: a wake-up said in plain words ==")
        said = copy.deepcopy(avg)
        said["name"] = "Said plainly"
        said["wakes"][0]["description"] = " checkout gets slow for a service. "
        chk = (await cx.post("/api/setup/check", json={"plan": said})).json()
        eq("a described wake-up has no problems", chk["problems"], [])
        wk = chk["plan"]["wakes"][0]
        eq("its description is trimmed", wk["description"], "checkout gets slow for a service")
        eq("its sentence says it", wk["sentence"], "Checkout gets slow for a service.")
        ck("the summary leads with it",
           chk["plan"]["summary"].startswith("When checkout gets slow for a service, "
                                             "checkout-triage"),
           chk["plan"]["summary"])
        for bad, why in (("a\nb", "one line"), ("slow " * 40, "160 characters")):
            wrong = copy.deepcopy(said)
            wrong["wakes"][0]["description"] = bad
            probs = (await cx.post("/api/setup/check", json={"plan": wrong})).json()["problems"]
            ck(f"a description that is not one plain line is flagged ({why})",
               any(p["where"] == "wakes.k1" and why in p["message"]
                   and p["message"].startswith("The plain description") for p in probs), probs)
        dashed = copy.deepcopy(said)
        dashed["wakes"][0]["description"] = "checkout gets slow — again"
        wk2 = (await cx.post("/api/setup/check", json={"plan": dashed})).json()["plan"]["wakes"][0]
        eq("an em dash in it becomes a comma, like every plan sentence", wk2["description"],
           "checkout gets slow, again")
        r = await cx.post("/api/setup/apply", json={"plan": chk["plan"]})
        ck("apply a described plan", r.status_code == 201, r.text[:300])
        stored = {t["name"]: t for t in store.list_catalog_triggers()}.get(wk["name"]) or {}
        eq("the trigger keeps the description", stored.get("description"),
           "checkout gets slow for a service")
        if r.status_code == 201:
            said_uid = r.json()["project"]["id"]
            ol = (await cx.get(f"/api/projects/{said_uid}/outline")).json()
            eq("the project page says it too", ol["wakes"][0]["sentence"],
               "Wakes when checkout gets slow for a service.")
        ck("the planner asks for a description",
           "description" in S.PLAN_SCHEMA["properties"]["wakes"]["items"]["properties"]
           and "plain description" in S.SYSTEM, "")

        print("== check: an MCP server already on the cell, a copied skill ==")
        r = await cx.post("/api/mcp-servers", json={"name": "deploys-mcp",
                                                    "url": "https://deploys.example.com/mcp"})
        ck("an existing MCP server", r.status_code == 201, r.text)
        attach = copy.deepcopy(swap)
        attach["tools"] = [{"key": "t5", "name": "deploys-mcp", "existing": True, "url": "",
                            "why": "", "can_act": False, "enabled": True}]
        attach["agents"][0]["mcp_servers"] = ["deploys-mcp"]
        attach["skills"] = [{"key": "s7", "name": "checkout-error-codes",
                             "description": "What the checkout error codes mean.",
                             "body": "# Codes\n\nE42 is a declined card.", "enabled": True}]
        chk = (await cx.post("/api/setup/check", json={"plan": attach})).json()
        eq("an attached server and a copied skill: no problems", chk["problems"], [])
        eq("the attached server keeps its name and address",
           (chk["plan"]["tools"][0]["name"], chk["plan"]["tools"][0]["url"],
            chk["plan"]["tools"][0]["existing"]),
           ("deploys-mcp", "https://deploys.example.com/mcp", True))
        # a server another project owns is not taken from it
        other_uid = next(p["id"] for p in store.list_projects()
                         if p["id"] != store.default_project_id())
        store.set_owned_by("mcp_server", "deploys-mcp", other_uid)
        probs = (await cx.post("/api/setup/check", json={"plan": attach})).json()["problems"]
        ck("a server another project owns cannot be attached",
           any(p["where"] == "tools.t5" and "belongs to the project" in p["message"] for p in probs),
           probs)
        store.set_owned_by("mcp_server", "deploys-mcp", store.default_project_id())
        moved = copy.deepcopy(attach)
        moved["name"] = "Attach test"
        r = await cx.post("/api/setup/apply", json={"plan": moved})
        ck("apply with a server from the default project", r.status_code == 201, r.text[:300])
        if r.status_code == 201:
            new_uid = r.json()["project"]["id"]
            eq("the attached server moved into the new project",
               store.get_mcp_server("deploys-mcp")["owned_by"], new_uid)
            store.put_in_project("mcp_server", "deploys-mcp", store.default_project_id())   # for the checks below
        missing = copy.deepcopy(attach)
        missing["tools"][0]["name"] = "gone-mcp"
        missing["agents"][0]["mcp_servers"] = []
        probs = (await cx.post("/api/setup/check", json={"plan": missing})).json()["problems"]
        ck("attaching a server that is not on the cell is flagged on the tool",
           any(p["where"] == "tools.t5" and "gone-mcp" in p["message"] for p in probs), probs)

        print("== apply: a plan edited in the console ==")
        edited = copy.deepcopy(attach)
        edited["name"] = "Edited in place"
        edited["watches"].append({
            "key": "w2", "existing": False, "name": "deploy-hooks", "connector": "webhook",
            "config": {"text_template": "{service} deployed",
                       "labels": [{"name": "service", "field": "service", "primary": True}]},
            "sentence": "", "needs": "send", "sample": None})
        edited["wakes"][0].update(avg["wakes"][0])
        edited["wakes"].append({
            "key": "k2", "name": "hourly-deploys", "sources": ["deploy-hooks"], "filters": [],
            "key_field": "", "condition": {"every": "60m"}, "cooldown": "5m", "window": "1h",
            "sentence": "", "knobs": []})
        edited["agents"][0].update({"prompt": "Look at the latency first.",
                                    "handoffs": [{"verdict": "dig", "agent": "checkout-rca",
                                                  "cooldown": "1h"}]})
        edited["agents"][1].update({"enabled": True})
        edited["agents"].append({"key": "a3", "name": "deploy-digest", "trigger": "hourly-deploys",
                                 "on_trigger": True, "prompt": "Summarize the deploys.",
                                 "handoffs": [], "mcp_servers": [], "sentence": "",
                                 "optional": False, "enabled": True})
        chk = (await cx.post("/api/setup/check", json={"plan": edited})).json()
        eq("the edited plan checks clean", chk["problems"], [])
        r = await cx.post("/api/setup/apply", json={"plan": chk["plan"]})
        ck("apply accepts the checked plan", r.status_code == 201, r.text)
        euid = r.json()["project"]["id"]
        trig = {t["name"]: t for t in store.list_catalog_triggers()}
        t1 = trig[chk["plan"]["wakes"][0]["name"]]
        eq("the average wake-up is set up on the swapped source",
           (t1["sources"], t1["condition"]["aggregate"], t1["condition"]["field"],
            t1["filters"], t1.get("owned_by")),
           (["payments-events"], "avg", "latency_ms",
            [{"field": "level", "op": "eq", "value": "error"}], euid))
        t2 = trig[chk["plan"]["wakes"][1]["name"]]
        eq("the schedule wake-up is set up", (t2["sources"], t2["condition"].get("every")),
           (["deploy-hooks"], "60m"))
        tri = store.get_catalog_agent(chk["plan"]["agents"][0]["name"])
        eq("the edited agent: its prompt, handoff and the attached server",
           (tri["prompt"], [(h["verdict"], h["cooldown"]) for h in tri["handoffs"]],
            tri["mcp_servers"]),
           ("Look at the latency first.\n\nWhen you are done, call conclude with what you found "
            "and a verdict (dig when it needs a closer look).", [("dig", "1h")], ["deploys-mcp"]))
        dig = store.get_catalog_agent(chk["plan"]["agents"][2]["name"])
        eq("the added agent is on the schedule", dig["trigger"], t2["name"])
        eq("the copied skill is the project's", [s["name"] for s in store.list_skills(euid)],
           ["checkout-error-codes"])
        eq("the attached server is not created again",
           [m["name"] for m in store.list_mcp_servers()].count("deploys-mcp"), 1)

        secret_plan = {"watches": [{"connector": "postgres",
                                    "config": {"dsn": "postgres://u:p@h/db", "table": "t"}}]}
        eq("the stored plan leaves a typed secret out",
           S.stored_plan(secret_plan)["watches"][0]["config"], {"dsn": "", "table": "t"})
        eq("a new polled source without its secret needs a credential",
           (S.needs_for("postgres", {"table": "t"}), S.needs_for("postgres", {"dsn": "x"}),
            S.needs_for("webhook", {})), ("credential", "none", "send"))

        print("== auth ==")
        r = await anon.post("/api/setup/plan", json={"goal": "x"})
        eq("no credential: 401", r.status_code, 401)
        r = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
        reader = r.json()["secret"]
        for method, path, js in (("post", "/api/setup/plan", {"goal": "x"}),
                                 ("post", "/api/setup/adjust", {"plan": {}, "instruction": "x"}),
                                 ("post", "/api/setup/apply", {"plan": {}}),
                                 ("post", "/api/setup/check", {"plan": {}}),
                                 ("get", f"/api/projects/{uid}/setup", None),
                                 ("put", f"/api/projects/{uid}/setup", {"step": "done"}),
                                 ("post", f"/api/projects/{uid}/setup/test-event",
                                  {"source": "checkout-errors"}),
                                 ("post", f"/api/projects/{uid}/setup/practice", None)):
            kw = {"json": js} if js is not None else {}
            r = await getattr(anon, method)(path, headers=H(reader), **kw)
            eq(f"a read key: {method.upper()} {path.split('/api/')[1].replace(uid, '<id>')} is "
               "403", r.status_code, 403)
        r = await anon.get(f"/api/projects/{ouid}/setup", headers=H(secret))
        eq("a project key cannot read its setup", r.status_code, 403)
        r = await anon.put(f"/api/projects/{ouid}/setup", headers=H(secret),
                           json={"step": "done"})
        eq("nor move it", r.status_code, 403)
        r = await cx.get("/api/projects/" + store.default_project_id() + "/setup")
        eq("a project set up another way has no setup: 404", r.status_code, 404)

        daemon.resolve_provider = real_resolve
        await cx.aclose()
        await anon.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
