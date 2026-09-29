"""tares-mcp — the stdio MCP server the agent spawns. A thin proxy to taresd's HTTP API.

Stateless: every tool call becomes one HTTP call to taresd. This is the only Tares surface the
agent sees.

Two tool groups: the READ surface (read/catalog/list_*) and the WRITE/SETUP surface
(subscribe/remember/discover_*/create_source/create_trigger/…), which lets an agent wire up its own
data sources and the triggers over them, inside a project. The design doc keeps admin ops off the MCP surface; exposing them here is a
deliberate MVP test of agent-operable onboarding (no auth — the proxy talks to the local daemon).
"""
from __future__ import annotations

import contextvars
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

TARESD = os.getenv("TARESD_URL", "http://127.0.0.1:8787")
# Bearer credentials. Over HTTP transports each caller presents its own token (the root auth token
# or a scoped API key) and we forward it per-request — the daemon enforces scopes per route. Over
# stdio (a local agent spawned the proxy) there is no inbound token; the env token is used.
AUTH_TOKEN = os.getenv("TARES_AUTH_TOKEN", "").strip()
_CALLER_TOKEN = contextvars.ContextVar("tares_caller_token", default="")


def _cx(timeout: float = 10):
    """An httpx client that carries the caller's token to taresd (env token as stdio fallback)."""
    tok = _CALLER_TOKEN.get() or AUTH_TOKEN
    return httpx.AsyncClient(timeout=timeout,
                             headers={"Authorization": f"Bearer {tok}"} if tok else {})

# stdio (default) is what a local agent spawns. For remote agents (the demo / a server), run with
# TARES_MCP_TRANSPORT=streamable-http (or sse) and the server listens on MCP_HOST:MCP_PORT — at
# /mcp (streamable-http) or /sse (sse), proxying tool calls to taresd as usual.
mcp = FastMCP("tares",
              host=os.getenv("TARES_MCP_HOST", "127.0.0.1"),
              port=int(os.getenv("TARES_MCP_PORT", "8788")))


def writable():
    """Decorator for a write/setup tool. (Kept as a distinct marker from a plain read tool; there is
    no longer a read-only mode, so it registers normally — the daemon enforces scopes per route.)"""
    return mcp.tool()


@mcp.tool()
async def read(selector: dict, window: str = "15m", include_payload: bool = False,
               project: str = "", sources: list[str] | None = None) -> str:
    """Read one correlated, time-ordered timeline of everything matching `selector` across all
    sources, or only a `project`'s sources, or only the `sources` you name. `selector` is a
    {label: value} conjunction, matched with strict AND, e.g. {"repo": "frontend"} or
    {"service": "api-server", "endpoint": "/login"}. An event matches only if it carries every
    named label with that value, so adding a label narrows and removing one widens. Use this to
    investigate any entity on the fly; once you know which sources matter, create_trigger() over
    them. Set `include_payload` when the one-line summaries aren't enough and you need each
    event's full lossless record: the return becomes JSON {timeline, events[]} where each event
    carries `raw`."""
    body = {"selector": selector, "window": window, "client": "mcp",
            "include_payload": include_payload}
    if project:
        body["project"] = project
    if sources:
        body["sources"] = sources
    async with _cx(30) as cx:
        r = await cx.post(f"{TARESD}/read", json=body)
    data = r.json()
    if r.status_code >= 400:
        return r.text
    if include_payload:
        return json.dumps({"timeline": data["payload"], "events": data["rows"]}, default=str)
    return data["payload"]


def _trigger_body(name: str, sources: list[str], condition: dict, filters, key_field: str,
                  emit, cooldown: str, project: str) -> dict:
    body = {"name": name, "sources": sources, "filters": filters or [],
            "key_field": key_field, "condition": condition, "emit": emit or {},
            "cooldown": cooldown}
    if project:
        body["project"] = project
    return body


