"""Projects are plain CRUD (Tares 2.0, TR-1812): tares/factory_projects.py over the session store.

Against the daemon in process:

  * the MCP create_project makes a factory project led by its maker, list_projects lists it and
    project_timeline reads it;
  * POST /api/projects makes one too, and a `template`, `params` or `objects` field is accepted and
    ignored; a blank name, a name taken, a goal too long, a wrong icon or an unknown kind is refused
    and nothing is made;
  * GET, PUT (name, goal, icon), pause, resume, summary and DELETE work, a wrong value changes
    nothing, and the default project takes a goal but no new name and cannot be deleted;
  * the factory Projects page (GET /api/factory/projects) lists the project with its new name,
    icon and goal, and drops it once deleted;
  * the routes that went answer 404 or 405: the template list, the legacy /api/usecases routes;
  * with tares_ee installed and a licence, the access routes (PATCH, members) still answer with the
    project as Host.projects().get gives it.

Run: .venv/bin/python tests/test_factory_projects.py (TARES_TEST_PG and PYTHONPATH=tests/pg_harness
for Postgres)
"""
import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-factory-projects-crud-")
os.environ["TARES_DB"] = os.path.join(TMP, "projects.duckdb")
os.environ.pop("TARES_AUTH_TOKEN", None)
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

EE = importlib.util.find_spec("tares_ee") is not None
if EE:
    sys.path.insert(0, os.path.join(ROOT, "ee", "tests"))
    import licensed
    licensed.install_test_root()
    os.environ["TARES_LICENSE"] = licensed.test_licence()

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {str(detail)[:300]}"))


