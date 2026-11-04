"""A private project stays out of the raw routes behind the AI SRE screens (TR-903).

Lee leads Zephyrwatch, a private project that holds a source (zephyr-logs), a watcher, a skill, a
doc, an incident and findings on the service zephyr-api. Kim is a teammate with an admin key who
is not on it. Every route below answers for the person asking: the incidents list and its counts
leave the incident out, the single incident and its actions answer 404 as for an incident that
does not exist, the raw timeline and the runbooks' memory notes leave out its rows, the findings,
the hit rate and the feedback export leave out its results, the worth answer on its run is a 404,
usage does not count its source, the dependents of its source and trigger are unknown, and a use
of its skill or doc is a 404. Lee and the cell's own key still see all of it, and an object shared
with a project Kim can see stays visible to her.

Run: .venv/bin/python tests/test_private_raw_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _env import scrub_os_environ
scrub_os_environ()
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-private-raw-routes-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-raw"
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import build
from tares import incidents, sre_home
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
        sre_home.clear_cache()
        return await call(who, "GET", path, **params)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        base = datetime.now(UTC).replace(microsecond=0)
        incidents.clock = lambda: base
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
        # a memory source both projects write to
        store.upsert_catalog_source("agent-memory", "agent_memory", "memory", "5s", {})
        runtime.reload_catalog()

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
        for text, project in (("ZEPHYR note about retries", vault), ("plain note about retries", None)):
            t = base - timedelta(minutes=2)
            store.append([Envelope(source="agent-memory", source_type="agent_memory",
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

        at = run("r-zephyr", "zephyr-seer", "watch-zephyr", "zephyr-api",
                 "Zephyrwatch broke the queue.", vault, 30)
        secret = store.create_incident("zephyr-api", vault, at)["id"]
        store.add_incident_note(secret, "r-zephyr", at)
        at = run("r-edge", "edge-seer", "watch-edge", "edge-proxy", "502s on edge-proxy.",
                 open_uid, 20)
        shown = store.create_incident("edge-proxy", open_uid, at)["id"]
        store.add_incident_note(shown, "r-edge", at)
        ck("the fixture ties the run to the private project", store.run_projects("r-zephyr") == [vault],
           str(store.run_projects("r-zephyr")))

        # answers on a finding: one on the private watcher, one on the open watcher
        store.set_feedback("r-zephyr", "p-lee", "Lee", True, "Zephyr note", "console", "zephyr-seer",
                           "r-zephyr", None, vault)
        store.set_feedback("r-edge", "p-lee", "Lee", False, "edge note", "console", "edge-seer",
                           "r-edge", None, open_uid)

        # a skill and a doc only Zephyrwatch has
        store.upsert_skill(vault, "zephyr-playbook", "Zephyr steps", "Restart the zephyr queue.")
        doc = store.create_doc(vault, "notes", "Zephyr notes", "The zephyr notes.")
        # and a skill both projects have
        store.upsert_skill(vault, "shared-playbook", "Shared steps", "Check the logs.")
        store.use_skill(open_uid, "shared-playbook")

        print("== incidents ==")
        r = await get("kim", "/api/incidents")
        ck("the list answers", r.status_code == 200, r.text[:120])
        ck("the list leaves the private incident out", secret not in json.dumps(r.json()) and not mentions(r.json()),
           r.text[:300])
        ck("the open one is there", shown in json.dumps(r.json()))
        ck("the counts leave the private one out", r.json()["counts"]["open"] == 1, str(r.json()["counts"]))
        r = await get("kim", f"/api/incidents/{secret}")
        gone = await get("kim", "/api/incidents/inc_doesnotexist")
        ck("the single incident is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("and says what an unknown incident says",
           r.text.replace(secret, "X") == gone.text.replace("inc_doesnotexist", "X"), f"{r.text} vs {gone.text}")
        for act in ("on_it", "resolve", "reopen"):
            r = await call("kim", "POST", f"/api/incidents/{secret}/{act}")
            ck(f"{act} is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("and changed nothing", store.get_incident(secret)["state"] == "open")
        r = await get("kim", f"/api/incidents/{shown}")
        ck("the open incident opens", r.status_code == 200, r.text[:120])

        print("== the raw timeline ==")
        r = await get("kim", "/api/sre/timeline/raw", service="zephyr-api")
        ck("a source only the private project owns gives no rows", r.status_code in (200, 404)
           and not mentions(r.json()), f"{r.status_code} {r.text[:200]}")
        r = await get("kim", "/api/sre/timeline/raw", service="service-a")
        ck("a shared source leaves out the private project's rows",
           r.status_code == 200 and not mentions(r.json()) and "plain line" in json.dumps(r.json()),
           f"{r.status_code} {r.text[:300]}")

        print("== the runbooks' memory notes ==")
        r = await get("kim", "/api/sre/runbooks")
        ck("the runbooks hold no private note", r.status_code == 200 and "ZEPHYR note" not in r.text, r.text[:300])
        ck("and keep the open note", "plain note about retries" in json.dumps(r.json()), r.text[:300])

        print("== findings and feedback ==")
        r = await get("kim", "/api/findings", show="all")
        ck("the findings leave out the private result", r.status_code == 200 and not mentions(r.json())
           and "r-edge" in json.dumps(r.json()), f"{r.status_code} {r.text[:300]}")
        r = await get("kim", "/api/watchers/hit-rate")
        ck("the hit rate leaves out the private watcher", r.status_code == 200 and not mentions(r.json())
           and "edge-seer" in json.dumps(r.json()), f"{r.status_code} {r.text[:300]}")
        r = await get("kim", "/api/feedback/export")
        ck("the feedback export leaves out its answers", r.status_code == 200 and not mentions(r.json())
           and "edge note" in json.dumps(r.json()), f"{r.status_code} {r.text[:300]}")
        r = await call("kim", "PUT", "/api/findings/r-zephyr/worth", {"worth": True})
        unknown = await call("kim", "PUT", "/api/findings/r-nothing/worth", {"worth": True})
        ck("an answer on the private run is a 404, as for an unknown run",
           r.status_code == 404 and r.text.replace("r-zephyr", "X") == unknown.text.replace("r-nothing", "X"),
           f"{r.status_code} {r.text[:120]} vs {unknown.status_code} {unknown.text}")
        r = await call("kim", "DELETE", "/api/findings/r-zephyr/worth")
        ck("taking one back is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("and the stored answer stays", len(store.feedback_for(["r-zephyr"])["r-zephyr"]) == 1)
        r = await call("kim", "PUT", "/api/findings/r-edge/worth", {"worth": True})
        ck("an answer on the open run works", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

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
        ck("the private source is unknown", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        r = await get("kim", "/api/catalog/dependents", kind="trigger", name="watch-zephyr")
        ck("the private trigger is unknown", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
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
            r = await get(who, "/api/incidents")
            ck(f"{who}: the list has the incident and counts it", secret in json.dumps(r.json())
               and r.json()["counts"]["open"] == 2, r.text[:200])
            ck(f"{who}: the incident opens", (await get(who, f"/api/incidents/{secret}")).status_code == 200)
            r = await get(who, "/api/sre/timeline/raw", service="zephyr-api")
            ck(f"{who}: the raw timeline has its rows", "ZEPHYR queue stuck" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/sre/timeline/raw", service="service-a")
            ck(f"{who}: and the private row on the shared source", "ZEPHYR private line" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/sre/runbooks")
            ck(f"{who}: the runbooks have the private note", "ZEPHYR note" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/findings", show="all")
            ck(f"{who}: the findings have it", "r-zephyr" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/watchers/hit-rate")
            ck(f"{who}: the hit rate has it", "zephyr-seer" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/feedback/export")
            ck(f"{who}: the export has it", "Zephyr note" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/usage")
            ck(f"{who}: usage counts the source", "zephyr-logs" in [s["name"] for s in r.json()["sources"]], r.text[:200])
            r = await get(who, "/api/catalog/dependents", kind="source", name="zephyr-logs")
            ck(f"{who}: the dependents answer", r.status_code == 200 and "watch-zephyr" in json.dumps(r.json()), r.text[:200])
        r = await call("lee", "PUT", "/api/findings/r-zephyr/worth", {"worth": False})
        ck("Lee answers on her own run", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