@writable()
async def create_trigger(name: str, sources: list[str], condition: dict,
                         filters: list[dict] | None = None, key_field: str = "",
                         emit: dict | None = None, cooldown: str = "5m",
                         project: str = "") -> str:
    """Create a trigger: a condition Tares evaluates continuously over one or more sources; when
    it trips, subscribed agents are woken with the correlated timeline. `sources` names the
    sources it watches (at least one). `filters` [{field, op, value}] (ops: eq, neq, contains, gt,
    lt, gte, lte) narrows them. `key_field` is the entity label (default: the primary label of the
    first source). `condition` is {aggregate: any|avg|count|max|min|sum, field: numeric label to
    aggregate (omit for count), predicate: e.g. '> 1.0' / '>= 5' / '== 0', window: detection
    window e.g. '1m'}. `emit` is {kind: what a firing is called e.g. error_spike, context_window:
    timeline the woken agent receives e.g. '15m'}. `project` is the project id the trigger belongs
    to (default: the default project); its sources join that project. catalog_describe a source
    to confirm its labels first. Wire agents to it with subscribe()."""
    body = _trigger_body(name, sources, condition, filters, key_field, emit, cooldown, project)
    async with _cx(10) as cx:
        r = await cx.post(f"{TARESD}/api/triggers", json=body)
    return r.text


@writable()
async def update_trigger(name: str, sources: list[str], condition: dict,
                         filters: list[dict] | None = None, key_field: str = "",
                         emit: dict | None = None, cooldown: str = "5m",
                         project: str = "") -> str:
    """Edit an EXISTING trigger in place: replace its sources, filters, key_field, condition,
    emit and cooldown, and move it to another `project` when given. Create new triggers with
    create_trigger(); this only updates one that already exists (renaming isn't supported, so
    keep `name` the same). See create_trigger for the shapes."""
    body = _trigger_body(name, sources, condition, filters, key_field, emit, cooldown, project)
    async with _cx(10) as cx:
        r = await cx.put(f"{TARESD}/api/triggers/{name}", json=body)
    return r.text


@writable()
async def subscribe(trigger: str, url: str) -> str:
    """Register a webhook to be woken (pushed) when a trigger fires. Returns a subscription id."""
    async with _cx(10) as cx:
        r = await cx.post(f"{TARESD}/subscribe", json={"trigger": trigger, "url": url})
    return r.json()["subscription_id"]


@mcp.tool()
async def catalog_list() -> str:
    """List available sources, triggers and projects."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/catalog")
    return r.text


@mcp.tool()
async def catalog_describe(handle: str) -> str:
    """Describe one catalog entry in full: schema (event types + typed fields, inferred from
    stored events), freshness, lineage, and sample records. Handles look like source:logs or
    trigger:error_spike. Use this to discover label names before creating a trigger or reading
    an unfamiliar source."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/catalog/{handle}")
    return r.text


@writable()
async def remember(key: str, content: str, memory_type: str = "observation") -> str:
    """Write an observation back to Tares's agent-memory lane. It becomes a source like any
    other: joined into correlated reads, so what you learned this incident appears in the
    timeline next time the same key acts up. memory_type: observation | aggregation | decision."""
    async with _cx(10) as cx:
        r = await cx.post(f"{TARESD}/remember",
                          json={"key": key, "content": content, "memory_type": memory_type})
    return r.text


@mcp.tool()
async def set_session_flow(flow: str = "challenger") -> str:
    """Mark this Claude Code session as running a named flow, e.g. "challenger" (a second model
    challenges your plan and every commit; Tares keeps the whole exchange and writes the session
    summary). Pass an empty string to clear it. Call it when the user asks for the flow at the
    start of a session. This tool only acknowledges: the Tares plugin sees the call in the
    transcript and marks the session on both the laptop and Tares."""
    flow = (flow or "").strip()
    if flow:
        return f"noted: the Tares plugin will mark this session as a {flow} session"
    return "noted: the Tares plugin will clear this session's flow"


