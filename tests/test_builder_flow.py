"""The AI-guided project builder's flow (TR-244, TR-246), through the real endpoints.

The builder creates nothing new on the engine: it creates an empty `custom` project first, then
each Apply creates an ordinary object in that project through the object's own API (`project` in
the body). An object made before, in the default project, is taken in by saving the project's
object list with it. Asserts membership, that a name collision fails cleanly without touching the
project, and that deleting the project takes its trigger and agent and leaves the source.

Run: .venv/bin/python tests/test_builder_flow.py
"""
import asyncio
import os

DB = "/tmp/tares-builder-flow.duckdb"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = "/tmp/tares-builder-flow.catalog.yaml"
# enable checks that a key resolves; it never calls Anthropic
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
for _p in (DB, DB + ".wal", os.environ["TARES_CATALOG"]):
    if os.path.exists(_p):
        os.remove(_p)

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SOURCE = {"name": "checkout_logs", "connector": "webhook", "poll": "5s",
          "config": {"event_type": "log", "text_template": "{msg}",
                     "labels": [{"name": "service", "field": "service", "primary": True}]}}
TRIGGER = {"name": "checkout_errors", "sources": ["checkout_logs"], "key_field": "service",
           "condition": {"aggregate": "count", "predicate": "> 5", "window": "1m"},
           "emit": {"kind": "error_spike", "context_window": "15m"}, "cooldown": "5m"}
AGENT = {"name": "checkout_sre", "trigger": "checkout_errors",
         "prompt": "Read the timeline and say what broke first.",
         "webhook_url": "https://example.invalid/findings", "webhook_token": "t0k"}


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        default = next(p["id"] for p in (await cx.get("/api/projects")).json()["projects"] if p["default"])

        print("== the project first, empty ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Checkout watch",
                                                 "objects": []})
        ck("empty custom project created", r.status_code == 201 and r.json()["objects"] == [], r.text)
        uid = r.json()["id"]

        print("== sources step: the source is made in the project ==")
        r = await cx.post("/api/sources", json={**SOURCE, "project": uid})
        ck("source created", r.status_code == 201, r.text)
        src = next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "checkout_logs")
        ck("source made by and in the project", src.get("owned_by") == uid and src["projects"] == [uid],
           str(src.get("projects")))

        print("== a name collision fails cleanly and leaves the project alone ==")
        r = await cx.post("/api/sources", json={**SOURCE, "project": uid})
        ck("duplicate source name -> 409", r.status_code == 409, r.text)
        after = (await cx.get(f"/api/projects/{uid}")).json()
        ck("project unchanged: still one object, no error",
           len(after["objects"]) == 1 and after["status"] == "active" and not after["last_error"],
           str(after))

        print("== triggers step ==")
        r = await cx.post("/api/triggers", json={**TRIGGER, "project": uid})
        ck("trigger created in the project", r.status_code == 201 and r.json()["project"] == uid, r.text)

        print("== agent step: create, enable ==")
        r = await cx.post("/api/agents/builtin", json={**AGENT, "project": uid})
        ck("agent created (disabled)", r.status_code == 201 and r.json()["enabled"] is False, r.text)
        r = await cx.post("/api/agents/builtin/checkout_sre/enable")
        ck("agent enabled through the real endpoint", r.status_code == 200 and r.json()["enabled"], r.text)

        print("== everything is in the project, the project page sees it all ==")
        proj = (await cx.get(f"/api/projects/{uid}")).json()
        kinds = sorted(o["kind"] for o in proj["objects"])
        ck("project lists source, trigger, agent", kinds == ["agent", "source", "trigger"], str(kinds))
        ck("the custom project's object list says the same",
           sorted(o["kind"] for o in proj["params"]["objects"]) == ["agent", "source", "trigger"],
           str(proj["params"]))
        trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
        ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
        ck("trigger in the project", trig["checkout_errors"].get("project") == uid)
        ck("agent in the project and enabled", ag["checkout_sre"].get("project") == uid
           and ag["checkout_sre"]["enabled"] is True, str(ag["checkout_sre"]))
        ck("the project wakes the agent on its trigger (its wiring), not a URL the builder made up",
           app.state.store.list_wakes(project=uid, agent="checkout_sre")
           == [{"project": uid, "trigger": "checkout_errors", "agent": "checkout_sre", "enabled": True}]
           and not any("checkout_sre" in s.get("url", "")
                       for s in (await cx.get("/api/subscriptions")).json()))
        r = await cx.get(f"/api/projects/{uid}/summary")
        ck("summary renders (runs, triggers)", r.status_code == 200 and "triggers" in r.json(), r.text[:200])

        print("== an object made in the default project is taken in by saving the list ==")
        r = await cx.post("/api/triggers", json={**TRIGGER, "name": "checkout_slow"})
        ck("a trigger made without a project sits in the default one",
           r.status_code == 201 and r.json()["project"] == default, r.text)
        objects = proj["params"]["objects"] + [{"kind": "trigger", "name": "checkout_slow"}]
        r = await cx.put(f"/api/projects/{uid}", json={"objects": objects})
        ck("saving the list with it moves it in", r.status_code == 200
           and "trigger:checkout_slow" in r.json()["report"]["added"], r.text[:300])
        trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
        ck("its project is now this one", trig["checkout_slow"]["project"] == uid)

        print("== appending an object that does not exist fails cleanly ==")
        r = await cx.put(f"/api/projects/{uid}", json={
            "objects": objects + [{"kind": "trigger", "name": "ghost"}]})
        ck("unknown object -> 400", r.status_code == 400, r.text)
        proj = (await cx.get(f"/api/projects/{uid}")).json()
        ck("project keeps its four objects", len(proj["objects"]) == 4, str(proj["objects"]))

        print("== delete takes the triggers and the agent, the source stays ==")
        r = await cx.delete(f"/api/projects/{uid}")
        ck("project deleted", r.status_code == 200
           and set(r.json()["deleted"]) == {"trigger:checkout_errors", "trigger:checkout_slow",
                                            "agent:checkout_sre"}
           and r.json()["released"] == ["source:checkout_logs"], r.text)
        src = next((s for s in (await cx.get("/api/sources")).json() if s["name"] == "checkout_logs"), None)
        ck("source still exists, in the default project",
           src is not None and src["projects"] == [default] and not src.get("owned_by"), str(src))
        ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
        ck("agent gone and unsubscribed", "checkout_sre" not in ag and not any(
            "checkout_sre" in s.get("url", "") for s in (await cx.get("/api/subscriptions")).json()))

        await cx.aclose()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
