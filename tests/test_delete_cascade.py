"""Deleting takes dependents along on request, instead of refusing bottom-up.

GET /api/catalog/dependents lists what stops working if an object goes; DELETE on a source with
cascade=true removes the triggers that read it and their agents too, in order. DELETE on a project
takes its triggers, agents and MCP servers; `delete_sources=a,b` also deletes those of its sources
no other project uses, and keeps (and reports) the ones another project still reads.

Run: .venv/bin/python tests/test_delete_cascade.py
"""
import asyncio
import os

DB = "/tmp/tares-delete-cascade.duckdb"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = "/tmp/tares-delete-cascade.catalog.yaml"
for _p in (DB, DB + ".wal", os.environ["TARES_CATALOG"]):
    if os.path.exists(_p):
        os.remove(_p)

import httpx

from tares.projects import PlannedObject, Template, register

P = F = 0


class CascadeTemplate(Template):
    """Tests only: source -> trigger -> agent under one prefix."""
    key = "test_cascade"
    title = "Test cascade"
    PARAMS = {"prefix": {"type": "string", "default": "t"}}

    def plan(self, params):
        p = params["prefix"]
        return [
            PlannedObject("source", "src", {**src(f"{p}_src")}),
            PlannedObject("trigger", "trig", {"name": f"{p}_trig", "sources": [f"{p}_src"],
                                              "key_field": "service",
                                              "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"},
                                              "emit": {"kind": "x"}, "cooldown": "1m"}),
            PlannedObject("agent", "agent", {"name": f"{p}_agent", "trigger": f"{p}_trig", "prompt": "look", "enabled": False}),
        ]


register(CascadeTemplate())


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def src(name):
    return {"name": name, "connector": "webhook", "poll": "5s",
            "config": {"event_type": "log", "text_template": "{msg}",
                       "labels": [{"name": "service", "field": "service", "primary": True}]}}


async def build(cx, prefix, project=""):
    """source -> two triggers -> one agent, all named with the prefix, in `project`."""
    body = {**src(f"{prefix}_src"), **({"project": project} if project else {})}
    assert (await cx.post("/api/sources", json=body)).status_code == 201
    for t in ("a", "b"):
        r = await cx.post("/api/triggers", json={
            "name": f"{prefix}_trig_{t}", "sources": [f"{prefix}_src"], "project": project,
            "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"},
            "emit": {"kind": "x"}, "cooldown": "1m"})
        assert r.status_code == 201, r.text
    r = await cx.post("/api/agents/builtin", json={
        "name": f"{prefix}_agent", "trigger": f"{prefix}_trig_a", "prompt": "look", "project": project})
    assert r.status_code == 201, r.text


