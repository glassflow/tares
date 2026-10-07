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
    carries `raw`. With a project key the read covers that project only, its findings
    included."""
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
                  emit, cooldown: str, project: str, description: str | None = None) -> dict:
    body = {"name": name, "sources": sources, "filters": filters or [],
            "key_field": key_field, "condition": condition, "emit": emit or {},
            "cooldown": cooldown}
    if project:
        body["project"] = project
    if description is not None:
        body["description"] = description
    return body


@writable()
async def create_trigger(name: str, sources: list[str], condition: dict,
                         filters: list[dict] | None = None, key_field: str = "",
                         emit: dict | None = None, cooldown: str = "5m",
                         project: str = "", description: str | None = None) -> str:
    """Create a trigger: a condition Tares evaluates continuously over one or more sources; when
    it trips, subscribed agents are woken with the correlated timeline. `sources` names the
    sources it watches (at least one). `filters` [{field, op, value}] (ops: eq, neq, in (a list), contains, gt,
    lt, gte, lte) narrows them. `key_field` is the entity label (default: the primary label of the
    first source). `condition` is {aggregate: any|avg|count|max|min|sum, field: numeric label to
    aggregate (omit for count), predicate: e.g. '> 1.0' / '>= 5' / '== 0', window: detection
    window e.g. '1m'}. `emit` is {kind: what a firing is called e.g. error_spike, context_window:
    timeline the woken agent receives e.g. '15m'}. `project` is the project id the trigger belongs
    to (default: the default project); its sources join that project. `description` (optional)
    says in one plain line what wakes it, phrased to follow "When", e.g. "an alert fires in the
    checkout service"; the project page uses it instead of the rule's own wording (at most 160
    characters, one line). catalog_describe a source to confirm its labels first. Wire agents
    to it with subscribe()."""
    body = _trigger_body(name, sources, condition, filters, key_field, emit, cooldown, project,
                         description)
    async with _cx(10) as cx:
        r = await cx.post(f"{TARESD}/api/triggers", json=body)
    return r.text


@writable()
async def update_trigger(name: str, sources: list[str], condition: dict,
                         filters: list[dict] | None = None, key_field: str = "",
                         emit: dict | None = None, cooldown: str = "5m",
                         project: str = "", description: str | None = None) -> str:
    """Edit an EXISTING trigger in place: replace its sources, filters, key_field, condition,
    emit and cooldown, and move it to another `project` when given. Create new triggers with
    create_trigger(); this only updates one that already exists (renaming isn't supported, so
    keep `name` the same). `description` left out keeps the current one, "" clears it. See
    create_trigger for the shapes."""
    body = _trigger_body(name, sources, condition, filters, key_field, emit, cooldown, project,
                         description)
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


# ── joining a project (TR-336): an outside agent works inside one project ────
# With a project key the project is implied (the key reads that one only). With any other
# credential, or none on an open instance, `project` names it (an id or a name); it may be left
# out while there is only one project.

async def _project_id(cx, project: str = "") -> tuple[str | None, str | None]:
    """(project id, None) or (None, the reason to tell the agent)."""
    project = (project or "").strip()
    r = await cx.get(f"{TARESD}/api/whoami")
    who = r.json() if r.status_code == 200 else {}
    if who.get("project"):
        if project and project not in (who["project"], who.get("project_name")):
            return None, (f"this key reads project {who.get('project_name') or who['project']} "
                          "only; leave project out")
        return who["project"], None
    r = await cx.get(f"{TARESD}/api/projects")
    if r.status_code >= 400:
        return None, r.text
    found = r.json().get("projects") or []
    if project:
        hit = next((p for p in found if project in (p["id"], p["name"])), None)
        if hit is None:
            return None, f"no project {project!r}; list_projects shows the ones there are"
        return hit["id"], None
    if len(found) == 1:
        return found[0]["id"], None
    return None, ("name the project, one of: " + ", ".join(p["name"] for p in found)
                  if found else "there is no project yet")


@mcp.tool()
async def list_projects() -> str:
    """List the projects you can work in: id, name, template and status. With a project key it is
    the one project the key belongs to."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/projects")
    if r.status_code >= 400:
        return r.text
    return json.dumps([{k: p.get(k) for k in ("id", "name", "template", "status")}
                       for p in r.json().get("projects") or []], default=str)


