"""End-to-end test for the project framework (templates, instances, ownership, engine, API, YAML).

Run: .venv/bin/python tests/test_projects.py   (no external services needed)

Uses a tests-only template (two webhook sources, a view, a trigger, an agent, an MCP server) so it
exercises every object kind without depending on a real template.
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
    description = "two webhook sources keyed by app, one view, one trigger, one agent, one mcp server"
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
        objs.append(PlannedObject("view", "view", {
            "name": f"{p}_view", "key_field": "app",
            "sources": [f"{p}_{a}" for a in params["apps"]]}))
        cond = {"aggregate": "count", "predicate": "> 0" if not params["fail"] else "nope",
                "window": "5m", "group_by": ["key_value"]}
        objs.append(PlannedObject("trigger", "trigger", {
            "name": f"{p}_trigger", "view": f"{p}_view", "condition": cond,
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


async def main():
    for p in (DB, DB + ".wal", CATALOG):
        if os.path.exists(p):
            os.remove(p)

    from tares.daemon import make_app
    app = make_app()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:

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
            check("six objects owned", kinds == ["agent", "mcp_server", "source", "source",
                                                 "trigger", "view"], str(kinds))
            check("status active, no error", inst["status"] == "active" and not inst["last_error"])
            r = await cx.post("/api/projects", json={
                "template": "test_demo", "name": "demo one", "params": {"apps": ["x"]}})
            check("duplicate name -> 400", r.status_code == 400, r.text)

            print("== objects are ordinary and carry owned_by ==")
            srcs = {s["name"]: s for s in (await cx.get("/api/sources")).json()}
            check("sources exist and are owned", srcs["t_ui"]["owned_by"] == uid
                  and srcs["t_api"]["owned_by"] == uid and not srcs["t_ui"]["customized"],
                  str({k: (v.get("owned_by"), v.get("customized")) for k, v in srcs.items()}))
            views = {v["name"]: v for v in (await cx.get("/api/views")).json()}
            check("view owned", views["t_view"]["owned_by"] == uid)
            trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
            check("trigger owned", trig["t_trigger"]["owned_by"] == uid)
            ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
            check("agent owned and disabled", ag["t_agent"]["owned_by"] == uid
                  and ag["t_agent"]["enabled"] is False)
            mcp = {m["name"]: m for m in (await cx.get("/api/mcp-servers")).json()["servers"]}
            check("mcp server owned", mcp["t_mcp"]["owned_by"] == uid)
            # the source works like any source: ingest lands
            r = await cx.post(f"/ingest/{srcs['t_ui']['ingest_key']}", json={"msg": "hello"})
            check("owned source ingests", r.status_code == 202, r.text)
            r = await cx.get(f"/api/projects/{uid}/summary")
            check("summary merges template summary + log",
                  r.json().get("apps") == ["ui", "api"] and
                  any(l["action"] == "created" for l in r.json()["log"]), r.text[:300])

            print("== TR-226 API traps ==")
            r = await cx.get("/api/agents/builtin/t_agent")   # a path that has never existed
            check("unknown /api path -> JSON 404, not the SPA",
                  r.status_code == 404 and "unknown API path" in r.text, f"{r.status_code} {r.text[:80]}")
            r = await cx.get("/api/nope/deeper")
            check("another unknown /api path -> 404", r.status_code == 404, r.text[:80])
            r = await cx.get("/api/sources/discover")
            check("GET discover -> 405 with the hint",
                  r.status_code == 405 and "POST-only" in r.text, f"{r.status_code} {r.text[:80]}")
            r = await cx.put("/api/views/t_view", json={
                "key_field": "app", "sources": ["t_ui", "t_api"]})
            check("PUT view without body name works", r.status_code == 200, r.text[:120])
            r = await cx.put("/api/views/t_view", json={
                "name": "other", "key_field": "app", "sources": ["t_ui"]})
            check("rename attempt still 400", r.status_code == 400, r.text[:120])
            r = await cx.post("/api/views", json={"key_field": "app", "sources": ["t_ui"]})
            check("create view without name -> 400", r.status_code == 400, r.text[:120])

            print("== customized protection ==")
            body = {**views["t_view"], "sources": ["t_ui"]}
            r = await cx.put("/api/views/t_view", json={"name": "t_view", "key_field": "app",
                                                        "sources": ["t_ui"], "filters": []})
            check("hand edit of an owned view is allowed", r.status_code == 200, r.text)
            v = {v["name"]: v for v in (await cx.get("/api/views")).json()}["t_view"]
            check("edited view flagged customized", v["customized"] is True and v["owned_by"] == uid)

            print("== update: add and remove ==")
            r = await cx.put(f"/api/projects/{uid}", json={"params": {"apps": ["ui", "web"]}})
            check("update -> 200", r.status_code == 200, r.text)
            rep = r.json()["report"]
            check("report: created web, deleted api, kept view",
                  "source:t_web" in rep["created"] and "source:t_api" in rep["deleted"]
                  and "view:t_view" in rep["kept"], str(rep))
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("t_api gone, t_web present", "t_api" not in names and "t_web" in names, str(names))
            v = {v["name"]: v for v in (await cx.get("/api/views")).json()}["t_view"]
            check("customized view kept the user's sources", v["sources"] == ["t_ui"], str(v))

            print("== repair ==")
            r = await cx.delete("/api/triggers/t_trigger")
            check("hand delete of an owned trigger is allowed", r.status_code == 200, r.text)
            inst = (await cx.get(f"/api/projects/{uid}")).json()
            miss = {o["key"]: o["missing"] for o in inst["objects"]}
            check("instance reports the trigger missing", miss.get("trigger") is True, str(miss))
            r = await cx.post(f"/api/projects/{uid}/repair", json={"key": "trigger"})
            check("repair -> 200", r.status_code == 200, r.text)
            trig = {t["name"] for t in (await cx.get("/api/triggers")).json()}
            check("trigger re-created", "t_trigger" in trig, str(trig))
            r = await cx.post(f"/api/projects/{uid}/repair", json={"key": "view"})
            v = {v["name"]: v for v in (await cx.get("/api/views")).json()}["t_view"]
            check("repair resets a customized view to the plan",
                  sorted(v["sources"]) == ["t_ui", "t_web"] and v["customized"] is False, str(v))

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
                  and "another project" in r.text, r.text)
            clash = [u for u in (await cx.get("/api/projects")).json()["projects"]
                     if u["name"] == "clash"]
            if clash:
                await cx.delete(f"/api/projects/{clash[0]['id']}")

            print("== export / import round trip ==")
            y = (await cx.get("/api/catalog/export")).text
            check("export has a projects section with params",
                  "projects:" in y and "template: test_demo" in y and "apps:" in y, y[-400:])
            r = await cx.post("/api/catalog/import", json={"yaml": y, "mode": "merge"})
            check("re-import of the export is idempotent", r.status_code == 200
                  and r.json()["projects"] == 1, r.text)
            check("still one instance", len((await cx.get("/api/projects")).json()["projects"]) == 1)
            r = await cx.post("/api/catalog/import", json={"yaml":
                "projects:\n  - template: test_demo\n    name: yaml one\n    params: {apps: [q], prefix: y}\n"})
            check("import creates a new instance from YAML", r.status_code == 200, r.text)
            ucs = {u["name"]: u for u in (await cx.get("/api/projects")).json()["projects"]}
            check("yaml one exists and owns y_q", "yaml one" in ucs and
                  any(o["name"] == "y_q" for o in ucs["yaml one"]["objects"]), str(list(ucs)))

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
            r = await cx.post(f"/api/usecases/{aid}/repair", json={"key": "view"})
            check("POST /api/usecases/{id}/repair", r.status_code == 200, r.text[:200])
            r = await cx.post(f"/api/usecases/{aid}/actions/nope", json={})
            check("POST /api/usecases/{id}/actions/{name} reaches the handler", r.status_code == 400, r.text[:200])
            r = await cx.post("/api/usecases/recipes/test_demo/detect")
            check("POST /api/usecases/recipes/{key}/detect", r.status_code == 200, r.text[:200])
            check("DELETE /api/usecases/{id}", (await cx.delete(f"/api/usecases/{aid}")).status_code == 200)
            check("aliases are not in the schema",
                  not any(p.startswith("/api/usecases") for p in (await cx.get("/openapi.json")).json()["paths"]))
            await cx.delete(f"/api/projects/{ucs['old form']['id']}")

            print("== hand-assembled project (template custom) ==")
            st = app.state.store
            check("custom is not offered as a template",
                  "custom" not in [x["key"] for x in (await cx.get("/api/projects/templates")).json()["templates"]])
            # free objects to assemble from: a source, a view, a trigger, an enabled agent
            r = await cx.post("/api/catalog/import", json={"yaml": (
                "sources:\n  - name: free_src\n    connector: webhook\n    poll: 5s\n"
                "    config: {event_type: log, text_template: '{msg}', labels: [{name: app, const: x, primary: true}]}\n"
                "views:\n  - name: free_view\n    key_field: app\n    sources: [free_src]\n"
                "triggers:\n  - name: free_trigger\n    view: free_view\n"
                "    condition: {aggregate: count, predicate: '> 0', window: 5m, group_by: [key_value]}\n"
                "    emit: {kind: demo}\n    cooldown: 1m\n"
                "agents:\n  - name: free_agent\n    trigger: free_trigger\n    prompt: say hi\n    enabled: true\n")})
            check("free objects imported", r.status_code == 200, r.text[:300])
            from tares.config import agent_url
            check("free_agent is subscribed", st.subscription_by_url(agent_url("free_agent")) is not None)
            objs = [{"kind": "source", "name": "free_src"}, {"kind": "view", "name": "free_view"},
                    {"kind": "trigger", "name": "free_trigger"}, {"kind": "agent", "name": "free_agent"}]
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine",
                                                     "objects": objs + [{"kind": "source", "name": "t_ui"}]})
            check("an object owned by another project is refused", r.status_code == 400 and "belongs" in r.text, r.text[:200])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine",
                                                     "objects": objs + [{"kind": "view", "name": "ghost"}]})
            check("a missing object is refused", r.status_code == 400 and "does not exist" in r.text, r.text[:200])
            srcs = {x["name"]: x["owned_by"] for x in (await cx.get("/api/sources")).json()}
            check("nothing adopted by the failed creates, and the other project's source is still its",
                  srcs["free_src"] is None and srcs["t_ui"] == uid, str(srcs))
            r = await cx.post("/api/projects", json={"template": "custom", "objects": objs})
            check("custom create without a name -> 400", r.status_code == 400 and "name" in r.text, r.text[:200])
            r = await cx.post("/api/projects", json={"template": "custom", "name": "mine", "objects": objs})
            check("custom create -> 201", r.status_code == 201, r.text[:300])
            cid = r.json()["id"]
            check("four objects adopted, none missing or customized",
                  len(r.json()["objects"]) == 4 and not any(o["missing"] or o["customized"] for o in r.json()["objects"]),
                  r.text[:300])
            src = next(x for x in (await cx.get("/api/sources")).json() if x["name"] == "free_src")
            check("source carries the ownership badge", src["owned_by"] == cid)
            # hand edit an adopted object: no customized flag (there is no planned version)
            r = await cx.put("/api/views/free_view", json={"name": "free_view", "key_field": "app", "sources": ["free_src"], "filters": []})
            v = next(x for x in (await cx.get("/api/views")).json() if x["name"] == "free_view")
            check("editing an adopted object does not mark it customized", r.status_code == 200 and not v["customized"], r.text[:200])
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
            r = await cx.post(f"/api/projects/{cid}/repair", json={"key": "view:free_view"})
            check("repair is refused", r.status_code == 400, r.text[:200])
            # pause remembers which agents were on; resume brings exactly those back
            await cx.post("/api/catalog/import", json={"yaml": (
                "triggers:\n  - name: off_trigger\n    view: free_view\n    paused: true\n"
                "    condition: {aggregate: count, predicate: '> 0', window: 5m, group_by: [key_value]}\n"
                "    emit: {kind: demo}\n    cooldown: 1m\n")})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "trigger", "name": "off_trigger"}]})
            check("a paused trigger adopted", r.status_code == 200, r.text[:200])
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
            await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            await cx.delete("/api/triggers/off_trigger")
            # an edit while paused keeps additions paused; a released agent drops out of the resume list
            r = await cx.post("/api/catalog/import", json={"yaml": (
                "agents:\n  - name: free_agent2\n    trigger: free_trigger\n    prompt: say hi\n    enabled: true\n")})
            r = await cx.post("/api/catalog/import", json={"yaml": (
                "agents:\n  - name: off_agent\n    trigger: free_trigger\n    prompt: say hi\n    enabled: false\n")})
            await cx.post(f"/api/projects/{cid}/pause")
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "agent", "name": "free_agent2"},
                                                                                {"kind": "agent", "name": "off_agent"}]})
            check("an agent that was off when added is not on the resume list",
                  r.status_code == 200 and "off_agent" not in r.json()["params"]["resume_agents"], r.text[:300])
            check("an agent added while paused is unsubscribed and remembered", r.status_code == 200
                  and st.subscription_by_url(agent_url("free_agent2")) is None
                  and sorted(r.json()["params"]["resume_agents"]) == ["free_agent", "free_agent2"], r.text[:300])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            check("releasing while paused leaves the agent as it was and forgets it",
                  r.json()["params"]["resume_agents"] == ["free_agent"]
                  and st.subscription_by_url(agent_url("free_agent2")) is None, r.text[:300])
            await cx.post(f"/api/projects/{cid}/resume")
            check("resume after the paused edit re-subscribes only what was on",
                  st.subscription_by_url(agent_url("free_agent")) is not None
                  and st.subscription_by_url(agent_url("free_agent2")) is None)
            check("it stays off after resume", st.subscription_by_url(agent_url("off_agent")) is None)
            await cx.delete("/api/agents/builtin/free_agent2")
            await cx.delete("/api/agents/builtin/off_agent")
            # a same-name object recreated by hand and claimed elsewhere is not released by us
            r = await cx.post("/api/mcp-servers", json={"name": "lost_mcp", "url": "https://example.invalid/a"})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "lost_mcp"}]})
            check("mcp server adopted", r.status_code == 200 and "mcp_server:lost_mcp" in r.json()["report"]["added"], r.text[:200])
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
            import yaml as _yaml
            doc = _yaml.safe_load(y)
            mine_exp = next(u for u in doc["projects"] if u["name"] == "mine")
            check("export leaves the lost object out of the original project",
                  not any(o["name"] == "lost_mcp" for o in mine_exp["objects"])
                  and any(o["name"] == "lost_mcp" for o in next(u for u in doc["projects"] if u["name"] == "claimer")["objects"]),
                  json.dumps(doc["projects"])[:400])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            m = next(x for x in (await cx.get("/api/mcp-servers")).json()["servers"] if x["name"] == "lost_mcp")
            check("releasing a lost object leaves the new owner's ownership alone",
                  r.status_code == 200 and m["owned_by"] == claimer, r.text[:200])
            await cx.delete(f"/api/projects/{claimer}")
            # an object recreated by hand under the same name, still unowned, is reclaimed on edit
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "lost_mcp"}]})
            await cx.delete("/api/mcp-servers/lost_mcp")
            await cx.post("/api/mcp-servers", json={"name": "lost_mcp", "url": "https://example.invalid/c"})
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs + [{"kind": "mcp_server", "name": "lost_mcp"}]})
            m = next(x for x in (await cx.get("/api/mcp-servers")).json()["servers"] if x["name"] == "lost_mcp")
            check("a recreated unowned object is reclaimed by the edit",
                  r.status_code == 200 and r.json()["report"]["reclaimed"] == ["mcp_server:lost_mcp"] and m["owned_by"] == cid,
                  r.text[:300])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs})
            await cx.delete("/api/mcp-servers/lost_mcp")
            # edit: drop the agent, add an mcp server
            r = await cx.post("/api/mcp-servers", json={"name": "free_mcp", "url": "https://example.invalid/mcp"})
            check("free mcp server created", r.status_code in (200, 201), r.text[:200])
            r = await cx.put(f"/api/projects/{cid}", json={"objects": objs[:3] + [{"kind": "mcp_server", "name": "free_mcp"}]})
            check("edit releases the agent and adopts the mcp server", r.status_code == 200
                  and r.json()["report"]["released"] == ["agent:free_agent"]
                  and r.json()["report"]["added"] == ["mcp_server:free_mcp"], r.text[:300])
            ag = next(x for x in (await cx.get("/api/agents/builtin")).json()["agents"] if x["name"] == "free_agent")
            check("the released agent still exists, unowned and still subscribed",
                  ag["owned_by"] is None and st.subscription_by_url(agent_url("free_agent")) is not None)
            y = (await cx.get("/api/catalog/export")).text
            check("export writes the object list for a custom project",
                  "template: custom" in y and "objects:" in y and "free_mcp" in y, y[-500:])
            r = await cx.post("/api/catalog/import", json={"yaml": y})
            check("re-import of the export is a no-op", r.status_code == 200 and
                  len([u for u in (await cx.get("/api/projects")).json()["projects"] if u["name"] == "mine"]) == 1, r.text[:200])
            await cx.post(f"/api/projects/{cid}/pause")
            r = await cx.delete(f"/api/projects/{cid}")
            check("delete releases, deletes nothing", r.status_code == 200 and r.json()["deleted"] == []
                  and len(r.json()["released"]) == 4, r.text[:200])
            check("deleting a paused project leaves its trigger unpaused",
                  not next(t for t in (await cx.get("/api/triggers")).json() if t["name"] == "free_trigger")["paused"])
            # a custom project whose only object is gone is left out of the export
            r = await cx.post("/api/mcp-servers", json={"name": "solo_mcp", "url": "https://example.invalid/s"})
            r = await cx.post("/api/projects", json={"template": "custom", "name": "solo",
                                                     "objects": [{"kind": "mcp_server", "name": "solo_mcp"}]})
            solo = r.json()["id"]
            await cx.delete("/api/mcp-servers/solo_mcp")
            y = (await cx.get("/api/catalog/export")).text
            check("export skips a custom project with nothing left", "solo" not in y, y[-300:])
            await cx.delete(f"/api/projects/{solo}")
            names = {x["name"] for x in (await cx.get("/api/sources")).json()}
            check("the source is still there, unowned", "free_src" in names and
                  next(x for x in (await cx.get("/api/sources")).json() if x["name"] == "free_src")["owned_by"] is None)
            for path in ("/api/agents/builtin/free_agent", "/api/triggers/free_trigger", "/api/views/free_view",
                         "/api/sources/free_src", "/api/mcp-servers/free_mcp"):
                await cx.delete(path)

            print("== delete ==")
            r = await cx.delete(f"/api/projects/{uid}?purge_events=true")
            check("delete -> ok", r.status_code == 200 and r.json()["ok"], r.text)
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("owned sources removed", not ({"t_ui", "t_web"} & names), str(names))
            trig = {t["name"] for t in (await cx.get("/api/triggers")).json()}
            check("owned trigger removed", "t_trigger" not in trig)
            check("instance gone", (await cx.get(f"/api/projects/{uid}")).status_code == 404)
            check("unknown id -> 404", (await cx.delete("/api/projects/uc_nope")).status_code == 404)
            # leave the catalog empty so the next boot seeds from YAML (the store stays open, so the
            # file cannot be removed here)
            await cx.delete(f"/api/projects/{ucs['yaml one']['id']}")
            check("catalog empty again", (await cx.get("/api/sources")).json() == [])

    print("== YAML seed on an empty catalog ==")
    with open(CATALOG, "w") as f:
        f.write("projects:\n  - template: test_demo\n    name: seeded\n"
                "    params: {apps: [a, b], prefix: s}\n")
    app2 = make_app()
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                     base_url="http://test") as cx:
            ucs = (await cx.get("/api/projects")).json()["projects"]
            check("seeded instance exists", len(ucs) == 1 and ucs[0]["name"] == "seeded", str(ucs))
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("seeded objects exist and are owned", {"s_a", "s_b"} <= names, str(names))
    os.remove(CATALOG)
    unregister("test_demo")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
