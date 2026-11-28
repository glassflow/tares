"""Projects without the source catalog (Tares 2.0, TR-1811).

A project is a row with its goal, icon and status; it holds no sources, triggers, agents or MCP servers
any more. Against the daemon in process:

  * a project made the way the MCP create_project and the console make one (template `custom`, no
    objects) is created, led by its maker, listed and opened;
  * naming an object answers 400 (it does not exist), and so does a template that is gone;
  * a project made from a template Tares no longer ships (shared_code_context, rius_rca,
    challenger_workflow, ai_sre_demo) opens as custom, takes a new name, goal and icon, pauses,
    resumes and is deleted;
  * the default project cannot be deleted; the template list is empty (custom and default are
    reachable by key only);
  * the routes of what went answer 404: a project's sources, outline, health, setup, stats, repair,
    actions and template detect.

Run: .venv/bin/python tests/test_projects.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-projects-test-")
os.environ["TARES_DB"] = os.path.join(TMP, "projects.duckdb")
os.environ.pop("TARES_AUTH_TOKEN", None)

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        store = app.state.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as cx:
            print("== a project as the factory makes one ==")
            r = await cx.post("/api/projects", json={"template": "custom", "kind": "software_factory",
                                                     "name": "alpha", "objects": [],
                                                     "goal": "ship the thing"})
            check("created (201)", r.status_code == 201, r.text[:200])
            p = r.json()
            check("custom, factory kind, its goal, led by its maker, nothing in it",
                  p.get("template") == "custom" and p.get("kind") == "software_factory"
                  and p.get("goal") == "ship the thing" and (p.get("lead") or {}).get("id") == "local"
                  and p.get("objects") == [] and p.get("setup") is None, str(p)[:300])
            uid = p["id"]
            r = await cx.get("/api/projects")
            check("listed", any(x["id"] == uid for x in r.json()["projects"]), r.text[:200])
            r = await cx.get(f"/api/projects/{uid}")
            check("opened", r.status_code == 200 and r.json()["name"] == "alpha", r.text[:200])
            r = await cx.get(f"/api/projects/{uid}/summary")
            check("its summary reads", r.status_code == 200 and r.json()["id"] == uid
                  and isinstance(r.json().get("log"), list), r.text[:200])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "alpha"})
            check("a second project of the same name is refused", r.status_code == 400, r.text[:200])

            print("== what a project can no longer hold ==")
            r = await cx.post("/api/projects", json={"template": "custom", "name": "beta",
                                                     "objects": [{"kind": "source", "name": "logs"}]})
            check("naming an object answers 400, it does not exist", r.status_code == 400
                  and "does not exist" in r.text and store.get_project_by_name("beta") is None,
                  r.text[:200])
            r = await cx.put(f"/api/projects/{uid}", json={"objects": [{"kind": "trigger", "name": "t"}]})
            check("and on an edit", r.status_code == 400 and "does not exist" in r.text, r.text[:200])
            r = await cx.post("/api/projects", json={"template": "rius_rca", "name": "gamma"})
            check("a retired template makes a custom project", r.status_code == 201
                  and r.json()["template"] == "custom",
                  r.text[:200])
            r = await cx.post("/api/projects", json={"template": "no_such", "name": "delta"})
            check("an unknown template answers 400", r.status_code == 400, r.text[:200])
            r = await cx.get("/api/projects/templates")
            check("the template list is empty", r.status_code == 200 and r.json()["templates"] == [],
                  r.text[:200])
            r = await cx.get("/api/projects/templates/custom")
            check("custom is reachable by key", r.status_code == 200 and r.json()["key"] == "custom",
                  r.text[:200])

            print("== a project from a template Tares no longer ships ==")
            for i, key in enumerate(("shared_code_context", "rius_rca", "challenger_workflow",
                                     "ai_sre_demo")):
                old = f"uc_old{i}"
                store.create_project(old, key, f"old {key}", {"repos": ["acme/app"],
                                                              "objects": [{"kind": "agent", "name": "a"}]})
                r = await cx.get(f"/api/projects/{old}")
                check(f"{key}: opens", r.status_code == 200 and r.json()["template"] == key
                      and r.json()["objects"] == [], r.text[:200])
                r = await cx.put(f"/api/projects/{old}", json={"name": f"renamed {key}"})
                check(f"{key}: renamed", r.status_code == 200 and r.json()["name"] == f"renamed {key}",
                      r.text[:200])
                r = await cx.put(f"/api/projects/{old}", json={"goal": "keep it going", "icon": "🚀"})
                check(f"{key}: goal and icon", r.status_code == 200 and r.json()["goal"] == "keep it going"
                      and r.json()["icon"] == "🚀", r.text[:200])
                r = await cx.post(f"/api/projects/{old}/pause", json={"sources": True})
                check(f"{key}: paused", r.status_code == 200 and r.json()["status"] == "paused", r.text[:200])
                r = await cx.post(f"/api/projects/{old}/resume")
                check(f"{key}: resumed", r.status_code == 200 and r.json()["status"] == "active",
                      r.text[:200])
                r = await cx.delete(f"/api/projects/{old}?delete_sources=all")
                check(f"{key}: deleted", r.status_code == 200 and store.get_project(old) is None,
                      r.text[:200])

            print("== the default project ==")
            default = store.default_project_id()
            r = await cx.delete(f"/api/projects/{default}")
            check("the default project cannot be deleted", r.status_code == 400, r.text[:200])
            r = await cx.put(f"/api/projects/{default}", json={"goal": "whatever is left"})
            check("but takes a goal", r.status_code == 200 and r.json()["goal"] == "whatever is left",
                  r.text[:200])

            print("== the routes of what went ==")
            for method, path in (("POST", f"/api/projects/{uid}/sources"),
                                 ("GET", f"/api/projects/{uid}/outline"),
                                 ("GET", f"/api/projects/{uid}/health"),
                                 ("GET", f"/api/projects/{uid}/setup"),
                                 ("POST", f"/api/projects/{uid}/stats"),
                                 ("POST", f"/api/projects/{uid}/repair"),
                                 ("POST", f"/api/projects/{uid}/actions/sync"),
                                 ("POST", "/api/projects/templates/custom/detect"),
                                 ("POST", "/api/setup/drafts")):
                r = await cx.request(method, path, json={})
                check(f"{method} {path.replace(uid, '<id>')} answers 404 or 405",
                      r.status_code in (404, 405), f"{r.status_code} {r.text[:120]}")

            r = await cx.delete(f"/api/projects/{uid}")
            check("the factory project is deleted", r.status_code == 200 and store.get_project(uid) is None,
                  r.text[:200])
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
