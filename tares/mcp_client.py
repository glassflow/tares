"""Client side of MCP — the daemon reaching OUT to external tool servers.

Not to be confused with tares/mcp_server.py (Tares serving its own surface). This module is what
Tares agents use to reach the customer's other tools: connect over streamable HTTP, present the
stored auth header, list tools, call one. stdio is deliberately unsupported: it would mean the
daemon spawning arbitrary commands, which is unacceptable on a hosted cell and a footgun anywhere.
"""
from __future__ import annotations

import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

CONNECT_TIMEOUT = 15.0
# One tool result may not eat the run's context: the loop answers in 2048 tokens and the model
# still has to hold the timeline, so a verbose server gets truncated, not obeyed.
RESULT_CAP = 8_000


def _headers(server: dict) -> dict:
    """The auth header plus any extra headers the registration carries (toolset selection,
    read-only mode). The auth header wins on a name clash: it is the secret."""
    out = {str(k): str(v) for k, v in (server.get("headers") or {}).items() if str(k).strip()}
    header = (server.get("auth_header") or "").strip() or "Authorization"
    value = (server.get("auth_value") or "").strip()
    if value:
        out[header] = value
    return out


async def resolve_servers(store, servers: list[dict]) -> list[dict]:
    """Copies of the server rows with `auth_value: credential:github/<name>` replaced by
    `Bearer <token>` from the stored credential, resolved now so a rotated token (or, for a GitHub
    App, an installation token with at least 20 minutes left) is used on the
    next connect. An unknown credential leaves the value empty and the connect fails with a
    named reason (a down or unauthenticated server is skipped by the run, not fatal)."""
    from .github_credentials import get_token, is_credential_ref
    out = []
    for s in servers:
        s = dict(s)
        if is_credential_ref(s.get("auth_value")):
            try:
                # the header stays fixed for the whole run: a token with 20 minutes left at least
                s["auth_value"] = f"Bearer {await get_token(store, s['auth_value'], min_life=1200)}"
            except ValueError as e:
                s["_auth_error"] = str(e)
                s["auth_value"] = ""
        out.append(s)
    return out


async def list_remote_tools(server: dict) -> list[dict]:
    """Connect, initialize, and list the server's tools. Raises on failure — the caller decides
    whether that is a 502 (a test button) or evidence (an agent run)."""
    if server.get("_auth_error"):
        raise ValueError(server["_auth_error"])
    async with streamablehttp_client(server["url"], headers=_headers(server),
                                     timeout=CONNECT_TIMEOUT) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_tools()
            return [{"name": t.name, "description": (t.description or "").strip()}
                    for t in res.tools]


def _flatten(result) -> str:
    """One string from a call_tool result: text blocks joined, anything else JSON-dumped."""
    parts = []
    for c in result.content or []:
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
        else:
            try:
                parts.append(json.dumps(c.model_dump(), default=str))
            except Exception:
                parts.append(repr(c))
    out = "\n".join(parts).strip()
    if len(out) > RESULT_CAP:
        out = out[:RESULT_CAP] + f"\n… truncated at {RESULT_CAP} characters"
    if getattr(result, "isError", False):
        out = f"tool error from server: {out or 'no detail'}"
    return out or "(empty result)"


class RemoteToolbox:
    """The external tools an agent's selected MCP servers offer, held open for one run.

    Tool names are prefixed `server__tool` so they can never collide with the built-in reads (or
    with each other across servers). A server that fails to connect is skipped and remembered in
    `failures` — the run continues on what remains, because a down tool server is evidence, not a
    reason to lose the finding.

    Each connection lives inside its OWN task (`_worker`): the mcp client stacks anyio cancel
    scopes that must be entered and exited by the same task, so sharing sessions across tasks (or
    holding them in one exit stack) detonates on unwind. Calls are passed to the owning worker
    over a queue and answered on a future.
    """

    def __init__(self, servers: list[dict]):
        self._servers = servers
        self._tasks: list = []
        self._queues: dict[str, object] = {}      # server name -> request queue
        self._route: dict[str, tuple[str, str]] = {}   # prefixed -> (server, tool)
        self.tool_defs: list[dict] = []
        self.failures: list[str] = []   # "name: reason", for the run log

    async def _worker(self, server: dict, ready, requests) -> None:
        """Owns the connection end to end. Resolves `ready` with the tool list (or the connect
        error), then serves call requests until it is handed None."""
        try:
            if server.get("_auth_error"):
                raise ValueError(server["_auth_error"])
            async with streamablehttp_client(server["url"], headers=_headers(server),
                                             timeout=CONNECT_TIMEOUT) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.list_tools()
                    ready.set_result(res.tools)
                    while True:
                        item = await requests.get()
                        if item is None:
                            return
                        tool, args, fut = item
                        try:
                            fut.set_result(await session.call_tool(tool, args or {}))
                        except Exception as e:
                            fut.set_exception(e)
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)

    async def __aenter__(self) -> "RemoteToolbox":
        import asyncio
        pending = []
        for server in self._servers:
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            requests: asyncio.Queue = asyncio.Queue()
            self._tasks.append(asyncio.create_task(self._worker(server, ready, requests)))
            self._queues[server["name"]] = requests
            pending.append((server, ready))
        for server, ready in pending:
            try:
                tools = await asyncio.wait_for(ready, timeout=CONNECT_TIMEOUT + 5)
            except Exception as e:
                detail = f"{type(e).__name__}: {str(e)[:120]}" if str(e).strip() else type(e).__name__
                self.failures.append(f"{server['name']}: {detail}")
                continue
            for t in tools:
                prefixed = f"{server['name']}__{t.name}"
                self._route[prefixed] = (server["name"], t.name)
                self.tool_defs.append({
                    "name": prefixed,
                    "description": f"[{server['name']}] {(t.description or '').strip()}"[:1024],
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                })
        return self

    async def __aexit__(self, *exc) -> None:
        import asyncio
        for q in self._queues.values():
            q.put_nowait(None)
        for task in self._tasks:
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()

    def owns(self, name: str) -> bool:
        return name in self._route

    async def call(self, name: str, args: dict, timeout: float = 60.0) -> str:
        import asyncio
        server, tool = self._route[name]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queues[server].put_nowait((tool, args, fut))
        result = await asyncio.wait_for(fut, timeout=timeout)
        return _flatten(result)
