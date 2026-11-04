"""A private project stays out of the service views (TR-906).

Lee leads Zephyrwatch, a private project that holds a source (zephyr-logs) with the service
zephyr-api and a GitHub source (zephyr-repo) with a repo. Kim is a teammate with an admin key who
is not on it. Every service view answers for the person asking: the services list, a single
service and its deploys, the watch view, the repos, the blind spots, the runbooks and the SRE
services and timeline screens leave out the private service and the repo, a private service answers
exactly like a name that does not exist, and a service both projects feed counts only what Kim can
see. Two people reading one warm cache each get their own answer. Lee and the cell's own key still
see all of it.

Run: .venv/bin/python tests/test_private_services.py
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

TMP = tempfile.mkdtemp(prefix="tares-private-services-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-services"
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import build
from tares import incidents, sre_home
from tares import watch as watch_mod
from tares.connectors import source_type_for
from tares.envelope import Envelope

P = F = 0
UTC = timezone.utc
HIDDEN_REPO = "example-org/zephyr-repo"
SHARED_REPO = "example-org/shared-lib"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def mentions(body) -> bool:
    text = json.dumps(body, default=str).lower()
    return "zephyr" in text


def names(body) -> list[str]:
    return [s["name"] for s in body["services"]]


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

    async def cold(who, path, **params):
        sre_home.clear_cache()
        return await call(who, "GET", path, **params)

    async def warm(who, path, **params):
        return await call(who, "GET", path, **params)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        base = datetime.now(UTC).replace(microsecond=0)
        incidents.clock = lambda: base
        for who in ("kim", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-svc@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        build(store, base)   # prod-otlp: edge-proxy and service-a, in the open project
        vault = "prj-zephyrwatch"
        store.create_project(vault, "custom", "Zephyrwatch", {}, goal="Keep zephyr-api healthy",
                             lead_person_id=pid["lee"])
        store.set_project_scope(vault, "private")

        # a source only Zephyrwatch owns: zephyr-api, plus two lines for the shared service-a
        store.upsert_catalog_source("zephyr-logs", source_type_for("otlp"), "otlp", "5s", {
            "labels": [{"name": "service", "field": "service.name", "primary": True}]})
        store.put_in_project("source", "zephyr-logs", vault)
        store.upsert_catalog_source("zephyr-repo", source_type_for("github_app"), "github_app", "1h", {
            "labels": [{"name": "repo", "field": "repo", "primary": True}]})
        store.put_in_project("source", "zephyr-repo", vault)
        # a watcher only Zephyrwatch has, on its source
        store.upsert_catalog_trigger("watch-zephyr", ["zephyr-logs"], {"every": "10m"}, {}, "10m")
        store.upsert_catalog_agent("zephyr-seer", "watch-zephyr", "Look at zephyr-api.",
                                   budget_usd=25.0, budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-zephyr", vault)
        store.put_in_project("agent", "zephyr-seer", vault)
        store.set_wake(vault, "watch-zephyr", "zephyr-seer", enabled=True)
        runtime.reload_catalog()

        def log(service, minutes, text, level="INFO"):
            t = base - timedelta(minutes=minutes)
            return Envelope(source="zephyr-logs", source_type=source_type_for("otlp"),
                            key_value=service, event_type=level, text=text, event_time=t,
                            payload={}, labels={"service": service}, ingest_time=t)

        env = [log("zephyr-api", 2 + i, f"zephyr handled {i}") for i in range(6)]
        env.append(log("zephyr-api", 4, "zephyr queue stuck", "ERROR"))
        env += [log("service-a", 3 + i, f"zephyr line on service-a {i}") for i in range(2)]
        env.append(Envelope(source="zephyr-repo", source_type=source_type_for("github_app"),
                            key_value=HIDDEN_REPO, event_type="commit", text="zephyr fix queue",
                            event_time=base - timedelta(minutes=12), payload={},
                            labels={"repo": HIDDEN_REPO}, ingest_time=base - timedelta(minutes=12)))
        # a private service with no repo linked: a blind spot only the people on Zephyrwatch may read
        env.append(log("zephyr-worker", 5, "zephyr worker idle"))
        # a commit only the private source holds, on a repo the shared service is built from
        env.append(Envelope(source="zephyr-repo", source_type=source_type_for("github_app"),
                            key_value="example-org/service-a", event_type="commit",
                            text="zephyr hotfix on service-a", event_time=base - timedelta(minutes=9),
                            payload={}, labels={"repo": "example-org/service-a"},
                            ingest_time=base - timedelta(minutes=9)))
        # a repo the open project's GitHub source has seen, linked to a shared and a private service
        env.append(Envelope(source="github", source_type=source_type_for("github_app"),
                            key_value=SHARED_REPO, event_type="commit", text="bump shared lib",
                            event_time=base - timedelta(minutes=15), payload={},
                            labels={"repo": SHARED_REPO}, ingest_time=base - timedelta(minutes=15)))
        store.append(env)
        store.set_service("zephyr-api", "test", repos=[HIDDEN_REPO, SHARED_REPO])
        store.set_service("service-a", "test", repos=["example-org/service-a", SHARED_REPO])
        sre_home.clear_cache()
        watch_mod.sync(store, runtime.catalog, base)   # the stored state /api/watch reads

        # what the cell holds, as the one who sees it all
        r = await cold("root", "/api/services")
        ck("the cell lists the private service", "zephyr-api" in names(r.json()), str(names(r.json())))
        whole = {s["name"]: s for s in r.json()["services"]}
        a_all = whole["service-a"]
        seen_by_kim = ("edge-proxy", "service-a")

        print("== one warm cache, two readers ==")
        sre_home.clear_cache()
        r = await warm("lee", "/api/services")
        ck("Lee reads first and warms the cache with the private service", "zephyr-api" in names(r.json()),
           str(names(r.json())))
        r = await warm("kim", "/api/services")
        ck("Kim reads the same cache and gets her own list", r.status_code == 200
           and "zephyr-api" not in names(r.json()) and all(n in names(r.json()) for n in seen_by_kim)
           and not mentions(r.json()), f"{r.status_code} {r.text[:300]}")
        kim_a = {s["name"]: s for s in r.json()["services"]}["service-a"]
        ck("and a service both projects feed counts only what she can see",
           kim_a["events_24h"] < a_all["events_24h"],
           f"{kim_a['events_24h']} vs {a_all['events_24h']}")
        r = await warm("lee", "/api/services")
        ck("Lee reads again and still sees all of it", "zephyr-api" in names(r.json())
           and {s["name"]: s for s in r.json()["services"]}["service-a"]["events_24h"] == a_all["events_24h"],
           str(names(r.json())))
        r = await warm("root", "/api/services")
        ck("so does the cell's own key", "zephyr-api" in names(r.json()))
        r = await warm("kim", "/api/services", hidden=1)
        ck("Kim still sees nothing of it with the hidden ones included", "zephyr-api" not in names(r.json())
           and not mentions(r.json()), str(names(r.json())))

        print("== a single service ==")
        r = await cold("kim", "/api/services/zephyr-api")
        unknown = await cold("kim", "/api/services/no-such-service")
        ck("the private service answers as a name that does not exist",
           r.status_code == unknown.status_code == 404
           and r.text.replace("zephyr-api", "X") == unknown.text.replace("no-such-service", "X"),
           f"{r.status_code} {r.text[:120]} vs {unknown.status_code} {unknown.text[:120]}")
        r = await cold("kim", "/api/services/zephyr-api/deploys")
        unknown = await cold("kim", "/api/services/no-such-service/deploys")
        ck("and so do its deploys", r.status_code == unknown.status_code == 404
           and r.text.replace("zephyr-api", "X") == unknown.text.replace("no-such-service", "X"),
           f"{r.status_code} {r.text[:120]} vs {unknown.status_code} {unknown.text[:120]}")
        r = await cold("kim", "/api/services/service-a")
        ck("a shared service opens", r.status_code == 200 and not mentions(r.json()), f"{r.status_code} {r.text[:200]}")
        ck("with only the events she can see", r.json()["events_24h"] < a_all["events_24h"],
           f"{r.json()['events_24h']} vs {a_all['events_24h']}")
        r = await cold("kim", "/api/sre/services/zephyr-api")
        unknown = await cold("kim", "/api/sre/services/no-such-service")
        ck("the service screen answers the same way", r.status_code == unknown.status_code
           and r.text.replace("zephyr-api", "X") == unknown.text.replace("no-such-service", "X"),
           f"{r.status_code} {r.text[:120]} vs {unknown.status_code} {unknown.text[:120]}")
        r = await cold("kim", "/api/sre/services/service-a")
        ck("and a shared one shows no private chart or repo", r.status_code == 200 and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")

        print("== the lists and the screens ==")
        r = await cold("kim", "/api/sre/services")
        ck("the services screen names nothing private", r.status_code == 200 and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/watch")
        ck("the watch view names nothing private", r.status_code == 200 and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/repos")
        ck("the repos leave out the private repo and its service", r.status_code == 200
           and not mentions(r.json()) and "example-org/service-a" in json.dumps(r.json()),
           f"{r.status_code} {r.text[:300]}")
        shared = [x for x in r.json()["repos"] if x["repo"] == SHARED_REPO]
        ck("a repo a private service is also linked to lists only the services she can see",
           len(shared) == 1 and shared[0]["services"] == ["service-a"], str(shared))
        r = await cold("kim", "/api/blind-spots")
        ck("the blind spots name nothing private", r.status_code == 200 and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("lee", "/api/blind-spots")
        ck("while the people on Zephyrwatch read its own", "zephyr-worker" in json.dumps(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/services/service-a/deploys")
        ck("a shared service's deploys leave out the private source's commits", r.status_code == 200
           and not mentions(r.json()), f"{r.status_code} {r.text[:300]}")
        r = await cold("lee", "/api/services/service-a/deploys")
        ck("and the people on Zephyrwatch read them", "zephyr hotfix" in json.dumps(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/sre/timeline/raw", service="service-a")
        ck("a shared service's raw timeline has none of the private lines", r.status_code == 200
           and not mentions(r.json()), f"{r.status_code} {r.text[:300]}")
        r = await cold("lee", "/api/sre/timeline/raw", service="service-a")
        ck("which the people on Zephyrwatch do read", "zephyr line on service-a" in json.dumps(r.json()),
           f"{r.status_code} {r.text[:300]}")
        k = (await cold("kim", "/api/sre/services/service-a")).json()["chart"]
        w = (await cold("lee", "/api/sre/services/service-a")).json()["chart"]
        ck("the service chart counts only the events she can see", sum(k["traffic"]) < sum(w["traffic"]),
           f"{sum(k['traffic'])} vs {sum(w['traffic'])}")
        r = await cold("kim", "/api/sre/runbooks")
        ck("the runbooks list no private service", r.status_code == 200 and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/sre/timeline")
        ck("the timeline names nothing private", r.status_code == 200 and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/sre/timeline", service="service-a")
        ck("and a shared service's timeline has none of the private lines", r.status_code == 200
           and not mentions(r.json()), f"{r.status_code} {r.text[:300]}")
        r = await cold("kim", "/api/sre/home")
        ck("the home names nothing private", r.status_code in (200, 404) and not mentions(r.json()),
           f"{r.status_code} {r.text[:300]}")

        print("== the people who can see Zephyrwatch ==")
        for who in ("lee", "root"):
            r = await cold(who, "/api/services/zephyr-api")
            ck(f"{who}: the service opens", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
            ck(f"{who}: its deploys answer", (await cold(who, "/api/services/zephyr-api/deploys")).status_code == 200)
            r = await cold(who, "/api/repos")
            ck(f"{who}: the repos have the private repo", HIDDEN_REPO in json.dumps(r.json()), r.text[:200])
            r = await cold(who, "/api/watch")
            ck(f"{who}: the watch view has the service", "zephyr-api" in json.dumps(r.json()), r.text[:200])
            r = await cold(who, "/api/sre/runbooks")
            ck(f"{who}: the runbooks list it", "zephyr-api" in json.dumps(r.json()), r.text[:200])
            r = await cold(who, "/api/sre/timeline/raw", service="zephyr-api")
            ck(f"{who}: the raw timeline has its rows", "zephyr queue stuck" in json.dumps(r.json()), r.text[:200])

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
