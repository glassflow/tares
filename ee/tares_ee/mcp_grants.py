# Tares enterprise licence, see ee/LICENSE
"""The MCP tool of grants that wait for consent (TR-922): `consent_grant`. `Team.mcp_tools()` hands it
out and `tares.mcp_server` registers it, hidden while the licence does not allow ee.

It is a plain function. The helpers it calls (`_cx`, `TARESD`, `_out`) are read from
`tares.mcp_server` when the tool runs, not when this file is imported: `mcp_server` imports the ee
team while it is still loading, so a top-level import here would be circular.
"""
from __future__ import annotations


async def consent_grant(id: str) -> str:
    """Say yes, as the owner of a repo a project grant names or as the project's lead (an admin,
    when it has none), to grant `id` (like `G7`). `read_grants` lists the ones waiting for you.
    Only a person it waits for can; the grant is in effect once all of them have. Never call it
    for someone else: the answer is their own."""
    from tares import mcp_server as srv
    async with srv._cx(10) as cx:
        r = await cx.post(f"{srv.TARESD}/api/grants/{id.strip()}/consent")
    return srv._out(r)


TOOLS = [consent_grant]