# ── setup surface: an agent can wire up its own data sources ─────────────────

@mcp.tool()
async def list_connectors() -> str:
    """List the connector types you can create a source with, each with its config fields, mode
    (poll | push), and whether it supports discovery. Read this first to learn what you can ingest
    and how to configure it."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/connectors")
    return r.text


@writable()
async def discover_source(connector: str, config: dict) -> str:
    """Introspect an upstream and get a proposed source config — no commitment. Works for
    connectors that can introspect: e.g. prometheus given {"url": "http://..."}, or github given
    {"repo": "owner/name"}. The returned `proposed_config` can be passed straight to create_source.
    (Use list_connectors to see which connectors support discovery.)"""
    async with _cx(20) as cx:
        r = await cx.post(f"{TARESD}/api/sources/discover",
                          json={"connector": connector, "config": config})
    return r.text


@writable()
async def discover_docker() -> str:
    """Scan the local Docker environment and get a list of proposed sources (one per container's
    logs, plus a detected Prometheus). Each item's `config` is ready to pass to create_source."""
    async with _cx(20) as cx:
        r = await cx.get(f"{TARESD}/api/discover/environment", params={"provider": "docker"})
    return r.text


@writable()
async def test_source(name: str, connector: str, config: dict, poll: str = "5s") -> str:
    """Dry-run a source config without creating it: poll connectors do one live poll and return a
    sample; push connectors return their ingest endpoint. Use before create_source to check the
    config works."""
    async with _cx(20) as cx:
        r = await cx.post(f"{TARESD}/api/sources/test",
                          json={"name": name, "connector": connector, "poll": poll, "config": config})
    return r.text


@writable()
async def create_source(name: str, connector: str, config: dict, poll: str = "5s",
                        project: str = "") -> str:
    """Create a data source so Tares starts ingesting it immediately (no restart). `config` is
    the connector's config (see list_connectors / discover_source); push connectors ignore `poll`.
    `project` is the project id the source joins (default: the default project); other projects
    can use it too. Returns {ok, name} or an error detail. After creating, use list_sources to
    watch it ingest and catalog_describe("source:<name>") to see its labels."""
    body = {"name": name, "connector": connector, "poll": poll, "config": config}
    if project:
        body["project"] = project
    async with _cx(15) as cx:
        r = await cx.post(f"{TARESD}/api/sources", json=body)
    return r.text


@mcp.tool()
async def list_sources() -> str:
    """List every configured source with its live health (status, events ingested, last error).
    Use it to confirm a source you created is actually ingesting."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/sources")
    return r.text


@mcp.tool()
async def source_fields(name: str, limit: int = 500) -> str:
    """Profile a source's real fields from recent events: every candidate field with its coverage,
    distinct count, and top values, plus which are already declared as labels/keys. Call this BEFORE
    choosing labels for a source — a label's `field` MUST be one of these exact field names (never
    invent one). The `labels` block echoes the source's current declared axes."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/sources/{name}/fields", params={"limit": limit})
    return r.text


class _BearerGate:
    """Pure-ASGI middleware: every HTTP request to the MCP server must carry *a* bearer token —
    the root auth token or a scoped API key. The token is forwarded to taresd per-request, which
    validates it and enforces scopes per tool call (the proxy can't check key hashes itself).
    Non-HTTP scopes (lifespan/websocket) pass through untouched."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            auth = headers.get("authorization", "")
            tok = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-tares-token", "")
            if not tok:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})
                return
            _CALLER_TOKEN.set(tok)
        await self.app(scope, receive, send)


def main():
    transport = os.getenv("TARES_MCP_TRANSPORT", "stdio")  # stdio | sse | streamable-http
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    # HTTP transports: build the ASGI app, gate it with the bearer token, run it under uvicorn.
    app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    if AUTH_TOKEN:
        app = _BearerGate(app)
    import uvicorn
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)


if __name__ == "__main__":
    main()
