"""tares-mcp — the stdio MCP server the agent spawns. A thin proxy to taresd's HTTP API.

Stateless: every tool call becomes one HTTP call to taresd. This is the only Tares surface the
agent sees.

Two tool groups: the READ surface (read/catalog/list_*) and the WRITE/SETUP surface
(discover_*/create_source/…), which lets an agent wire up its own data sources inside a project. The design doc keeps admin ops off the MCP surface; exposing them here is a
deliberate MVP test of agent-operable onboarding (no auth — the proxy talks to the local daemon).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp import types
from mcp.types import TextContent

import logging

from . import docs as docs_mod
from . import team as team_mod


def _factory_token() -> str:
    """The token a tares-factory station presents: `tares factory` keeps it in a file only the
    person can read and names the file in the station's settings (never the token itself)."""
    path = os.getenv("TARES_FACTORY_TOKEN_FILE", "").strip()
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return ""


# A tares-factory station names the Tares its crew uses in TARES_FACTORY_URL. It wins over
# TARESD_URL, which the plugin's MCP config sets from the plugin option (127.0.0.1:8787 unless the
# person changed it), so stations reach the same Tares as `tares factory` on any address.
TARESD = (os.getenv("TARES_FACTORY_URL", "").strip()
          or os.getenv("TARESD_URL", "http://127.0.0.1:8787")).rstrip("/")
# Bearer credentials. Over HTTP transports each caller presents its own token (the root auth token
# or a scoped API key) and we forward it per-request — the daemon enforces scopes per route. Over
# stdio (a local agent spawned the proxy) there is no inbound token; the env token is used.
AUTH_TOKEN = _factory_token() or os.getenv("TARES_AUTH_TOKEN", "").strip()
_CALLER_TOKEN = contextvars.ContextVar("tares_caller_token", default="")
# Over HTTP one server is shared by many sessions, so who calls comes from each request's
# headers (set by _CallerHeaders), never from this process's environment.
_CALLER_STATION = contextvars.ContextVar("tares_caller_station", default="")
_CALLER_ROLE = contextvars.ContextVar("tares_caller_role", default="")
_CALLER_PARENT = contextvars.ContextVar("tares_caller_parent", default="")
_CALLER_PROJECT = contextvars.ContextVar("tares_caller_project", default="")
_CALLER_CREW = contextvars.ContextVar("tares_caller_crew", default="")
_CALLER_RUNTIME = contextvars.ContextVar("tares_caller_runtime", default="")
# Set by main() for the HTTP transports. Never inferred from the environment at call time: tests
# import this module in-process and must stay on the stdio path.
_HTTP = False

_CALLER_VARS = {"token": _CALLER_TOKEN, "station": _CALLER_STATION, "role": _CALLER_ROLE,
                "parent": _CALLER_PARENT, "project": _CALLER_PROJECT, "crew": _CALLER_CREW,
                "runtime": _CALLER_RUNTIME}
_CALLER_HEADERS = {"station": "x-tares-station", "role": "x-tares-role",
                   "parent": "x-tares-parent", "project": "x-tares-project",
                   "crew": "x-tares-crew", "runtime": "x-tares-runtime"}
_CALLER_ENV = {"station": "FACTORY_STATION", "role": "FACTORY_ROLE", "parent": "FACTORY_PARENT",
               "project": "TARES_PROJECT", "crew": "FACTORY_CREW", "runtime": "FACTORY_RUNTIME"}