@mcp.tool()
async def join_project(url: str, project: str = "") -> str:
    """Join a project: Tares POSTs every firing of every trigger in the project to `url` (your
    agent's webhook), the triggers added later included. The body carries dispatch_id, trigger,
    project, key (the entity), fired_at, payload (the timeline) and run_ids (the Tares agents it
    woke). Returns the subscription id."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/subscribe", json={"url": url})
    return r.text


@mcp.tool()
async def stats(by: str, window: str = "30m", where: dict | None = None, top: int = 20,
                sources: list[str] | None = None, project: str = "") -> str:
    """Counts per value of the label `by` over the project's sources, the last `window` against
    the window before, largest change first. `where` {label: value} narrows the events counted,
    `sources` narrows the sources. Use it to see what moved before reading one entity."""
    body = {"by": by, "window": window, "top": top}
    if where:
        body["where"] = where
    if sources:
        body["sources"] = sources
    async with _cx(30) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/stats", json=body)
    return r.json()["stats"] if r.status_code == 200 else r.text


@mcp.tool()
async def list_skills(project: str = "") -> str:
    """List the project's skills: name and description. Load one with get_skill before relying
    on it, only when your task matches its description."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/skills")
    if r.status_code >= 400:
        return r.text
    return json.dumps([{"name": s["name"], "description": s["description"]} for s in r.json()])


@mcp.tool()
async def get_skill(name: str, project: str = "") -> str:
    """The instructions of one of the project's skills, as markdown."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/skills/{name}")
    return r.json()["body"] if r.status_code == 200 else r.text


@mcp.tool()
async def list_findings(entity: str = "", agent: str = "", limit: int = 20,
                        project: str = "") -> str:
    """The findings recorded in the project, newest first: its Tares agents' and the ones
    external agents recorded. `entity` and `agent` narrow them. Read them before concluding, so
    you build on what is known instead of repeating it."""
    params = {"limit": limit, **({"entity": entity} if entity else {}),
              **({"agent": agent} if agent else {})}
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/findings", params=params)
    return json.dumps(r.json()["findings"], default=str) if r.status_code == 200 else r.text


@mcp.tool()
async def record_finding(entity: str, finding: str, verdict: str = "", label: str = "",
                         project: str = "", headline: str = "", next_step: str = "") -> str:
    """Record what you concluded about an entity (a service, a repo, a customer) in the project.
    It lands on the entity's timeline next to the Tares agents' findings and in the project's
    activity, under your key's name. `finding` is markdown; `verdict` one lowercase word (for
    example rca or resolved); `label` the label the entity is a value of when it is not the
    project's usual one (for example service). `headline` is one line (at most 100 characters)
    saying what you found, and `next_step` what a person should do (empty when nothing needs
    doing): both show on the project's Overview."""
    body = {"entity": entity, "finding": finding}
    if headline:
        body["headline"] = headline
    if next_step:
        body["next_step"] = next_step
    if verdict:
        body["verdict"] = verdict
    if label:
        body["label"] = label
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/findings", json=body)
    return r.text


@mcp.tool()
async def project_timeline(limit: int = 20, project: str = "") -> str:
    """What happened in the project, newest first: each firing with the runs it woke and their
    findings, and the findings recorded without a firing (external agents' among them)."""
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/timeline", params={"limit": limit})
    return r.text


