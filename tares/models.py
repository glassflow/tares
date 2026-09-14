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

Two implementations: Anthropic (the Messages API, or a gateway speaking it) and OpenAI (chat
completions, which is also what LiteLLM, OpenRouter, Ollama and vLLM speak). Adding a provider
means one class here; the loops, tracing and metering do not change (TR-299, TR-300).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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
        with _tracing.generation(tracer, model, body["messages"], {"max_tokens": max_tokens},
                                 provider=self.kind) as gen:
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
        with _tracing.generation(tracer, model, wire, {"max_tokens": max_tokens},
                                 provider=self.kind) as gen:
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


# ── OpenAI chat completions ────────────────────────────────────────────────────
class OpenAIProvider(Provider):
    """The OpenAI chat-completions API, or any server that speaks it: LiteLLM, OpenRouter, Ollama,
    vLLM, Azure. `base_url` includes the version segment (`https://api.openai.com/v1`,
    `http://localhost:11434/v1`); `headers` carry the bearer token and anything a router wants
    (an OpenRouter app header, say). `label` names the provider in error messages."""
    kind = "openai"

    def __init__(self, base_url: str, headers: dict, timeout: float = 120, label: str = "openai"):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.timeout = timeout
        self.label = label

    # neutral -> wire
    @staticmethod
    def render_tools(tools: list) -> list:
        return [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools]

    @staticmethod
    def render(system: str, messages: list) -> list:
        out: list = [{"role": "system", "content": system}] if system else []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]}
                        for r in m.get("results") or []]
            elif role == "assistant" and m.get("raw") is not None:
                out.append(m["raw"])
            elif role == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or None,
                            "tool_calls": [{"id": tc.id, "type": "function", "function": {
                                "name": tc.name, "arguments": json.dumps(tc.arguments)}}
                                for tc in m["tool_calls"]]})
            else:
                content = m.get("content")
                if isinstance(content, list):
                    # Anthropic-shaped history (a conversation that started on the other
                    # provider): keep the text, the rest has no meaning here.
                    content = "\n".join(b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text")
                out.append({"role": role, "content": content})
        return out

    # wire -> neutral
    @staticmethod
    def _arguments(raw: Any) -> dict:
        if isinstance(raw, dict):
            return raw
        try:
            val = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {"arguments": raw}
        return val if isinstance(val, dict) else {"arguments": val}

    @classmethod
    def parse(cls, resp: dict, cost_header: str | None = None) -> ModelReply:
        choice = (resp.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):   # some servers answer with content parts
            text = "\n".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls = [ToolCall(id=str(tc.get("id") or f"call_{i}"),
                          name=str((tc.get("function") or {}).get("name") or ""),
                          arguments=cls._arguments((tc.get("function") or {}).get("arguments")))
                 for i, tc in enumerate(message.get("tool_calls") or [])]
        usage = cls._usage(resp.get("usage") or {}, cost_header)
        raw = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            raw["tool_calls"] = message["tool_calls"]
        return ModelReply(text=text, tool_calls=calls, usage=usage, model=resp.get("model"),
                          stop_reason=choice.get("finish_reason"), raw=raw)

    @staticmethod
    def _usage(u: dict, cost_header: str | None = None) -> dict:
        # OpenAI's prompt_tokens INCLUDE the cached part; Anthropic's input_tokens exclude cache
        # reads. Both are carried as reported, the pricing layer knows which is which (TR-304).
        details = u.get("prompt_tokens_details") or {}
        usage = {"input_tokens": int(u.get("prompt_tokens") or 0),
                 "output_tokens": int(u.get("completion_tokens") or 0),
                 "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": int(details.get("cached_tokens") or 0)}
        # A router that prices the call reports it: OpenRouter in `usage.cost`, LiteLLM in a
        # response header. That figure wins over any table (TR-304).
        cost = u.get("cost")
        if cost is None and cost_header not in (None, ""):
            try:
                cost = float(cost_header)
            except ValueError:
                cost = None
        if cost is not None:
            usage["cost_usd"] = float(cost)
        return usage

    def _body(self, model, system, tools, messages, max_tokens, tools_allowed, stream) -> dict:
        body = {"model": model, "max_tokens": max_tokens,
                "messages": self.render(system, messages)}
        if tools:
            body["tools"] = self.render_tools(tools)
            if not tools_allowed:
                body["tool_choice"] = "none"
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    def _trace_output(self, reply: ModelReply) -> list:
        # the tracer reads content blocks in the Anthropic shape (tracing._part)
        blocks: list = [{"type": "text", "text": reply.text}] if reply.text else []
        blocks += [{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                   for tc in reply.tool_calls]
        return blocks

    async def complete(self, *, model, system, tools, messages, max_tokens,
                       tools_allowed=True, tracer=None) -> ModelReply:
        body = self._body(model, system, tools, messages, max_tokens, tools_allowed, False)
        with _tracing.generation(tracer, model, body["messages"], {"max_tokens": max_tokens},
                                 provider=self.kind) as gen:
            async with httpx.AsyncClient(timeout=self.timeout) as cx:
                r = await cx.post(f"{self.base_url}/chat/completions", headers=self.headers,
                                  json=body)
            if r.status_code >= 400:
                raise ModelError(f"{self.label} {r.status_code}: {r.text[:300]}")
            reply = self.parse(r.json(), r.headers.get("x-litellm-response-cost"))
            gen.set_output(self._trace_output(reply))
            gen.set_usage(reply.usage)
            gen.set_response_model(reply.model)
            gen.set_finish_reason(reply.stop_reason)
        return reply

    async def stream(self, *, model, system, tools, messages, max_tokens, tracer=None):
        body = self._body(model, system, tools, messages, max_tokens, True, True)
        with _tracing.generation(tracer, model, body["messages"], {"max_tokens": max_tokens},
                                 provider=self.kind) as gen:
            text_parts: list[str] = []
            calls: dict[int, dict] = {}       # index -> {id, name, arguments (str so far)}
            finish = resp_model = None
            usage_block: dict = {}
            cost_header = None
            async with httpx.AsyncClient(timeout=self.timeout) as cx:
                async with cx.stream("POST", f"{self.base_url}/chat/completions",
                                     headers=self.headers, json=body) as r:
                    if r.status_code >= 400:
                        detail = (await r.aread()).decode(errors="replace")[:300]
                        raise ModelError(f"{self.label} {r.status_code}: {detail}")
                    cost_header = r.headers.get("x-litellm-response-cost")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        resp_model = chunk.get("model") or resp_model
                        if chunk.get("usage"):
                            usage_block = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                gen.record_first_token()
                                text_parts.append(piece)
                                yield piece
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(int(tc.get("index") or 0),
                                                        {"id": "", "name": "", "arguments": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                            if choice.get("finish_reason"):
                                finish = choice["finish_reason"]
            tool_calls = [{"id": c["id"] or f"call_{i}", "type": "function",
                           "function": {"name": c["name"], "arguments": c["arguments"]}}
                          for i, c in sorted(calls.items())]
            assembled = {"choices": [{"finish_reason": finish, "message": {
                "role": "assistant", "content": "".join(text_parts) or None,
                **({"tool_calls": tool_calls} if tool_calls else {})}}],
                "usage": usage_block, "model": resp_model}
            reply = self.parse(assembled, cost_header)
            gen.set_output(self._trace_output(reply))
            gen.set_usage(reply.usage)
            gen.set_response_model(reply.model)
            gen.set_finish_reason(reply.stop_reason)
        yield reply
