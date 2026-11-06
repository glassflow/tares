# Tares enterprise licence, see ee/LICENSE
"""The guided project setup, the part that needs more than the owner: a draft made with a person's
key is led by that person, a failed apply gives a draft back with its lead, scope and members, and
a setup with no draft is led by whoever sets it up. Runs on a licensed cell with a stand-in model
provider; the rest of the guided setup is tests/test_setup_flow.py.

Run: .venv/bin/python ee/tests/test_setup_flow_team.py
"""
import asyncio
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp()
ROOT = "root-token-setup-flow-team"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ.pop("TARES_PUBLIC_URL", None)
os.environ.pop("TARES_MCP_URL", None)

import httpx

from tares import setup_flow as S
from tares.models import ModelReply, ToolCall

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


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


def plan_with(**kw):
    p = copy.deepcopy(GOOD)
    p.update(kw)
    return p


async def main():
    from tares import daemon
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        store = app.state.store
        runtime = app.state.runtime
        runner = app.state.agents
        runner.attach_loop()
        real_resolve = daemon.resolve_provider
        daemon.resolve_provider = lambda store, pid=None: (STUB, "test")

        def boom(*a, **k):
            raise RuntimeError("disk on fire")

        D = plan_with(name="Draft checkout", goal="Tell me in Slack about checkout errors",
                      tools=[], skills=[], notes=[])
        D["watches"] = [{**GOOD["watches"][0], "name": "Draft Errors", "config": {
            "event_type": "log", "text_template": "{service}: {msg}",
            "labels": [{"name": "service", "field": "service", "primary": True}, "region"]}}]
        D["wakes"] = [{**GOOD["wakes"][0], "key": "k1", "name": "draft-spike",
                       "sources": ["Draft Errors"]}]
        D["agents"] = [{**GOOD["agents"][0], "name": "draft-triage", "trigger": "draft-spike",
                        "handoffs": [], "mcp_servers": [], "slack": True}]

        async def settled_(uid):
            for _ in range(300):
                st = (await cx.get(f"/api/projects/{uid}/setup")).json()
                if (st.get("planning") or {}).get("state") != "running":
                    return st
                await asyncio.sleep(0.02)
            return st

        print("== drafts: the lead, the scope and the members ==")
        dana = (await cx.post("/api/people", json={"name": "Dana", "email": "dana@x.dev"})).json()["id"]
        eli = (await cx.post("/api/people", json={"name": "Eli", "email": "eli@x.dev"})).json()["id"]
        dk = (await cx.post("/api/keys", json={"name": "dana", "scopes": ["admin"],
                                              "person_id": dana})).json()["secret"]
        LD = copy.deepcopy(D)
        LD.update(name="Lead check", goal="Tell me about every new lead")
        LD["watches"][0]["name"] = "Lead Events"
        LD["wakes"][0].update(name="lead-new", sources=["Lead Events"])
        LD["agents"][0].update(name="lead-look", trigger="lead-new", slack=False)
        STUB.script = [[("propose_plan", LD)]]
        r = await cx.post("/api/setup/drafts", headers=H(dk), json={"goal": LD["goal"], "who": "tares"})
        ck("a draft made with a person's key (202)", r.status_code == 202, r.text)
        ldraft = r.json()["project"]["id"]
        eq("the person who made the draft leads it", store.get_project(ldraft)["lead_person_id"], dana)
        eq("the response says so too", (r.json()["project"].get("lead") or {}).get("name"), "Dana")
        st = await settled_(ldraft)
        await cx.patch(f"/api/projects/{ldraft}", headers=H(dk), json={"scope": "private"})
        await cx.post(f"/api/projects/{ldraft}/members", headers=H(dk), json={"person_id": eli})
        real_upsert = store.upsert_catalog_trigger
        store.upsert_catalog_trigger = boom
        r = await cx.post("/api/setup/apply", json={"plan": st["plan"], "project": ldraft})
        store.upsert_catalog_trigger = real_upsert
        eq("a failing step: 400", r.status_code, 400)
        back = store.get_project(ldraft)
        eq("the draft comes back a draft, with its lead and scope",
           (back["status"], back["lead_person_id"], back["scope"]), ("draft", dana, "private"))
        eq("... and its members", [x["person_id"] for x in store.list_project_members(ldraft)], [eli])
        r = await cx.post("/api/setup/apply", json={"plan": st["plan"], "project": ldraft})
        ck("set up for real (201)", r.status_code == 201, r.text)
        back = store.get_project(ldraft)
        eq("the project keeps its lead, scope and members",
           (back["status"], back["lead_person_id"], back["scope"],
            [x["person_id"] for x in store.list_project_members(ldraft)]),
           ("active", dana, "private", [eli]))

        print("== no draft: whoever sets it up leads it ==")
        def fresh(tag):
            raw = plan_with(who="own", agents=[], name=f"Lead {tag}",
                            own_agent={"name": "claude-code", "wake": "webhook",
                                       "sentence": "Your agent is woken at its webhook"})
            raw["watches"][0]["name"] = f"lead-{tag}"
            raw["wakes"][0]["sources"] = [f"lead-{tag}"]
            raw["wakes"][0]["name"] = f"lead-{tag}-spike"
            plan, errs = S.normalize(raw, store, runtime.catalog)
            eq(f"the {tag} plan is valid", errs, [])
            return plan
        r = await cx.post("/api/setup/apply", headers=H(dk), json={"plan": fresh("person")})
        ck("set up with a person's key (201)", r.status_code == 201, r.text[:300])
        eq("that person leads the project", store.get_project(r.json()["project"]["id"])["lead_person_id"], dana)

        daemon.resolve_provider = real_resolve
        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
