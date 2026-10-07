"""MCP tools for the software factory on Tares (P-TR-176, M2 onward): the plan's milestones, and
later the crew, the ledger, the build record, reviews and the desk. Registered on the same server
as tares/mcp_server.py, which imports this module at its end.
"""
from __future__ import annotations

import json

from .mcp_server import TARESD, _cx, _out, _project_id, mcp, writable


# ── the plan (TR-411) ─────────────────────────────────────────────────────────

@writable()
async def write_milestone(name: str, goal: str | None = None, checks: list[dict] | None = None,
                          position: float | None = None, id: str = "", project: str = "") -> str:
    """Write one of the project's milestones, or change it when one with this `name` exists
    (names are unique). Each milestone ships something a person can check: `goal` says what in
    one line, `checks` are its acceptance checks, [{"check": "<command or short manual step>",
    "expect": "<what it should show>"}], that the releaser runs when the milestone ships.
    `position` orders them (a new one goes last). To rename one, pass its current name or id as
    `id` and the new `name`. Give tickets a milestone with write_ticket(milestone=<name>). When
    the project's tickets live in Linear, its milestones come from Linear: add and rename them
    there and write only the goal and checks here."""
    body: dict = {"name": name}
    if goal is not None or not id:
        body["goal"] = goal or ""        # a rename keeps the goal and checks it was not given
    if checks is not None or not id:
        body["checks"] = checks or []
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


# ── the ledger (TR-427, TR-428, TR-410) ───────────────────────────────────────

@writable()
async def assign_ticket(ticket: str, holder: str, project: str = "") -> str:
    """Give a ticket (T3, its id, or its Linear identifier) to the station that works it, by the
    station's name (for example shop-build-m1). Only the orchestrator, or the person's own
    session, assigns. A ticket with no stage yet starts at todo."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/tickets/{ticket}/assign",
                          json={"holder": holder})
    return _out(r, ("label", "title", "holder", "stage"))


@writable()
async def set_stage(ticket: str, stage: str, reason: str = "", pr: str = "",
                    project: str = "") -> str:
    """Move a ticket you hold: `stage` is doing (you started), review (its PR is up: pass `pr`,
    the pull request's URL), changes (the reviewer asked for changes), merged, or blocked (say
    why in `reason`). Only the releaser marks a ticket shipped. A ticket held by another station
    is refused; ask the orchestrator. For a ticket that lives in Linear, also move it in Linear
    with Linear's own tools. Name your branch factory/<ticket> (factory/T3) so Tares finds the
    pull request on its own too."""
    body = {"stage": stage, "reason": reason}
    if pr:
        body["pr"] = pr
    async with _cx(20) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/tickets/{ticket}/stage", json=body)
    if r.status_code >= 400:
        return _out(r)
    t = r.json()
    out = {k: t.get(k) for k in ("label", "title", "holder", "stage", "stage_reason", "status")}
    if t.get("pr"):
        out["pr"] = {k: t["pr"].get(k) for k in ("url", "head", "ci", "verdict", "merged", "error")}
    return json.dumps(out, default=str)


# ── the build, recorded (TR-433, TR-435, TR-436) ──────────────────────────────

@writable()
async def assume(question: str, choice: str, why: str, tickets: list[str] | None = None,
                 affects: str = "", project: str = "") -> str:
    """Record an assumption you made so the work does not block: the `question` the plan did not
    answer, the `choice` you made, `why`, the `tickets` it touches (T3, ...), and who it
    `affects`. Returns its number (A1, A2, ...): tell the orchestrator that number in one line and
    carry on. The orchestrator confirms open assumptions with the person once per milestone.
    Never use this for what must go to the person directly (prod deploys, secrets, money, new
    contacts, deleting data)."""
    body = {"question": question, "choice": choice, "why": why, "affects": affects,
            "tickets": tickets or []}
    async with _cx(10) as cx:
        uid, why_not = await _project_id(cx, project)
        if uid is None:
            return why_not
        r = await cx.post(f"{TARESD}/api/projects/{uid}/assumptions", json=body)
    return _out(r, ("label", "question", "choice", "tickets", "state"))


@mcp.tool()
async def list_assumptions(state: str = "", project: str = "") -> str:
    """The project's assumptions, A1 first: the question, the choice, why, the tickets, and the
    state (open: not yet confirmed; kept; overturned, with the person's words). `state`
    narrows the list."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/assumptions", params={"state": state})
    if r.status_code >= 400:
        return _out(r)
    return json.dumps([{k: a.get(k) for k in ("label", "question", "choice", "why", "tickets",
                                              "state", "words", "made_by")}
                       for a in r.json().get("assumptions") or []], default=str)


