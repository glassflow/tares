"""MCP tools for the software factory on Tares (P-TR-176, M2 onward): the plan's milestones, and
later the crew, the ledger, the build record, reviews and the desk. Registered on the same server
as tares/mcp_server.py, which imports this module at its end.
"""
from __future__ import annotations

import json

from .mcp_server import TARESD, _cx, _out, _project_id, mcp, writable


# ── the plan (TR-411) ─────────────────────────────────────────────────────────

@writable()
async def write_milestone(name: str, goal: str = "", checks: list[dict] | None = None,
                          position: float | None = None, project: str = "") -> str:
    """Write one of the project's milestones, or change it when one with this `name` exists.
    Each milestone ships something a person can check: `goal` says what in one line, `checks`
    are its acceptance checks, [{"check": "<command or short manual step>", "expect": "<what it
    should show>"}], that the releaser runs when the milestone ships. `position` orders them (a
    new one goes last). Give tickets a milestone with write_ticket(milestone=<name>). When the
    project's tickets live in Linear, its milestones come from Linear: add them there and write
    only the goal and checks here."""
    body: dict = {"name": name, "goal": goal, "checks": checks or []}
    if position is not None:
        body["position"] = position
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/milestones", json=body)
    return _out(r, ("id", "name", "goal", "checks", "tickets"))


@mcp.tool()
async def list_milestones(project: str = "") -> str:
    """The project's milestones in order: name, goal, acceptance checks, how many tickets and
    how many are done, which are ready, and whether the milestone is finished. Work one
    milestone at a time, in this order."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/milestones")
    if r.status_code >= 400:
        return _out(r)
    return json.dumps([{k: m.get(k) for k in ("name", "goal", "checks", "tickets", "done",
                                              "ready", "finished")}
                       for m in r.json().get("milestones") or []], default=str)
