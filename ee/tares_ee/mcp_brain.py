# Tares enterprise licence, see ee/LICENSE
"""The MCP tools of the second brain (TR-1200): `ask_brain`, `search_brain` and `get_brain_doc`.
`Team.mcp_tools()` hands them out and `tares.mcp_server` registers them, hidden while the licence
does not allow ee.

They call the cell's own `/api/brain/*` routes (`brain/ask.py`), which hold the key to lore, the
daily caps and the call log. A call says it came through MCP in `X-Tares-Via`, and the station
header the server already sends names the session's station, so each call is kept with who made it.

They are plain functions. The helpers they call (`_cx`, `TARESD`, `_out`) are read from
`tares.mcp_server` when a tool runs, not when this file is imported: `mcp_server` imports the ee
team while it is still loading, so a top-level import here would be circular.
"""
from __future__ import annotations

import json

_VIA = {"X-Tares-Via": "mcp"}
_DEEP_WAIT = 150   # seconds: lore's deep ask waits up to 120, and the cell adds its own work


def _say(data: dict, keys: tuple[str, ...]) -> str:
    return json.dumps({k: data[k] for k in keys if k in data}, default=str)


async def ask_brain(question: str, deep: bool = False) -> str:
    """Ask the second brain a question about the connected repos and docs and get an answer with
    citations (path and line, or a link). Use it before grepping when you need to know how
    something works, where it lives or why, across the code. `deep` takes longer (up to two
    minutes) and counts against a smaller daily cap: use it for a question the quick answer did
    not settle. Read `notes`: a repo still indexing is named there, and you grep it yourself."""
    from tares import mcp_server as srv
    async with srv._cx(_DEEP_WAIT if deep else 60) as cx:
        r = await cx.post(f"{srv.TARESD}/api/brain/ask", json={"question": question, "deep": deep},
                          headers=_VIA)
    if r.status_code >= 400:
        return srv._out(r)
    data = r.json()
    if data.get("ready") is False:
        return _say(data, ("ready", "message", "notes"))
    return _say(data, ("answer", "citations", "notes", "repos", "mode"))


async def search_brain(query: str, repo: str = "", k: int = 12) -> str:
    """Search the second brain for the places in the connected repos and docs that mention
    `query`: each hit names its repo, path and lines, a snippet, and a `document_id` get_brain_doc
    reads whole. `repo` (owner/name) narrows it to one repo. A repo still indexing is named in
    `notes`: grep it yourself."""
    from tares import mcp_server as srv
    async with srv._cx(30) as cx:
        r = await cx.get(f"{srv.TARESD}/api/brain/search", params={"q": query, "repo": repo, "k": k},
                         headers=_VIA)
    if r.status_code >= 400:
        return srv._out(r)
    data = r.json()
    if data.get("ready") is False:
        return _say(data, ("ready", "message", "notes"))
    return _say(data, ("hits", "notes", "repos"))


async def get_brain_doc(id: str) -> str:
    """Read one document from the second brain, whole: the `document_id` of a search hit or of a
    citation. Gives its title, address and text."""
    from tares import mcp_server as srv
    async with srv._cx(30) as cx:
        r = await cx.get(f"{srv.TARESD}/api/brain/docs/{id}", headers=_VIA)
    if r.status_code >= 400:
        return srv._out(r)
    return _say(r.json(), ("kind", "title", "uri", "metadata", "text"))


TOOLS = [ask_brain, search_brain, get_brain_doc]