@writable()
async def decide_assumption(assumption: str, state: str, words: str = "",
                            project: str = "") -> str:
    """Record what the person said about an assumption (A3): `state` kept or overturned (or open
    again), `words` the person's own words, required when overturned. Only the orchestrator, or
    the person's own session, may. Answering a batch on the desk does this for you."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/assumptions/{assumption}/state",
                          json={"state": state, "words": words})
    return _out(r, ("label", "state", "words"))


@writable()
async def add_check(ticket: str, command: str, result: str, commit: str = "",
                    broke_test: str = "", project: str = "") -> str:
    """Record a check you ran on a ticket: the exact `command`, what it showed (`result`), the
    `commit` it ran on, and, for a test you added, `broke_test`: the test you saw fail when you
    broke the code it covers (then pass again once restored). One line goes into the Checks part
    of the ticket's working doc; the rest of the doc is untouched. Record each check after you
    run it, never one you did not run: the reviewer compares them with your session."""
    body = {"command": command, "result": result, "commit": commit, "broke_test": broke_test}
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/tickets/{ticket}/checks", json=body)
    return _out(r, ("line", "working_doc"))


@writable()
async def remember_for_project(line: str, project: str = "") -> str:
    """Keep one thing you learned about this project (for example "tests need the dev database
    running") in its memory, dated and signed; every session on the project reads it. A rule
    worth keeping for every project goes to the orchestrator instead (an FYI); only it adds to
    the shared memory."""
    async with _cx(10) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/memory/lines", json={"text": line})
    return _out(r, ("id", "added"))


# ── review and merge (TR-441..445) ────────────────────────────────────────────

@writable()
async def record_review(ticket: str, head: str, verdict: str, verified: str,
                        not_verified: str = "", findings: list[dict] | None = None,
                        resolved: dict | None = None, project: str = "") -> str:
    """Record your review of a ticket's pull request, the same verdict you post to GitHub:
    `head` the commit you reviewed, `verdict` pass, changes or block, `verified` what you ran
    and what it showed, `not_verified` what you did not cover and why, `findings`
    [{"severity": "P1|P2|P3", "file": "", "line": 0, "blocking": true, "text": ""}], and on a
    re-review `resolved` marking each earlier finding {"F1": "fixed", "F2": "open"}. A pass
    leaves no blocking finding open. Only the reviewer records reviews."""
    body = {"head": head, "verdict": verdict, "verified": verified,
            "not_verified": not_verified, "findings": findings or [], "resolved": resolved or {}}
    async with _cx(15) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{TARESD}/api/projects/{uid}/tickets/{ticket}/reviews", json=body)
    if r.status_code >= 400:
        return _out(r)
    d = r.json()
    return json.dumps({"round": d["round"], "verdict": d["verdict"],
                       "findings": [f["label"] for f in d["findings"]], "fixed": d["fixed"],
                       "still_open": d["open"]})


@mcp.tool()
async def review_queue() -> str:
    """The pull requests waiting for review across every project, in the order to take them:
    those blocking their milestone first, then the longest waiting. Each says whether the
    builder's Codex layer is clean and whether an earlier verdict is for an older commit."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/review-queue")
    if r.status_code >= 400:
        return _out(r)
    return json.dumps([{k: q.get(k) for k in ("project_name", "ticket", "title", "holder", "pr",
                                              "head", "waiting_minutes", "blocks_milestone",
                                              "layer1_clean")}
                       | {"verdict_note": (q.get("verdict") or {}).get("note")}
                       for q in r.json().get("queue") or []], default=str)


@mcp.tool()
async def review_brief(ticket: str, head: str = "", project: str = "") -> str:
    """Start every review here: the pull request as GitHub has it (it says STOP when the PR is
    merged or its head moved past `head`), the ticket and its working doc, the builder's checks
    compared with its recorded session (unmatched first: check those yourself), Codex's
    findings and the waivers to judge, earlier rounds and their open findings, assumptions and
    decisions, and what keeps coming back in this repo. Then review to your own bar."""
    async with _cx(30) as cx:
        uid, why = await _project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{TARESD}/api/projects/{uid}/tickets/{ticket}/brief",
                         params={"head": head})
    return r.json()["brief"] if r.status_code == 200 else _out(r)


@mcp.tool()
async def recurring_findings(repo: str) -> str:
    """Kinds of finding that came back in several tickets of a repo (owner/name) in the last
    30 days, with the tickets and examples. Check these first in a review."""
    async with _cx(10) as cx:
        r = await cx.get(f"{TARESD}/api/recurring-findings", params={"repo": repo})
    return _out(r)


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
            and s.get("state") in ("working", "waiting", "quiet")] if crew.status_code == 200 else []
    if not orch:
        return ("Recorded as handed to the crew, but no orchestrator is running: tell the person "
                "to run `factory crew up` in a terminal, then send the [TF:SHIP] message.")
    note = ""
    if orch[0].get("state") == "quiet":
        note = (f"\n{orch[0]['name']} has been silent for {orch[0].get('quiet_minutes')} min: if "
                "the message bounces, tell the person to run `factory crew watch` or "
                "`factory crew up`.")
    return (f"Recorded. Now send {orch[0]['name']} a message with SendMessage:{note}\n"
            f"[TF:SHIP] {project or uid} is ready to build\nproject: {project or uid}\n"
            f"repo: {repo or '(the repo named in its AGENTS.md)'}")
