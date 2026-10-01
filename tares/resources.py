"""Every part on the cell, each with the projects that use it (TR-351, the All resources page).

A part is a source, a wake-up (trigger), an agent, a tool (MCP server), a skill or a project key.
`used_by` comes from the projects' object lists (usecase_objects): the one place membership lives,
so the page shows whatever membership is, whether a part is in one project or several. A part
in no project shows an empty list; that is what the page is for, among other things.

Read-only: it reads the store and the running catalog and changes nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import goal as G
from .connectors import SPECS

KINDS = ("sources", "triggers", "agents", "tools", "skills", "keys")


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat() if isinstance(dt, datetime) else str(dt)


def _members(store) -> dict[tuple[str, str], list[dict]]:
    """(kind, name) -> the projects whose object list has it, as {id, name}, by project name."""
    out: dict[tuple[str, str], list[dict]] = {}
    for p in store.list_projects():
        if p.get("status") == "draft":     # a draft uses nothing yet
            continue
        for o in store.list_project_objects(p["id"]):
            out.setdefault((o["kind"], o["name"]), []).append({"id": p["id"], "name": p["name"]})
    for v in out.values():
        v.sort(key=lambda x: x["name"].lower())
    return out


def _with(members, kind: str, name: str, owner: str | None, projects: dict) -> list[dict]:
    """The projects using a part; the project that made it when no list names it (a part made
    before object lists recorded every kind)."""
    used = list(members.get((kind, name), []))
    if not used and owner and owner in projects:
        used = [{"id": owner, "name": projects[owner]}]
    return used


def _host(url: str | None) -> str:
    try:
        return urlsplit(str(url or "")).hostname or str(url or "")
    except ValueError:
        return str(url or "")


def list_resources(store, catalog, runtime_health: dict | None = None, now=None) -> dict:
    """{sources, triggers, agents, tools, skills, keys}: each a list of rows with `name`, `what`
    (what it is in plain words), `state` and `used_by` ([{id, name}])."""
    now = now or datetime.now(timezone.utc)
    members = _members(store)
    projects = {p["id"]: p["name"] for p in store.list_projects() if p.get("status") != "draft"}
    health = G._health_for(runtime_health, store, list(catalog.sources))

    sources = []
    for name, cfg in sorted(catalog.sources.items()):
        spec = SPECS.get(cfg.connector, {})
        if spec.get("internal"):
            continue
        st = G.source_state(store, cfg, health.get(name), now)
        title = G.source_title(cfg)
        sources.append({
            "name": name, "kind": cfg.connector, "kind_label": spec.get("label") or cfg.connector,
            "what": title if title != name else (spec.get("label") or cfg.connector),
            "state": st["state"], "detail": st["detail"], "last_event_at": st["last_event_at"],
            "used_by": _with(members, "source", name, None, projects)})

    triggers = []
    owners = {t["name"]: t.get("owned_by") for t in store.list_catalog_triggers()}
    for t in sorted(catalog.triggers, key=lambda t: t.name):
        triggers.append({
            "name": t.name, "what": G.wake_sentence(t, catalog.sources),
            "sources": list(t.sources), "state": "paused" if t.paused else "active",
            "last_fired_at": _iso(store.last_fired_any(t.name)),
            "used_by": _with(members, "trigger", t.name, owners.get(t.name), projects)})

    agents = []
    for a in sorted(store.list_catalog_agents(), key=lambda a: a["name"]):
        runs = store.list_agent_runs(a["name"], limit=1)
        last = runs[0] if runs else None
        on = store.agent_enabled(a["name"])   # some project's wiring wakes it
        model = a.get("model") or "the default model"
        agents.append({
            "name": a["name"],
            "what": f"Tares agent on {model}" + (f", woken by {a['trigger']}" if a.get("trigger") else ""),
            "state": "on" if on else "off",
            "last_run_at": _iso(last.get("started_at")) if last else None,
            "last_run_status": last.get("status") if last else None,
            "used_by": _with(members, "agent", a["name"], a.get("owned_by"), projects)})

    tools = []
    for m in sorted(store.list_mcp_servers(), key=lambda m: m["name"]):
        tools.append({
            "name": m["name"], "what": f"MCP server at {_host(m.get('url'))}", "url": m.get("url"),
            "state": "set" if (m.get("auth_value") or m.get("headers")) else "no credentials",
            "used_by": _with(members, "mcp_server", m["name"], m.get("owned_by"), projects)})

    skills = []
    for sk in store.list_all_skills():
        pid = sk.get("project")
        skills.append({
            "name": sk["name"], "what": sk.get("description") or "",
            "state": "active",
            "used_by": [{"id": pid, "name": projects[pid]}] if pid in projects else []})
    skills.sort(key=lambda s: (s["name"], s["used_by"][0]["name"] if s["used_by"] else ""))

    keys = []
    for k in store.list_api_keys():
        if k.get("revoked_at"):
            continue
        pid = k.get("project")
        keys.append({
            "name": k["name"], "what": ("Project key: reads the project and records findings"
                                        if pid else f"Cell key: {', '.join(sorted(k['scopes']))}"),
            "prefix": k.get("prefix"),
            "state": "used" if k.get("last_used_at") else "never used",
            "last_used_at": _iso(k.get("last_used_at")),
            "used_by": [{"id": pid, "name": projects[pid]}] if pid in projects else []})
    keys.sort(key=lambda k: k["name"].lower())

    return {"sources": sources, "triggers": triggers, "agents": agents, "tools": tools,
            "skills": skills, "keys": keys}