async def main():
    from tares.daemon import make_app
    import tares.mcp_server as m
    app = make_app()
    store = app.state.store

    def mk(timeout=10):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    m.TARESD = "http://t"
    m._cx = mk

    async with app.router.lifespan_context(app), mk() as cx:
        print("== made through MCP ==")
        out = json.loads(await m.create_project("Shop", "Sell things"))
        uid = out.get("id")
        ck("create_project answers with the id, name and goal",
           uid and out.get("name") == "Shop" and out.get("goal") == "Sell things", out)
        p = store.get_project(uid) or {}
        ck("a factory project, led by its maker, stored as custom",
           p.get("kind") == "software_factory" and p.get("lead_person_id") == "local"
           and p.get("template") == "custom" and p.get("status") == "active", p)
        listed = json.loads(await m.list_projects())
        ck("list_projects lists it", any(x["id"] == uid and x["name"] == "Shop" for x in listed), listed)
        tl = json.loads(await m.project_timeline(project="Shop"))
        ck("project_timeline reads it", isinstance(tl, dict) and "threads" in tl, tl)
        again = await m.create_project("Shop", "")
        ck("a second Shop is refused", "already exists" in again, again)

        print("== made through the route ==")
        r = await cx.post("/api/projects", json={"template": "rius_rca", "name": "Blog",
                                                 "params": {"repos": ["a/b"]},
                                                 "objects": [{"kind": "source", "name": "logs"}],
                                                 "kind": "software_factory", "icon": "🚀"})
        b = r.json()
        ck("created (201), the template, params and objects ignored",
           r.status_code == 201 and b.get("template") == "custom" and b.get("params") == {}
           and b.get("objects") == [] and b.get("icon") == "🚀", r.text)
        blog = b.get("id")
        ck("the card: lead, members, default",
           b.get("lead", {}).get("id") == "local" and b.get("members") == [
               {"id": "local", "name": b["lead"]["name"], "reason": "lead"}]
           and b.get("default") is False, b)
        before = len(store.list_projects())
        for body, why in (({"name": "  "}, "a blank name"), ({"name": "Blog"}, "a name taken"),
                          ({"name": "Long", "goal": "x" * 201}, "a goal past 200 characters"),
                          ({"name": "Bad", "icon": "ab"}, "an icon that is not one emoji"),
                          ({"name": "Kind", "kind": "sre"}, "an unknown kind")):
            r = await cx.post("/api/projects", json=body)
            ck(f"{why} answers 400", r.status_code == 400, r.text)
        ck("and nothing was made", len(store.list_projects()) == before, store.list_projects())

        print("== read and changed ==")
        r = await cx.get(f"/api/projects/{blog}")
        ck("GET", r.status_code == 200 and r.json()["name"] == "Blog", r.text)
        r = await cx.get("/api/projects")
        ck("listed", {blog, uid} <= {x["id"] for x in r.json()["projects"]}, r.text)
        r = await cx.put(f"/api/projects/{blog}", json={"name": "Blog two", "goal": "  Move\nthe blog ",
                                                        "icon": "🔥"})
        ck("PUT renames, sets the goal (one line) and the icon",
           r.status_code == 200 and r.json()["name"] == "Blog two"
           and r.json()["goal"] == "Move the blog" and r.json()["icon"] == "🔥", r.text)
        r = await cx.put(f"/api/projects/{blog}", json={"name": "Shop", "goal": "other"})
        ck("a name another project has is refused, the goal unchanged",
           r.status_code == 400 and store.get_project(blog)["goal"] == "Move the blog", r.text)
        r = await cx.put(f"/api/projects/{blog}", json={"goal": "Changed", "icon": "<b>"})
        ck("a wrong icon changes nothing", r.status_code == 400
           and store.get_project(blog)["goal"] == "Move the blog", r.text)
        r = await cx.put(f"/api/projects/{blog}", json={"icon": None})
        ck("the icon cleared, the goal kept", r.status_code == 200 and r.json()["icon"] is None
           and r.json()["goal"] == "Move the blog", r.text)
        r = await cx.put(f"/api/projects/{blog}", json={"icon": "🔥"})
        r = await cx.post(f"/api/projects/{blog}/pause", json={"sources": True})
        ck("paused (an old body is ignored)", r.status_code == 200 and r.json()["status"] == "paused",
           r.text)
        r = await cx.post(f"/api/projects/{blog}/resume")
        ck("resumed", r.status_code == 200 and r.json()["status"] == "active", r.text)
        r = await cx.get(f"/api/projects/{blog}/summary")
        log = [x["action"] for x in r.json().get("log") or []]
        ck("the summary has the log, each change of name, goal or icon in it",
           r.status_code == 200 and log[:2] == ["resumed", "paused"] and log[-1] == "created"
           and set(log[2:-1]) == {"updated"} and len(log) == 6
           and r.json()["ready_to_close"] is False, r.text)
        r = await cx.get("/api/projects/uc_nothere")
        ck("an unknown project answers 404", r.status_code == 404, r.text)

        print("== the factory Projects page reads them ==")
        rows = (await cx.get("/api/factory/projects")).json()["projects"]
        row = next((x for x in rows if x["id"] == blog), None)
        ck("the row has the new name, icon and goal", row and row["name"] == "Blog two"
           and row["icon"] == "🔥" and row["goal"] == "Move the blog", rows)

        if EE:
            print("== ee access still answers with the project ==")
            r = await cx.patch(f"/api/projects/{blog}", json={"scope": "private"})
            ck("PATCH scope private", r.status_code == 200 and r.json().get("scope") == "private"
               and r.json().get("lead", {}).get("id") == "local", r.text)
            r = await cx.patch(f"/api/projects/{blog}", json={"scope": "team"})
            ck("and back to team", r.status_code == 200 and r.json().get("scope") == "team", r.text)
            ann = store.create_person("Ann", email="ann@example.com")["id"]
            r = await cx.post(f"/api/projects/{blog}/members", json={"person_id": ann})
            ck("a member added, the project in the answer", r.status_code == 200
               and [x["id"] for x in r.json().get("members") or []] == ["local", ann], r.text)
            r = await cx.delete(f"/api/projects/{blog}/members/{ann}")
            ck("and taken off", r.status_code == 200
               and [x["id"] for x in r.json().get("members") or []] == ["local"], r.text)
        else:
            print("== ee access: tares_ee is not installed, skipped ==")

        print("== the default project ==")
        default = store.default_project_id()
        r = await cx.put(f"/api/projects/{default}", json={"goal": "whatever is left"})
        ck("takes a goal", r.status_code == 200 and r.json()["goal"] == "whatever is left"
           and r.json()["default"] is True, r.text)
        r = await cx.put(f"/api/projects/{default}", json={"name": "Mine now"})
        ck("but no new name", r.status_code == 400, r.text)
        r = await cx.delete(f"/api/projects/{default}")
        ck("and cannot be deleted", r.status_code == 400 and store.get_project(default), r.text)

        print("== the routes that went ==")
        for method, path in (("GET", "/api/projects/templates"),
                             ("GET", "/api/projects/templates/custom"),
                             ("GET", "/api/usecases"), ("GET", "/api/usecases/recipes"),
                             ("POST", "/api/usecases"), ("GET", f"/api/usecases/{blog}"),
                             ("POST", f"/api/projects/{blog}/subscribe"),
                             ("GET", f"/api/projects/{blog}/external-agents"),
                             ("GET", f"/api/projects/{blog}/stats")):
            r = await cx.request(method, path, json={"name": "x"} if method == "POST" else None)
            ck(f"{method} {path.replace(blog, '<id>')} answers 404 or 405",
               r.status_code in (404, 405), f"{r.status_code} {r.text[:120]}")

        print("== deleted ==")
        r = await cx.delete(f"/api/projects/{blog}")
        ck("DELETE", r.status_code == 200 and r.json() == {"ok": True}
           and store.get_project(blog) is None, r.text)
        rows = (await cx.get("/api/factory/projects")).json()["projects"]
        ck("gone from the Projects page", blog not in {x["id"] for x in rows}, rows)
        r = await cx.delete(f"/api/projects/{blog}")
        ck("a second DELETE answers 404", r.status_code == 404, r.text)

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
