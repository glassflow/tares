# Tares enterprise licence, see ee/LICENSE
"""The MCP tools of the questions a spec asks a repo's owner (TR-636, TR-923): `ask_area`,
`answer_question` and `list_questions`. `Team.mcp_tools()` hands them out and `tares.mcp_server`
registers them, hidden while the licence does not allow ee.

They are plain functions. The helpers they call (`_cx`, `TARESD`, `_out`, `_project_id`) are read
from `tares.mcp_server` when a tool runs, not when this file is imported: `mcp_server` imports the
ee team while it is still loading, so a top-level import here would be circular.
"""
from __future__ import annotations

import json


def _question_line(q: dict) -> dict:
    return {"id": q.get("id"), "repo": q.get("repo"), "text": q.get("text"), "state": q.get("state"),
            "to": (q.get("to_person") or {}).get("name"),
            "asked_by": (q.get("asked_by_person") or {}).get("name"), "asked_at": q.get("asked_at"),
            "answer": q.get("answer"), "answered_at": q.get("answered_at")}


async def ask_area(repo: str, text: str, project: str = "") -> str:
    """Ask the owner of a repo (`owner/name`) a question while the spec is written. It lands in
    their my_work as a spec question until they answer it; ask it here and tell the person you
    did, then carry on with what does not depend on the answer. Refused with 'has no owner on
    this cell' when nobody owns the repo."""
    from tares import mcp_server as srv
    async with srv._cx(10) as cx:
        uid, why = await srv._project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{srv.TARESD}/api/projects/{uid}/questions",
                          json={"repo": repo, "text": text})
    if r.status_code >= 400:
        return srv._out(r)
    return json.dumps(_question_line(r.json()), default=str)


async def answer_question(id: str, answer: str, project: str = "") -> str:
    """Answer a question a spec asked you as the owner of a repo (Q1, Q2... as my_work lists
    them). Only the person it is addressed to may answer."""
    from tares import mcp_server as srv
    async with srv._cx(10) as cx:
        uid, why = await srv._project_id(cx, project)
        if uid is None:
            return why
        r = await cx.post(f"{srv.TARESD}/api/projects/{uid}/questions/{id}/answer",
                          json={"answer": answer})
    if r.status_code >= 400:
        return srv._out(r)
    return json.dumps(_question_line(r.json()), default=str)


async def list_questions(state: str = "", project: str = "") -> str:
    """The questions a spec asked area owners, oldest first, with the answers. `state` is open
    or answered; empty lists both."""
    from tares import mcp_server as srv
    async with srv._cx(10) as cx:
        uid, why = await srv._project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{srv.TARESD}/api/projects/{uid}/questions", params={"state": state})
    if r.status_code >= 400:
        return srv._out(r)
    return json.dumps([_question_line(q) for q in r.json().get("questions") or []], default=str)


TOOLS = [ask_area, answer_question, list_questions]
