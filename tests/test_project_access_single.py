"""Core with no licence: changing a project's lead, privacy and members belongs to ee (TR-921).

Without an ee team, `PATCH /api/projects/{uid}` and the two `/members` routes answer 402
`licence_required` (they are FALLBACKS rows, so the person sees "needs a licence" and not a 404 or
the console page), and the MCP tool `set_project_access` is not in the tool list. What stays in
core keeps working: the person who makes a project leads it, and a project shows its lead, scope
and members.

The licensed behaviour of the same routes and the tool is ee/tests/test_project_access.py and
ee/tests/test_private_*.py. Who may see a private project is core, in every licence state:
tests/test_private_any_licence.py.

Run: .venv/bin/python tests/test_project_access_single.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-access-single-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
# auth is off: the one person using the cell is the owner, `local`, and an admin

import httpx

from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares import mcp_server as ms
from tares.daemon import make_app

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def exact_402(r):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and "enterprise licence" in b["message"] and "lead, privacy or members" in b["message"]
            and "—" not in b["message"])


async def routes():
    print("== routes without ee ==")
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
        me = (await cx.get("/api/whoami")).json()
        mine = (me.get("person") or {}).get("id") or "local"
        ck("the owner is a person on this cell", bool(mine), str(me))
        r = await cx.post("/api/projects", json={"template": "custom", "name": "P1", "objects": []})
        uid = r.json().get("id")
        ck("a project is made", r.status_code in (200, 201) and bool(uid), f"{r.status_code} {r.text[:100]}")
        p = (await cx.get(f"/api/projects/{uid}")).json()
        lead = p.get("lead")
        lead_id = lead.get("id") if isinstance(lead, dict) else lead
        ck("the owner who made it leads it", lead_id == mine, str(lead))
        ck("it is a team project", p.get("scope") == "team", str(p.get("scope")))
        ck("GET shows the members", "members" in p, str(sorted(p)))

        for label, method, path, body in (
                ("make it private", "PATCH", f"/api/projects/{uid}", {"scope": "private"}),
                ("hand it to a lead", "PATCH", f"/api/projects/{uid}", {"lead_person_id": mine}),
                ("add a member", "POST", f"/api/projects/{uid}/members", {"person_id": mine}),
                ("remove a member", "DELETE", f"/api/projects/{uid}/members/{mine}", None)):
            r = await cx.request(method, path, json=body)
            ck(f"{label}: {method} answers 402 licence_required with {{code, message, url}}", exact_402(r),
               f"{r.status_code} {r.text[:100]}")

        p2 = (await cx.get(f"/api/projects/{uid}")).json()
        ck("nothing changed: still a team project", p2.get("scope") == "team", str(p2.get("scope")))
        lead2 = p2.get("lead")
        ck("nothing changed: same lead", (lead2.get("id") if isinstance(lead2, dict) else lead2) == mine, str(lead2))
        r = await cx.get("/api/projects")
        ck("the project list still answers", r.status_code == 200, f"{r.status_code}")
        h = app.state.host
        h.store.set_project_scope(uid, "private")   # the store write stays in core
        p3 = (await cx.get(f"/api/projects/{uid}")).json()
        ck("a private project the owner leads still shows in full", p3.get("scope") == "private"
           and not p3.get("hidden"), str(p3)[:200])


def tools():
    print("== MCP tools without ee ==")
    names = [t.name for t in asyncio.run(ms.mcp.list_tools())]
    ck("set_project_access is not a tool", "set_project_access" not in names, str(names))
    ck("list_people (a core tool) is still there", "list_people" in names, str(names))
    people = next(t for t in asyncio.run(ms.mcp.list_tools()) if t.name == "list_people")
    ck("its description does not point at the missing tool", "set_project_access" not in (people.description or ""),
       people.description)
    ck("no ee tool is registered", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))


asyncio.run(routes())
tools()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
