"""The AI-guided project builder's backend (TR-242, TR-246): build mode on the assist endpoint.

No live LLM. Checks the step-scoped toolsets, the proposal schemas, the build system prompt and
that POST /api/agent/chat accepts mode + step (and still refuses without a key).

Run: .venv/bin/python tests/test_agent_build.py
"""
import asyncio
import os

os.environ["TARES_DB"] = "/tmp/agent_build_t.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/none_agent_build.yaml"
os.environ.pop("TARES_ANTHROPIC_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
for _p in ("/tmp/agent_build_t.duckdb", "/tmp/agent_build_t.duckdb.wal"):
    if os.path.exists(_p):
        os.remove(_p)

import httpx
from tares import agent

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


READ = {t["name"] for t in agent.TOOLS}
ALL_PROPOSALS = {t["name"] for t in agent.PROPOSAL_TOOLS + agent.BUILD_PROPOSAL_TOOLS}


def names(tools):
    return {t["name"] for t in tools}


print("== toolsets ==")
ck("ask mode is the read tools plus the catalog cards",
   names(agent.tools_for()) == READ | {"propose_labels", "propose_trigger"},
   str(names(agent.tools_for())))
ck("ask mode never offers the build-only cards",
   not names(agent.tools_for()) & {"propose_source", "propose_agent"})
expected = {
    "sources": {"propose_source", "propose_project"},
    "watch": {"propose_labels", "propose_trigger"},
    "agent": {"propose_agent"},
}
for step, cards in expected.items():
    got = names(agent.tools_for("build", step))
    ck(f"build/{step} = read tools + exactly {sorted(cards)}", got == READ | cards, str(got - READ))
ck("an unknown build step gets no proposal tools at all",
   names(agent.tools_for("build", "nope")) == READ)
ck("BUILD_STEPS lists the steps in flow order",
   list(agent.BUILD_STEPS) == ["sources", "watch", "agent"])

print("== schemas ==")
by_name = {t["name"]: t for t in agent.BUILD_PROPOSAL_TOOLS}
src = by_name["propose_source"]["input_schema"]
ck("propose_source requires name, connector, needs, reasoning",
   set(src["required"]) == {"name", "connector", "needs", "reasoning"}, str(src["required"]))
ck("propose_source needs is a list of field names",
   src["properties"]["needs"]["type"] == "array"
   and src["properties"]["needs"]["items"]["type"] == "string")
ck("propose_source config is an object, poll optional",
   src["properties"]["config"]["type"] == "object" and "poll" not in src["required"])
pj = by_name["propose_project"]["input_schema"]
ck("propose_project requires template, name, needs, reasoning",
   set(pj["required"]) == {"template", "name", "needs", "reasoning"}, str(pj["required"]))
ck("read tools include list_templates, list_projects and detect_template",
   {"list_templates", "list_projects", "detect_template"} <= READ)
ag = by_name["propose_agent"]["input_schema"]
ck("propose_agent requires name, trigger, prompt, delivery, reasoning",
   set(ag["required"]) == {"name", "trigger", "prompt", "delivery", "reasoning"}, str(ag["required"]))
ck("propose_agent delivery is a kind from slack|webhook|none plus an optional typed URL",
   ag["properties"]["delivery"]["properties"]["kind"]["enum"] == ["slack", "webhook", "none"]
   and set(ag["properties"]["delivery"]["properties"]) == {"kind", "url"}
   and ag["properties"]["delivery"]["required"] == ["kind"])
ck("every proposal tool requires reasoning",
   all("reasoning" in t["input_schema"]["required"]
       for t in agent.PROPOSAL_TOOLS + agent.BUILD_PROPOSAL_TOOLS))
ck("every proposal tool maps to a card kind",
   set(agent._PROPOSAL_KIND) == ALL_PROPOSALS, str(set(agent._PROPOSAL_KIND) ^ ALL_PROPOSALS))
ck("card kinds are the object kinds the console knows",
   set(agent._PROPOSAL_KIND.values()) == {"labels", "trigger", "source", "agent", "project"})
tr = next(t for t in agent.PROPOSAL_TOOLS if t["name"] == "propose_trigger")["input_schema"]
ck("propose_trigger names sources (and filters, key_field), never a view",
   "sources" in tr["required"] and "view" not in tr["properties"]
   and tr["properties"]["sources"]["type"] == "array"
   and tr["properties"]["filters"]["items"]["properties"]["op"]["enum"][0] == "eq"
   and "key_field" in tr["properties"], str(tr["required"]))
ck("the read tools read, they do not query a view",
   "read" in READ and "query" not in READ and "propose_view" not in ALL_PROPOSALS)

print("== prompts ==")
base, build = agent.system_prompt(), agent.system_prompt("build")
ck("ask prompt has no build section", "BUILD MODE" not in base)
ck("build prompt is the ask prompt plus the build section",
   build.startswith(base) and "BUILD MODE" in build)
for marker in ("ASK BEFORE YOU GUESS", "INSTALLED connectors", "`needs`", "source_fields",
               "One card per object", "propose NOTHING in that turn",
               "ask what the agent should do", "delivery.url", "TEMPLATES FIRST",
               "never invent a name", "`http_poll` source"):
    ck(f"build prompt says: {marker}", marker in build)


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        print("== endpoint ==")
        r = await cx.post("/api/agent/chat", json={
            "messages": [{"role": "user", "content": "watch my logs"}],
            "mode": "build", "step": "sources"})
        ck("build turn without a key -> 400 (same gate as Ask)", r.status_code == 400, r.text)
        r = await cx.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        ck("ask turn without a key -> 400", r.status_code == 400, r.text)
        print("== agent proposal names a real trigger ==")
        # the guard reads the daemon's trigger list; with none, every name is unknown
        agent._SELF = "http://t"
        real_client = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: real_client(transport=httpx.ASGITransport(app=app), **{k: v for k, v in kw.items() if k != "base_url"}, base_url=kw.get("base_url", "http://t"))
        try:
            have = await agent._trigger_names({})
        finally:
            httpx.AsyncClient = real_client
        ck("no triggers on a fresh cell -> empty list, not None", have == [], repr(have))
        await cx.aclose()
    await handoff_cards()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


async def handoff_cards():
    """An agent card may hand off only to an agent that exists or was proposed earlier in the
    same reply; the target card goes first."""
    print("== agent cards with a handoff ==")
    from tares.models import ModelReply, ToolCall

    def card(cid, name, handoffs=None, runs=True):
        a = {"name": name, "trigger": "spike", "prompt": "p", "delivery": {"kind": "none"},
             "reasoning": "r", "runs_on_trigger": runs}
        if handoffs:
            a["handoffs"] = handoffs
        return ToolCall(id=cid, name="propose_agent", arguments=a)

    class Stub:
        def __init__(self, calls):
            self.replies = [ModelReply(text="", tool_calls=calls, usage={}),
                            ModelReply(text="done", tool_calls=[], usage={})]
            self.results = []

        async def stream(self, *, messages, **kw):
            last = messages[-1]
            if last.get("role") == "tool":
                self.results.extend(r["content"] for r in last["results"])
            yield self.replies.pop(0)

    async def run(calls):
        stub = Stub(calls)
        lines = [l async for l in agent._run_agent(stub, [{"role": "user", "content": "x"}], None,
                                                   {}, None, None, agent._tracing.Observation(None),
                                                   "build", "agent")]
        cards = [l for l in lines if '"type": "proposal"' in l]
        return cards, stub.results

    real_t, real_a = agent._trigger_names, agent._agent_names

    async def triggers(h):
        return ["spike"]

    async def agents(h):
        return ["already-there"]
    agent._trigger_names, agent._agent_names = triggers, agents
    try:
        hand = [{"verdict": "investigate", "agent": "rca"}]
        cards, res = await run([card("a", "triage", hand)])
        ck("a handoff to an agent that does not exist shows no card",
           not cards and any("no agent named rca" in r for r in res), str(res))
        cards, res = await run([card("a", "rca", runs=False), card("b", "triage", hand)])
        ck("the target proposed first in the same reply: both cards show",
           len(cards) == 2 and '"runs_on_trigger": false' in cards[0], str(res))
        cards, res = await run([card("a", "triage", [{"verdict": "x", "agent": "already-there"}])])
        ck("a handoff to an existing agent shows the card", len(cards) == 1, str(res))
    finally:
        agent._trigger_names, agent._agent_names = real_t, real_a


asyncio.run(main())