# ── a project's docs and tickets (TR-403, TR-404): a spec session writes the spec, the plan, the
# AGENTS.md, the starting prompt and a working doc per ticket; the session that builds the project
# reads them back. Tickets live in Linear when the project uses it (Tares keeps a synced list),
# otherwise in Tares. ─────────────────────────────────────────────────────────────────────────

def _out(r: httpx.Response, keys: tuple[str, ...] | None = None) -> str:
    if r.status_code >= 400:
        try:
            return f"error {r.status_code}: {r.json().get('detail', r.text)}"
        except ValueError:
            return f"error {r.status_code}: {r.text}"
    data = r.json()
    if keys:
        data = {k: data.get(k) for k in keys}
    return json.dumps(data, default=str)


@writable()
async def create_project(name: str, goal: str = "") -> str:
    """Create an empty Tares project for a piece of work you are about to spec, and tie this
    session to it. `name` is the project's handle (unique on Tares); `goal` is one line saying
    what it is for. Then write its docs with write_doc and its tickets (write_ticket, or
    link_linear_project when the tickets live in Linear). Returns the project's id and name."""
    body = {"template": "custom", "kind": "software_factory", "name": name.strip(), "objects": [],
            **({"goal": goal.strip()} if goal.strip() else {})}
    async with _cx(15) as cx:
        r = await cx.post(f"{TARESD}/api/projects", json=body)
    return _out(r, ("id", "name", "goal"))


@writable()
async def write_doc(kind: str, title: str, body: str, id: str = "", project: str = "") -> str:
    """Write a markdown doc into the project, or replace one when `id` is given. `kind` is one
    of: `start` (the prompt a session that builds the project starts with: what to read first,
    which ticket to take, how to work), `spec` (what is built and why), `plan` (milestones and
    the order of work), `agents` (the AGENTS.md of the build: conventions, commands, how to
    test), `working` (the working doc of one ticket: context, steps, files likely touched, how to
    verify; link it with attach_working_doc), `note` (anything else). Working docs and every
    other doc stay in Tares; they never go to Linear. A rule or fact that holds for every
    project goes in the shared docs with add_to_global instead. Returns the doc's id."""
    payload = {"kind": kind, "title": title, "body": body, "by": "claude code session"}
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        if id:
            r = await cx.put(f"{TARESD}/api/projects/{uid}/docs/{id}", json=payload)
        else:
            r = await cx.post(f"{TARESD}/api/projects/{uid}/docs", json=payload)
    return _out(r, ("id", "kind", "title", "updated_at"))


@mcp.tool()
async def list_docs(kind: str = "", project: str = "") -> str:
    """The project's docs in reading order (starting prompt, spec, plan, AGENTS.md, memory,
    notes, working docs): id, kind, title, size, when changed. No bodies: read one with get_doc.
    `kind` narrows the list. Docs marked `global` (the AGENTS.md and the memory for all
    projects) belong to every project: read them too, they hold the user's standing rules."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/docs", params={"kind": kind})
    if r.status_code >= 400:
        return _out(r)
    return json.dumps([{k: d.get(k) for k in ("id", "kind", "title", "global", "size",
                                               "updated_at")}
                       for d in r.json()], default=str)


@mcp.tool()
async def get_doc(id: str, project: str = "") -> str:
    """One of the project's docs, with its markdown body."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/docs/{id}")
    return _out(r, ("id", "kind", "title", "body", "updated_at", "updated_by"))


