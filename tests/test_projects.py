"""End-to-end test for the project framework (templates, instances, membership, engine, API, YAML).

Run: .venv/bin/python tests/test_projects.py   (no external services needed)

Uses a tests-only template (two webhook sources, a trigger over both, an agent, an MCP server) so
it exercises every object kind without depending on a real template. Also covers the default
project every cell has, and the rules that make a project the unit: a trigger, agent or MCP server
is in exactly one project, a source in any number.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-projects-test.duckdb"
CATALOG = "/tmp/tares-projects-test.catalog.yaml"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = CATALOG

import httpx
import json

from tares.projects import PlannedObject, Template, register
from tares.projects.registry import unregister

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class DemoTemplate(Template):
    key = "test_demo"
    title = "Test demo"
    description = "two webhook sources keyed by app, one trigger over both, one agent, one mcp server"
    PARAMS = {"apps": {"type": "list", "required": True, "help": "app names"},
              "prefix": {"type": "string", "default": "t"},
              "fail": {"type": "boolean", "default": False, "help": "plan an invalid trigger"}}

    def plan(self, params):
        p = params["prefix"]
        objs = []
        for app in params["apps"]:
            objs.append(PlannedObject("source", f"source:{app}", {
                "name": f"{p}_{app}", "connector": "webhook", "poll": "5s",
                "config": {"event_type": "log", "text_template": "{msg}",
                           "labels": [{"name": "app", "const": app, "primary": True}]}}))
        cond = {"aggregate": "count", "predicate": "> 0" if not params["fail"] else "nope",
                "window": "5m", "group_by": ["key_value"]}
        objs.append(PlannedObject("trigger", "trigger", {
            "name": f"{p}_trigger", "sources": [f"{p}_{a}" for a in params["apps"]],
            "key_field": "app", "condition": cond,
            "emit": {"kind": "demo"}, "cooldown": "1m"}))
        objs.append(PlannedObject("mcp_server", "mcp", {
            "name": f"{p}_mcp", "url": "https://example.invalid/mcp"}))
        objs.append(PlannedObject("agent", "agent", {
            "name": f"{p}_agent", "trigger": f"{p}_trigger", "prompt": "say hi",
            "mcp_servers": [f"{p}_mcp"], "enabled": False}))
        return objs

    def summary(self, instance, store):
        return {"apps": instance["params"]["apps"]}


register(DemoTemplate())

COND = {"aggregate": "count", "predicate": "> 0", "window": "5m", "group_by": ["key_value"]}


async def main():
    for p in (DB, DB + ".wal", CATALOG):
        if os.path.exists(p):
            os.remove(p)

    from tares.daemon import make_app
    app = make_app()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:

            print("== the default project ==")
            ps = (await cx.get("/api/projects")).json()["projects"]
            check("a fresh cell has exactly one project, the default one",
                  len(ps) == 1 and ps[0]["default"] is True and ps[0]["template"] == "default"
                  and ps[0]["name"] == "Default", json.dumps(ps)[:300])
            did = ps[0]["id"]
            st = app.state.store
            check("its id is in settings", st.get_setting("default_project") == did)
            r = await cx.delete(f"/api/projects/{did}")
            check("the default project cannot be deleted",
                  r.status_code == 400 and "the default project cannot be deleted" in r.text, r.text)
            r = await cx.put(f"/api/projects/{did}", json={"params": {}})
            check("the default project has no settings to edit", r.status_code == 400, r.text)
            r = await cx.post("/api/projects", json={"template": "default", "name": "second"})
            check("a second default project cannot be created", r.status_code == 400, r.text)
            keys = [x["key"] for x in (await cx.get("/api/projects/templates")).json()["templates"]]
            check("default is not offered as a template", "default" not in keys, str(keys))

            print("== templates ==")
            r = await cx.get("/api/projects/templates")
            keys = [x["key"] for x in r.json()["templates"]]
            check("test template listed with params", "test_demo" in keys and
                  any("apps" in x["params"] for x in r.json()["templates"]), r.text)
            check("every template carries a sentence field (the landing screen's starters)",
                  all("sentence" in x for x in r.json()["templates"]), r.text[:200])

            print("== create ==")
            r = await cx.post("/api/projects", json={"template": "nope", "params": {}})
            check("unknown template -> 400", r.status_code == 400, r.text)
            r = await cx.post("/api/projects", json={"template": "test_demo", "params": {}})
            check("missing required param -> 400", r.status_code == 400, r.text)
            r = await cx.post("/api/projects", json={
                "template": "test_demo", "name": "demo one", "params": {"apps": ["ui", "api"]}})
            check("create -> 201", r.status_code == 201, r.text)
            inst = r.json(); uid = inst["id"]
            kinds = sorted(o["kind"] for o in inst["objects"])
            check("five objects, no view", kinds == ["agent", "mcp_server", "source", "source",
                                                     "trigger"], str(kinds))
            check("status active, no error, not the default",
                  inst["status"] == "active" and not inst["last_error"] and inst["default"] is False)
            r = await cx.post("/api/projects", json={
                "template": "test_demo", "name": "demo one", "params": {"apps": ["x"]}})
            check("duplicate name -> 400", r.status_code == 400, r.text)
            ps = (await cx.get("/api/projects")).json()["projects"]
            check("the default project is listed first", ps[0]["id"] == did and ps[1]["id"] == uid,
                  str([p["name"] for p in ps]))

            print("== objects are ordinary and carry their project ==")
            srcs = {s["name"]: s for s in (await cx.get("/api/sources")).json()}
            check("sources exist, were created by the project and are its members",
                  srcs["t_ui"]["owned_by"] == uid and srcs["t_api"]["owned_by"] == uid
                  and srcs["t_ui"]["projects"] == [uid] and not srcs["t_ui"]["customized"],
                  str({k: (v.get("owned_by"), v.get("projects")) for k, v in srcs.items()}))
            trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
            t = trig["t_trigger"]
            check("trigger carries its project, sources, filters and key_field",
                  t["project"] == uid and t["owned_by"] == uid and t["sources"] == ["t_ui", "t_api"]
                  and t["filters"] == [] and t["key_field"] == "app" and "view" not in t, str(t))
            ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
            check("agent in the project and disabled", ag["t_agent"]["project"] == uid
                  and ag["t_agent"]["enabled"] is False)
            mcp = {m["name"]: m for m in (await cx.get("/api/mcp-servers")).json()["servers"]}
            check("mcp server in the project", mcp["t_mcp"]["project"] == uid)
            # the source works like any source: ingest lands
            r = await cx.post(f"/ingest/{srcs['t_ui']['ingest_key']}", json={"msg": "hello"})
            check("owned source ingests", r.status_code == 202, r.text)
            r = await cx.get(f"/api/projects/{uid}/summary")
            check("summary merges template summary + log",
                  r.json().get("apps") == ["ui", "api"] and
                  any(l["action"] == "created" for l in r.json()["log"]), r.text[:300])
            r = await cx.post("/read", json={"selector": {"app": "ui"}, "window": "1h", "project": uid})
            check("/read narrowed to a project reads its sources",
                  r.status_code == 200 and r.json()["sources"] == ["t_ui"] and "hello" in r.json()["payload"],
                  r.text[:300])
            r = await cx.post("/read", json={"selector": {"app": "ui"}, "window": "1h", "project": did})
            check("/read narrowed to another project sees none of them",
                  r.status_code == 200 and r.json()["count"] == 0, r.text[:300])

            print("== TR-226 API traps ==")
            r = await cx.get("/api/agents/builtin/t_agent")   # a path that has never existed
            check("unknown /api path -> JSON 404, not the SPA",
                  r.status_code == 404 and "unknown API path" in r.text, f"{r.status_code} {r.text[:80]}")
            r = await cx.get("/api/nope/deeper")
            check("another unknown /api path -> 404", r.status_code == 404, r.text[:80])
            r = await cx.get("/api/sources/discover")
            check("GET discover -> 405 with the hint",
                  r.status_code == 405 and "POST-only" in r.text, f"{r.status_code} {r.text[:80]}")
            for method, path in (("get", "/api/views"), ("post", "/api/views"),
                                 ("put", "/api/views/t_view"), ("post", "/query"),
                                 ("post", "/derive")):
                r = await getattr(cx, method)(path, **({"json": {}} if method != "get" else {}))
                check(f"{method.upper()} {path} -> 404, views were removed",
                      r.status_code == 404 and "views were removed" in r.text, f"{r.status_code} {r.text[:80]}")
            body = {"name": "t_trigger", "sources": ["t_ui", "t_api"], "key_field": "app",
                    "condition": COND, "emit": {"kind": "demo"}, "cooldown": "1m"}
            r = await cx.put("/api/triggers/t_trigger", json={**body, "name": "other"})
            check("trigger rename attempt still 400", r.status_code == 400, r.text[:120])
            r = await cx.put("/api/triggers/t_trigger", json={**body, "view": "v"})
            check("a trigger naming a view -> 400 with the reason",
                  r.status_code == 400 and "views were removed" in r.text
                  and "sources" in r.text, r.text[:200])
            r = await cx.post("/api/triggers", json={**body, "name": "no_sources", "sources": []})
            check("a trigger with no sources -> 400", r.status_code == 400 and "sources" in r.text,
                  r.text[:200])
            r = await cx.post("/api/triggers", json={**body, "name": "bad_filter",
                                                     "filters": [{"field": "app", "op": "=="}]})
            check("a trigger filter is validated like the old view filter",
                  r.status_code == 400 and "field, op and value" in r.text, r.text[:200])
            r = await cx.post("/api/triggers", json={**body, "name": "nope", "project": "uc_nope"})
            check("a trigger in an unknown project -> 400", r.status_code == 400, r.text[:200])

            print("== customized protection ==")
            r = await cx.put("/api/triggers/t_trigger", json={**body, "sources": ["t_ui"]})
            check("hand edit of an owned trigger is allowed, stays in its project",
                  r.status_code == 200 and r.json()["project"] == uid, r.text)
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["t_trigger"]
            check("edited trigger flagged customized", t["customized"] is True and t["owned_by"] == uid)

            print("== update: add and remove ==")
            r = await cx.put(f"/api/projects/{uid}", json={"params": {"apps": ["ui", "web"]}})
            check("update -> 200", r.status_code == 200, r.text)
            rep = r.json()["report"]
            check("report: created web, deleted api, kept the customized trigger",
                  "source:t_web" in rep["created"] and "source:t_api" in rep["deleted"]
                  and "trigger:t_trigger" in rep["kept"], str(rep))
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("t_api gone, t_web present", "t_api" not in names and "t_web" in names, str(names))
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["t_trigger"]
            check("customized trigger kept the user's sources", t["sources"] == ["t_ui"], str(t))

            print("== repair ==")
            r = await cx.post(f"/api/projects/{uid}/repair", json={"key": "trigger"})
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["t_trigger"]
            check("repair resets a customized trigger to the plan",
                  r.status_code == 200 and sorted(t["sources"]) == ["t_ui", "t_web"]
                  and t["customized"] is False, str(t))
            r = await cx.delete("/api/triggers/t_trigger")
            check("hand delete of an owned trigger is allowed", r.status_code == 200, r.text)
            inst = (await cx.get(f"/api/projects/{uid}")).json()
            miss = {o["key"]: o["missing"] for o in inst["objects"]}
            check("instance reports the trigger missing", miss.get("trigger") is True, str(miss))
            r = await cx.post(f"/api/projects/{uid}/repair", json={"key": "trigger"})
            check("repair -> 200", r.status_code == 200, r.text)
            trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
            check("trigger re-created in the project", trig.get("t_trigger", {}).get("project") == uid,
                  str(list(trig)))

            print("== pause / resume ==")
            r = await cx.post(f"/api/projects/{uid}/pause")
            check("pause -> paused", r.json()["status"] == "paused", r.text)
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["t_trigger"]
            check("owned trigger paused", t["paused"] is True)
            r = await cx.post(f"/api/projects/{uid}/resume")
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["t_trigger"]
            check("resume -> active, trigger running",
                  r.json()["status"] == "active" and t["paused"] is False)
            check("plain pause leaves sources running", not any(x["paused"] for x in (await cx.get("/api/sources")).json()))
            # a source paused by hand before stays paused across pause+sources / resume
            await cx.post("/api/sources/t_web/pause")
            r = await cx.post(f"/api/projects/{uid}/pause", json={"sources": True})
            src = {x["name"]: x for x in (await cx.get("/api/sources")).json()}
            check("pause with sources pauses the project's running sources",
                  r.json()["status"] == "paused" and src["t_ui"]["paused"] is True and src["t_web"]["paused"] is True
                  and r.json()["params"].get("paused_sources") == ["t_ui"], r.text[:300])
            r = await cx.post(f"/api/projects/{uid}/resume")
            src = {x["name"]: x for x in (await cx.get("/api/sources")).json()}
            check("resume brings back only what pause stopped",
                  r.json()["status"] == "active" and src["t_ui"]["paused"] is False and src["t_web"]["paused"] is True
                  and "paused_sources" not in r.json()["params"], r.text[:300])
            await cx.post("/api/sources/t_web/resume")

            print("== all-or-nothing create ==")
            before = {s["name"] for s in (await cx.get("/api/sources")).json()}
            r = await cx.post("/api/projects", json={
                "template": "test_demo", "name": "broken",
                "params": {"apps": ["z"], "prefix": "b", "fail": True}})
            check("invalid plan -> 400", r.status_code == 400, r.text)
            after = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("nothing created by the failed create", before == after, str(after - before))
            bad = [u for u in (await cx.get("/api/projects")).json()["projects"]
                   if u["name"] == "broken"]
            check("failed instance kept with status error + last_error",
                  bad and bad[0]["status"] == "error" and bad[0]["last_error"], str(bad))
            await cx.delete(f"/api/projects/{bad[0]['id']}")

            print("== ownership conflict ==")
            r = await cx.post("/api/projects", json={
                "template": "test_demo", "name": "clash", "params": {"apps": ["ui"]}})
            check("a plan may not take another project's object", r.status_code == 400
                  and "another project" in r.text and "demo one" in r.text, r.text)
            clash = [u for u in (await cx.get("/api/projects")).json()["projects"]
                     if u["name"] == "clash"]
            if clash:
                await cx.delete(f"/api/projects/{clash[0]['id']}")

            print("== triggers, agents and MCP servers belong to one project ==")
            r = await cx.post("/api/sources", json={"name": "loose", "connector": "webhook",
                                                    "config": {"labels": [{"name": "app", "field": "app", "primary": True}]}})
            check("a source created without a project -> the default project",
                  r.status_code == 201 and next(s for s in (await cx.get("/api/sources")).json()
                                                if s["name"] == "loose")["projects"] == [did], r.text)
            r = await cx.post("/api/triggers", json={"name": "loose_t", "sources": ["loose"],
                                                     "condition": COND})
            check("a trigger created without a project -> the default project",
                  r.status_code == 201 and r.json()["project"] == did, r.text)
            r = await cx.post("/api/triggers", json={"name": "cross", "project": uid,
                                                     "sources": ["t_ui", "loose"], "condition": COND})
            check("a trigger in a project over a source outside it -> 201", r.status_code == 201, r.text)
            src = {x["name"]: x for x in (await cx.get("/api/sources")).json()}
            check("the source joined the trigger's project and stays in the default one "
                  "(a default trigger still reads it)",
                  sorted(src["loose"]["projects"]) == sorted([did, uid]), str(src["loose"]["projects"]))
            r = await cx.post("/api/agents/builtin", json={"name": "wrong", "trigger": "t_trigger",
                                                           "prompt": "x"})
            check("an agent in the default project on another project's trigger -> 400",
                  r.status_code == 400 and "project" in r.text, r.text)
            r = await cx.post("/api/agents/builtin", json={"name": "right", "trigger": "loose_t",
                                                           "prompt": "x", "mcp_servers": ["t_mcp"]})
            check("an agent using another project's MCP server -> 400",
                  r.status_code == 400 and "MCP server" in r.text, r.text)
            r = await cx.post("/api/agents/builtin", json={"name": "right", "trigger": "cross",
                                                           "prompt": "x", "project": uid,
                                                           "mcp_servers": ["t_mcp"]})
            check("an agent in its trigger's project with that project's MCP server -> 201",
                  r.status_code == 201 and r.json()["project"] == uid, r.text)
            r = await cx.post("/api/mcp-servers", json={"name": "m_default", "url": "https://example.invalid/d"})
            check("an MCP server created without a project -> the default project",
                  r.status_code == 201 and r.json()["project"] == did, r.text)
            r = await cx.put("/api/triggers/cross", json={"name": "cross", "project": did,
                                                          "sources": ["t_ui", "loose"], "condition": COND})
            check("moving a trigger whose agent uses its project's MCP server -> 400",
                  r.status_code == 400 and "t_mcp" in r.text, r.text)
            r = await cx.put("/api/agents/builtin/right", json={"trigger": "cross", "prompt": "x"})
            check("an agent edit keeps its project", r.status_code == 200, r.text)
            r = await cx.put("/api/triggers/cross", json={"name": "cross", "project": did,
                                                          "sources": ["t_ui", "loose"], "condition": COND})
            check("moving a trigger to another project -> 200", r.status_code == 200, r.text)
            ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
            check("its agent came along", ag["right"]["project"] == did, str(ag["right"]["project"]))
            inst = (await cx.get(f"/api/projects/{uid}")).json()
            check("the old project no longer lists them",
                  not any(o["name"] in ("cross", "right") for o in inst["objects"]),
                  json.dumps(inst["objects"])[:300])
            await cx.delete("/api/agents/builtin/right")
            await cx.delete("/api/triggers/cross")

            print("== sources are shared ==")
            r = await cx.post(f"/api/projects/{uid}/sources", json={"name": "loose"})
            check("add an existing source to a project", r.status_code == 200 and any(
                o["kind"] == "source" and o["name"] == "loose" for o in r.json()["objects"]), r.text[:300])
            r = await cx.post(f"/api/projects/{uid}/sources", json={"name": "ghost"})
            check("add an unknown source -> 404", r.status_code == 404, r.text)
            r = await cx.delete(f"/api/projects/{uid}/sources/t_ui")
            check("remove a source a trigger of the project reads -> 400",
                  r.status_code == 400 and "t_trigger" in r.text, r.text)
            r = await cx.delete(f"/api/projects/{did}/sources/loose")
            check("remove a source the default project's trigger reads -> 400",
                  r.status_code == 400 and "loose_t" in r.text, r.text)
            await cx.delete("/api/triggers/loose_t")
            r = await cx.delete(f"/api/projects/{did}/sources/loose")
            check("remove a source from the default project while another project has it -> 200",
                  r.status_code == 200, r.text)
            r = await cx.delete(f"/api/projects/{uid}/sources/loose")
            check("removing it from its last project sends it back to the default one",
                  r.status_code == 200 and next(s for s in (await cx.get("/api/sources")).json()
                                                if s["name"] == "loose")["projects"] == [did], r.text[:200])
            r = await cx.delete(f"/api/projects/{did}/sources/loose")
            check("the default project will not let go of a source in no other project",
                  r.status_code == 400, r.text)
            await cx.delete("/api/sources/loose")
            r = await cx.get("/catalog")
            check("the catalog lists sources with projects, triggers with sources, and projects",
                  "views" not in r.json() and any(p["id"] == did for p in r.json()["projects"])
                  and all("sources" in t for t in r.json()["triggers"]), r.text[:300])
            r = await cx.get("/api/catalog/dependents", params={"kind": "source", "name": "t_ui"})
            check("dependents of a source: the triggers that read it and their agents",
                  r.json()["dependents"] == [{"kind": "agent", "name": "t_agent"},
                                             {"kind": "trigger", "name": "t_trigger"}], r.text)
            r = await cx.get("/api/catalog/dependents", params={"kind": "view", "name": "x"})
            check("dependents of a view -> 400", r.status_code == 400, r.text)

            print("== export / import round trip ==")
            y = (await cx.get("/api/catalog/export")).text
            import yaml as _yaml
            doc = _yaml.safe_load(y)
            check("export has a projects section with params and no views",
                  "projects:" in y and "template: test_demo" in y and "apps:" in y
                  and "views" not in doc, y[-400:])
            t = next(x for x in doc["triggers"] if x["name"] == "t_trigger")
            check("an exported trigger names its project and sources",
                  t["project"] == "demo one" and t["sources"] == ["t_ui", "t_web"]
                  and "view" not in t, str(t))
            check("the default project is not in the projects section",
                  not any(p["name"] == "Default" for p in doc["projects"]), str(doc["projects"]))
            r = await cx.post("/api/catalog/import", json={"yaml": y, "mode": "merge"})
            check("re-import of the export is idempotent", r.status_code == 200
                  and r.json()["projects"] == 1, r.text)
            ps = (await cx.get("/api/projects")).json()["projects"]
            check("still one instance besides the default", len(ps) == 2, str([p["name"] for p in ps]))
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["t_trigger"]
            check("the trigger is still in its project", t["project"] == uid, str(t))
            r = await cx.post("/api/catalog/import", json={"yaml":
                "projects:\n  - template: test_demo\n    name: yaml one\n    params: {apps: [q], prefix: y}\n"})
            check("import creates a new instance from YAML", r.status_code == 200, r.text)
            ucs = {u["name"]: u for u in (await cx.get("/api/projects")).json()["projects"]}
            check("yaml one exists and owns y_q", "yaml one" in ucs and
                  any(o["name"] == "y_q" for o in ucs["yaml one"]["objects"]), str(list(ucs)))
            r = await cx.post("/api/catalog/import", json={"yaml": (
                "triggers:\n  - name: typo\n    project: no such project\n    sources: [y_q]\n"
                "    condition: {aggregate: count, predicate: '> 0', window: 5m}\n")})
            check("an import naming an unknown project is refused",
                  r.status_code == 400 and "unknown project" in r.text, r.text[:200])
            r = await cx.post("/api/catalog/import", json={"yaml": (
                "triggers:\n  - name: y_extra\n    project: yaml one\n    sources: [y_q]\n"
                "    condition: {aggregate: count, predicate: '> 0', window: 5m}\n")})
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}.get("y_extra") or {}
            check("an imported trigger lands in the project it names",
                  r.status_code == 200 and t.get("project") == ucs["yaml one"]["id"], r.text[:200])

            print("== pre-1.14 names still work (aliases, two releases) ==")
            r = await cx.post("/api/catalog/import", json={"yaml":
                "usecases:\n  - recipe: test_demo\n    name: old form\n    params: {apps: [o], prefix: old}\n"})
            check("old-form catalog (usecases: + recipe:) imports", r.status_code == 200
                  and r.json()["projects"] == 1, r.text)
            ucs = {u["name"]: u for u in (await cx.get("/api/projects")).json()["projects"]}
            check("old form created the project under the template", ucs.get("old form", {}).get("template") == "test_demo")
            r = await cx.post("/api/catalog/import", json={"yaml":
                "projects:\n  - template: test_demo\n    name: a\n"
                "usecases:\n  - recipe: test_demo\n    name: b\n"})
            check("a document with both sections is rejected", r.status_code == 400 and "usecases:" in r.text, r.text[:200])
            y = (await cx.get("/api/catalog/export")).text
            check("export writes the new form only", "projects:" in y and "usecases:" not in y and "recipe:" not in y)
            r = await cx.get("/api/usecases/recipes")
            check("GET /api/usecases/recipes -> recipes", r.status_code == 200 and "recipes" in r.json(), r.text[:200])
            r = await cx.get("/api/usecases")
            old_list = r.json().get("usecases") or []
            check("GET /api/usecases -> usecases, each with recipe and recipe_title",
                  r.status_code == 200 and old_list and all(u.get("recipe") == u["template"]
                  and u.get("recipe_title") for u in old_list), r.text[:300])
            r = await cx.post("/api/usecases", json={"recipe": "test_demo", "name": "via alias",
                                                     "params": {"apps": ["z"], "prefix": "al"}})
            check("POST /api/usecases with recipe -> 201", r.status_code == 201 and r.json()["template"] == "test_demo", r.text[:200])
            aid = r.json()["id"]
            check("GET /api/usecases/{id}", (await cx.get(f"/api/usecases/{aid}")).status_code == 200)
            check("GET /api/usecases/{id}/summary", (await cx.get(f"/api/usecases/{aid}/summary")).status_code == 200)
            check("POST /api/usecases/{id}/pause", (await cx.post(f"/api/usecases/{aid}/pause")).json()["status"] == "paused")
            check("POST /api/usecases/{id}/resume", (await cx.post(f"/api/usecases/{aid}/resume")).json()["status"] == "active")
            r = await cx.put(f"/api/usecases/{aid}", json={"params": {"apps": ["z", "y"], "prefix": "al"}})
            check("PUT /api/usecases/{id}", r.status_code == 200 and "al_y" in str(r.json()["report"]["created"]), r.text[:200])
            r = await cx.post(f"/api/usecases/{aid}/repair", json={"key": "trigger"})
            check("POST /api/usecases/{id}/repair", r.status_code == 200, r.text[:200])
            r = await cx.post(f"/api/usecases/{aid}/actions/nope", json={})
            check("POST /api/usecases/{id}/actions/{name} reaches the handler", r.status_code == 400, r.text[:200])
            r = await cx.post("/api/usecases/recipes/test_demo/detect")
            check("POST /api/usecases/recipes/{key}/detect", r.status_code == 200, r.text[:200])
            check("DELETE /api/usecases/{id}",
                  (await cx.delete(f"/api/usecases/{aid}?delete_sources=al_z,al_y")).status_code == 200)
            check("aliases are not in the schema",
                  not any(p.startswith("/api/usecases") for p in (await cx.get("/openapi.json")).json()["paths"]))
            await cx.delete(f"/api/projects/{ucs['old form']['id']}?delete_sources=old_o")

            print("== hand-assembled project (template custom) ==")
            check("custom is not offered as a template",
                  "custom" not in [x["key"] for x in (await cx.get("/api/projects/templates")).json()["templates"]])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "empty", "objects": []})
            check("a custom project may start empty", r.status_code == 201 and r.json()["objects"] == [],
                  r.text[:200])
            await cx.delete(f"/api/projects/{r.json()['id']}")
            # free objects to assemble from: a source, a trigger, an enabled agent (all in the
            # default project, since the document names no project)
            r = await cx.post("/api/catalog/import", json={"yaml": (
                "sources:\n  - name: free_src\n    connector: webhook\n    poll: 5s\n"
                "    config: {event_type: log, text_template: '{msg}', labels: [{name: app, const: x, primary: true}]}\n"
                "triggers:\n  - name: free_trigger\n    sources: [free_src]\n    key_field: app\n"
                "    condition: {aggregate: count, predicate: '> 0', window: 5m, group_by: [key_value]}\n"
                "    emit: {kind: demo}\n    cooldown: 1m\n"
                "agents:\n  - name: free_agent\n    trigger: free_trigger\n    prompt: say hi\n    enabled: true\n")})
            check("free objects imported", r.status_code == 200, r.text[:300])
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["free_trigger"]
            check("an imported trigger with no project is in the default project", t["project"] == did, str(t))
            from tares.config import agent_url
            check("free_agent is subscribed", st.subscription_by_url(agent_url("free_agent")) is not None)
            objs = [{"kind": "source", "name": "free_src"}, {"kind": "trigger", "name": "free_trigger"},
                    {"kind": "agent", "name": "free_agent"}]
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine",
                                                     "objects": objs + [{"kind": "trigger", "name": "t_trigger"}]})
            check("a trigger of another project is refused, naming it",
                  r.status_code == 400 and "belongs" in r.text and "demo one" in r.text, r.text[:200])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine",
                                                     "objects": objs + [{"kind": "trigger", "name": "ghost"}]})
            check("a missing object is refused", r.status_code == 400 and "does not exist" in r.text, r.text[:200])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine",
                                                     "objects": objs + [{"kind": "view", "name": "free_view"}]})
            check("a view object is refused with the reason", r.status_code == 400 and "views were removed" in r.text,
                  r.text[:200])
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
            check("nothing moved by the failed creates",
                  t["free_trigger"]["project"] == did and t["t_trigger"]["project"] == uid, str(t))
            r = await cx.post("/api/projects", json={"template": "custom", "objects": objs})
            check("custom create without a name -> 400", r.status_code == 400 and "name" in r.text, r.text[:200])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine",
                                                     "objects": objs + [{"kind": "source", "name": "t_ui"}]})
            check("custom create -> 201, a source of another project is shared, not refused",
                  r.status_code == 201, r.text[:300])
            cid = r.json()["id"]
            check("four objects taken in, none missing or customized",
                  len(r.json()["objects"]) == 4 and not any(o["missing"] or o["customized"] for o in r.json()["objects"]),
                  r.text[:300])
            src = {x["name"]: x for x in (await cx.get("/api/sources")).json()}
            check("the shared source is in both projects and still created by the first",
                  sorted(src["t_ui"]["projects"]) == sorted([uid, cid]) and src["t_ui"]["owned_by"] == uid,
                  str(src["t_ui"]))
            check("the source taken from the default project left it (no default trigger reads it)",
                  src["free_src"]["projects"] == [cid], str(src["free_src"]["projects"]))
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["free_trigger"]
            check("the trigger moved into the custom project", t["project"] == cid, str(t))
            await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            src = {x["name"]: x for x in (await cx.get("/api/sources")).json()}
            check("letting go of the shared source leaves it with its other project",
                  src["t_ui"]["projects"] == [uid], str(src["t_ui"]["projects"]))
            # hand edit an adopted object: no customized flag (there is no planned version)
            r = await cx.put("/api/triggers/free_trigger", json={
                "name": "free_trigger", "sources": ["free_src"], "key_field": "app",
                "condition": COND, "emit": {"kind": "demo"}, "cooldown": "1m"})
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["free_trigger"]
            check("editing an adopted object does not mark it customized, nor move it",
                  r.status_code == 200 and not t["customized"] and t["project"] == cid, r.text[:200])
            # activity: a run and a firing show on the page
            for i in range(22):
                st.start_agent_run(f"run_c{i}", "free_agent", "free_trigger", "d1", "x", "h")
                st.finish_agent_run(f"run_c{i}", "ok" if i else "error", rounds=1)
            from datetime import datetime, timezone
            st.set_fired("free_trigger", "x", datetime.now(timezone.utc))
            sm = (await cx.get(f"/api/projects/{cid}/summary")).json()
            check("summary lists the agent's runs, capped, with totals over all of them",
                  {r["agent"] for r in sm["runs"]} == {"free_agent"} and len(sm["runs"]) == 20
                  and sm["runs_total"] == 22 and sm["runs_ok"] == 21, json.dumps(sm)[:300])
            check("summary lists the trigger with its last firing",
                  sm["triggers"][0]["name"] == "free_trigger" and sm["triggers"][0]["last_fired"]
                  and sm["trigger_last_fired"], json.dumps(sm.get("triggers"))[:200])
            r = await cx.post(f"/api/projects/{cid}/repair", json={"key": "trigger:free_trigger"})
            check("repair is refused", r.status_code == 400, r.text[:200])
            # pause remembers which agents were on; resume brings exactly those back
            await cx.post("/api/catalog/import", json={"yaml": (
                "triggers:\n  - name: off_trigger\n    sources: [free_src]\n    paused: true\n"
                "    condition: {aggregate: count, predicate: '> 0', window: 5m, group_by: [key_value]}\n"
                "    emit: {kind: demo}\n    cooldown: 1m\n")})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "trigger", "name": "off_trigger"}]})
            check("a paused trigger taken in", r.status_code == 200, r.text[:200])
            r = await cx.post(f"/api/projects/{cid}/pause")
            check("pause remembers only the trigger that was on", r.json()["params"]["resume_triggers"] == ["free_trigger"], r.text[:300])
            check("pause unsubscribes the agent and pauses the trigger", r.json()["status"] == "paused"
                  and st.subscription_by_url(agent_url("free_agent")) is None
                  and next(t for t in (await cx.get("/api/triggers")).json() if t["name"] == "free_trigger")["paused"])
            r = await cx.post(f"/api/projects/{cid}/pause")
            check("a second pause keeps the remembered agents", r.json()["params"]["resume_agents"] == ["free_agent"], r.text[:300])
            r = await cx.post(f"/api/projects/{cid}/resume")
            check("resume re-subscribes the agent", r.json()["status"] == "active"
                  and st.subscription_by_url(agent_url("free_agent")) is not None
                  and "resume_agents" not in r.json()["params"], r.text[:300])
            trs = {t["name"]: t["paused"] for t in (await cx.get("/api/triggers")).json()}
            check("resume unpauses only the trigger that was on", trs["free_trigger"] is False and trs["off_trigger"] is True, str(trs))
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            t = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}["off_trigger"]
            check("a released trigger goes back to the default project",
                  r.status_code == 200 and t["project"] == did, str(t))
            await cx.delete("/api/triggers/off_trigger")
            # an agent on a trigger of this project joins it on creation; while paused, a second
            # pause keeps what the first one remembered
            await cx.post(f"/api/projects/{cid}/pause")
            r = await cx.post("/api/agents/builtin", json={"name": "free_agent2", "trigger": "free_trigger",
                                                           "prompt": "say hi", "project": cid})
            check("an agent created on the paused project's trigger", r.status_code == 201, r.text[:200])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            check("dropping an agent whose trigger stays is refused",
                  r.status_code == 400 and "free_agent2" in r.text, r.text[:300])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "agent", "name": "free_agent2"}]})
            check("the edit that keeps it is fine and the remembered agents are unchanged",
                  r.status_code == 200 and r.json()["params"]["resume_agents"] == ["free_agent"], r.text[:300])
            await cx.post(f"/api/projects/{cid}/resume")
            check("resume re-subscribes only what was on",
                  st.subscription_by_url(agent_url("free_agent")) is not None
                  and st.subscription_by_url(agent_url("free_agent2")) is None)
            await cx.delete("/api/agents/builtin/free_agent2")
            await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            # a same-name object recreated by hand and claimed elsewhere is not released by us
            r = await cx.post("/api/mcp-servers", json={"name": "lost_mcp", "url": "https://example.invalid/a"})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "lost_mcp"}]})
            check("mcp server taken in", r.status_code == 200 and "mcp_server:lost_mcp" in r.json()["report"]["added"], r.text[:200])
            r = await cx.delete("/api/mcp-servers/lost_mcp")
            check("hand delete of the adopted mcp server", r.status_code == 200, r.text[:200])
            await cx.post("/api/mcp-servers", json={"name": "lost_mcp", "url": "https://example.invalid/b"})
            r = await cx.post("/api/projects", json={"template": "custom", "name": "claimer",
                                                     "objects": [{"kind": "mcp_server", "name": "lost_mcp"}]})
            check("the recreated mcp server now belongs to another project", r.status_code == 201, r.text[:300])
            claimer = r.json().get("id")
            got = (await cx.get(f"/api/projects/{cid}")).json()
            check("the lost object shows as missing on the original project",
                  next(o for o in got["objects"] if o["name"] == "lost_mcp")["missing"], json.dumps(got["objects"])[:300])
            y = (await cx.get("/api/catalog/export")).text
            doc = _yaml.safe_load(y)
            mine_exp = next(u for u in doc["projects"] if u["name"] == "mine")
            check("export leaves the lost object out of the original project",
                  not any(o["name"] == "lost_mcp" for o in mine_exp["objects"])
                  and any(o["name"] == "lost_mcp" for o in next(u for u in doc["projects"] if u["name"] == "claimer")["objects"]),
                  json.dumps(doc["projects"])[:400])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            m = next(x for x in (await cx.get("/api/mcp-servers")).json()["servers"] if x["name"] == "lost_mcp")
            check("releasing a lost object leaves the new owner's ownership alone",
                  r.status_code == 200 and m["project"] == claimer, r.text[:200])
            r = await cx.delete(f"/api/projects/{claimer}")
            check("deleting a project deletes its MCP server",
                  r.status_code == 200 and "mcp_server:lost_mcp" in r.json()["deleted"]
                  and not any(x["name"] == "lost_mcp" for x in (await cx.get("/api/mcp-servers")).json()["servers"]),
                  r.text[:200])
            # an object deleted by hand and recreated under the same name sits in the default
            # project; an edit that still lists it takes it back
            await cx.post("/api/mcp-servers", json={"name": "lost_mcp", "url": "https://example.invalid/c"})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "lost_mcp"}]})
            await cx.delete("/api/mcp-servers/lost_mcp")
            await cx.post("/api/mcp-servers", json={"name": "lost_mcp", "url": "https://example.invalid/d"})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "lost_mcp"}]})
            m = next(x for x in (await cx.get("/api/mcp-servers")).json()["servers"] if x["name"] == "lost_mcp")
            check("a recreated object in the default project is reclaimed by the edit",
                  r.status_code == 200 and r.json()["report"]["reclaimed"] == ["mcp_server:lost_mcp"] and m["project"] == cid,
                  r.text[:300])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            await cx.delete("/api/mcp-servers/lost_mcp")
            # edit: drop the trigger (its agent goes with it), add an mcp server
            r = await cx.post("/api/mcp-servers", json={"name": "free_mcp", "url": "https://example.invalid/mcp"})
            check("free mcp server created", r.status_code in (200, 201), r.text[:200])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs[1:] + [{"kind": "mcp_server", "name": "free_mcp"}]})
            check("dropping a source a staying trigger reads is refused",
                  r.status_code == 400 and "free_trigger" in r.text, r.text[:200])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": [objs[0], objs[2], {"kind": "mcp_server", "name": "free_mcp"}]})
            check("dropping a trigger whose agent stays is refused",
                  r.status_code == 400 and "free_agent" in r.text, r.text[:300])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs[:1] + [{"kind": "mcp_server", "name": "free_mcp"}]})
            check("edit releases the trigger with its agent and takes the mcp server in", r.status_code == 200
                  and sorted(r.json()["report"]["released"]) == ["agent:free_agent", "trigger:free_trigger"]
                  and r.json()["report"]["added"] == ["mcp_server:free_mcp"], r.text[:300])
            ag = next(x for x in (await cx.get("/api/agents/builtin")).json()["agents"] if x["name"] == "free_agent")
            check("the released agent still exists, in the default project and still subscribed",
                  ag["project"] == did and st.subscription_by_url(agent_url("free_agent")) is not None, str(ag["project"]))
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "free_mcp"}]})
            check("taking the trigger and agent back in", r.status_code == 200, r.text[:300])
            y = (await cx.get("/api/catalog/export")).text
            check("export writes the object list for a custom project",
                  "template: custom" in y and "objects:" in y and "free_mcp" in y, y[-500:])
            r = await cx.post("/api/catalog/import", json={"yaml": y})
            check("re-import of the export is a no-op", r.status_code == 200 and
                  len([u for u in (await cx.get("/api/projects")).json()["projects"] if u["name"] == "mine"]) == 1, r.text[:200])
            await cx.post(f"/api/projects/{cid}/pause")
            r = await cx.delete(f"/api/projects/{cid}?delete_sources=free_src&purge_events=true")
            check("delete takes the triggers, agents and MCP servers, and the named source",
                  r.status_code == 200
                  and sorted(r.json()["deleted"]) == ["agent:free_agent", "mcp_server:free_mcp",
                                                      "source:free_src", "trigger:free_trigger"]
                  and r.json()["kept"] == [], r.text[:300])
            names = {x["name"] for x in (await cx.get("/api/sources")).json()}
            check("the source is gone", "free_src" not in names, str(names))
            # a custom project whose only object is gone keeps an empty list in the export
            r = await cx.post("/api/mcp-servers", json={"name": "solo_mcp", "url": "https://example.invalid/s"})
            r = await cx.post("/api/projects", json={"template": "custom", "name": "solo",
                                                     "objects": [{"kind": "mcp_server", "name": "solo_mcp"}]})
            solo = r.json()["id"]
            await cx.delete("/api/mcp-servers/solo_mcp")
            doc = _yaml.safe_load((await cx.get("/api/catalog/export")).text)
            check("export keeps a custom project with nothing left, as an empty project",
                  next(u for u in doc["projects"] if u["name"] == "solo")["objects"] == [], str(doc["projects"]))
            await cx.delete(f"/api/projects/{solo}")

            print("== delete ==")
            # a source another project uses is kept and reported
            r = await cx.post("/api/projects", json={"template": "custom", "name": "sharer",
                                                     "objects": [{"kind": "source", "name": "t_web"}]})
            sharer = r.json()["id"]
            r = await cx.delete(f"/api/projects/{uid}?purge_events=true&delete_sources=t_ui,t_web")
            check("delete -> ok", r.status_code == 200 and r.json()["ok"], r.text)
            check("delete reports the source another project uses as kept",
                  r.json()["kept"] == ["t_web"] and "source:t_ui" in r.json()["deleted"], r.text)
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("the unshared source is removed, the shared one stays", "t_ui" not in names
                  and "t_web" in names, str(names))
            trig = {t["name"] for t in (await cx.get("/api/triggers")).json()}
            check("owned trigger removed", "t_trigger" not in trig)
            check("owned agent and MCP server removed",
                  not any(a["name"] == "t_agent" for a in (await cx.get("/api/agents/builtin")).json()["agents"])
                  and not any(m["name"] == "t_mcp" for m in (await cx.get("/api/mcp-servers")).json()["servers"]))
            check("instance gone", (await cx.get(f"/api/projects/{uid}")).status_code == 404)
            check("unknown id -> 404", (await cx.delete("/api/projects/uc_nope")).status_code == 404)
            r = await cx.delete(f"/api/projects/{sharer}")
            src = next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "t_web")
            check("a source left in no project goes to the default one",
                  r.status_code == 200 and r.json()["released"] == ["source:t_web"]
                  and src["projects"] == [did], r.text[:200])
            await cx.delete("/api/sources/t_web")
            r = await cx.delete(f"/api/projects/{ucs['yaml one']['id']}?delete_sources=nope")
            check("delete_sources naming a source outside the project -> 400", r.status_code == 400, r.text)
            # leave the catalog empty so the next boot seeds from YAML (the store stays open, so the
            # file cannot be removed here)
            await cx.delete(f"/api/projects/{ucs['yaml one']['id']}?delete_sources=y_q")
            check("catalog empty again", (await cx.get("/api/sources")).json() == []
                  and (await cx.get("/api/triggers")).json() == [], (await cx.get("/api/sources")).text[:200])

    print("== YAML seed on an empty catalog ==")
    with open(CATALOG, "w") as f:
        f.write("projects:\n  - template: test_demo\n    name: seeded\n"
                "    params: {apps: [a, b], prefix: s}\n")
    app2 = make_app()
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                     base_url="http://test") as cx:
            ucs = [u for u in (await cx.get("/api/projects")).json()["projects"] if not u["default"]]
            check("seeded instance exists", len(ucs) == 1 and ucs[0]["name"] == "seeded", str(ucs))
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("seeded objects exist and are owned", {"s_a", "s_b"} <= names, str(names))
            ps = (await cx.get("/api/projects")).json()["projects"]
            check("the default project survived the restart, the same one",
                  ps[0]["default"] and ps[0]["id"] == did, str(ps[0]))
    os.remove(CATALOG)
    unregister("test_demo")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
