"""The model provider adapter (tares/models.py): neutral conversation in, neutral reply out, and
the Anthropic wire format exactly as the loops sent it before the adapter existed (TR-299).

Run: .venv/bin/python tests/test_model_provider.py   (no external services needed)
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares.models import (AnthropicProvider, ModelError, ModelReply, ToolCall, add_usage,
                          empty_usage, tool_message)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


CALLS: list = []
ANSWER: dict = {}
STATUS = [200]


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        CALLS.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
        out = json.dumps(ANSWER).encode()
        self.send_response(STATUS[0])
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


TOOLS = [{"name": "read", "description": "read", "input_schema": {"type": "object", "properties": {}}}]


async def main():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    p = AnthropicProvider(base, {"x-api-key": "k", "anthropic-version": "2023-06-01"}, timeout=5)

    print("== rendering: neutral conversation -> Anthropic messages ==")
    tc = ToolCall(id="tu_1", name="read", arguments={"selector": {"service": "a"}})
    reply = ModelReply(text="looking", tool_calls=[tc], usage={}, raw=[
        {"type": "text", "text": "looking"},
        {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"selector": {"service": "a"}}}])
    convo = [{"role": "user", "content": "hi"}, reply.as_message(),
             tool_message([("tu_1", "the timeline")])]
    wire = AnthropicProvider.render(convo)
    check("user text passes through", wire[0] == {"role": "user", "content": "hi"}, str(wire[0]))
    check("assistant replays its own raw blocks", wire[1] == {"role": "assistant", "content": reply.raw},
          str(wire[1]))
    check("tool results become a user turn of tool_result blocks",
          wire[2] == {"role": "user", "content": [
              {"type": "tool_result", "tool_use_id": "tu_1", "content": "the timeline"}]}, str(wire[2]))
    no_raw = AnthropicProvider.render([{"role": "assistant", "content": "t", "tool_calls": [tc]}])
    check("assistant without raw is rebuilt from text + tool calls",
          no_raw[0]["content"] == [{"type": "text", "text": "t"},
                                   {"type": "tool_use", "id": "tu_1", "name": "read",
                                    "input": {"selector": {"service": "a"}}}], str(no_raw))
    hist = AnthropicProvider.render([{"role": "assistant", "content": "earlier answer"}])
    check("the console's plain assistant history passes through",
          hist == [{"role": "assistant", "content": "earlier answer"}], str(hist))

    print("== complete: the wire request and the parsed reply ==")
    ANSWER.clear()
    ANSWER.update({"model": "claude-x", "stop_reason": "tool_use",
                   "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"},
                               {"type": "tool_use", "id": "tu_9", "name": "read", "input": {"k": 1}}],
                   "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}})
    r = await p.complete(model="claude-x", system="sys", tools=TOOLS,
                         messages=[{"role": "user", "content": "hi"}], max_tokens=99)
    sent = CALLS[-1]
    check("posted to /v1/messages", sent["path"] == "/v1/messages", sent["path"])
    check("credential header on the request", sent["headers"].get("x-api-key") == "k")
    check("body: model, system, tools, max_tokens, messages",
          sent["body"] == {"model": "claude-x", "max_tokens": 99, "system": "sys", "tools": TOOLS,
                           "messages": [{"role": "user", "content": "hi"}]}, str(sent["body"]))
    check("no tool_choice when tools are allowed", "tool_choice" not in sent["body"])
    check("text parts joined with newlines", r.text == "one\ntwo", repr(r.text))
    check("tool calls parsed", r.tool_calls == [ToolCall("tu_9", "read", {"k": 1})], str(r.tool_calls))
    check("usage in the Anthropic field names, missing ones zero",
          r.usage == {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3}, str(r.usage))
    check("model and stop reason", (r.model, r.stop_reason) == ("claude-x", "tool_use"))
    check("raw is the provider content, for replay", r.raw == ANSWER["content"])

    await p.complete(model="claude-x", system="sys", tools=TOOLS,
                     messages=[{"role": "user", "content": "hi"}], max_tokens=99, tools_allowed=False)
    check("tools disabled -> tool_choice none, tools still declared",
          CALLS[-1]["body"].get("tool_choice") == {"type": "none"} and CALLS[-1]["body"]["tools"] == TOOLS,
          str(CALLS[-1]["body"].get("tool_choice")))

    ANSWER.clear(); ANSWER.update({"content": [{"type": "text", "text": "bare"}]})
    r = await p.complete(model="m", system="s", tools=[], messages=[{"role": "user", "content": "x"}],
                         max_tokens=1)
    check("a reply without a usage block still parses", r.usage["input_tokens"] == 0 and r.text == "bare")

    print("== errors ==")
    STATUS[0] = 401
    ANSWER.clear(); ANSWER.update({"type": "error", "error": {"message": "API key is invalid."}})
    try:
        await p.complete(model="m", system="s", tools=[], messages=[{"role": "user", "content": "x"}],
                         max_tokens=1)
        check("4xx raises ModelError", False, "no error")
    except ModelError as e:
        check("4xx raises ModelError naming provider and status", str(e).startswith("anthropic 401: "),
              str(e))
    STATUS[0] = 200

    print("== usage accounting ==")
    total = empty_usage()
    add_usage(total, {"input_tokens": 1, "output_tokens": 2})
    add_usage(total, {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5, "cost_usd": 0.5})
    check("calls counted and fields summed",
          total == {"calls": 2, "input_tokens": 4, "output_tokens": 6, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 5, "cost_usd": 0.5}, str(total))
    check("no cost_usd key until a provider reports one", "cost_usd" not in empty_usage())

    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
