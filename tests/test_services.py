"""Services as a list the cell knows (TR-742, the read side).

service_label and signal_kind (which sources hold services, what kind of signal an event is),
canonical (merges, loops, the hop limit), list_services and get_service over the shared AI SRE
fixture (four services, github is not one, the 10-minute and 24-hour counts, a service silent for
two days, by_feed and labels), the window edges, what a person's changes do (display name, hidden,
merged, repos), the label override setting, GET /api/services and GET /api/services/{name} (scopes:
a project key gets 403), a read changes nothing, and the 200,000-event cell answering in under 2
seconds.

Run: .venv/bin/python tests/test_services.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
ROOT = "root-token-services"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_WATCH_SECONDS"] = "3600"   # a watch pass mid-build would cache half-built counts

import httpx

from fixtures.sre_cell import build
from tares import sre
from tares.config import Catalog, SourceCfg, catalog_from_db
from tares.envelope import Envelope
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def src(name, connector, labels=None):
    return SourceCfg(name, "event_stream", connector, 5, {"labels": labels or []})


def by_name(items):
    return {i["name"]: i for i in items}


def pure():
    print("== which sources hold services, and what kind of signal ==")
    prim = [{"name": "service", "field": "s", "primary": True}]
    eq("the label named service", sre.service_label(
        src("a", "otlp", [{"name": "app", "primary": True}, {"name": "service"}]), {}), "service")
    eq("else the primary label", sre.service_label(src("a", "vercel", [
        {"name": "project", "primary": True}]), {}), "project")
    eq("no label, no service", sre.service_label(src("a", "webhook"), {}), None)
    for c in ("finding", "memory", "claude_code", "reference", "github", "github_app",
              "github_issues", "linear"):
        eq(f"{c} is never a service source", sre.service_label(src("a", c, prim), {}), None)
    eq("an override names another label", sre.service_label(
        src("a", "otlp", prim), {"a": "namespace"}), "namespace")
    eq("a null override says not a service source", sre.service_label(
        src("a", "otlp", prim), {"a": None}), None)
    eq("an override beats the connector list", sre.service_label(
        src("a", "github", prim), {"a": "repo"}), "repo")
    k = sre.signal_kind
    eq("otlp span is traces", k("otlp", "span", None), "traces")
    eq("otlp with a metric type is metrics", k("otlp", "jobs", "gauge"), "metrics")
    eq("other otlp is logs", k("otlp", "ERROR", None), "logs")
    eq("prometheus is metrics", k("prometheus", "x", None), "metrics")
    eq("alertmanager is alerts", k("alertmanager", "x", None), "alerts")
    eq("prometheus_alerts is alerts", k("prometheus_alerts", "x", None), "alerts")
    for c in ("docker_logs", "loki", "vercel"):
        eq(f"{c} is logs", k(c, "x", None), "logs")
    eq("github is deploys", k("github", "commit", None), "deploys")
    eq("anything else is events", k("webhook", "x", None), "events")

    print("== canonical ==")
    m = lambda d: {n: {"merged_into": t} for n, t in d.items()}
    eq("not merged", sre.canonical("a", {}), "a")
    eq("one hop", sre.canonical("a", m({"a": "b"})), "b")
    eq("two hops", sre.canonical("a", m({"a": "b", "b": "c"})), "c")
    eq("a loop stops at the last service before it repeats",
       sre.canonical("a", m({"a": "b", "b": "a"})), "b")
    eq("a loop of four stops at the last one before it repeats",
       sre.canonical("a", m({"a": "b", "b": "c", "c": "d", "d": "a"})), "d")
    eq("a merge into itself stays", sre.canonical("a", m({"a": "a"})), "a")
    chain = m({f"n{i}": f"n{i + 1}" for i in range(7)})
    eq("five hops at most", sre.canonical("n0", chain), "n5")
    eq("from n2 the last is n7", sre.canonical("n2", chain), "n7")
    eq("a null merged_into is no merge", sre.canonical("a", {"a": {"merged_into": None}}), "a")


def fresh(**kw):
    path = os.path.join(tempfile.mkdtemp(dir=TMP), "s.duckdb")
    store = Store(path)
    made = build(store, NOW, **kw)
    return store, catalog_from_db(store), made


def reading():
    print("== the fixture cell: four services, github is not one ==")
    store, cat, made = fresh()
    items = sre.list_services(store, cat, NOW)
    got = by_name(items)
    eq("the four services, busiest first", [i["name"] for i in items],
       ["service-a", "edge-proxy", "docs-site", "service-a-receiver"])
    ck("github is not a service", "example-org/service-a" not in got and "github" not in got)
    eq("service-a: 10 minutes", got["service-a"]["events_10m"], 3)
    eq("service-a: 24 hours", got["service-a"]["events_24h"], 6)
    eq("service-a signals", got["service-a"]["signals"], ["logs", "metrics", "traces"])
    eq("service-a feeds", got["service-a"]["feeds"], [{"name": "prod-otlp", "kind": "logs"},
                                                     {"name": "prod-otlp", "kind": "metrics"},
                                                     {"name": "prod-otlp", "kind": "traces"}])
    eq("service-a last seen", got["service-a"]["last_seen"], (NOW - timedelta(minutes=1)).isoformat())
    eq("edge-proxy 10m and 24h", (got["edge-proxy"]["events_10m"], got["edge-proxy"]["events_24h"]), (3, 5))
    eq("edge-proxy signals", got["edge-proxy"]["signals"], ["logs"])
    eq("docs-site (vercel, primary label project)", (got["docs-site"]["events_10m"],
       got["docs-site"]["events_24h"], got["docs-site"]["signals"],
       got["docs-site"]["feeds"]), (0, 2, ["logs"], [{"name": "docs-site", "kind": "logs"}]))
    r = got["service-a-receiver"]
    eq("a service silent for two days is still listed", (r["events_10m"], r["events_24h"]), (0, 0))
    eq("with its last seen", r["last_seen"], (NOW - timedelta(days=2)).isoformat())
    eq("and the signals it ever sent", r["signals"], ["logs"])
    eq("an item has exactly these fields", sorted(got["service-a"]),
       sorted(["name", "display", "hidden", "merged", "signals", "feeds", "events_10m",
               "events_24h", "last_seen", "watched", "watched_by"]))
    eq("display is the name until renamed", (got["service-a"]["display"], got["service-a"]["hidden"],
                                             got["service-a"]["merged"]), ("service-a", False, []))

    one = sre.get_service(store, cat, "service-a", NOW)
    eq("by_feed splits the numbers by signal", one["by_feed"], [
        {"name": "prod-otlp", "kind": "logs", "events_10m": 1, "events_24h": 2,
         "last_seen": (NOW - timedelta(minutes=1)).isoformat()},
        {"name": "prod-otlp", "kind": "metrics", "events_10m": 1, "events_24h": 2,
         "last_seen": (NOW - timedelta(minutes=6)).isoformat()},
        {"name": "prod-otlp", "kind": "traces", "events_10m": 1, "events_24h": 2,
         "last_seen": (NOW - timedelta(minutes=4)).isoformat()}])
    eq("the labels on its events", one["labels"], ["service"])
    eq("service-a has the list item's fields too", {k: one[k] for k in got["service-a"]},
       got["service-a"])
    edge = sre.get_service(store, cat, "edge-proxy", NOW)
    eq("edge-proxy's labels", edge["labels"],
       ["namespace", "nginx_status_code", "service"])
    eq("a silent service's by_feed", sre.get_service(store, cat, "service-a-receiver", NOW)["by_feed"],
       [{"name": "prod-otlp", "kind": "logs", "events_10m": 0, "events_24h": 0,
         "last_seen": (NOW - timedelta(days=2)).isoformat()}])
    eq("no label names when nothing came in 24 hours",
       sre.get_service(store, cat, "service-a-receiver", NOW)["labels"], [])
    eq("an unknown service", sre.get_service(store, cat, "nope", NOW), None)

    print("== the window edges ==")
    s2 = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "e.duckdb"))
    s2.upsert_catalog_source("api", "event_stream", "otlp", "5s",
                             {"labels": [{"name": "service", "primary": True}]})

    def ev(delta):
        t = NOW - delta
        return Envelope("api", "event_stream", "edge", "INFO", "x", t, {}, {"service": "edge"}, t)
    s2.append([ev(timedelta(minutes=10)), ev(timedelta(minutes=10, seconds=1)),
               ev(timedelta(hours=24)), ev(timedelta(hours=24, seconds=1))])
    e = sre.list_services(s2, catalog_from_db(s2), NOW)[0]
    eq("exactly 10 minutes back counts in 10m, one second more does not; same for 24h",
       (e["events_10m"], e["events_24h"]), (1, 3))

    print("== a read changes nothing ==")
    before = (store.sc.execute("SELECT COUNT(*) FROM services").fetchone()[0],
              store.con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
              store.list_catalog_sources())
    sre.list_services(store, cat, NOW)
    sre.get_service(store, cat, "service-a", NOW)
    after = (store.sc.execute("SELECT COUNT(*) FROM services").fetchone()[0],
             store.con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
             store.list_catalog_sources())
    eq("no service row, event or source changed, and there is no service row at all", after, before)
    eq("the services table starts empty", before[0], 0)
    empty = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "n.duckdb"))
    eq("an empty cell lists none", sre.list_services(empty, Catalog({}, []), NOW), [])
    eq("a catalog with no service source lists none", sre.list_services(
        empty, Catalog({"g": src("g", "github", [{"name": "repo", "primary": True}])}, []), NOW), [])
    ck("services is a state table", "services" in empty._state_tables())

    print("== what a person changed ==")
    store.set_service("service-a", "tester", display="Service A")
    got = by_name(sre.list_services(store, cat, NOW))
    eq("a display name", (got["service-a"]["name"], got["service-a"]["display"]), ("service-a", "Service A"))
    eq("the rest is untouched", got["service-a"]["events_24h"], 6)
    st = store.set_service("service-a", "tester", hidden=False)
    eq("only the given fields change", (st["display"], st["hidden"], st["updated_by"]),
       ("Service A", False, "tester"))
    store.set_service("service-a", "tester", display=None)
    eq("None clears a field", by_name(sre.list_services(store, cat, NOW))["service-a"]["display"],
       "service-a")

    store.set_service("docs-site", "tester", hidden=True)
    eq("hidden is dropped from the list", [i["name"] for i in sre.list_services(store, cat, NOW)],
       ["service-a", "edge-proxy", "service-a-receiver"])
    shown = by_name(sre.list_services(store, cat, NOW, include_hidden=True))
    eq("and shown with hidden=1", (sorted(shown), shown["docs-site"]["hidden"]),
       (["docs-site", "edge-proxy", "service-a", "service-a-receiver"], True))
    ck("a hidden service still resolves by name", sre.get_service(store, cat, "docs-site", NOW)
       is not None)
    store.set_service("docs-site", "tester", hidden=False)

    store.set_service("edge-proxy", "tester", merged_into="service-a")
    items = sre.list_services(store, cat, NOW)
    got = by_name(items)
    eq("a merged service is not its own row", sorted(got), ["docs-site", "service-a",
                                                           "service-a-receiver"])
    eq("its counts are added to the target", (got["service-a"]["events_10m"],
       got["service-a"]["events_24h"]), (6, 11))
    eq("the target lists what was merged", got["service-a"]["merged"], ["edge-proxy"])
    eq("signals and feeds stay one set", (got["service-a"]["signals"], len(got["service-a"]["feeds"])),
       (["logs", "metrics", "traces"], 3))
    eq("get_service of the merged name is the target", sre.get_service(
        store, cat, "edge-proxy", NOW)["name"], "service-a")
    ck("its labels include the merged service's",
       "nginx_status_code" in sre.get_service(store, cat, "service-a", NOW)["labels"])
    store.set_service("edge-proxy", "tester", merged_into="gateway")
    got = by_name(sre.list_services(store, cat, NOW))
    eq("a merge target never seen is listed with the merged counts",
       (got["gateway"]["events_10m"], got["gateway"]["events_24h"], got["gateway"]["merged"],
        got["gateway"]["signals"]), (3, 5, ["edge-proxy"], ["logs"]))
    eq("and service-a is back to its own", got["service-a"]["events_24h"], 6)
    store.set_service("service-a-receiver", "tester", merged_into="edge-proxy")
    got = by_name(sre.list_services(store, cat, NOW))
    eq("a chain ends at the last target and lists every name in it",
       (got["gateway"]["merged"], "service-a-receiver" in got, "edge-proxy" in got),
       (["edge-proxy", "service-a-receiver"], False, False))
    store.set_service("edge-proxy", "tester", merged_into=None)
    store.set_service("service-a-receiver", "tester", merged_into=None)
    eq("un-merging brings the four back", sorted(by_name(sre.list_services(store, cat, NOW))),
       ["docs-site", "edge-proxy", "service-a", "service-a-receiver"])
    store.set_service("edge-proxy", "tester", merged_into="service-a-receiver")
    store.set_service("service-a-receiver", "tester", merged_into="edge-proxy")
    items = sre.list_services(store, cat, NOW)
    ck("a merge loop ends and loses no event",
       sum(i["events_24h"] for i in items) == 13, str([(i["name"], i["events_24h"]) for i in items]))
    store.set_service("edge-proxy", "tester", merged_into=None)
    store.set_service("service-a-receiver", "tester", merged_into=None)

    store.set_service("service-a", "tester", repos=["example-org/service-a"])
    a = by_name(sre.list_services(store, cat, NOW))["service-a"]
    eq("repos: service-a gains deploys", a["signals"], ["deploys", "logs", "metrics", "traces"])
    eq("its deploys count with its events", (a["events_10m"], a["events_24h"]), (3, 8))
    one = sre.get_service(store, cat, "service-a", NOW)
    eq("and show in by_feed", [(f["name"], f["kind"], f["events_24h"]) for f in one["by_feed"]
                               if f["kind"] == "deploys"], [("github", "deploys", 2)])
    ck("a service with no repos has no deploys",
       "deploys" not in by_name(sre.list_services(store, cat, NOW))["edge-proxy"]["signals"])
    store.set_service("service-a", "tester", repos=[])
    eq("clearing repos", by_name(sre.list_services(store, cat, NOW))["service-a"]["signals"],
       ["logs", "metrics", "traces"])
    eq("the rows are kept", sorted(store.service_settings()),
       ["docs-site", "edge-proxy", "service-a", "service-a-receiver"])

    print("== the label override setting ==")
    store.set_setting("sre.service_labels", json.dumps({"prod-otlp": None}))
    eq("null: prod-otlp holds no services", [i["name"] for i in sre.list_services(store, cat, NOW)],
       ["docs-site"])
    store.set_setting("sre.service_labels", json.dumps({"prod-otlp": "namespace"}))
    eq("another label: services are its values", [(i["name"], i["events_24h"]) for i in
       sre.list_services(store, cat, NOW) if i["name"] != "docs-site"], [("default", 5)])
    store.set_setting("sre.service_labels", "not json")
    eq("a broken setting is ignored", len(sre.list_services(store, cat, NOW)), 4)
    store.set_setting("sre.service_labels", None)


async def over_http():
    print("== GET /api/services ==")
    from tares import daemon
    app = daemon.make_app()
    H = {"Authorization": f"Bearer {ROOT}"}
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)
        store = app.state.store
        r = await cx.get("/api/services")
        eq("an empty cell", (r.status_code, r.json()["services"]), (200, []))
        ck("with the time it was read", "updated_at" in r.json(), r.text)
        eq("one service that is not there", (await cx.get("/api/services/service-a")).status_code, 404)
        eq("a read adds nothing", store.sc.execute("SELECT COUNT(*) FROM services").fetchone()[0], 0)

        now = datetime.now(timezone.utc)
        build(store, now)
        app.state.runtime.reload_catalog()
        r = await cx.get("/api/services")
        body = r.json()
        eq("the four services", [i["name"] for i in body["services"]],
           ["service-a", "edge-proxy", "docs-site", "service-a-receiver"])
        eq("the counts", [(i["events_10m"], i["events_24h"]) for i in body["services"]],
           [(3, 6), (3, 5), (0, 2), (0, 0)])
        store.set_service("docs-site", "tester", hidden=True)
        eq("hidden=0 leaves a hidden one out", len((await cx.get("/api/services?hidden=0")).json()["services"]), 3)
        eq("hidden=1 has it", len((await cx.get("/api/services?hidden=1")).json()["services"]), 4)
        r = await cx.get("/api/services/service-a")
        eq("one service", (r.status_code, r.json()["name"], [f["kind"] for f in r.json()["by_feed"]],
                           r.json()["labels"]), (200, "service-a", ["logs", "metrics", "traces"], ["service"]))
        eq("a hidden one by name", (await cx.get("/api/services/docs-site")).status_code, 200)
        eq("an unknown one", (await cx.get("/api/services/nope")).status_code, 404)
        store.set_service("edge-proxy", "tester", merged_into="gateway")
        eq("a merged name answers with its target", (await cx.get("/api/services/edge-proxy")).json()["name"],
           "gateway")
        store.set_service("edge-proxy", "tester", merged_into=None)

        print("== who may read it ==")
        pid = (await cx.post("/api/projects", json={"template": "custom", "name": "Alpha",
                                                    "objects": []})).json()["id"]
        pk = (await cx.post(f"/api/projects/{pid}/keys", json={"name": "agent"})).json()["secret"]
        rk = (await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()["secret"]
        for path in ("/api/services", "/api/services/service-a"):
            eq(f"a project key gets 403 on {path}",
               (await cx.get(path, headers={"Authorization": f"Bearer {pk}"})).status_code, 403)
            eq(f"a read key reads {path}",
               (await cx.get(path, headers={"Authorization": f"Bearer {rk}"})).status_code, 200)
            eq(f"no credential gets 401 on {path}", (await httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t").get(path)).status_code, 401)

        print("== 200,000 events ==")
        t0 = time.time()
        made = build(store, now, events=200_000)
        built = time.time() - t0
        store.set_service("docs-site", "tester", hidden=False)
        t0 = time.time()
        r = await cx.get("/api/services")
        took = time.time() - t0
        print(f"  build {built:.1f}s; GET /api/services with {made['events'] + 26:,} events took {took:.3f}s")
        got = by_name(r.json()["services"])
        eq("edge-proxy and service-a hold the lines (the small cell was built twice)",
           (got["edge-proxy"]["events_24h"], got["service-a"]["events_24h"]),
           (10 + 100_000, 12 + 100_000))
        ck("the whole list answers in under 2 seconds", took < 2.0, f"{took:.2f}s")
        t0 = time.time()
        r = await cx.get("/api/services/service-a")
        one = time.time() - t0
        print(f"  GET /api/services/service-a took {one:.3f}s")
        ck("and one service too", r.status_code == 200 and one < 2.0, f"{one:.2f}s")


pure()
reading()
asyncio.run(over_http())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
