"""The AI-guided project builder's ownership flow (TR-244, TR-246), through the real endpoints.

The builder creates nothing new on the engine: each Apply creates an ordinary object via its own
API, then appends it to a `custom` project. This mirrors that sequence exactly: source, then the
project owning it, then view / trigger / agent each created and appended; the agent is enabled
through the same endpoint the console uses. Asserts ownership, that a name collision fails
cleanly without touching the project, and that deleting the project releases the objects.

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
VIEW = {"name": "checkout_timeline", "key_field": "service", "sources": ["checkout_logs"]}
TRIGGER = {"name": "checkout_errors", "view": "checkout_timeline",
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

        print("== sources step: create the source, then the project that owns it ==")
        r = await cx.post("/api/sources", json=SOURCE)
        ck("source created", r.status_code == 201, r.text)
        r = await cx.post("/api/projects", json={
            "template": "custom", "name": "Checkout watch",
            "objects": [{"kind": "source", "name": "checkout_logs"}]})
        ck("custom project created around the source", r.status_code == 201, r.text)
        proj = r.json()
        uid = proj["id"]
        objects = [{"kind": "source", "name": "checkout_logs"}]
        src = next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "checkout_logs")
        ck("source owned by the project", src.get("owned_by") == uid, str(src.get("owned_by")))

        print("== a name collision fails cleanly and leaves the project alone ==")
        r = await cx.post("/api/sources", json=SOURCE)
        ck("duplicate source name -> 409", r.status_code == 409, r.text)
        after = (await cx.get(f"/api/projects/{uid}")).json()
        ck("project unchanged: still one object, no error",
           len(after["objects"]) == 1 and after["status"] == "active" and not after["last_error"],
           str(after))

        async def append(kind, name):
            objects.append({"kind": kind, "name": name})
            r = await cx.put(f"/api/projects/{uid}", json={"objects": objects})
            ck(f"{kind} {name} appended to the project", r.status_code == 200
               and f"{kind}:{name}" in r.json().get("report", {}).get("added", []), r.text)

        print("== views step ==")
        r = await cx.post("/api/views", json=VIEW)
        ck("view created", r.status_code == 201, r.text)
        await append("view", "checkout_timeline")

        print("== triggers step ==")
        r = await cx.post("/api/triggers", json=TRIGGER)
        ck("trigger created", r.status_code == 201, r.text)
        await append("trigger", "checkout_errors")

        print("== agent step: create, enable, append ==")
        r = await cx.post("/api/agents/builtin", json=AGENT)
        ck("agent created (disabled)", r.status_code == 201 and r.json()["enabled"] is False, r.text)
        r = await cx.post("/api/agents/builtin/checkout_sre/enable")
        ck("agent enabled through the real endpoint", r.status_code == 200 and r.json()["enabled"], r.text)
        await append("agent", "checkout_sre")

        print("== everything is owned, the project page sees it all ==")
        proj = (await cx.get(f"/api/projects/{uid}")).json()
        kinds = sorted(o["kind"] for o in proj["objects"])
        ck("project lists source, view, trigger, agent",
           kinds == ["agent", "source", "trigger", "view"], str(kinds))
        views = {v["name"]: v for v in (await cx.get("/api/views")).json()}
        trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
        ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
        ck("view owned", views["checkout_timeline"].get("owned_by") == uid)
        ck("trigger owned", trig["checkout_errors"].get("owned_by") == uid)
        ck("agent owned and enabled", ag["checkout_sre"].get("owned_by") == uid
           and ag["checkout_sre"]["enabled"] is True, str(ag["checkout_sre"]))
        ck("the agent is subscribed to its trigger, not to a URL the builder made up",
           any(s.get("trigger") == "checkout_errors" and "tares://agent/checkout_sre" in s.get("url", "")
               for s in (await cx.get("/api/subscriptions")).json()))
        r = await cx.get(f"/api/projects/{uid}/summary")
        ck("summary renders (runs, triggers)", r.status_code == 200 and "triggers" in r.json(), r.text[:200])

        print("== appending an object that does not exist fails cleanly ==")
        r = await cx.put(f"/api/projects/{uid}", json={
            "objects": objects + [{"kind": "view", "name": "ghost"}]})
        ck("unknown object -> 400", r.status_code == 400, r.text)
        proj = (await cx.get(f"/api/projects/{uid}")).json()
        ck("project keeps its four objects", len(proj["objects"]) == 4, str(proj["objects"]))

        print("== delete releases, never deletes ==")
        r = await cx.delete(f"/api/projects/{uid}")
        ck("project deleted with objects released", r.status_code == 200
           and len(r.json().get("released", [])) == 4 and not r.json().get("deleted"), r.text)
        names = {s["name"] for s in (await cx.get("/api/sources")).json()}
        ck("source still exists", "checkout_logs" in names)
        src = next(s for s in (await cx.get("/api/sources")).json() if s["name"] == "checkout_logs")
        ck("source no longer owned", not src.get("owned_by"), str(src.get("owned_by")))
        ag = {a["name"]: a for a in (await cx.get("/api/agents/builtin")).json()["agents"]}
        ck("agent still exists and still enabled", "checkout_sre" in ag and ag["checkout_sre"]["enabled"])

        await cx.aclose()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
