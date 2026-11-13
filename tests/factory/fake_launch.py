"""The launch route of a fake Tares (GET /api/factory/stations/{station}/launch, TR-867), for the crew tests.

A station start (TR-870) takes its launch from the cell, so a fake Tares has to give one. The answer is not
written out by hand: it is built by the real runtime, `RuntimeClaudeCode().launch_spec(...)`, from the same
kind of ctx the real route builds, so a test sees the spec the cell would give. What the fake supplies is
what a real cell reads from its own store: the project rows, the rules (a function of the project's name
and the work role) and the crew's autonomy.

`launch_answer(...)` is `(status, body)` for a path that is the launch route, None for any other path;
`CAPABILITY` is what /health lists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tares.crew.state import STATION_NAME_RULE, station_name_ok  # noqa: E402
from tares.integrations.builtin.runtime_claude_code import RuntimeClaudeCode  # noqa: E402

CAPABILITY = "launch_spec"
_ROUTE = re.compile(r"^/api/factory/stations/([^/]+)/launch$")
WORK_ROLES = ("orchestrator", "reviewer", "releaser", "builder")


def launch_answer(raw_path: str, headers: dict, projects: list, rules, crew_autonomy: str, base: str,
                  monitoring=None):
    """The launch route's answer for `raw_path` (path and query), or None when it is another route.

    headers: the request's headers (X-Tares-Station says who asks; without it the call is refused, as a key
    alone is). projects: the project rows ({id, name, ...}). rules(project_name, work_role): the rendered
    {allow, soft_deny} of the station's work role. crew_autonomy: the crew's level. base: the cell's own
    address. monitoring: an instance for the spec's monitoring env and settings, or None."""
    u = urlparse(raw_path)
    m = _ROUTE.match(u.path)
    if not m:
        return None
    station = unquote(m.group(1)).strip()
    if not headers.get("X-Tares-Station"):
        return 403, {"detail": "this needs an admin or the crew station itself"}
    if headers.get("X-Tares-Station") != station:
        return 403, {"detail": f"this key is for station {headers.get('X-Tares-Station')}; it cannot read the launch of {station}"}
    if not station_name_ok(station):
        return 422, {"detail": f"{station!r} cannot name a station: {STATION_NAME_RULE}"}
    q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
    role = (q.get("role") or headers.get("X-Tares-Role") or "").strip().lower()
    if not role:
        return 422, {"detail": f"cannot tell the role of {station!r}: add role=orchestrator|reviewer|releaser|builder|helper"}
    parent = q.get("parent", "")
    named = q.get("project", "").strip()
    p = None
    if named:
        found = [x for x in projects if named in (x.get("id"), x.get("name"))]
        if not found:
            return 404, {"detail": "unknown project"}
        p = found[0]
    work_role = role
    if role == "helper":
        work_role = (q.get("parent_role") or "").strip().lower()
        if work_role not in WORK_ROLES:
            return 422, {"detail": "a helper works under its parent's role: add parent_role=orchestrator|reviewer|releaser|builder"}
    ctx = {
        "crew": q.get("crew") or "crew", "orchestrator": q.get("orchestrator", ""), "parent": parent,
        "parent_role": work_role, "project": p["name"] if p else "", "project_id": p["id"] if p else "",
        "repo": q.get("repo", ""), "human": q.get("human", ""), "authorized_at": q.get("authorized_at", ""),
        "autonomy": q.get("autonomy", ""), "crew_autonomy": crew_autonomy, "model": q.get("model", ""),
        "effort": q.get("effort", ""), "autocompact": q.get("autocompact", ""), "cluster": q.get("cluster", ""),
        "tares_url": q.get("tares_url") or base, "mcp_url": q.get("mcp_url") or f"{base}/mcp",
        "token_file": q.get("token_file", "") == "true",
        "rules": rules(p["name"] if p else "", work_role), "monitoring": monitoring,
    }
    try:
        spec = RuntimeClaudeCode().launch_spec(role, station, ctx)
    except ValueError as e:
        return 422, {"detail": str(e)}
    return 200, {"runtime": "claude_code", "station": station, "role": role, "command": spec.command,
                 "env": spec.env, "settings": spec.settings, "placeholders": spec.placeholders}