@mcp.tool()
async def read_global_docs() -> str:
    """The docs every project shares: the AGENTS.md for all projects (the user's standing rules)
    and the memory (facts and preferences that hold across projects), as markdown. Read them at
    the start of any work on a project and follow them; do not ask the user again for what they
    already say."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/docs/global")
    if r.status_code >= 400:
        return _out(r)
    data = r.json()
    if not any(data.values()):
        return "No shared docs yet: they are made with the first project that has docs."
    return "\n\n".join(d["body"].rstrip() for d in data.values() if d)


@writable()
async def add_to_global(doc: str, line: str) -> str:
    """Add one line to a doc every project shares: `doc` is `agents` (the AGENTS.md for all
    projects: a rule every session should follow, e.g. "always build on a branch, never commit
    to main") or `memory` (a fact or preference that holds across projects, e.g. "the household
    uses euros"). Ask the user first, and add only what holds beyond this project; a project's
    own rules go in its own AGENTS.md. A line already there is not added twice."""
    async with _cx(15) as cx:
        r = await cx.post(f"{TARESD}/api/docs/global/{doc.strip().lower()}/lines",
                          json={"text": line, "by": "claude code session"})
    if r.status_code >= 400:
        return _out(r)
    d = r.json()
    return json.dumps({"doc": d["title"], "id": d["id"], "added": d["added"]})


@mcp.tool()
async def list_tickets(project: str = "") -> str:
    """The project's tickets in plan order: id, title, status (todo, in_progress, done,
    canceled), the Linear identifier and link when they live in Linear, and the id and title of
    each one's working doc. Also says which Linear project they live in, if any, and when it was
    last synced. Read one with get_ticket to get its working doc."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/tickets")
    if r.status_code >= 400:
        return _out(r)
    data = r.json()
    lin = data.get("linear")
    return json.dumps({
        "linear": ({k: lin.get(k) for k in ("name", "url", "synced_at", "error")} if lin else None),
        "tickets": [{k: t.get(k) for k in ("id", "identifier", "title", "status", "url",
                                            "working_doc", "working_doc_title")}
                    for t in data.get("tickets") or []]}, default=str)


@mcp.tool()
async def get_ticket(ticket: str, project: str = "") -> str:
    """One ticket, by its Tares id or its Linear identifier (for example ENG-12), with the body
    of its working doc: everything you need to work on it."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/tickets/{ticket}")
    return _out(r)


@writable()
async def write_ticket(title: str, status: str = "", position: float | None = None,
                       id: str = "", project: str = "") -> str:
    """Add a ticket to the project's list, or change one when `id` is given. Only for a project
    whose tickets live in Tares: when they live in Linear, create and change them in Linear and
    Tares picks them up on its next sync. `status` is todo, in_progress, done or canceled;
    `position` orders the list (a new ticket goes to the end). Returns the ticket."""
    body: dict = {"title": title}
    if status:
        body["status"] = status
    if position is not None:
        body["position"] = position
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        if id:
            if not title:
                body.pop("title")
            r = await cx.put(f"{TARESD}/api/projects/{uid}/tickets/{id}", json=body)
        else:
            r = await cx.post(f"{TARESD}/api/projects/{uid}/tickets", json=body)
    return _out(r, ("id", "title", "status", "position"))


@writable()
async def attach_working_doc(ticket: str, doc: str, project: str = "") -> str:
    """Link a working doc (write it first with write_doc, kind `working`) to a ticket, by the
    ticket's Tares id or Linear identifier. Works for tickets in Linear too: the working doc
    stays in Tares."""
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.put(f"{TARESD}/api/projects/{uid}/tickets/{ticket}",
                         json={"working_doc": doc})
    return _out(r, ("id", "identifier", "title", "working_doc"))


@writable()
async def link_linear_project(linear_project: str, project: str = "") -> str:
    """Tie the project to the Linear project its tickets live in, by the Linear project's id,
    URL or name. Tares then keeps its ticket list in sync with Linear (about once a minute) and
    runs a first sync now. Needs Linear connected on Tares (Settings). Returns the tickets
    found."""
    async with _cx(60) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/linear",
                          json={"project": linear_project})
    if r.status_code >= 400:
        return _out(r)
    data = r.json()
    return json.dumps({"linear": {k: (data.get("linear") or {}).get(k)
                                  for k in ("name", "url", "synced_at", "error")},
                       "tickets": [{k: t.get(k) for k in ("identifier", "title", "status")}
                                   for t in data.get("tickets") or []]}, default=str)


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
