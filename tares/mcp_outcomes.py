"""The outcome tools of the Software Factory edition (TR-757): what needs me, how a project is
doing, start a project, hand it to the crew. Each is one call to the same API the home page
reads. Registered on the same server as tares/mcp_server.py, which imports this module at its
end; the edition filter there lists them first on a factory cell.
"""
from __future__ import annotations

import json

from . import mcp_factory
from .mcp_server import TARESD, _cx, create_project, mcp, writable


@mcp.tool()
async def what_needs_me() -> str:
    """What is waiting for the person right now: the same list the home page shows under
    "Waiting for you", oldest first with the projects ready to close last, each with what it is, which project it belongs to and how
    long it has waited. Start here when the person asks what to do next or what is stuck."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/home")
    if r.status_code >= 400:
        return f"error {r.status_code}: {r.text}"
    home = r.json()
    return json.dumps({"headline": home.get("headline"),
                       "waiting_for_you": home.get("waiting_for_you") or []}, default=str)


@mcp.tool()
async def how_is_project(project: str) -> str:
    """How one project is doing, as its row on the home page shows it: where it stands, the
    milestone it is on, tickets merged and blocked out of all, builders working, repos and when
    it last moved. `project` is its name or id."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/home")
    if r.status_code >= 400:
        return f"error {r.status_code}: {r.text}"
    rows = r.json().get("projects") or []
    hit = next((p for p in rows if project.strip() in (p.get("id"), p.get("name"))), None)
    if hit is None:
        names = ", ".join(p["name"] for p in rows)
        return f"No project called {project}. Projects: {names}." if names else \
            f"No project called {project}. There are no open projects."
    return json.dumps(hit, default=str)


@writable()
async def start_project(name: str, goal: str = "") -> str:
    """Start a new project for a piece of work the person wants built. `name` is its short name,
    unique on Tares; `goal` is one line saying what it is for. It starts empty: spec it next
    (the spec skill), then hand it to the crew with hand_project_to_crew. Returns the project's
    id and name."""
    return await create_project(name, goal)


@writable()
async def hand_project_to_crew(project: str, repos: list[dict] | None = None,
                               autonomy: str = "") -> str:
    """Hand a planned project to the crew to build. `project` is its name or id. `repos` is one
    {path, name} per repo the project changes: `path` the folder it is checked out in on this
    machine, `name` its GitHub repo (owner/name), the repo that holds the plan first. Pass
    `autonomy` only when the person wants this project stricter than the crew, for example
    L3-review, where every merge waits for their yes. Says whether a crew is running and what to
    send it next."""
    return await mcp_factory.hand_over(project=project, repos=repos, autonomy=autonomy)
