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
                          position: float | None = None, id: str = "", project: str = "") -> str:
    """Write one of the project's milestones, or change it when one with this `name` exists
    (names are unique). Each milestone ships something a person can check: `goal` says what in
    one line, `checks` are its acceptance checks, [{"check": "<command or short manual step>",
    "expect": "<what it should show>"}], that the releaser runs when the milestone ships.
    `position` orders them (a new one goes last). To rename one, pass its current name or id as
    `id` and the new `name`. Give tickets a milestone with write_ticket(milestone=<name>). When
    the project's tickets live in Linear, its milestones come from Linear: add and rename them
    there and write only the goal and checks here."""
    body: dict = {"name": name, "goal": goal, "checks": checks or []}
    if position is not None:
        body["position"] = position
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        if id:
            cur = await cx.get(f"{TARESD}/api/projects/{uid}/milestones")
            m = next((x for x in (cur.json().get("milestones") or []) if cur.status_code == 200
                      and id in (x["id"], x["name"])), None)
            if m and m["name"] == name:
                body.pop("name")   # not a rename (a Linear milestone refuses one)
            r = await cx.put(f"{TARESD}/api/projects/{uid}/milestones/{id}", json=body)
        else:
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


# ── the crew (TR-419, TR-420, TR-425) ─────────────────────────────────────────

@mcp.tool()
async def get_crew_settings() -> str:
    """The tares-factory crew's settings for every project: `autonomy` (L3-review: the person
    approves each merge; L4-ship: a builder merges after the reviewer's pass and green CI;
    L5-dark: the releaser may deploy to prod after verifying), `release_profile`, `prod_pattern`
    (what counts as prod), `challenger` (builders run the Codex challenger), `builders_max`.
    Unset ones are null: ask the person, one at a time, with a recommendation."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/crew/settings")
    return _out(r, ("autonomy", "release_profile", "prod_pattern", "challenger", "builders_max"))


@writable()
async def set_crew_settings(autonomy: str = "", release_profile: str | None = None,
                            prod_pattern: str | None = None, challenger: bool | None = None,
                            builders_max: int | None = None) -> str:
    """Save the crew's settings the person chose (only the ones given change). Only the person's
    own session or the orchestrator may."""
    body = {"autonomy": autonomy or None, "release_profile": release_profile,
            "prod_pattern": prod_pattern, "challenger": challenger, "builders_max": builders_max}
    async with _cx(10) as cx:
        r = await cx.put(f"{TARESD}/api/crew/settings", json=body)
    return _out(r, ("autonomy", "release_profile", "prod_pattern", "challenger", "builders_max"))


@mcp.tool()
async def read_grants(project: str = "") -> str:
    """What the person allows the crew to do, in their own words with the date: the grants for
    every project, and the project's own when `project` is given. Never act on a permission
    that is not here or in the person's own words in your session."""
    params = {"project": project} if project else {}
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/grants", params=params)
    if r.status_code >= 400:
        return _out(r)
    d = r.json()
    parts = [d.get("all") or "No grants for every project yet."]
    if project:
        parts.append(d.get("project") or f"No grants of {project}'s own yet.")
    return "\n\n".join(p.rstrip() for p in parts)


@writable()
async def add_grant(words: str, scope: str = "all", project: str = "") -> str:
    """Keep one grant: `words` are the person's own words, quoted exactly as they said them
    (never your summary); Tares adds today's date. `scope` is `all` (every project) or
    `project` (this project only, name it in `project`). Only the person's own session or the
    orchestrator, relaying the person verbatim, may add one."""
    async with _cx(10) as cx:
        r = await cx.post(f"{TARESD}/api/grants",
                          json={"words": words, "scope": scope, "project": project,
                                "by": "claude code session"})
    return _out(r, ("line", "added"))


def _station_line(s: dict) -> dict:
    return {k: s.get(k) for k in ("name", "role", "parent", "state", "state_reason",
                                  "quiet_minutes", "project_name", "session", "last_at",
                                  "children")} | {"earlier_sessions": len(s.get("earlier") or [])}


@mcp.tool()
async def list_crew() -> str:
    """The crew as Tares sees it: one row per station (orchestrator, reviewer, releaser,
    builders, helpers) with its state (working, waiting, quiet, ended), the project it is on,
    when it last spoke, the stations it started, and how many earlier sessions played it."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/crew")
    if r.status_code >= 400:
        return _out(r)
    return json.dumps([_station_line(s) for s in r.json().get("stations") or []], default=str)


@mcp.tool()
async def get_station(name: str) -> str:
    """One station with its current session and the earlier sessions that played it."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/crew/{name}")
    return _out(r)


@writable()
async def hand_over(project: str = "", repo: str = "") -> str:
    """Hand a specced project to the crew: Tares records it (the project page says when). Then
    send `crew-orchestrator` a `[TF:SHIP]` message with SendMessage naming the project and the
    repo path. If no crew is running (list_crew shows no orchestrator working or waiting), tell
    the person to run `factory crew up` first."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/handover",
                          json={"repo": repo, "by": "claude code session"})
        if r.status_code >= 400:
            return _out(r)
        crew = await cx.get(f"{TARESD}/api/crew")
    orch = [s for s in (crew.json().get("stations") or []) if s.get("role") == "orchestrator"
            and s.get("state") in ("working", "waiting")] if crew.status_code == 200 else []
    if not orch:
        return ("Recorded as handed to the crew, but no orchestrator is running: tell the person "
                "to run `factory crew up` in a terminal, then send the [TF:SHIP] message.")
    return (f"Recorded. Now send {orch[0]['name']} a message with SendMessage:\n"
            f"[TF:SHIP] {project or uid} is ready to build\nproject: {project or uid}\n"
            f"repo: {repo or '(the repo named in its AGENTS.md)'}")