def _bearer(headers) -> str:
    """The token in `Authorization: Bearer <x>`, else `x-tares-token`; "" when there is none."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer"):
        return auth[6:].strip()
    return headers.get("x-tares-token", "").strip()


def _caller(name: str) -> str:
    """Who is calling: token, station, role, parent or project. Over HTTP, from the current
    request's headers (the context variable when there is no request object); over stdio, from
    the environment of the session that spawned this proxy, as before."""
    if _HTTP:
        try:
            req = mcp.get_context().request_context.request
        except Exception:
            req = None
        if req is not None:
            if name == "token":
                return _bearer(req.headers)
            return req.headers.get(_CALLER_HEADERS[name], "").strip()
        return _CALLER_VARS[name].get()
    if name == "token":
        return AUTH_TOKEN
    return os.getenv(_CALLER_ENV[name], "").strip()


def _default_project() -> str:
    """The project a call means when it names none: X-Tares-Project over HTTP, TARES_PROJECT
    (a builder station's) over stdio."""
    return _caller("project")


def _station_headers() -> dict:
    """Which tares-factory station the caller is, and the crew it belongs to (TR-633), sent with
    every call so Tares applies who may write what. Empty for a session that is not a station."""
    out = {}
    for name, header in (("station", "X-Tares-Station"), ("role", "X-Tares-Role"),
                         ("parent", "X-Tares-Parent"), ("crew", "X-Tares-Crew")):
        v = _caller(name)
        if v:
            out[header] = v
    return out


class _TaresdClient(httpx.AsyncClient):
    """Says where taresd was expected when it can't be reached, instead of httpx's bare "All
    connection attempts failed": the server starts and lists its tools without a daemon (what a
    directory scanner does), so the first tool call is where a missing daemon shows. Catches in
    send() rather than a custom transport, which would stop httpx reading proxy env vars."""

    async def send(self, request, **kwargs):
        try:
            return await super().send(request, **kwargs)
        except httpx.ConnectError as e:
            raise httpx.ConnectError(
                f"Tares isn't reachable at {TARESD} ({e}). If it runs on this machine, start it "
                "with `tares up`; otherwise check TARESD_URL.", request=request) from e


def _cx(timeout: float = 10):
    """An httpx client that carries the caller's token and station to taresd. Over HTTP the
    server's own token is never spent for a caller that sent none."""
    tok = _caller("token")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    headers.update(_station_headers())
    return _TaresdClient(timeout=timeout, headers=headers)

# stdio (default) is what a local agent spawns. For remote agents (the demo / a server), run with
# TARES_MCP_TRANSPORT=streamable-http (or sse) and the server listens on MCP_HOST:MCP_PORT — at
# /mcp (streamable-http) or /sse (sse), proxying tool calls to taresd as usual.
# What each edition's agent sees (TR-757); the crew's tools stay on everywhere. The outcome tools, listed first on a factory cell (tares/mcp_outcomes.py registers them).
# The two that read the home page need the factory home, so they stay off platform.
FACTORY_TOOLS = frozenset({"what_needs_me", "how_is_project", "start_project",
                           "hand_project_to_crew"})
FACTORY_ONLY = frozenset({"what_needs_me", "how_is_project"})
# The tools an ee team adds (Team.mcp_tools(), registered by _register_ee_tools). They show only
# while the cell's licence allows ee, for every caller, crew stations included. They are in none of
# the sets above, so the edition filter leaves them alone.
EE_TOOLS: set[str] = set()
_LICENCE_TEXT = ("licence_required: This needs an enterprise licence. "
                 "https://docs.glassflow.ai/tares/enterprise")
# what a Tares Cloud cell says instead (tares.licence.CLOUD_WORDS): no licence words, no link
_CLOUD_TEXT = "This is not available on this workspace right now."


def _off_text(name: str, edition: str) -> str:
    return f"This tool is not part of the {edition} edition of this Tares."


# A tidy tool list, not a permission boundary: the daemon checks every call with the caller's own
# key, and X-Tares-Role is a plain header any client can send. Hiding a tool only keeps the list
# short for a person; it never decides what a caller may do.
_SURFACES: dict[str, tuple[float, str]] = {}


async def _surface() -> str:
    """The edition as the daemon says for this caller's token, kept for TARES_MCP_SURFACE_TTL
    seconds (30). Any error gives "": an unknown edition shows every tool."""
    tok = _caller("token")
    now = time.monotonic()
    hit = _SURFACES.get(tok)
    try:
        ttl = float(os.getenv("TARES_MCP_SURFACE_TTL", "30"))
    except ValueError:
        ttl = 30.0
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        async with _cx(5) as cx:
            r = await cx.get(f"{TARESD}/api/whoami")
        r.raise_for_status()
        who = r.json()
        out = str(who["edition"])
    except Exception:
        return ""
    if len(_SURFACES) > 256:
        _SURFACES.clear()
    _SURFACES[tok] = (now, out)
    return out


_EE_ON: dict[str, tuple[float, bool]] = {}


async def _ee_on() -> bool:
    """Whether the licence allows ee, as the daemon says for this caller's token (`GET /api/licence`,
    `ee_allowed`), kept for TARES_MCP_SURFACE_TTL seconds. Any error gives False."""
    tok = _caller("token")
    now = time.monotonic()
    hit = _EE_ON.get(tok)
    try:
        ttl = float(os.getenv("TARES_MCP_SURFACE_TTL", "30"))
    except ValueError:
        ttl = 30.0
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        async with _cx(5) as cx:
            r = await cx.get(f"{TARESD}/api/licence")
        r.raise_for_status()
        out = bool(r.json()["ee_allowed"])
    except Exception:
        return False
    if len(_EE_ON) > 256:
        _EE_ON.clear()
    _EE_ON[tok] = (now, out)
    return out


async def _refusal_text() -> str:
    """What a hidden ee tool answers when called: the licence words on a cell known to be
    self-hosted, plain words on a Tares Cloud cell (the daemon's /health shows
    `workspace_api_url` there) and whenever the daemon does not say."""
    try:
        async with _cx(5) as cx:
            r = await cx.get(f"{TARESD}/health")
        r.raise_for_status()
        if not r.json().get("workspace_api_url"):
            return _LICENCE_TEXT
    except Exception:
        pass
    return _CLOUD_TEXT


def _visible(name: str, edition: str, station: bool, ee_on: bool = True) -> bool:
    """Whether a tool is listed (and runs) for this caller on this edition."""
    if name in EE_TOOLS and not ee_on:
        return False
    if station or edition not in ("factory", "sre", "platform"):   # unknown edition: every tool
        return True
    if name in FACTORY_ONLY:
        return edition == "factory"
    if name in FACTORY_TOOLS:
        return edition in ("factory", "platform")
    return True


class TaresMCP(FastMCP):
    """FastMCP that lists, and runs, only the tools the cell's edition shows its caller."""

    async def list_tools(self):
        tools = await super().list_tools()
        ee_on = await _ee_on() if EE_TOOLS else True
        if _caller("role"):   # a crew station sees every tool but the ee ones without a licence
            return [t for t in tools if t.name not in EE_TOOLS or ee_on]
        edition = await _surface()
        tools = [t for t in tools if _visible(t.name, edition, False, ee_on)]
        first = FACTORY_TOOLS if edition == "factory" else frozenset()
        return sorted(tools, key=lambda t: t.name not in first)   # stable: outcome tools first

    async def call_tool(self, name, arguments):
        if name in EE_TOOLS and not await _ee_on():
            return types.CallToolResult(content=[TextContent(type="text", text=await _refusal_text())])
        if not _caller("role"):
            edition = await _surface()
            if not _visible(name, edition, False):
                # A CallToolResult skips the output check the server runs on a listed tool.
                return types.CallToolResult(content=[TextContent(type="text", text=_off_text(name, edition))])
        return await super().call_tool(name, arguments)


mcp = TaresMCP("tares",
               host=os.getenv("TARES_MCP_HOST", "127.0.0.1"),
               port=int(os.getenv("TARES_MCP_PORT", "8788")))


def writable():
    """Decorator for a write/setup tool. (Kept as a distinct marker from a plain read tool; there is
    no longer a read-only mode, so it registers normally — the daemon enforces scopes per route.)"""
    return mcp.tool()


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


# ── joining a project (TR-336): an outside agent works inside one project ────
# With a project key the project is implied (the key reads that one only). With any other
# credential, or none on an open instance, `project` names it (an id or a name); it may be left
# out while there is only one project.

async def _project_id(cx, project: str = "") -> tuple[str | None, str | None]:
    """(project id, None) or (None, the reason to tell the agent). With no `project`, a builder
    station's TARES_PROJECT (X-Tares-Project over HTTP) names it."""
    project = (project or "").strip() or _default_project()
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
    # a private project you are not on comes back as a bare `hidden` row (for an admin to hand its
    # lead over in the console): to a tool it does not exist
    found = [p for p in r.json().get("projects") or [] if not p.get("hidden")]
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
                       for p in r.json().get("projects") or [] if not p.get("hidden")], default=str)


