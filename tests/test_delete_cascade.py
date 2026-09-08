"""Deleting takes dependents along on request, instead of refusing bottom-up.

GET /api/catalog/dependents lists what stops working if an object goes; DELETE on a source or
view with cascade=true removes those too, in order; DELETE on a custom project with
`delete=kind:name,...` deletes the chosen objects and releases the rest, refusing a choice that
leaves something pointing at nothing.

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
    """Tests only: source -> view -> trigger -> agent under one prefix."""
    key = "test_cascade"
    title = "Test cascade"
    PARAMS = {"prefix": {"type": "string", "default": "t"}}

    def plan(self, params):
        p = params["prefix"]
        return [
            PlannedObject("source", "src", {**src(f"{p}_src")}),
            PlannedObject("view", "view", {"name": f"{p}_view", "key_field": "service", "sources": [f"{p}_src"]}),
            PlannedObject("trigger", "trig", {"name": f"{p}_trig", "view": f"{p}_view",
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


async def build(cx, prefix):
    """source -> view -> two triggers -> one agent, all named with the prefix."""
    assert (await cx.post("/api/sources", json=src(f"{prefix}_src"))).status_code == 201
    assert (await cx.post("/api/views", json={"name": f"{prefix}_view", "key_field": "service",
                                              "sources": [f"{prefix}_src"]})).status_code == 201
    for t in ("a", "b"):
        assert (await cx.post("/api/triggers", json={
            "name": f"{prefix}_trig_{t}", "view": f"{prefix}_view",
            "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"},
            "emit": {"kind": "x"}, "cooldown": "1m"})).status_code == 201
    assert (await cx.post("/api/agents/builtin", json={
        "name": f"{prefix}_agent", "trigger": f"{prefix}_trig_a", "prompt": "look"})).status_code == 201


async def names(cx):
    return {
        "source": {s["name"] for s in (await cx.get("/api/sources")).json()},
        "view": {v["name"] for v in (await cx.get("/api/views")).json()},
        "trigger": {t["name"] for t in (await cx.get("/api/triggers")).json()},
        "agent": {a["name"] for a in (await cx.get("/api/agents/builtin")).json()["agents"]},
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
        ck("a source's dependents: agent, then triggers, then view (delete order)",
           deps == [("agent", "w_agent"), ("trigger", "w_trig_a"), ("trigger", "w_trig_b"), ("view", "w_view")], str(deps))
        r = await cx.get("/api/catalog/dependents?kind=view&name=w_view")
        ck("a view's dependents: its triggers and their agents, not itself",
           {(d["kind"], d["name"]) for d in r.json()["dependents"]}
           == {("agent", "w_agent"), ("trigger", "w_trig_a"), ("trigger", "w_trig_b")})
        r = await cx.get("/api/catalog/dependents?kind=trigger&name=w_trig_b")
        ck("a trigger with no agent has no dependents", r.json()["dependents"] == [])
        r = await cx.get("/api/catalog/dependents?kind=nope&name=x")
        ck("unknown kind -> 400", r.status_code == 400)

        print("== source delete: refuse, then cascade ==")
        r = await cx.delete("/api/sources/w_src")
        ck("without cascade -> 409 naming the view", r.status_code == 409 and "w_view" in r.text, r.text)
        r = await cx.delete("/api/sources/w_src?cascade=true")
        ck("with cascade -> 200 listing what went",
           r.status_code == 200 and set(r.json()["deleted"]) == {"agent:w_agent", "trigger:w_trig_a", "trigger:w_trig_b", "view:w_view"}, r.text)
        n = await names(cx)
        ck("source, view, triggers and agent are all gone",
           not ({"w_src"} & n["source"] or {"w_view"} & n["view"] or {"w_trig_a", "w_trig_b"} & n["trigger"] or {"w_agent"} & n["agent"]), str(n))
        subs = (await cx.get("/api/subscriptions")).json()
        ck("no subscription left behind", not any("w_" in (s.get("trigger") or "") for s in subs), str(subs))

        print("== view delete: cascade ==")
        await build(cx, "v")
        r = await cx.delete("/api/views/v_view")
        ck("without cascade -> 409", r.status_code == 409, r.text)
        r = await cx.delete("/api/views/v_view?cascade=true")
        ck("with cascade -> 200", r.status_code == 200 and len(r.json()["deleted"]) == 3, r.text)
        n = await names(cx)
        ck("source stays, view and its triggers and agent go",
           "v_src" in n["source"] and "v_view" not in n["view"] and not {"v_trig_a", "v_trig_b"} & n["trigger"] and "v_agent" not in n["agent"], str(n))

        print("== custom project delete: pick what goes ==")
        await build(cx, "p")
        objects = [{"kind": "source", "name": "p_src"}, {"kind": "view", "name": "p_view"},
                   {"kind": "trigger", "name": "p_trig_a"}, {"kind": "trigger", "name": "p_trig_b"},
                   {"kind": "agent", "name": "p_agent"}]
        r = await cx.post("/api/projects", json={"template": "custom", "name": "P", "objects": objects})
        ck("custom project owns the five", r.status_code == 201 and len(r.json()["objects"]) == 5, r.text)
        uid = r.json()["id"]
        r = await cx.delete(f"/api/projects/{uid}?delete=view:p_view")
        ck("a pick whose dependents stay is refused by name",
           r.status_code == 400 and "p_trig_a" in r.text, r.text)
        ck("the project is untouched after the refusal", (await cx.get(f"/api/projects/{uid}")).status_code == 200)
        r = await cx.delete(f"/api/projects/{uid}?delete=agent:p_agent,trigger:p_trig_a")
        ck("a consistent pick deletes those and releases the rest",
           r.status_code == 200 and set(r.json()["deleted"]) == {"agent:p_agent", "trigger:p_trig_a"}
           and set(r.json()["released"]) == {"source:p_src", "view:p_view", "trigger:p_trig_b"}, r.text)
        n = await names(cx)
        ck("deleted are gone, released remain and are unowned",
           "p_agent" not in n["agent"] and "p_trig_a" not in n["trigger"] and "p_trig_b" in n["trigger"]
           and "p_src" in n["source"] and not next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "p_src").get("owned_by"))

        await build(cx, "q")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Q", "objects": [
            {"kind": "source", "name": "q_src"}, {"kind": "view", "name": "q_view"},
            {"kind": "trigger", "name": "q_trig_a"}, {"kind": "trigger", "name": "q_trig_b"}, {"kind": "agent", "name": "q_agent"}]})
        uid = r.json()["id"]
        r = await cx.delete(f"/api/projects/{uid}?delete=source:q_src,view:q_view,trigger:q_trig_a,trigger:q_trig_b,agent:q_agent&purge_events=true")
        ck("select all deletes everything", r.status_code == 200 and len(r.json()["deleted"]) == 5 and r.json()["released"] == [], r.text)
        n = await names(cx)
        ck("nothing named q_ remains", not any(x.startswith("q_") for k in n.values() for x in k), str(n))
        r = await cx.delete(f"/api/projects/{uid}?delete=source:nope")
        ck("deleted project -> 404", r.status_code == 404)

        print("== custom project delete: none keeps everything ==")
        await build(cx, "k")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "K", "objects": [{"kind": "source", "name": "k_src"}]})
        uid = r.json()["id"]
        r = await cx.delete(f"/api/projects/{uid}?delete=none")
        ck("delete=none releases, deletes nothing", r.status_code == 200 and r.json()["deleted"] == [] and r.json()["released"] == ["source:k_src"], r.text)

        print("== template project delete: unpicked objects stay ==")
        r = await cx.post("/api/projects", json={"template": "test_cascade", "name": "T", "params": {}})
        ck("test template project created", r.status_code == 201, r.text[:200])
        uid = r.json()["id"]
        # keep the source (and so its view and trigger, which need it): pick only the agent
        r = await cx.delete(f"/api/projects/{uid}?delete=agent:t_agent")
        ck("template project: picked agent goes, the rest is released",
           r.status_code == 200 and r.json()["deleted"] == ["agent:t_agent"]
           and set(r.json()["released"]) == {"source:t_src", "view:t_view", "trigger:t_trig"}, r.text[:300])
        n = await names(cx)
        ck("kept source exists and is unowned", "t_src" in n["source"]
           and not next(x for x in (await cx.get("/api/sources")).json() if x["name"] == "t_src").get("owned_by"))
        r = await cx.post("/api/projects", json={"template": "test_cascade", "name": "T2", "params": {"prefix": "u"}})
        uid = r.json()["id"]
        r = await cx.delete(f"/api/projects/{uid}")
        ck("template project without picks still takes everything",
           r.status_code == 200 and len(r.json()["deleted"]) == 4 and r.json()["released"] == [], r.text[:300])

        await cx.aclose()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
