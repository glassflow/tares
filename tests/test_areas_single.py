"""Core with no licence: the area owner routes and tools belong to ee (TR-919).

Without an ee team the four `/api/areas` routes answer 402 `licence_required` (they are FALLBACKS
rows, so the person sees "needs a licence" and not the console page or a 404), the MCP tools
`list_areas` and `set_area` are not in the tool list, and the cell still reads its home, people
and projects without them: nobody owns a repo, the team home is the single-person one.
`norm_repo`, the one helper both sides use, lives in `tares/factory.py` and keeps its rules.

The licensed behaviour of the same routes and tools is ee/tests/test_areas.py.

Run: .venv/bin/python tests/test_areas_single.py
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

TMP = tempfile.mkdtemp(prefix="tares-areas-single-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-areas-single"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares import factory as factory_mod, mcp_server as ms
from tares.daemon import make_app

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def exact_402(r):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and "enterprise licence" in b["message"] and "—" not in b["message"])


def helper():
    print("== norm_repo is in factory.py ==")
    for pasted in ("https://github.com/GlassFlow/Tares", "https://github.com/glassflow/tares.git",
                   "github.com/glassflow/tares/", "https://www.github.com/glassflow/tares/pull/393",
                   "git@github.com:glassflow/tares.git", "  http://github.com/glassflow/tares  ", "Glassflow/Tares"):
        ck(f"accepted: {pasted.strip()}", factory_mod.norm_repo(pasted) == "glassflow/tares")
    for bad in ("https://gitlab.com/glassflow/tares", "https://github.com/glassflow", "x", "a/b/c", "", None):
        try:
            factory_mod.norm_repo(bad)
            ok = False
        except ValueError:
            ok = True
        ck(f"refused: {bad!r}", ok)
    ck("tares/ has no areas module any more", not os.path.exists(os.path.join(ROOT, "tares", "areas.py")))


async def routes():
    print("== routes without ee ==")
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(AUTH)) as cx:
        for method, path, body in (("GET", "/api/areas", None), ("PUT", "/api/areas", []),
                                   ("PUT", "/api/areas/acme/web", {"person_id": "local"}),
                                   ("DELETE", "/api/areas/acme/web", None)):
            r = await cx.request(method, path, json=body)
            ck(f"{method} {path}: 402 licence_required with {{code, message, url}}", exact_402(r),
               f"{r.status_code} {r.text[:100]}")
        r = await cx.post("/api/keys", json={"name": "ing", "scopes": ["ingest"]})
        ing = r.json().get("secret", "")
        r = await cx.put("/api/areas/acme/web", headers=H(ing), json={"person_id": "local"})
        ck("a key that may only ingest reaches the answer: 402, not 403 (the scope rule stays in core)",
           exact_402(r), f"{r.status_code} {r.text[:100]}")
        r = await cx.get("/api/areas", headers=H(ing))
        ck("... but it has no read scope, so the list is 403 as it always was", r.status_code == 403,
           f"{r.status_code} {r.text[:100]}")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "P1", "objects": []})
        uid = r.json().get("id")
        r = await cx.post(f"/api/projects/{uid}/keys", json={"name": "pk", "scopes": ["read"]})
        pk = r.json().get("secret", "")
        r = await cx.get(f"/api/projects/{uid}", headers=H(pk))
        ck("a project key without any area still reads its project", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        r = await cx.get("/api/areas", headers=H(pk))
        ck("... and is refused the cell's area list, as before", r.status_code == 403, f"{r.status_code}")
        r = await cx.get("/api/people")
        ck("people still answer", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        h = app.state.host
        ck("area_owner answers None on this cell", h.area_owner("acme/web") is None)
        ck("the team has no areas", team_mod.get().areas(h.store) == [])


def tools():
    print("== MCP tools without ee ==")
    names = [t.name for t in asyncio.run(ms.mcp.list_tools())]
    ck("list_areas is not a tool", "list_areas" not in names, str(names))
    ck("set_area is not a tool", "set_area" not in names, str(names))
    ck("ask_area is not a tool", "ask_area" not in names, str(names))
    ck("no ee tool is registered", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))


helper()
asyncio.run(routes())
tools()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
