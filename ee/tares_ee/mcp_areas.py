# Tares enterprise licence, see ee/LICENSE
"""The MCP tools of the area owners (TR-627): `list_areas` and `set_area`. `Team.mcp_tools()` hands
them out and `tares.mcp_server` registers them, hidden while the licence does not allow ee.

They are plain functions. The helpers they call (`_cx`, `TARESD`, `_out`, `_person_in`) are read from
`tares.mcp_server` when a tool runs, not when this file is imported: `mcp_server` imports the ee team
while it is still loading, so a top-level import here would be circular.
"""
from __future__ import annotations

from tares.factory import norm_repo


async def list_areas() -> str:
    """Who owns which GitHub repo on this Tares: [{repo, person: {id, name}, note}]. A repo is
    `owner/name`; matching ignores case."""
    from tares import mcp_server as srv
    async with srv._cx(10) as cx:
        r = await cx.get(f"{srv.TARESD}/api/areas")
    return srv._out(r)


async def set_area(repo: str, person: str) -> str:
    """Say who owns a GitHub repo (`owner/name`). `person` is their id or email (list_people
    shows them). An admin may set or change any repo; anyone else may only claim a repo nobody
    owns yet, for themselves."""
    from tares import mcp_server as srv
    async with srv._cx(10) as cx:
        r = await cx.get(f"{srv.TARESD}/api/people")
        if r.status_code >= 400:
            return srv._out(r)
        p = srv._person_in(r.json().get("people") or [], person)
        if p is None:
            return f"no person {person!r}; list_people shows the ones there are"
        try:
            repo = norm_repo(repo.strip().strip("/"))
        except ValueError as e:
            return str(e)
        owner, name = repo.split("/")
        r = await cx.put(f"{srv.TARESD}/api/areas/{owner}/{name}", json={"person_id": p["id"]})
    return srv._out(r)


TOOLS = [list_areas, set_area]
