# Tares enterprise licence, see ee/LICENSE
"""A private project stays out of the raw routes (TR-903).

Lee leads Zephyrwatch, a private project that holds a source (zephyr-logs), a watcher, a skill, a
doc and findings on the service zephyr-api. Kim is a teammate with an admin key who is not on it.
Every route below answers for the person asking (the AI SRE routes this test also covered went
with the edition, TR-1809): usage does not count its source, the dependents of its source and trigger are unknown, and a use
of its skill or doc is a 404. Lee and the cell's own key still see all of it, and an object shared
with a project Kim can see stays visible to her.

Run: .venv/bin/python ee/tests/test_private_raw_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-private-raw-routes-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-raw"
os.environ["TARES_EDITION"] = "platform"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import build
from tares.connectors import source_type_for
from tares.envelope import Envelope

P = F = 0
UTC = timezone.utc
SECRET = ("zephyr", "ZEPHYR")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def mentions(body) -> bool:
    return "zephyr" in json.dumps(body, default=str).lower()


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys, pid = {}, {}

    async def call(who, method, path, body=None, **params):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body, params=params or None)

    async def get(who, path, **params):
        return await call(who, "GET", path, **params)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        base = datetime.now(UTC).replace(microsecond=0)
        for who in ("kim", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-rr@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        build(store, base)
        open_uid, vault = "prj-edgewatch", "prj-zephyrwatch"
        store.create_project(open_uid, "custom", "Edgewatch", {}, goal="Keep edge-proxy healthy")
        store.create_project(vault, "custom", "Zephyrwatch", {}, goal="Keep zephyr-api healthy",
                             lead_person_id=pid["lee"])
        store.set_project_scope(vault, "private")

        # a source only Zephyrwatch owns, with a trigger on it and a watcher on the trigger
        store.upsert_catalog_source("zephyr-logs", source_type_for("otlp"), "otlp", "5s", {
            "labels": [{"name": "service", "field": "service.name", "primary": True}]})
        store.put_in_project("source", "zephyr-logs", vault)
        store.upsert_catalog_trigger("watch-zephyr", ["zephyr-logs"], {"every": "10m"}, {}, "10m")
        store.upsert_catalog_agent("zephyr-seer", "watch-zephyr", "Look at zephyr-api.",
                                   budget_usd=25.0, budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-zephyr", vault)
        store.put_in_project("agent", "zephyr-seer", vault)
        # a private trigger and watcher on the shared source too
        store.upsert_catalog_trigger("watch-zephyr-shared", ["prod-otlp"], {"every": "20m"}, {}, "20m")
        store.upsert_catalog_agent("zephyr-shared-seer", "watch-zephyr-shared", "Look at zephyr-api.",
                                   budget_usd=5.0, budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-zephyr-shared", vault)
        store.put_in_project("agent", "zephyr-shared-seer", vault)
        # and a trigger Edgewatch owns on the shared source
        store.upsert_catalog_trigger("watch-edge", ["prod-otlp"], {"every": "30m"}, {}, "30m")
        store.upsert_catalog_agent("edge-seer", "watch-edge", "Look at edge-proxy.",
                                   budget_usd=20.0, budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-edge", open_uid)
        store.put_in_project("agent", "edge-seer", open_uid)
        # a trigger Edgewatch owns that reads the private source (the check below removes the
        # membership the cell would add, so the early return in the dependents route is what answers)
        store.upsert_catalog_trigger("watch-edge-mirror", ["zephyr-logs"], {"every": "15m"}, {}, "15m")
        store.upsert_catalog_agent("edge-mirror-seer", "watch-edge-mirror", "Look at edge-proxy.",
                                   budget_usd=5.0, budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-edge-mirror", open_uid)
        store.put_in_project("agent", "edge-mirror-seer", open_uid)
        # a memory source both projects write to
        store.upsert_catalog_source("agent-memory", "agent_memory", "memory", "5s", {})
        # and one only Zephyrwatch uses, whose notes carry no project of their own
        store.upsert_catalog_source("zephyr-memory", "agent_memory", "memory", "5s", {})
        store.put_in_project("source", "zephyr-memory", vault)
        runtime.reload_catalog()
        store.delete_project_object(open_uid, "source", "source:zephyr-logs")

        def event(source, service, text, project=None, minutes=3):
            t = base - timedelta(minutes=minutes)
            labels = {"service": service, **({"tares_project": vault} if project else {})}
            return Envelope(source=source, source_type=source_type_for("otlp"), key_value=service,
                            event_type="ERROR", text=text, event_time=t, payload={}, labels=labels,
                            ingest_time=t)

        store.append([
            event("zephyr-logs", "zephyr-api", "ZEPHYR queue stuck", minutes=4),
            event("prod-otlp", "service-a", "ZEPHYR private line on a shared source", vault, 5),
            event("prod-otlp", "service-a", "plain line from a shared source", None, 6),
        ])
        for text, project, src in (("ZEPHYR note about retries", vault, "agent-memory"),
                                   ("plain note about retries", None, "agent-memory"),
                                   ("ZEPHYR private-only note", None, "zephyr-memory")):
            t = base - timedelta(minutes=2)
            store.append([Envelope(source=src, source_type="agent_memory",
                                   key_value="service-a", event_type="memory", text=text,
                                   event_time=t, payload={"project": project} if project else {},
                                   labels={}, ingest_time=t)])

        def run(rid, agent, trigger, key, finding, project, minutes, verdict="rca"):
            at = base - timedelta(minutes=minutes)
            store.start_agent_run(rid, agent, trigger, "", key, "h", project=project)
            store.finish_agent_run(rid, "ok", finding=finding, outcome="finding", verdict=verdict)
            with store._swrite:
                store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])
            return at

        run("r-zephyr", "zephyr-seer", "watch-zephyr", "zephyr-api",
            "Zephyrwatch broke the queue.", vault, 30)
        run("r-edge", "edge-seer", "watch-edge", "edge-proxy", "502s on edge-proxy.", open_uid, 20)
        # a result a shared watcher raised for both projects, and one the private watcher raised
        # with no project of its own
        run("r-shared", "edge-seer", "watch-edge", "edge-proxy", "shared result on edge-proxy.",
            open_uid, 10)
        store.add_run_projects("r-shared", [vault, open_uid])
        run("r-zephyr-np", "zephyr-seer", "watch-zephyr", "zephyr-api", "Zephyr result, no project.",
            None, 9)
        run("r-edge-np", "edge-seer", "watch-edge", "edge-proxy", "Edge result, no project.", None, 8)
        ck("the fixture ties the run to the private project", store.run_projects("r-zephyr") == [vault],
           str(store.run_projects("r-zephyr")))

        # a skill and a doc only Zephyrwatch has
        store.upsert_skill(vault, "zephyr-playbook", "Zephyr steps", "Restart the zephyr queue.")
        doc = store.create_doc(vault, "notes", "Zephyr notes", "The zephyr notes.")
        # and a skill both projects have
        store.upsert_skill(vault, "shared-playbook", "Shared steps", "Check the logs.")
        store.use_skill(open_uid, "shared-playbook")

        print("== usage ==")
        r = await get("kim", "/api/usage")
        names = [s["name"] for s in r.json()["sources"]]
        ck("usage lists no private source", r.status_code == 200 and "zephyr-logs" not in names
           and "prod-otlp" in names, str(names))
        total = sum(s["events"] for s in r.json()["sources"])
        ck("and its event count adds up to the sources shown", r.json()["events"] == total,
           f"{r.json()['events']} vs {total}")

        print("== dependents ==")
        r = await get("kim", "/api/catalog/dependents", kind="source", name="zephyr-logs")
        none_src = await get("kim", "/api/catalog/dependents", kind="source", name="no-such-source")
        ck("the private source answers as one that is not there",
           r.status_code == none_src.status_code and r.json() == none_src.json() == {"dependents": []},
           f"{r.status_code} {r.text[:120]} vs {none_src.status_code} {none_src.text[:120]}")
        ck("even when a trigger she can see reads that private source",
           "watch-edge-mirror" in [t.name for t in runtime.catalog.triggers]
           and "zephyr-logs" in next(t.sources for t in runtime.catalog.triggers if t.name == "watch-edge-mirror"))
        r = await get("kim", "/api/catalog/dependents", kind="trigger", name="watch-zephyr")
        none_trg = await get("kim", "/api/catalog/dependents", kind="trigger", name="no-such-trigger")
        ck("and so does the private trigger",
           r.status_code == none_trg.status_code and r.json() == none_trg.json() == {"dependents": []},
           f"{r.status_code} {r.text[:120]} vs {none_trg.status_code} {none_trg.text[:120]}")
        r = await get("kim", "/api/catalog/dependents", kind="source", name="prod-otlp")
        ck("a shared source lists the dependents she can see and not the private ones",
           r.status_code == 200 and "watch-edge" in json.dumps(r.json()) and not mentions(r.json()),
           f"{r.status_code} {r.text[:200]}")

        print("== use ==")
        r = await call("kim", "POST", f"/api/projects/{open_uid}/skills/zephyr-playbook/use")
        ck("using the private skill is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("and the open project does not get it", "zephyr-playbook" not in
           [s["name"] for s in store.list_skills(open_uid)])
        r = await call("kim", "POST", f"/api/projects/{open_uid}/skills/shared-playbook/use")
        ck("a skill both projects have can be used", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = await call("kim", "POST", f"/api/projects/{open_uid}/docs/{doc}/use")
        ck("using the private doc is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("and the open project does not get it", doc not in [d["id"] for d in store.list_docs(open_uid)])

        print("== the people who can see Zephyrwatch ==")
        for who in ("lee", "root"):
            r = await get(who, "/api/usage")
            ck(f"{who}: usage counts the source", "zephyr-logs" in [s["name"] for s in r.json()["sources"]], r.text[:200])
            r = await get(who, "/api/catalog/dependents", kind="source", name="zephyr-logs")
            ck(f"{who}: the dependents answer", r.status_code == 200 and "watch-zephyr" in json.dumps(r.json()), r.text[:200])

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