async def names(cx):
    return {
        "source": {s["name"] for s in (await cx.get("/api/sources")).json()},
        "trigger": {t["name"] for t in (await cx.get("/api/triggers")).json()},
        "agent": {a["name"] for a in (await cx.get("/api/agents/builtin")).json()["agents"]},
        "mcp_server": {m["name"] for m in (await cx.get("/api/mcp-servers")).json()["servers"]},
    }


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        print("== dependents ==")
        await build(cx, "w")
        r = await cx.get("/api/catalog/dependents?kind=source&name=w_src")
        deps = [(d["kind"], d["name"]) for d in r.json()["dependents"]]
        ck("a source's dependents: agent, then the triggers that read it (delete order)",
           deps == [("agent", "w_agent"), ("trigger", "w_trig_a"), ("trigger", "w_trig_b")], str(deps))
        r = await cx.get("/api/catalog/dependents?kind=trigger&name=w_trig_a")
        ck("a trigger's dependents: its agents, not itself",
           [(d["kind"], d["name"]) for d in r.json()["dependents"]] == [("agent", "w_agent")], r.text)
        r = await cx.get("/api/catalog/dependents?kind=trigger&name=w_trig_b")
        ck("a trigger with no agent has no dependents", r.json()["dependents"] == [])
        r = await cx.get("/api/catalog/dependents?kind=view&name=x")
        ck("kind view -> 400 (views were removed)", r.status_code == 400)
        r = await cx.get("/api/catalog/dependents?kind=nope&name=x")
        ck("unknown kind -> 400", r.status_code == 400)

        print("== source delete: refuse, then cascade ==")
        r = await cx.delete("/api/sources/w_src")
        ck("without cascade -> 409 naming the triggers", r.status_code == 409 and "w_trig_a" in r.text
           and "w_trig_b" in r.text, r.text)
        r = await cx.delete("/api/sources/w_src?cascade=true")
        ck("with cascade -> 200 listing what went",
           r.status_code == 200 and set(r.json()["deleted"]) == {"agent:w_agent", "trigger:w_trig_a", "trigger:w_trig_b"}, r.text)
        n = await names(cx)
        ck("source, triggers and agent are all gone",
           not ({"w_src"} & n["source"] or {"w_trig_a", "w_trig_b"} & n["trigger"] or {"w_agent"} & n["agent"]), str(n))
        subs = (await cx.get("/api/subscriptions")).json()
        ck("no subscription left behind", not any("w_" in (s.get("trigger") or "") for s in subs), str(subs))

        print("== a shared source: cascade reaches every project's triggers ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "V1", "objects": []})
        v1 = r.json()["id"]
        r = await cx.post("/api/projects", json={"template": "custom", "name": "V2", "objects": []})
        v2 = r.json()["id"]
        await build(cx, "v", project=v1)
        r = await cx.post("/api/triggers", json={
            "name": "v_other", "sources": ["v_src"], "project": v2,
            "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"}})
        ck("a second project's trigger over the same source", r.status_code == 201, r.text)
        src_row = next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "v_src")
        ck("the source is in both projects", sorted(src_row["projects"]) == sorted([v1, v2]),
           str(src_row["projects"]))
        r = await cx.delete("/api/sources/v_src?cascade=true")
        ck("cascade takes the triggers of both projects",
           r.status_code == 200 and set(r.json()["deleted"]) == {"agent:v_agent", "trigger:v_trig_a",
                                                                 "trigger:v_trig_b", "trigger:v_other"}, r.text)
        for uid in (v1, v2):
            await cx.delete(f"/api/projects/{uid}")

        print("== project delete: its triggers, agents and MCP servers go, sources on request ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "P", "objects": []})
        uid = r.json()["id"]
        await build(cx, "p", project=uid)
        r = await cx.post("/api/mcp-servers", json={"name": "p_mcp", "url": "https://example.invalid/m",
                                                    "project": uid})
        ck("an MCP server in the project", r.status_code == 201 and r.json()["project"] == uid, r.text)
        r = await cx.get(f"/api/projects/{uid}")
        ck("the project lists its five objects",
           sorted((o["kind"], o["name"]) for o in r.json()["objects"])
           == [("agent", "p_agent"), ("mcp_server", "p_mcp"), ("source", "p_src"),
               ("trigger", "p_trig_a"), ("trigger", "p_trig_b")], r.text[:400])
        r = await cx.delete(f"/api/projects/{uid}?delete_sources=nope")
        ck("naming a source that is not the project's is refused",
           r.status_code == 400 and "nope" in r.text, r.text)
        ck("the project is untouched after the refusal", (await cx.get(f"/api/projects/{uid}")).status_code == 200)
        r = await cx.delete(f"/api/projects/{uid}")
        ck("without delete_sources: triggers, agent and MCP server go, the source is released",
           r.status_code == 200
           and set(r.json()["deleted"]) == {"agent:p_agent", "trigger:p_trig_a", "trigger:p_trig_b", "mcp_server:p_mcp"}
           and r.json()["released"] == ["source:p_src"] and r.json()["kept"] == [], r.text)
        n = await names(cx)
        default = next(p["id"] for p in (await cx.get("/api/projects")).json()["projects"] if p["default"])
        ck("the released source remains, in the default project, with no creator",
           "p_src" in n["source"] and "p_agent" not in n["agent"] and "p_mcp" not in n["mcp_server"]
           and next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "p_src")["projects"] == [default]
           and not next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "p_src").get("owned_by"),
           str(n))
        subs = (await cx.get("/api/subscriptions")).json()
        ck("no subscription left behind", not any("p_" in (s.get("trigger") or "") for s in subs), str(subs))

        r = await cx.post("/api/projects", json={"template": "custom", "name": "Q", "objects": []})
        uid = r.json()["id"]
        await build(cx, "q", project=uid)
        await cx.post("/ingest/q_src", json={"service": "x", "msg": "hi"})
        r = await cx.delete(f"/api/projects/{uid}?delete_sources=q_src&purge_events=true")
        ck("delete_sources deletes the source no other project uses",
           r.status_code == 200 and "source:q_src" in r.json()["deleted"] and r.json()["released"] == []
           and r.json()["purged_events"] >= 1, r.text)
        n = await names(cx)
        ck("nothing named q_ remains", not any(x.startswith("q_") for k in n.values() for x in k), str(n))
        r = await cx.delete(f"/api/projects/{uid}")
        ck("deleted project -> 404", r.status_code == 404)

        print("== a source another project uses is kept and reported ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "K", "objects": []})
        k = r.json()["id"]
        await build(cx, "k", project=k)
        r = await cx.post("/api/projects", json={"template": "custom", "name": "K2",
                                                 "objects": [{"kind": "source", "name": "k_src"}]})
        k2 = r.json()["id"]
        r = await cx.delete(f"/api/projects/{k}?delete_sources=k_src")
        ck("the shared source is kept and listed under kept",
           r.status_code == 200 and r.json()["kept"] == ["k_src"] and "source:k_src" not in r.json()["deleted"], r.text)
        src_row = next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "k_src")
        ck("it stays in the other project", src_row["projects"] == [k2], str(src_row["projects"]))
        await cx.delete(f"/api/projects/{k2}?delete_sources=k_src")
        ck("deleting the last project that uses it can take it", "k_src" not in (await names(cx))["source"])

        print("== template project delete ==")
        r = await cx.post("/api/projects", json={"template": "test_cascade", "name": "T", "params": {}})
        ck("test template project created", r.status_code == 201, r.text[:200])
        uid = r.json()["id"]
        r = await cx.delete(f"/api/projects/{uid}?delete_sources=none")
        ck("template project, delete_sources=none: trigger and agent go, the source is released",
           r.status_code == 200 and set(r.json()["deleted"]) == {"agent:t_agent", "trigger:t_trig"}
           and r.json()["released"] == ["source:t_src"], r.text[:300])
        n = await names(cx)
        ck("kept source exists, in the default project", "t_src" in n["source"]
           and next(x for x in (await cx.get("/api/sources")).json() if x["name"] == "t_src")["projects"] == [default])
        r = await cx.post("/api/projects", json={"template": "test_cascade", "name": "T2", "params": {"prefix": "u"}})
        uid = r.json()["id"]
        r = await cx.delete(f"/api/projects/{uid}?delete_sources=u_src")
        ck("template project with its source named takes everything",
           r.status_code == 200 and len(r.json()["deleted"]) == 3 and r.json()["released"] == [], r.text[:300])
        r = await cx.post("/api/projects", json={"template": "test_cascade", "name": "T3", "params": {}})
        ck("the released source can be planned again by a new project", r.status_code == 201, r.text[:300])
        r = await cx.post("/api/projects", json={"template": "test_cascade", "name": "T4", "params": {"prefix": "w"}})
        r = await cx.delete(f"/api/projects/{r.json()['id']}")
        ck("template project, no choice given: the source it created goes too",
           r.status_code == 200 and "source:w_src" in r.json()["deleted"] and r.json()["released"] == [],
           r.text[:300])

        await cx.aclose()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
