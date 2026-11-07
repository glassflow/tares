# Tares enterprise licence, see ee/LICENSE
"""The area routes and the project access routes with ee installed but the cell not licensed (TR-919, TR-921).

With the ee package installed, which is how the wheel ships, `register_ee` adds the `/api/areas`
routes whatever the licence, so the 402 `licence_required` comes only from each route's own check.
The same holds for `PATCH /api/projects/{uid}` and the two `/members` routes (TR-921).
Core's tests/test_areas_single.py runs with no ee team and so never reaches that check. Here the ee
team is present and the licence is missing or has no enterprise feature: all four
routes answer 402 and write nothing. A licence that expired keeps working, as it does for every
ee action (an expired licence is still valid). The licensed behaviour is ee/tests/test_areas.py.

The ee team also hands out the two MCP tools, so `Team.mcp_tools()` is pinned here.

In process, no ports: the key is stored through PUT /api/licence.

Run: .venv/bin/python ee/tests/test_areas_licence.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-areas-licence-")
ROOT = "root-token-areas-licence"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod
from tares_ee import areas as areas_mod, mcp_areas
from tares_ee.team import Team

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


AREAS_REASON = "Repo owners need an enterprise licence."
ACCESS_REASON = "Changing a project's lead, privacy or members needs an enterprise licence."


def exact_402(r, reason=AREAS_REASON):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and b["message"] == reason)


ROUTES = (("GET", "/api/areas", None),
          ("PUT", "/api/areas", [{"repo": "acme/web", "person_id": "local"}]),
          ("PUT", "/api/areas/acme/web", {"person_id": "local"}),
          ("DELETE", "/api/areas/acme/web", None))
# TR-921: who leads a project, whether it is private and who is on it; {uid} and {pid} are filled in
ACCESS = (("PATCH", "/api/projects/{uid}", {"lead_person_id": "{pid}"}),
          ("POST", "/api/projects/{uid}/members", {"person_id": "{pid}"}),
          ("DELETE", "/api/projects/{uid}/members/{pid}", None))


def fill(path, body, uid, pid):
    return path.format(uid=uid, pid=pid), ({k: v.format(pid=pid) for k, v in body.items()} if body else None)


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store

        pid = store.create_person("Pia", "pia@x.dev")["id"]
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Shop", "objects": []})).json()["id"]

        def access_state():
            p = store.get_project(uid)
            return (p["lead_person_id"], p["scope"], [m["id"] for m in store.list_project_members(uid)])

        before = access_state()

        async def licence(text):
            r = await cx.put("/api/licence", json={"key": text})
            ck("the licence is stored", r.status_code == 200, r.text[:200])

        async def all_refused(why):
            for method, path, body in ROUTES:
                r = await cx.request(method, path, json=body)
                ck(f"{why}: {method} {path} is 402 licence_required", exact_402(r), f"{r.status_code} {r.text[:120]}")
            ck(f"{why}: nothing was written", areas_mod.load(store) == [], str(areas_mod.load(store)))
            for method, path, body in ACCESS:
                p, b = fill(path, body, uid, pid)
                r = await cx.request(method, p, json=b)
                ck(f"{why}: {method} {path} is 402 licence_required", exact_402(r, ACCESS_REASON),
                   f"{r.status_code} {r.text[:120]}")
            ck(f"{why}: the project's lead and members are unchanged", access_state() == before,
               f"{access_state()} {before}")

        print("== ee is installed ==")
        ck("the ee areas routes are registered whatever the licence",
           {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) if r.path.startswith("/api/areas")}
           >= {("GET", "/api/areas"), ("PUT", "/api/areas"), ("PUT", "/api/areas/{owner}/{name}"),
               ("DELETE", "/api/areas/{owner}/{name}")})
        ck("no licence: the team is still the single-person one", type(team_mod.get()).__name__ == "SingleTeam")

        print("== no licence ==")
        await all_refused("no licence")

        print("== a licence with no enterprise feature ==")
        await licence(licensed.test_licence(features=("other",)))
        st = (await cx.get("/api/licence")).json()
        ck("valid but not for ee", st["valid"] and not st["ee_allowed"], str(st))
        await all_refused("no enterprise feature")

        print("== licensed ==")
        await licence(licensed.test_licence())
        r = await cx.put("/api/areas/acme/web", json={"person_id": "local"})
        ck("a licensed set works (200)", r.status_code == 200, r.text[:200])
        r = await cx.get("/api/areas")
        ck("and the list shows it", r.status_code == 200 and [a["repo"] for a in r.json()] == ["acme/web"], r.text[:200])
        r = await cx.patch(f"/api/projects/{uid}", json={"lead_person_id": pid})
        ck("a licensed hand over to a lead works (200)", r.status_code == 200, r.text[:200])
        ck("and the project has that lead", access_state()[0] == pid, str(access_state()))
        r = await cx.post(f"/api/projects/{uid}/members", json={"person_id": pid})
        ck("a licensed member add works (200)", r.status_code == 200, r.text[:200])
        handed = access_state()

        print("== licence removed again: the same routes refuse and the saved area stays ==")
        await licence(licensed.test_licence(features=("other",)))
        for method, path, body in ROUTES:
            r = await cx.request(method, path, json=body)
            ck(f"{method} {path} is 402 again", exact_402(r), f"{r.status_code} {r.text[:120]}")
        for method, path, body in ACCESS:
            p, b = fill(path, body, uid, pid)
            r = await cx.request(method, p, json=b)
            ck(f"{method} {path} is 402 again", exact_402(r, ACCESS_REASON), f"{r.status_code} {r.text[:120]}")
        ck("the saved area is untouched", [e["repo"] for e in areas_mod.load(store)] == ["acme/web"])
        ck("the project's lead and members are untouched", access_state() == handed, f"{access_state()} {handed}")

        print("== expired: still valid, so the routes keep working ==")
        gone = (date.today() - timedelta(days=3)).isoformat()
        await licence(licensed.test_licence(expires=gone))
        st = (await cx.get("/api/licence")).json()
        ck("expired but valid", st["valid"] and st["expired"] and st["ee_allowed"], str(st))
        r = await cx.get("/api/areas")
        ck("the list still answers (200)", r.status_code == 200, r.text[:200])
        r = await cx.delete("/api/areas/acme/web")
        ck("a delete still works (200)", r.status_code == 200, r.text[:200])

        print("== the MCP tools the team hands out ==")
        names = [f.__name__ for f in Team().mcp_tools()]
        ck("Team.mcp_tools() is list_areas, set_area and set_project_access",
           names == ["list_areas", "set_area", "set_project_access"], str(names))
        ck("the first two are the plain functions of mcp_areas", list(Team().mcp_tools())[:2] == list(mcp_areas.TOOLS))

        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