@mcp.tool()
async def project_timeline(limit: int = 20, project: str = "") -> str:
    """What happened in the project, newest first: the hand-overs pushed through without every
    area owner's sign-off."""
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


LIST_CAP = 8_000   # bytes a list tool answers with by default: a station's context fills fast


def _fingerprint(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _fit(rows: list, limit: int, offset: int, cap: int, again: str) -> tuple[list, str]:
    """`rows[offset:offset+limit]`, fewer when they pass `cap` bytes as JSON, and the sentence
    saying how to read the rest ("" when nothing is left out). `again` is the call to repeat,
    with `{offset}` where the next offset goes."""
    page = rows[offset:offset + limit] if limit > 0 else rows[offset:]
    while len(page) > 1 and len(json.dumps(page, default=str)) > cap:
        page = page[:max(1, len(page) * 3 // 4)]
    left = len(rows) - offset - len(page)
    if left <= 0:
        return page, ""
    return page, (f"{left} more of {len(rows)} left out to keep this small: "
                  + again.format(offset=offset + len(page)))


def _clip(text, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


@writable()
async def create_project(name: str, goal: str = "") -> str:
    """Create an empty Tares project for a piece of work you are about to spec, and tie this
    session to it. `name` is the project's handle (unique on Tares); `goal` is one line saying
    what it is for. Then write its docs with write_doc and its tickets (write_ticket; when they
    live in Linear or GitHub, link_tickets first).
    Returns the project's id and name."""
    body = {"kind": "software_factory", "name": name.strip(),
            **({"goal": goal.strip()} if goal.strip() else {})}
    async with _cx(15) as cx:
        r = await cx.post(f"{TARESD}/api/projects", json=body)
    return _out(r, ("id", "name", "goal"))


@mcp.tool()
async def list_people() -> str:
    """The people on this Tares: id, name, email, GitHub login and whether they are an admin."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/people")
    if r.status_code >= 400:
        return _out(r)
    return json.dumps([{k: p.get(k) for k in ("id", "name", "email", "emails", "github", "is_admin")}
                       for p in r.json().get("people") or []], default=str)


def _person_in(people: list[dict], who: str) -> dict | None:
    """The person with this id or email (main or extra, any case)."""
    who = who.strip()
    low = who.lower()
    return next((p for p in people if who == p["id"] or low in
                 [e.lower() for e in ([p.get("email")] + (p.get("emails") or [])) if e]), None)


@writable()
async def write_doc(kind: str, title: str, body: str, id: str = "", project: str = "") -> str:
    """Write a markdown doc into the project, or replace one when `id` is given. `kind` is one
    of: `start` (the prompt a session that builds the project starts with: what to read first,
    which ticket to take, how to work), `spec` (what is built and why), `plan` (milestones and
    the order of work), `agents` (the AGENTS.md of the build: conventions, commands, how to
    test), `working` (the working doc of one ticket: context, steps, files likely touched, how to
    verify; link it with attach_working_doc), `note` (anything else). Working docs and every
    other doc stay in Tares; they never go to Linear or GitHub. A rule or fact that holds for every
    project goes in the shared docs with add_to_global instead. Returns the doc's id."""
    payload = {"kind": kind, "title": title, "body": body,
               "by_default": "claude code session"}
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
async def list_docs(kind: str = "", project: str = "", limit: int = 40, offset: int = 0) -> str:
    """The project's docs in reading order (starting prompt, spec, plan, AGENTS.md, memory,
    notes): id, kind, title, size. No bodies: read one with get_doc. `kind` narrows the list.
    Working docs are left out unless `kind="working"` (a ticket's working doc comes with
    get_ticket). Docs marked `global` (the AGENTS.md and the memory for all projects) belong to
    every project: read them too, they hold the user's standing rules. At most `limit` docs;
    a last entry {"more": ...} says how to read the rest."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/docs", params={"kind": kind})
    if r.status_code >= 400:
        return _out(r)
    docs = [d for d in r.json() if kind or d.get("kind") != "working"]
    rows = [{k: d.get(k) for k in ("id", "kind", "title", "global", "size")} for d in docs]
    page, more = _fit(rows, limit, offset, LIST_CAP - 200,
                      f'list_docs(kind="{kind}", offset={{offset}})')
    return json.dumps(page + ([{"more": more}] if more else []), default=str)


DOC_MAX = 12_000      # characters of a doc get_doc answers whole by default
DOC_HEAD = 3_000      # of those, the part of its first section a long doc shows
DOC_HEADINGS = 60     # headings listed for a long doc


@mcp.tool()
async def get_doc(id: str, project: str = "", section: str = "") -> str:
    """One of the project's docs, with its markdown body. A doc over 12,000 characters comes as
    its headings (each with its size) and the start of its first section, not whole:
    `section="<heading>"` reads one section (a heading of the doc, any case, with or without
    its #), `section="all"` the whole doc. A shorter doc comes whole unless you name a section."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/docs/{id}")
    if r.status_code >= 400:
        return _out(r)
    doc = r.json()
    out = {k: doc.get(k) for k in ("id", "kind", "title", "body", "updated_at", "updated_by")}
    body, want = out["body"] or "", section.strip()
    if want.lower() == "all" or (not want and len(body) <= DOC_MAX):
        return json.dumps(out, default=str)
    secs = _doc_sections(body)
    if want:
        hits = _find_sections(secs, want)
        if not hits:
            names = [h for h, _ in secs if h]
            return f"no section {section!r} in doc {id}: it has {names}"
        return json.dumps({**out, "body": "".join(hits), "section": section}, default=str)
    heads = [{"heading": h or "(top)", "chars": len(text)} for h, text in secs]
    out["body"] = _clip(secs[0][1], DOC_HEAD) if secs else _clip(body, DOC_HEAD)
    out["sections"] = heads[:DOC_HEADINGS] + ([{"more": len(heads) - DOC_HEADINGS}]
                                              if len(heads) > DOC_HEADINGS else [])
    out["note"] = (f"{len(body)} characters, so only the start of the first section is here. "
                   f'get_doc("{id}", section="<heading>") reads one section, section="all" the whole doc.')
    return json.dumps(out, default=str)


@mcp.tool()
async def read_global_docs() -> str:
    """The docs every project shares: the AGENTS.md for all projects (the user's standing rules)
    and the memory (facts and preferences that hold across projects), as markdown, then the
    caller's own rules and memory when they have any (they add to the team's; when they conflict,
    the team rule wins). Read them at the start of any work on a project and follow them; do not
    ask the user again for what they already say."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/docs/global")
    if r.status_code >= 400:
        return _out(r)
    data = r.json()
    text = docs_mod.read_all_text(data)
    if not text:
        return "No shared docs yet: they are made with the first project that has docs."
    return text


@writable()
async def add_to_global(doc: str, line: str, scope: str = "team") -> str:
    """Add one line to a doc every project shares: `doc` is `agents` (the AGENTS.md for all
    projects: a rule every session should follow, e.g. "always build on a branch, never commit
    to main") or `memory` (a fact or preference that holds across projects, e.g. "the household
    uses euros"). Ask the user first, and add only what holds beyond this project; a project's
    own rules go in its own AGENTS.md. A line already there is not added twice. `scope` is `team`
    (the default: the docs every person shares) or `personal` (the caller's own AGENTS.md or
    memory, read only by them: a preference of theirs that is not the team's)."""
    scope = (scope or "team").strip().lower()
    if scope not in ("team", "personal"):
        return "scope is `team` or `personal`"
    area = "personal" if scope == "personal" else "global"
    async with _cx(15) as cx:
        r = await cx.post(f"{TARESD}/api/docs/{area}/{doc.strip().lower()}/lines",
                          json={"text": line, "by_default": "claude code session"})
    if r.status_code >= 400:
        return _out(r)
    d = r.json()
    out = {"doc": d["title"], "id": d["id"], "added": d["added"]}
    if d.get("pending"):    # the cell's memory service did not take it yet; a retry is queued
        out["pending"] = True
    return json.dumps(out)


def _handover(b: dict) -> str:
    """The pick-up brief as markdown a session reads top to bottom."""
    lines = []
    t = b.get("ticket")
    if t:
        head = "Ticket in progress" if b.get("in_progress") else "Next ticket"
        ident = f" ({t['identifier']})" if t.get("identifier") else ""
        lines += [f"## {head}: {t['title']}{ident}", f"id: {t['id']}", ""]
        if t.get("working_doc_body"):
            lines += ["### Working doc", t["working_doc_body"].strip(), ""]
        if t.get("assumptions"):
            lines += ["### Assumptions made on it"]
            lines += [f"- {a['label']} ({a['state']}): {a['question']} -> {a['choice']}"
                      for a in t["assumptions"]]
            lines.append("")
        if t.get("checks"):
            lines += ["### Checks run (newest last)"]
            lines += [f"- [{(c.get('commit') or '')[:7]}] `{c['command']}` -> {c['result']}"
                      for c in t["checks"][-8:]]
            lines.append("")
    if b.get("memory"):
        lines += ["## Project memory", b["memory"].strip(), ""]
    if b.get("decisions"):
        lines += ["## What the person decided (their words)"]
        lines += [f"- {d.get('label') or ''} {d['question']}: \"{d['words']}\"".strip()
                  for d in b["decisions"]]
        lines.append("")
    p = b.get("previous_session")
    if p:
        lines += ["## The session that was on it",
                  f"state: {p.get('state') or 'unknown'}"
                  + (f" ({p['reason']})" if p.get("reason") else "")
                  + (f", last active {p['quiet_minutes']} min ago" if p.get("quiet_minutes") is not None else "")]
        if p.get("said_lately"):
            lines += ["", "### What it said last"]
            lines += [f"> {m['text'].strip()}".replace("\n", "\n> ") for m in p["said_lately"]]
        if p.get("files_touched"):
            lines += ["", "### Files it touched (most recent first)"]
            lines += [f"- {f}" for f in p["files_touched"]]
        lines.append("")
    if b.get("notes"):
        lines += ["## Builders' notes"]
        for n in b["notes"]:
            lines += [f"### {n['title']}", n["body"].strip(), ""]
    lines += ["## Checklist"] + [f"{i}. {c}" for i, c in enumerate(b.get("checklist") or [], 1)]
    return "\n".join(lines)


@writable()
async def pick_up(project: str = "", take_over: bool = False) -> str:
    """Pick the build up where an earlier session stopped. Call it first when you are told to
    work on a project and a ticket is already in progress (list_tickets says so), or when told
    to pick up. Returns the hand-over: the ticket with its working doc, what the previous
    session said last and the files it touched, the builders' notes and a checklist. It takes
    the build over (the previous session is marked replaced) unless that session may still be
    open: then it says so and does nothing; ask the person, and call again with take_over=true
    only if they say so."""
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/pickup")
        if r.status_code >= 400:
            return _out(r)
        brief = r.json()
        if not brief.get("in_progress"):
            return ("Nothing to pick up: no ticket is in progress. Start the next ticket the way "
                    "the starting prompt says.\n\n" + _handover(brief))
        prev = brief.get("previous_session") or {}
        if brief.get("may_be_open") and not take_over:
            # no previous session in the hand-over: it is one this caller must not read
            seen = (f" (state {prev.get('state') or 'active'}, last active "
                    f"{prev.get('quiet_minutes')} min ago)") if prev else ""
            return (f"STOP: the session that was building may still be open{seen}. "
                    "Two sessions on one branch collide. "
                    "Ask the person whether to take over; only if they say yes, call pick_up "
                    "again with take_over=true. Read-only hand-over for context:\n\n"
                    + _handover(brief))
        r = await cx.post(f"{TARESD}/api/projects/{uid}/pickup", json={"take_over": True})
    if r.status_code >= 400:
        return _out(r)
    return "You have taken the build over.\n\n" + _handover(r.json())


@mcp.tool()
async def list_tickets(project: str = "", milestone: str = "", open: bool = False,
                       limit: int = 60, offset: int = 0, detail: str = "") -> str:
    """The project's tickets in plan order: id, label (the Linear identifier such as ENG-12, the
    GitHub identifier such as GH-12, else T<n>), title, status (todo, in_progress, done,
    canceled), milestone, depends_on, the GitHub repo it changes (`repo`, owner/name, for a
    project that spans several), whether it is ready (all it depends on is done) or which tickets
    block it, who it is for (`assignee_note`: "assignee: Kiran" or "unowned (kiran@x.com is not on
    this cell)"). Work the ready tickets in this order. Also says where they live: `linear` is
    the Linear project and `github` the repo and issue label, each with when it was last synced
    (null when the project does not use it); with both null the tickets live in Tares. Read one
    with get_ticket to get its working doc.
    Small by default: `milestone` (a name) and `open=True` (not done or canceled) narrow it, at
    most `limit` tickets come back and within about 8,000 bytes (`more` says how to read the
    rest with `offset`), and each ticket leaves out its link when they share one pattern
    (`ticket_url`, with `{label}`), its working doc and its assignee object. `detail="full"`
    gives every ticket with every field, as before."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/tickets")
    if r.status_code >= 400:
        return _out(r)
    data = r.json()
    lin = data.get("linear")
    gh = data.get("github")
    every = data.get("tickets") or []
    tickets = [t for t in every if (not milestone or t.get("milestone_name") == milestone)
               and (not open or t.get("status") not in ("done", "canceled"))]
    full = detail == "full"
    keys = (("id", "label", "number", "identifier", "title", "status", "milestone_name",
             "depends_on", "ready", "blocked_by", "holder", "stage", "stage_reason", "repo", "url",
             "working_doc", "working_doc_title", "assignee", "assignee_raw") if full else
            ("id", "label", "number", "title", "status", "milestone_name", "depends_on", "ready",
             "blocked_by", "holder", "stage", "stage_reason", "repo"))
    pattern = "" if full else _url_pattern(tickets)
    if not full and not pattern:
        keys += ("url",)     # links of their own: keep them
    rows = [{k: t.get(k) for k in keys}
            | {"pr": _pr_brief(t.get("pr"))}
            | ({"pr_note": t.get("pr_note")} if full or t.get("pr_note") else {})
            | {"assignee_note": _assignee_note(t)}
            for t in tickets]
    out = {
        "linear": ({k: lin.get(k) for k in ("name", "url", "synced_at", "error")} if lin else None),
        "github": ({k: gh.get(k) for k in ("repo", "issue_label", "url", "synced_at", "error")}
                   if gh else None)}
    if full:
        out["tickets"] = rows
    else:
        page, more = _fit(rows, limit, offset, LIST_CAP - 1_000,
                          f'list_tickets(milestone="{milestone}", open={open}, offset={{offset}})')
        out["tickets"] = page
        if pattern:
            out["ticket_url"] = pattern
        if more:
            out["more"] = more
    if any(t.get("status") == "in_progress" for t in every):
        out["note"] = ("A ticket is already in progress: unless you are the session working on "
                       "it, call pick_up before anything else.")
    return json.dumps(out, default=str)


def _url_pattern(tickets: list[dict]) -> str:
    """"https://linear.app/acme/issue/{label}" when every ticket's link is that prefix plus its
    label, so a compact list can leave the links out; "" otherwise."""
    urls = [(t.get("url") or "", t.get("label") or "") for t in tickets]
    if not urls or not all(u and lab and u.endswith("/" + lab) for u, lab in urls):
        return ""
    prefixes = {u[:-len(lab)] for u, lab in urls}
    return prefixes.pop() + "{label}" if len(prefixes) == 1 else ""


def _assignee_note(t: dict) -> str:
    """"assignee: Kiran", or "unowned (kiran@x.com is not on this cell)", or "unowned"."""
    a = t.get("assignee")
    if a:
        return f"assignee: {a['name']}"
    raw = t.get("assignee_raw")
    return f"unowned ({raw} is not on this cell)" if raw else "unowned"


def _pr_brief(pr: dict | None) -> dict | None:
    if not pr:
        return None
    return {k: pr.get(k) for k in ("url", "head", "ci", "verdict", "merged", "state", "error")
            if pr.get(k) not in (None, "")}


@mcp.tool()
async def get_ticket(ticket: str, project: str = "", detail: str = "", section: str = "",
                     since: str = "") -> str:
    """One ticket, by its Tares id, its number (T3), its Linear identifier (for example
    ENG-12) or its GitHub identifier (for example GH-12), with the body of its working doc, who
    holds it and where it stands, who it is for (`assignee`, or `assignee_raw` when that person is
    not on this cell), its pull request as GitHub has it (head, CI, the reviewer's verdict, merged), its history, and the
    crew's messages about it: everything you need to work on it.
    Small by default: a working doc over 4,000 characters comes as its Goal, Steps and Progress
    with `working_doc_sections` (every heading and its size; `section="<heading>"` reads one,
    `section="all"` the whole doc), the latest 8 messages and checks and the first 5 of its
    decisions (this ticket's first; `messages_total`, `checks_total` count them all,
    `decisions_total` the decisions the ticket carries; list_decisions has the rest), and reviews with only their open findings in words. `detail="status"` is only where it stands
    (stage, holder, PR, verdict), for a tick; `detail="full"` is everything, uncut. Each answer
    carries a `fingerprint`: pass it back as `since` and an unchanged ticket answers
    {"unchanged": true} alone."""
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/tickets/{ticket}")
        if r.status_code >= 400:
            return _out(r)
        out = r.json()
        out["assignee_note"] = _assignee_note(out)
        msgs = await cx.get(f"{TARESD}/api/projects/{uid}/tickets/{out['id']}/messages")
    if msgs.status_code == 200:
        out["messages"] = [{k: m.get(k) for k in ("at", "from", "to", "type", "first_line")}
                           for m in msgs.json().get("messages") or []]
    if section:
        return _doc_section(out, section)
    if detail != "full":
        out = _ticket_status(out) if detail == "status" else _ticket_gist(out)
    pr = out.get("pr")    # when GitHub was last read is no change to the ticket
    fp = _fingerprint({**out, "pr": {k: v for k, v in pr.items() if k != "checked_at"}}
                      if isinstance(pr, dict) else out)
    if since and since == fp:
        return json.dumps({"unchanged": True, "fingerprint": fp})
    return json.dumps({**out, "fingerprint": fp}, default=str)


TICKET_DOC_MAX = 4_000            # characters of a working doc get_ticket gives whole
TICKET_DOC_KEEP = ("goal", "steps", "progress")
TICKET_MESSAGES = 8               # the latest messages, checks and decisions get_ticket gives
TICKET_CHECKS = 8
TICKET_DECISIONS = 5


def _finding(f) -> dict | str:
    """An open finding as its gist; a fixed or waived one as its label alone."""
    if not isinstance(f, dict):
        return f
    if f.get("state") not in (None, "", "open"):
        return f"{f.get('label')} {f.get('state')}"
    return {"label": f.get("label"), "severity": f.get("severity"), "blocking": f.get("blocking"),
            "where": f"{f.get('file') or ''}:{f.get('line') or ''}".strip(":"),
            "text": _clip(f.get("text"), 200)}


def _doc_sections(body: str) -> list[tuple[str, str]]:
    """The doc as [(heading, text)], split at its level-1 and level-2 headings; text before the
    first heading goes under "". A # line inside a ``` fence is code, not a heading."""
    out, head, start, pos, fenced = [], "", 0, 0, False
    for line in body.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            m = re.match(r"#{1,2} +(.+?)\s*$", line)
            if m:
                if pos > start or head:
                    out.append((head, body[start:pos]))
                head, start = m.group(1).strip(), pos
        pos += len(line)
    out.append((head, body[start:]))
    return [(h, t) for h, t in out if h or t.strip()]


def _find_sections(secs: list[tuple[str, str]], section: str) -> list[str]:
    want = section.strip().lstrip("#").strip().lower()
    return [text for h, text in secs if h.lower() == want]


def _doc_section(t: dict, section: str) -> str:
    body = t.get("working_doc_body") or ""
    if section.strip().lower() == "all":
        return json.dumps({"label": t.get("label"), "working_doc_body": body})
    hits = _find_sections(_doc_sections(body), section)
    if not hits:
        names = [h for h, _ in _doc_sections(body) if h]
        return f"no section {section!r} in the working doc of {t.get('label')}: it has {names}"
    return json.dumps({"label": t.get("label"), "section": section, "body": "".join(hits)})


def _ticket_status(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "label", "title", "status", "stage", "stage_reason",
                                  "holder", "milestone_name", "depends_on", "ready", "blocked_by",
                                  "verdict", "updated_at", "assignee_note")} | {
        "pr": _pr_brief(t.get("pr"))}


def _ticket_gist(t: dict) -> dict:
    out = dict(t)
    body = t.get("working_doc_body") or ""
    if len(body) > TICKET_DOC_MAX:
        secs = _doc_sections(body)
        keep = [text.split("\n### Checks")[0] if h.lower() == "progress" else text
                for h, text in secs if h.lower() in TICKET_DOC_KEEP]
        out["working_doc_body"] = "".join(keep)
        out["working_doc_sections"] = [{"heading": h or "(top)", "chars": len(text),
                                        "shown": h.lower() in TICKET_DOC_KEEP} for h, text in secs]
    msgs = t.get("messages") or []
    out["messages"] = [{**m, "first_line": _clip(m.get("first_line"), 120)}
                       for m in msgs[-TICKET_MESSAGES:]]
    out["messages_total"] = len(msgs)
    out["reviews"] = [{"round": v.get("round"), "verdict": v.get("verdict"),
                       "head": str(v.get("head") or "")[:12], "at": v.get("at"), "by": v.get("by"),
                       "findings": [_finding(f) for f in v.get("findings") or []],
                       "fixed": v.get("fixed")}
                      for v in t.get("reviews") or []]
    out["checks"] = [{k: v for k, v in {
        "command": _clip(c.get("command"), 120), "result": _clip(c.get("result"), 100),
        "match": c.get("match"), "commit": str(c.get("commit") or "")[:12], "at": c.get("at"),
        "by": c.get("by"), "recorded": c.get("recorded"), "broke_test": c.get("broke_test")}.items()
        if v not in (None, "")} for c in (t.get("checks") or [])[-TICKET_CHECKS:]]
    out["checks_total"] = len(t.get("checks") or [])
    out["decisions"] = [{"label": d.get("label"), "question": _clip(d.get("question"), 160),
                         "words": d.get("words"), "choice": _clip(d.get("choice"), 160),
                         "standing": d.get("standing"), "at": d.get("at")}
                        for d in (t.get("decisions") or [])[:TICKET_DECISIONS]]
    out["decisions_total"] = len(t.get("decisions") or [])
    out["history"] = [{k: h.get(k) for k in ("at", "field", "value", "reason", "by")}
                      for h in t.get("history") or []]
    return out


@writable()
async def write_ticket(title: str, status: str = "", position: float | None = None,
                       milestone: str | None = None, depends_on: list[str] | None = None,
                       id: str = "", project: str = "", repo: str | None = None,
                       assignee: str | None = None) -> str:
    """Add a ticket to the project's list, or change one when `id` is given (its id or T<n>).
    When the project's tickets live in Linear or GitHub (link_tickets), a new ticket is created
    there, one per call: write the milestones first, then the tickets in dependency order. A repeat
    of the same title finds the issue again instead of making a second. `depends_on` is resolved
    to the tickets already written; `milestone` must be one already written. Its `status`,
    `assignee` and `position` come from Linear or GitHub, so naming them on a new ticket is
    refused. An existing Linear or GitHub ticket can be retitled, moved to a milestone and, for
    Linear, reordered here (`position`); its status, assignee and dependencies change there, and
    attach_working_doc links its working doc. For a project whose tickets live in Tares: `status`
    is todo, in_progress, done or canceled; `position` orders the list (a new ticket goes to the
    end); `milestone` is the name of a milestone written with write_milestone ("" takes it out of
    its milestone); `depends_on` lists the tickets (T<n> or id) that must be done first ([] clears
    them); `assignee` is the person it is for (a person id or an email; an email nobody on the cell
    has is kept as written and the ticket shows as unowned; "" clears it), and assigning someone
    to a ticket of a private project adds them to it; naming an assignee needs an enterprise
    licence, without one leave it out. `repo` is the GitHub repo the ticket changes (owner/name),
    for a project that spans several repos ("" clears it). Returns the ticket with its label
    (T<n>)."""
    body: dict = {"title": title}
    if repo is not None:
        body["repo"] = repo
    if assignee is not None:
        body["assignee"] = assignee
    if status:
        body["status"] = status
    if position is not None:
        body["position"] = position
    if milestone is not None:
        body["milestone"] = milestone
    if depends_on is not None:
        body["depends_on"] = depends_on
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
    return _out(r, ("id", "label", "title", "status", "position", "milestone_name",
                    "depends_on", "repo", "assignee", "assignee_raw"))


@writable()
async def attach_working_doc(ticket: str, doc: str, project: str = "") -> str:
    """Link a working doc (write it first with write_doc, kind `working`) to a ticket, by the
    ticket's Tares id, Linear identifier or GitHub identifier (GH-12). Works for tickets in
    Linear or GitHub too: the working doc stays in Tares."""
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.put(f"{TARESD}/api/projects/{uid}/tickets/{ticket}",
                         json={"working_doc": doc})
    return _out(r, ("id", "identifier", "title", "working_doc"))


@writable()
async def link_tickets(integration: str, link_fields: dict | None = None,
                       project: str = "") -> str:
    """Tie the project to where its tickets live, `integration` "linear" or "github" (Jira is
    not covered). `link_fields` holds only non-secret fields: for Linear `project`, the Linear
    project's id, URL or name; for GitHub `repo` (owner/name or URL) and `issue_label` (default
    `tares:<project name>`). The cell's own Linear or GitHub connection is used, so never pass a
    key or a token (it is refused). Tares then keeps its ticket list in sync (about once a
    minute) and reads it once now. After it, write_milestone and write_ticket create the
    milestones and tickets in Linear or GitHub. Returns the link and the tickets found."""
    async with _cx(60) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.put(f"{TARESD}/api/projects/{uid}/tickets/link",
                         json={"integration": integration, "link_fields": link_fields or {}})
    if r.status_code >= 400:
        return _out(r)
    data = r.json()
    keys = ("name", "url", "synced_at", "error") if integration == "linear" else \
        ("repo", "issue_label", "url", "synced_at", "error")
    return json.dumps({integration: {k: (data.get(integration) or {}).get(k) for k in keys},
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
        if scope["type"] == "http" and not _CALLER_TOKEN.get():
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


class _CallerHeaders:
    """Pure-ASGI middleware, outermost: reads who is calling (bearer, station, role, parent,
    project) from each HTTP request's headers into context variables, so many Claude Code
    sessions can share one server and Tares still knows each one. Runs whether or not the
    server has a token of its own."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            _CALLER_TOKEN.set(_bearer(headers))
            for name, header in _CALLER_HEADERS.items():
                _CALLER_VARS[name].set(headers.get(header, "").strip())
        await self.app(scope, receive, send)


from . import mcp_factory  # noqa: E402,F401  (registers the factory's tools on `mcp`)
from . import mcp_outcomes  # noqa: E402,F401  (the outcome tools of the factory edition)


def _register_ee_tools() -> None:
    """Register the tools of the ee team (if any) on `mcp` and name them in EE_TOOLS. A failure is
    logged once and takes back what was registered; a tool that cannot be taken back stays named in
    EE_TOOLS, so the licence still hides it. A name that is already a core tool is skipped."""
    EE_TOOLS.clear()
    ee = team_mod._ee()
    if ee is None:
        return
    log = logging.getLogger("tares.mcp")
    added: list[str] = []
    try:
        taken = {t.name for t in mcp._tool_manager.list_tools()}
        for fn in ee.mcp_tools():
            if fn.__name__ in taken:
                log.warning("ee tool %s skipped: a core tool has that name", fn.__name__)
                continue
            mcp.tool()(fn)
            added.append(fn.__name__)
            EE_TOOLS.add(fn.__name__)
    except Exception as e:
        log.warning("ee tools did not register: %s: %s", type(e).__name__, e)
        for name in added:
            try:
                mcp.remove_tool(name)
            except Exception:
                continue   # still registered: it stays in EE_TOOLS and is hidden without a licence
            EE_TOOLS.discard(name)


_register_ee_tools()


def main():
    global _HTTP
    transport = os.getenv("TARES_MCP_TRANSPORT", "stdio")  # stdio | sse | streamable-http
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    # HTTP transports: identity per request, gated by the bearer token when the server has one,
    # run under uvicorn.
    _HTTP = True
    # Stateless: no session is kept between requests. Every Claude Code process opens a session
    # and none closes it, so a stateful server grows until its container is killed, and every
    # connected client then sees "connection closed". Who is calling comes from each request's
    # headers, so nothing here needs a session. Set here, because the FastMCP constructor's
    # default wins over FASTMCP_STATELESS_HTTP.
    mcp.settings.stateless_http = True
    app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    app = _CallerHeaders(_BearerGate(app) if AUTH_TOKEN else app)
    import uvicorn
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)


if __name__ == "__main__":
    main()
