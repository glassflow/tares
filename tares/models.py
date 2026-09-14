"""The model provider adapter: one interface both model loops call.

The agent runner (builtin_agents.py) and Ask (agent.py) each drive a tool loop: send the
conversation and the tool definitions, get back text and tool calls, run the tools, append the
results, repeat. Which provider answers is not the loop's business. This module owns the wire
format; the loops hold the conversation in a neutral shape and read a neutral reply.

Neutral conversation, one dict per message:
- {"role": "user", "content": str}
- {"role": "assistant", "content": str, "tool_calls": [ToolCall...], "raw": <provider content>}
  (what `ModelReply.as_message()` returns; `raw` lets the provider replay its own blocks exactly)
- {"role": "tool", "results": [{"id": str, "content": str}]}
Anything else with a role and a content is passed through as the provider's own shape, so a
conversation that arrives already in that shape (the console's Ask history) still works.

Tool definitions are the neutral schema {name, description, input_schema}; a provider maps them.
Usage is one dict per call in the Anthropic field names, with `cost_usd` when a provider reports
the cost itself (routers do); the loops sum it.

Today there is one implementation, Anthropic. Adding a provider means one class here; the loops,
tracing and metering do not change (TR-299).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from . import tracing as _tracing

USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")


class ModelError(RuntimeError):
    """The provider answered with an error status. The message names the provider and status."""


class ModelUnavailable(RuntimeError):
    """The provider cannot be used at all (a missing package, no credential)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelReply:
    """One model turn. `text` joins the text parts with newlines, unstripped; `raw` is the
    provider's own assistant content, replayed verbatim on the next call."""
    text: str
    tool_calls: list[ToolCall]
    usage: dict
    model: str | None = None
    stop_reason: str | None = None
    raw: Any = None

    def as_message(self) -> dict:
        return {"role": "assistant", "content": self.text, "tool_calls": self.tool_calls,
                "raw": self.raw}


def empty_usage() -> dict:
    return {"calls": 0, **{f: 0 for f in USAGE_FIELDS}}


def add_usage(total: dict, usage: dict) -> None:
    """Sum one call's usage into a running total (the loops keep one per run or turn)."""
    total["calls"] = total.get("calls", 0) + 1
    for f in USAGE_FIELDS:
        total[f] = total.get(f, 0) + int(usage.get(f) or 0)
    if usage.get("cost_usd") is not None:
        total["cost_usd"] = float(total.get("cost_usd") or 0.0) + float(usage["cost_usd"])


def tool_message(results: list[tuple[str, str]]) -> dict:
    """The neutral message carrying tool results: (call id, output) pairs."""
    return {"role": "tool", "results": [{"id": i, "content": c} for i, c in results]}


class Provider:
    """What a loop needs from a provider. `complete` is one blocking call; `stream` yields text
    deltas as they arrive and a `ModelReply` last."""
    kind = ""

    async def complete(self, *, model: str, system: str, tools: list, messages: list,
                       max_tokens: int, tools_allowed: bool = True, tracer=None) -> ModelReply:
        raise NotImplementedError

    def stream(self, *, model: str, system: str, tools: list, messages: list,
               max_tokens: int, tracer=None) -> AsyncIterator[str | ModelReply]:
        raise NotImplementedError


# ── Anthropic ──────────────────────────────────────────────────────────────────
class AnthropicProvider(Provider):
    """The Anthropic Messages API, or any gateway that speaks it (LiteLLM, a Bedrock or Vertex
    proxy). `headers` carry the credential and the API version; `base_url` is where the calls go."""
    kind = "anthropic"

    def __init__(self, base_url: str, headers: dict, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.timeout = timeout

    # neutral -> wire
    @staticmethod
    def render(messages: list) -> list:
        out = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
                    for r in m.get("results") or []]})
            elif role == "assistant" and m.get("raw") is not None:
                out.append({"role": "assistant", "content": m["raw"]})
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                blocks += [{"type": "tool_use", "id": tc.id, "name": tc.name,
                            "input": tc.arguments} for tc in m["tool_calls"]]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role, "content": m.get("content")})
        return out

    # wire -> neutral
    @staticmethod
    def parse(msg: dict) -> ModelReply:
        content = msg.get("content") or []
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [ToolCall(id=str(b["id"]), name=str(b["name"]), arguments=b.get("input") or {})
                 for b in content if b.get("type") == "tool_use"]
        u = msg.get("usage") or {}   # defensive: a stub may answer without a usage block
        usage = {f: int(u.get(f) or 0) for f in USAGE_FIELDS}
        return ModelReply(text=text, tool_calls=calls, usage=usage, model=msg.get("model"),
                          stop_reason=msg.get("stop_reason"), raw=content)

    def _body(self, model, system, tools, messages, max_tokens, tools_allowed) -> dict:
        # The tools stay declared even when disabled (a conversation holding tool_use blocks
        # must define them); tool_choice none is what disables them.
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "tools": tools, "messages": self.render(messages)}
        if not tools_allowed:
            body["tool_choice"] = {"type": "none"}
        return body

    async def complete(self, *, model, system, tools, messages, max_tokens,
                       tools_allowed=True, tracer=None) -> ModelReply:
        body = self._body(model, system, tools, messages, max_tokens, tools_allowed)
        with _tracing.generation(tracer, model, body["messages"], {"max_tokens": max_tokens}) as gen:
            async with httpx.AsyncClient(timeout=self.timeout) as cx:
                r = await cx.post(f"{self.base_url}/v1/messages", headers=self.headers, json=body)
            if r.status_code >= 400:
                raise ModelError(f"anthropic {r.status_code}: {r.text[:300]}")
            msg = r.json()
            reply = self.parse(msg)
            gen.set_output(reply.raw)
            gen.set_usage(msg.get("usage") or {})
            gen.set_response_model(reply.model)
            gen.set_finish_reason(reply.stop_reason)
        return reply

    async def stream(self, *, model, system, tools, messages, max_tokens, tracer=None):
        try:
            import anthropic
        except ImportError:
            raise ModelUnavailable("the agent needs the 'anthropic' package "
                                   "(pip install tares[agent])")
        client = anthropic.AsyncAnthropic(base_url=self.base_url, default_headers=self.headers)
        wire = self.render(messages)
        with _tracing.generation(tracer, model, wire, {"max_tokens": max_tokens}) as gen:
            async with client.messages.stream(model=model, max_tokens=max_tokens, system=system,
                                              tools=tools, messages=wire) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        gen.record_first_token()
                        yield event.delta.text
                final = await stream.get_final_message()
            # SDK objects -> plain dicts, so the reply is provider-neutral and replays as JSON
            content = [b.model_dump(exclude_none=True) for b in final.content]
            usage = final.usage.model_dump() if final.usage is not None else {}
            reply = self.parse({"content": content, "usage": usage, "model": final.model,
                                "stop_reason": getattr(final, "stop_reason", None)})
            gen.set_output(reply.raw)
            gen.set_usage(final.usage)
            gen.set_response_model(final.model)
            gen.set_finish_reason(reply.stop_reason)
        yield reply
