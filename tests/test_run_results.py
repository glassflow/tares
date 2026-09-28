"""Structured run results (TR-220): what a run produced, read off its tool calls and deliveries.

Part 1 feeds `results.from_tool_call` the shapes real MCP servers answer with. Part 2 drives the
real run path (`_run` into the model loop) with a scripted provider and a fake GitHub MCP server,
and checks what is stored on the run.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares import results as R
from tares.models import ModelReply, ToolCall

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


PR_OUT = json.dumps({"number": 42, "html_url": "https://github.com/acme/shop/pull/42",
                     "state": "open", "title": "Fix pricing route"})
PUSH_OUT = json.dumps({"ref": "refs/heads/fix", "object": {
    "sha": "9f2c81d4a1b2c3d4e5f60718293a4b5c6d7e8f90", "type": "commit"}})
COMMIT_URL_OUT = "Pushed. https://github.com/acme/shop/commit/9f2c81d4a1b2c3d4e5f60718293a4b5c6d7e8f90"


def part1():
    print("== reading results off tool calls ==")
    r = R.from_tool_call("github__create_pull_request", {"title": "Fix pricing route"}, PR_OUT)
    check("a created pull request", r == {"kind": "pr", "label": "PR #42 Fix pricing route",
                                          "url": "https://github.com/acme/shop/pull/42"}, str(r))
    r = R.from_tool_call("github__push_files", {}, PUSH_OUT)
    check("pushed files: the commit sha", r == {"kind": "commit", "label": "commit 9f2c81d"}, str(r))
    r = R.from_tool_call("github__create_or_update_file", {}, COMMIT_URL_OUT)
    check("a commit url when the server gives one", r and r.get("url", "").endswith("/commit/9f2c81d4a1b2c3d4e5f60718293a4b5c6d7e8f90"), str(r))
    r = R.from_tool_call("slack__slack_post_message", {"channel": "#oncall"}, '{"ok": true, "ts": "1.2"}')
    check("a Slack post names its channel", r == {"kind": "slack", "label": "Slack #oncall"}, str(r))
    check("a failed call produces nothing",
          R.from_tool_call("github__create_pull_request", {}, "tool error from server: 422") is None)
    check("a read produces nothing", R.from_tool_call("github__get_pull_request", {}, PR_OUT) is None)
    check("a pull request call with no url in the answer produces nothing",
          R.from_tool_call("github__create_pull_request", {}, '{"ok": true}') is None)
    w = R.webhook_result("https://ingest.example.com/v1/reports?token=secret")
    check("a write-back names the host only, never the path or token",
          w == {"kind": "webhook", "label": "write-back to ingest.example.com"}, str(w))
    check("custom results from conclude", R.custom_results(["opened JIRA-12", " ", "x"]) ==
          [{"kind": "custom", "label": "opened JIRA-12"}, {"kind": "custom", "label": "x"}])
    m = R.merge([{"kind": "pr", "label": "a", "url": "u"}, {"kind": "pr", "label": "b", "url": "u"},
                 {"kind": "bogus", "label": "z"}, None])
    check("merge drops duplicates and unknown kinds", m == [{"kind": "pr", "label": "a", "url": "u"}], str(m))


class Provider:
    kind = "anthropic"

    def __init__(self, replies):
        self.replies = list(replies)

    async def complete(self, **kw):
        return self.replies.pop(0)


def reply(calls):
    return ModelReply(text="", tool_calls=calls, usage={"input_tokens": 1, "output_tokens": 1})


class GitHub:
    """A toolbox standing in for a GitHub MCP server."""
    tool_defs = [{"name": "github__create_pull_request", "description": "open a PR",
                  "input_schema": {"type": "object"}}]
    failures = []

    def owns(self, name):
        return name.startswith("github__")

    async def call(self, name, args):
        return PR_OUT


class Store:
    def __init__(self):
        self.results = None
        self.finished = {}

    def last_finding(self, *a):
        return None

    def agent_runs_today(self, *a, **k):
        return 0

    def agent_cost_total(self, name):
        return 0.0

    def finish_agent_run(self, run_id, status, **kw):
        self.finished = {"status": status, **kw}

    def set_run_results(self, run_id, results):
        self.results = results

    def record_run_usage(self, *a, **k):
        pass

    def record_model_usage(self, *a, **k):
        pass


class NoTracing:
    def tracer_for(self, name):
        return None


async def run(agent, replies):
    store = Store()
    r = object.__new__(ba.AgentRunner)
    r.store, r.tracing = store, NoTracing()
    provider = Provider(replies)

    async def loop(agent, trigger, key, payload, prov, model, usage, tracer, obs, concluded=None,
                   produced=None):
        return await r._loop_with(agent, trigger, key, payload, provider, GitHub(), usage, tracer,
                                  obs, model=model, concluded=concluded, produced=produced)

    async def record(*a, **k):
        return []

    r._loop, r._record = loop, record
    r._callback_anchor = lambda agent, trigger, key: (key, {})
    status, err = await r._run(agent, "incident", "shop", "payload", "run_1", "d1")
    return status, err, store


async def part2():
    ba.resolve_for_agent = lambda store, agent: (Provider([]), "test", "anthropic", "")
    ba.default_model_for = lambda store, provider_id: "claude-test"
    ba.price_usage = lambda *a: 0.0

    print("== a run that opens a pull request ==")
    agent = {"name": "fixer", "prompt": "Fix it. End with conclude."}
    status, err, store = await run(agent, [
        reply([ToolCall(id="t1", name="github__create_pull_request",
                        arguments={"title": "Fix pricing route"})]),
        reply([ToolCall(id="t2", name="conclude", arguments={
            "outcome": "finding", "summary": "opened a fix", "produced": ["asked on-call to review"]})]),
    ])
    check("the run is ok", (status, err) == ("ok", None), f"{status} {err}")
    check("the pull request is a result, from the tool call",
          (store.results or [])[:1] == [{"kind": "pr", "label": "PR #42 Fix pricing route",
                                         "url": "https://github.com/acme/shop/pull/42"}], str(store.results))
    check("the self-reported line is a custom result",
          {"kind": "custom", "label": "asked on-call to review"} in (store.results or []), str(store.results))

    print("== a run that only reads ==")
    status, err, store = await run({"name": "reader", "prompt": "Look."}, [
        ModelReply(text="all fine", tool_calls=[], usage={"input_tokens": 1, "output_tokens": 1})])
    check("no results stored", store.results is None, str(store.results))

    print("== a run that fails after acting keeps what it did ==")
    status, err, store = await run({"name": "fixer", "prompt": "Fix it. End with conclude."}, [
        reply([ToolCall(id="t1", name="github__create_pull_request", arguments={})]),
        ModelReply(text="", tool_calls=[], usage={"input_tokens": 1, "output_tokens": 1})])
    check("the run did not conclude", status == "empty", status)
    check("but the pull request it opened is on the run",
          any(r["kind"] == "pr" for r in (store.results or [])), str(store.results))


if __name__ == "__main__":
    part1()
    asyncio.run(part2())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
