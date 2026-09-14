"""The OpenAI chat-completions provider (tares/models.py, TR-300): the neutral conversation
rendered to chat messages and function tools, replies parsed back, streaming assembled from SSE
deltas, and the agent runner's loop driven end to end on it, unchanged.

Run: .venv/bin/python tests/test_openai_provider.py   (no external services needed)
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares.models import (ModelError, ModelReply, OpenAIProvider, ToolCall, empty_usage,
                          tool_call_in_text, tool_message)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


CALLS: list = []
ANSWERS: list = []          # queued responses: a dict (JSON) or a list of SSE chunks
STATUS = [200]
HEADERS: dict = {}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        CALLS.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
        answer = ANSWERS.pop(0) if ANSWERS else {"choices": [{"message": {"content": "?"}}]}
        self.send_response(STATUS[0])
        for k, v in HEADERS.items():
            self.send_header(k, v)
        if isinstance(answer, list):
            out = "".join(f"data: {json.dumps(c) if not isinstance(c, str) else c}\n\n"
                          for c in answer).encode()
            self.send_header("content-type", "text/event-stream")
        else:
            out = json.dumps(answer).encode()
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


TOOLS = [{"name": "read", "description": "read a timeline",
          "input_schema": {"type": "object", "properties": {"selector": {"type": "object"}}}}]


def completion(text=None, tool_calls=None, usage=None, finish="stop", model="gpt-x"):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": model, "choices": [{"finish_reason": finish, "message": msg}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


async def main():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    p = OpenAIProvider(base, {"Authorization": "Bearer k"}, timeout=5)

    print("== rendering: neutral conversation -> chat messages ==")
    tc = ToolCall(id="call_1", name="read", arguments={"selector": {"service": "a"}})
    convo = [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "looking", "tool_calls": [tc]},
             tool_message([("call_1", "the timeline")])]
    wire = OpenAIProvider.render("be brief", convo)
    check("system prompt is the first message", wire[0] == {"role": "system", "content": "be brief"})
    check("user text passes through", wire[1] == {"role": "user", "content": "hi"})
    check("assistant tool calls become function calls with JSON arguments",
          wire[2] == {"role": "assistant", "content": "looking", "tool_calls": [
              {"id": "call_1", "type": "function",
               "function": {"name": "read", "arguments": '{"selector": {"service": "a"}}'}}]},
          str(wire[2]))
    check("tool results become tool-role messages",
          wire[3] == {"role": "tool", "tool_call_id": "call_1", "content": "the timeline"}, str(wire[3]))
    check("no system message when the prompt is empty", OpenAIProvider.render("", [])[0:1] == [])
    raw = {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function",
                                                                  "function": {"name": "read", "arguments": "{}"}}]}
    check("an assistant reply with raw replays verbatim",
          OpenAIProvider.render("", [{"role": "assistant", "content": "", "tool_calls": [], "raw": raw}])[0] is raw)
    flat = OpenAIProvider.render("", [{"role": "assistant", "content": [
        {"type": "text", "text": "earlier"}, {"type": "tool_use", "id": "t", "name": "n", "input": {}}]}])
    check("Anthropic-shaped history keeps its text only",
          flat == [{"role": "assistant", "content": "earlier"}], str(flat))
    check("tools become function tools",
          OpenAIProvider.render_tools(TOOLS) == [{"type": "function", "function": {
              "name": "read", "description": "read a timeline",
              "parameters": TOOLS[0]["input_schema"]}}])

    print("== complete: request and parsed reply ==")
    ANSWERS.append(completion(text="one", tool_calls=[
        {"id": "call_9", "type": "function", "function": {"name": "read", "arguments": '{"k": 1}'}},
        {"id": "call_10", "type": "function", "function": {"name": "read", "arguments": "not json"}}],
        usage={"prompt_tokens": 12, "completion_tokens": 3,
               "prompt_tokens_details": {"cached_tokens": 4}}, finish="tool_calls"))
    r = await p.complete(model="gpt-x", system="sys", tools=TOOLS,
                         messages=[{"role": "user", "content": "hi"}], max_tokens=99)
    sent = CALLS[-1]
    check("posted to /v1/chat/completions", sent["path"] == "/v1/chat/completions", sent["path"])
    check("bearer header on the request", sent["headers"].get("Authorization") == "Bearer k")
    check("body: model, max_tokens, system+user messages, function tools",
          sent["body"] == {"model": "gpt-x", "max_tokens": 99,
                           "messages": [{"role": "system", "content": "sys"},
                                        {"role": "user", "content": "hi"}],
                           "tools": OpenAIProvider.render_tools(TOOLS)}, str(sent["body"]))
    check("no stream fields on a blocking call", "stream" not in sent["body"])
    check("text parsed", r.text == "one")
    check("tool calls parsed with JSON arguments",
          r.tool_calls[0] == ToolCall("call_9", "read", {"k": 1}), str(r.tool_calls[0]))
    check("malformed arguments are kept, not dropped",
          r.tool_calls[1].arguments == {"arguments": "not json"}, str(r.tool_calls[1]))
    check("usage mapped to the Anthropic field names, cached prompt tokens as cache reads",
          r.usage == {"input_tokens": 12, "output_tokens": 3, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 4}, str(r.usage))
    check("model and finish reason", (r.model, r.stop_reason) == ("gpt-x", "tool_calls"))
    check("raw carries the tool calls for replay",
          r.raw["tool_calls"][0]["id"] == "call_9" and r.raw["role"] == "assistant", str(r.raw))

    ANSWERS.append(completion(text="done"))
    await p.complete(model="gpt-x", system="sys", tools=TOOLS,
                     messages=[{"role": "user", "content": "hi"}], max_tokens=9, tools_allowed=False)
    check("tools disabled -> tool_choice none, tools still declared",
          CALLS[-1]["body"].get("tool_choice") == "none" and "tools" in CALLS[-1]["body"])
    ANSWERS.append(completion(text="done"))
    await p.complete(model="gpt-x", system="", tools=[], messages=[{"role": "user", "content": "hi"}],
                     max_tokens=9)
    check("no tools -> no tools key and no tool_choice",
          "tools" not in CALLS[-1]["body"] and "tool_choice" not in CALLS[-1]["body"])

    print("== provider-reported cost ==")
    ANSWERS.append(completion(text="x", usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0042}))
    r = await p.complete(model="m", system="", tools=[], messages=[{"role": "user", "content": "hi"}],
                         max_tokens=9)
    check("OpenRouter's usage.cost becomes cost_usd", r.usage.get("cost_usd") == 0.0042, str(r.usage))
    HEADERS["x-litellm-response-cost"] = "0.01"
    ANSWERS.append(completion(text="x"))
    r = await p.complete(model="m", system="", tools=[], messages=[{"role": "user", "content": "hi"}],
                         max_tokens=9)
    check("LiteLLM's cost header becomes cost_usd", r.usage.get("cost_usd") == 0.01, str(r.usage))
    HEADERS.clear()
    ANSWERS.append(completion(text="x"))
    r = await p.complete(model="m", system="", tools=[], messages=[{"role": "user", "content": "hi"}],
                         max_tokens=9)
    check("no cost reported -> no cost_usd key", "cost_usd" not in r.usage, str(r.usage))

    print("== stream: text deltas, tool-call deltas, usage last ==")
    ANSWERS.append([
        {"model": "gpt-s", "choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "re", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "ad", "arguments": '{"sel'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ector": 1}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
        "[DONE]",
    ])
    pieces, reply = [], None
    async for item in p.stream(model="gpt-s", system="sys", tools=TOOLS,
                               messages=[{"role": "user", "content": "hi"}], max_tokens=50):
        if isinstance(item, str):
            pieces.append(item)
        else:
            reply = item
    check("stream request asks for usage in the stream",
          CALLS[-1]["body"].get("stream") is True and
          CALLS[-1]["body"].get("stream_options") == {"include_usage": True}, str(CALLS[-1]["body"]))
    check("text deltas yielded as they arrive", pieces == ["Hel", "lo"], str(pieces))
    check("the reply comes last, with the joined text", isinstance(reply, ModelReply) and reply.text == "Hello")
    check("tool call assembled across deltas",
          reply.tool_calls == [ToolCall("call_a", "read", {"selector": 1})], str(reply.tool_calls))
    check("usage from the final chunk", reply.usage["input_tokens"] == 7 and reply.usage["output_tokens"] == 2)
    check("model and finish reason from the stream", (reply.model, reply.stop_reason) == ("gpt-s", "tool_calls"))
    replay = OpenAIProvider.render("", [reply.as_message()])[0]
    check("the streamed reply replays with its tool calls",
          replay["tool_calls"][0]["function"] == {"name": "read", "arguments": '{"selector": 1}'}, str(replay))

    print("== errors ==")
    STATUS[0] = 429
    ANSWERS.append({"error": {"message": "slow down"}})
    try:
        await p.complete(model="m", system="", tools=[], messages=[{"role": "user", "content": "hi"}], max_tokens=1)
        check("4xx raises ModelError", False, "no error")
    except ModelError as e:
        check("4xx raises ModelError naming the provider and status", str(e).startswith("openai 429: "), str(e))
    ANSWERS.append({"error": {"message": "slow down"}})
    try:
        async for _ in p.stream(model="m", system="", tools=[], messages=[{"role": "user", "content": "hi"}], max_tokens=1):
            pass
        check("4xx on a stream raises ModelError", False, "no error")
    except ModelError as e:
        check("4xx on a stream raises ModelError", "429" in str(e), str(e))
    STATUS[0] = 200
    named = OpenAIProvider(base, {}, label="litellm")
    STATUS[0] = 500; ANSWERS.append({})
    try:
        await named.complete(model="m", system="", tools=[], messages=[{"role": "user", "content": "hi"}], max_tokens=1)
    except ModelError as e:
        check("a router's label names it in the error", str(e).startswith("litellm 500"), str(e))
    STATUS[0] = 200

    print("== the agent runner's loop on the OpenAI provider, unchanged ==")

    class Toolbox:
        tool_defs = TOOLS
        calls: list = []

        def owns(self, name):
            return name == "read"

        async def call(self, name, args):
            self.calls.append((name, args))
            return "evidence: latency spiked at 09:00"

    class Store:
        def last_finding(self, *a):
            return None

    runner = object.__new__(ba.AgentRunner)
    runner.store = Store()
    ANSWERS.append(completion(text=None, tool_calls=[
        {"id": "call_r", "type": "function", "function": {"name": "read", "arguments": '{"selector": {"service": "billing"}}'}}],
        finish="tool_calls", usage={"prompt_tokens": 100, "completion_tokens": 10}))
    ANSWERS.append(completion(text="Root cause: a deploy at 08:58.", usage={"prompt_tokens": 150, "completion_tokens": 20}))
    usage = empty_usage()
    agent = {"name": "sre", "prompt": "investigate", "model": "gpt-x"}
    toolbox = Toolbox()
    finding, rounds, tool_calls, external, exhausted, partial = await runner._loop_with(
        agent, "latency_high", "billing", "the timeline", p, toolbox, usage)
    check("the run concluded with the model's finding",
          finding == "Root cause: a deploy at 08:58." and not exhausted, f"{finding!r} {exhausted}")
    check("two rounds, one tool call, the external tool recorded",
          (rounds, tool_calls, external) == (2, 1, ["read"]), f"{rounds} {tool_calls} {external}")
    check("the tool was called with the parsed arguments",
          toolbox.calls == [("read", {"selector": {"service": "billing"}})], str(toolbox.calls))
    second = CALLS[-1]["body"]["messages"]
    check("the second request replays the assistant's tool call and carries the tool result",
          second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call_r"
          and second[-1] == {"role": "tool", "tool_call_id": "call_r",
                             "content": "evidence: latency spiked at 09:00"}, str(second[-2:]))
    check("usage summed over both calls",
          (usage["calls"], usage["input_tokens"], usage["output_tokens"]) == (2, 250, 30), str(usage))

    print("== small-model slips: tool name case, unknown tools (TR-306) ==")
    ANSWERS.append(completion(text=None, tool_calls=[
        {"id": "c1", "type": "function", "function": {"name": "READ ", "arguments": '{"selector": {"service": "x"}}'}},
        {"id": "c2", "type": "function", "function": {"name": "frobnicate", "arguments": "{}"}}],
        finish="tool_calls"))
    ANSWERS.append(completion(text="Finding."))
    toolbox = Toolbox(); toolbox.calls = []
    finding, rounds, tool_calls, external, exhausted, partial = await runner._loop_with(
        agent, "latency_high", "billing", "the timeline", p, toolbox, empty_usage())
    check("a mis-cased tool name still reaches the tool", toolbox.calls == [("read", {"selector": {"service": "x"}})], str(toolbox.calls))
    second = CALLS[-1]["body"]["messages"]
    unknown = next(m for m in second if m.get("role") == "tool" and m.get("tool_call_id") == "c2")
    check("an unknown tool answers with the list of real tools, and the run goes on",
          "unknown tool 'frobnicate'" in unknown["content"] and "read" in unknown["content"] and finding == "Finding.", str(unknown))

    print("== a tool call written as JSON text (llama-style) is run, not filed as the finding ==")
    check("bare JSON object", tool_call_in_text('{"name": "read", "parameters": {"selector": {"service": "a"}}}', TOOLS)
          == ToolCall("text_call", "read", {"selector": {"service": "a"}}))
    check("embedded in prose, arguments key", tool_call_in_text('I will call the tool now: {"name": "Read", "arguments": {"selector": {"service": "b"}}} and wait.', TOOLS)
          == ToolCall("text_call", "read", {"selector": {"service": "b"}}))
    check("function-shaped object", tool_call_in_text('{"function": {"name": "read"}, "input": {"x": 1}}', TOOLS)
          == ToolCall("text_call", "read", {"x": 1}))
    check("prose with braces that is not a call", tool_call_in_text('Latency {p99} rose; the cause is {"name": "nobody"}', TOOLS) is None)
    check("a real finding with no JSON", tool_call_in_text("Root cause: the deploy.", TOOLS) is None)
    check("string arguments are parsed", tool_call_in_text('{"name": "read", "arguments": "{\\"k\\": 2}"}', TOOLS).arguments == {"k": 2})
    ANSWERS.append(completion(text='To narrow it down I will use the read tool.\n\n{"name": "read", "parameters": {"selector": {"service": "checkout"}}}'))
    ANSWERS.append(completion(text="Finding: the 09:00 deploy."))
    toolbox = Toolbox(); toolbox.calls = []
    finding, rounds, tool_calls, external, exhausted, partial = await runner._loop_with(
        agent, "latency_high", "billing", "the timeline", p, toolbox, empty_usage())
    check("the meant call ran and the run concluded on the next round",
          toolbox.calls == [("read", {"selector": {"service": "checkout"}})] and finding == "Finding: the 09:00 deploy." and rounds == 2 and tool_calls == 1,
          f"{toolbox.calls} {finding!r} {rounds} {tool_calls}")
    second = CALLS[-1]["body"]["messages"]
    check("the replayed assistant turn carries a real tool call the tool message answers",
          second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["function"]["name"] == "read"
          and second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == second[-2]["tool_calls"][0]["id"], str(second[-2:]))

    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
