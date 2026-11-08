# Tares enterprise licence, see ee/LICENSE
"""The MCP tool of project access (TR-921): `set_project_access`. `Team.mcp_tools()` hands it out and
`tares.mcp_server` registers it, hidden while the licence does not allow ee.

It is a plain function. The helpers it calls (`_cx`, `TARESD`, `_out`, `_project_id`, `_person_in`)
are read from `tares.mcp_server` when the tool runs, not when this file is imported: `mcp_server`
imports the ee team while it is still loading, so a top-level import here would be circular.
"""
from __future__ import annotations


async def set_project_access(project: str = "", lead: str = "", scope: str = "",
                             add_members: list[str] | None = None,
                             remove_members: list[str] | None = None) -> str:
    """Say who leads a project, whether it is `team` or `private`, and who is on it. `lead` is
    the person to lead it, `scope` is team or private, `add_members` and `remove_members` are
    people to put on or take off it. A person is given by id or email (list_people shows them).
    Only the project's lead changes the scope or its members; an admin may change the lead.
    Leave out what you do not want to change. The changes are made one after another and the
    first refusal stops the rest, so earlier ones stay. Returns the project's lead, scope and
    members."""
    from tares import mcp_server as srv
    async with srv._cx(15) as cx:
        uid, why = await srv._project_id(cx, project)
        if uid is None:
            return why
        r = await cx.get(f"{srv.TARESD}/api/people")
        if r.status_code >= 400:
            return srv._out(r)
        people = r.json().get("people") or []
        add = [x for x in (add_members or []) if x.strip()]
        remove = [x for x in (remove_members or []) if x.strip()]
        wanted = {}
        for ref in [lead] + add + remove:
            if ref.strip():
                wanted[ref] = srv._person_in(people, ref)
                if wanted[ref] is None:
                    return f"no person {ref!r}; list_people shows the ones there are"
        # the lead goes last: once handed over, the caller can no longer change scope or members
        if scope.strip():
            r = await cx.patch(f"{srv.TARESD}/api/projects/{uid}", json={"scope": scope.strip()})
            if r.status_code >= 400:
                return srv._out(r)
        for ref in add:
            r = await cx.post(f"{srv.TARESD}/api/projects/{uid}/members",
                              json={"person_id": wanted[ref]["id"]})
            if r.status_code >= 400:
                return srv._out(r)
        for ref in remove:
            r = await cx.delete(f"{srv.TARESD}/api/projects/{uid}/members/{wanted[ref]['id']}")
            if r.status_code >= 400:
                return srv._out(r)
        if lead.strip():
            r = await cx.patch(f"{srv.TARESD}/api/projects/{uid}",
                               json={"lead_person_id": wanted[lead]["id"]})
            if r.status_code >= 400:
                return srv._out(r)
        r = await cx.get(f"{srv.TARESD}/api/projects/{uid}")
    return srv._out(r, ("id", "name", "lead", "scope", "members"))


TOOLS = [set_project_access]
