"""Services as a list the cell knows (TR-742, the write side).

PATCH /api/services/{name} (display name, hidden, merge: what is refused and why, what is kept
when a field is left out, who is recorded), PUT /api/settings/service-labels (what it stores,
what it refuses, that it replaces the mapping and changes which label names a service), the
30-second cache of the computed services (one compute shared by the list and a single service,
dropped by every write and by a changed source, never used when a test passes its own `now`),
and who may write (a read key and a project key get 403, no credential 401).

The 200,000-event cell is in tests/test_services.py; this one builds the small cell only.

Run: .venv/bin/python tests/test_services_write.py
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
ROOT = "root-token-services-write"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

from fixtures.sre_cell import build
from tares import sre
from tares.config import catalog_from_db
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


def names(r):
    return [i["name"] for i in r.json()["services"]]


def by_name(r):
    return {i["name"]: i for i in r.json()["services"]}


class Clock:
    """The cache's clock: a test moves it instead of sleeping."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Computes:
    """Counts the real computes behind the services (the seam is sre._compute)."""
    def __init__(self):
        self.n = 0
        self.real = sre._compute
        self.during = None   # a function to run inside the next compute, once

    def __call__(self, *a, **kw):
        self.n += 1
        out = self.real(*a, **kw)
        if self.during:
            f, self.during = self.during, None
            f()
        return out


async def writes():
    from tares import daemon
    app = daemon.make_app()
    H = {"Authorization": f"Bearer {ROOT}"}
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)
        store, runtime = app.state.store, app.state.runtime
        now = datetime.now(timezone.utc)
        build(store, now)
        runtime.reload_catalog()
        clock = Clock()
        sre.cache_clock = clock

        print("== PATCH: display name, hidden, merge ==")
        r = await cx.patch("/api/services/service-a", json={"display": "  Service A  "})
        eq("a rename answers the service", (r.status_code, r.json()["name"], r.json()["display"]),
           (200, "service-a", "Service A"))
        eq("it answers what GET answers", r.json(), (await cx.get("/api/services/service-a")).json())
        ck("with by_feed and labels", "by_feed" in r.json() and "labels" in r.json(), str(sorted(r.json())))
        eq("the list shows the display name, the name is the same",
           [(i["name"], i["display"]) for i in (await cx.get("/api/services")).json()["services"]
            if i["name"] == "service-a"], [("service-a", "Service A")])
        r = await cx.patch("/api/services/service-a", json={"hidden": False})
        eq("a field left out stays", (r.json()["display"], r.json()["hidden"]), ("Service A", False))
        await cx.patch("/api/services/service-a", json={"hidden": True})
        r = await cx.patch("/api/services/service-a", json={"display": "Service A"})
        eq("a hidden service stays hidden when only its name is sent", r.json()["hidden"], True)
        await cx.patch("/api/services/service-a", json={"hidden": False})
        r = await cx.patch("/api/services/service-a", json={"display": None})
        eq("null clears the display name", r.json()["display"], "service-a")
        await cx.patch("/api/services/service-a", json={"display": "Service A"})
        r = await cx.patch("/api/services/service-a", json={"display": "   "})
        eq("blank text clears it too", r.json()["display"], "service-a")
        eq("nothing else changed", (r.json()["events_24h"], r.json()["signals"]),
           (6, ["logs", "metrics", "traces"]))

        r = await cx.patch("/api/services/docs-site", json={"hidden": True})
        eq("hiding answers the service, hidden", (r.status_code, r.json()["hidden"]), (200, True))
        eq("the default list drops it", names(await cx.get("/api/services")),
           ["service-a", "edge-proxy", "service-a-receiver"])
        eq("hidden=1 has it", len(names(await cx.get("/api/services?hidden=1"))), 4)
        eq("a hidden service can still be changed", (await cx.patch(
            "/api/services/docs-site", json={"display": "Docs"})).status_code, 200)
        r = await cx.patch("/api/services/docs-site", json={"hidden": False})
        eq("and shown again", (r.json()["hidden"], r.json()["display"]), (False, "Docs"))
        eq("it is back in the list", len(names(await cx.get("/api/services"))), 4)

        r = await cx.patch("/api/services/edge-proxy", json={"merged_into": "service-a-ui"})
        eq("a merge answers the target, with the merged counts", (
            r.status_code, r.json()["name"], r.json()["merged"], r.json()["events_10m"],
            r.json()["events_24h"]), (200, "service-a-ui", ["edge-proxy"], 3, 5))
        eq("the list has the target and not the merged name", names(await cx.get("/api/services")),
           ["service-a", "service-a-ui", "docs-site", "service-a-receiver"])
        r = await cx.patch("/api/services/service-a", json={"merged_into": "service-a-ui"})
        eq("a second service merged into it adds up", (r.json()["merged"], r.json()["events_24h"]),
           (["edge-proxy", "service-a"], 11))
        r = await cx.patch("/api/services/edge-proxy", json={"display": "Edge"})
        eq("a merged name can be changed, the answer is its target", (r.status_code, r.json()["name"]),
           (200, "service-a-ui"))
        eq("and its row took the change", store.service_settings()["edge-proxy"]["display"], "Edge")
        await cx.patch("/api/services/edge-proxy", json={"merged_into": None})
        r = await cx.patch("/api/services/service-a", json={"merged_into": ""})
        eq("null or empty undoes a merge", (r.json()["name"], sorted(names(await cx.get("/api/services")))),
           ("service-a", ["docs-site", "edge-proxy", "service-a", "service-a-receiver"]))
        r = await cx.patch("/api/services/service-a", json={"merged_into": "edge-proxy"})
        eq("a merge into a service that exists", (r.json()["name"], r.json()["merged"]),
           ("edge-proxy", ["service-a"]))
        r = await cx.patch("/api/services/edge-proxy", json={"merged_into": "docs-site"})
        eq("what is merged into a service follows it when it is merged on", (
            r.json()["name"], r.json()["merged"]), ("docs-site", ["edge-proxy", "service-a"]))
        for n in ("service-a", "edge-proxy"):
            await cx.patch(f"/api/services/{n}", json={"merged_into": None})

        print("== PATCH: who is recorded ==")
        eq("the credential's name", store.service_settings()["service-a"]["updated_by"],
           "auth token (env)")
        ak = (await cx.post("/api/keys", json={"name": "ops-admin", "scopes": ["admin"]})).json()["secret"]
        await cx.patch("/api/services/service-a", json={"hidden": False},
                       headers={"Authorization": f"Bearer {ak}"})
        eq("an admin key's name", store.service_settings()["service-a"]["updated_by"], "ops-admin")
        ck("the time", store.service_settings()["service-a"]["updated_at"] is not None)

        print("== PATCH: what is refused ==")
        before = store.service_settings()
        bad = [
            ("a list", ["hidden"], "JSON object"),
            ("a string", "hidden", "JSON object"),
            ("an empty body", {}, "nothing to change"),
            ("an unknown field", {"colour": "red"}, "cannot change colour"),
            ("repos, which the API does not set", {"repos": ["example-org/service-a"]},
             "cannot change repos"),
            ("a field that is not allowed beside a good one", {"hidden": True, "by": "x"},
             "cannot change by"),
            ("a display name that is not text", {"display": 5}, "display must be text"),
            ("a display name over 100 characters", {"display": "x" * 101}, "at most 100"),
            ("hidden that is not true or false", {"hidden": "yes"}, "hidden must be true"),
            ("a merge target that is not text", {"merged_into": 7}, "merged_into must be text"),
            ("a merge target that is far too long", {"merged_into": "n" * 201}, "at most 200"),
            ("a merge into itself", {"merged_into": "service-a"}, "cannot be merged into itself"),
            ("a merge into itself with spaces around", {"merged_into": " service-a "},
             "cannot be merged into itself"),
        ]
        for label, body, want in bad:
            r = await cx.patch("/api/services/service-a", json=body)
            ck(f"{label}: 400 saying why", r.status_code == 400 and want in r.json()["detail"],
               f"{r.status_code} {r.text}")
        ck("a body that is not JSON: 422 or 400", (await cx.patch(
            "/api/services/service-a", content=b"nope", headers={"content-type": "application/json"}
        )).status_code in (400, 422))
        ck("a good field beside a bad one writes nothing", (await cx.patch(
            "/api/services/service-a", json={"display": "Fine", "hidden": "yes"})).status_code == 400)
        eq("none of it wrote anything", store.service_settings(), before)
        eq("a name over 200 characters", (await cx.patch("/api/services/" + "n" * 201, json={
            "hidden": True})).status_code, 400)

        r = await cx.patch("/api/services/nope", json={"hidden": True})
        eq("a name the cell does not know: 404", (r.status_code, r.json()["detail"]),
           (404, "no service named nope"))
        ck("and no row was made", "nope" not in store.service_settings())
        r = await cx.patch("/api/services/x", json={"merged_into": "x"})
        eq("a merge into itself is 400 before the name is looked up", r.status_code, 400)
        eq("a service with no row yet can be changed", (await cx.patch(
            "/api/services/service-a-receiver", json={"display": "Receiver"})).status_code, 200)

        await cx.patch("/api/services/edge-proxy", json={"merged_into": "service-a"})
        r = await cx.patch("/api/services/docs-site", json={"merged_into": "edge-proxy"})
        ck("a merge into a service that is merged elsewhere is refused, and says where to",
           r.status_code == 400 and "already merged into service-a" in r.json()["detail"]
           and "merge docs-site into service-a instead" in r.json()["detail"], r.text)
        r = await cx.patch("/api/services/service-a", json={"merged_into": "edge-proxy"})
        eq("no loop: the target is merged into this service", r.status_code, 400)
        eq("the first merge is intact", store.service_settings()["edge-proxy"]["merged_into"], "service-a")
        eq("and the refused one wrote nothing", store.service_settings()["docs-site"]["merged_into"], None)
        await cx.patch("/api/services/edge-proxy", json={"merged_into": None})

        # a chain made from the far end in: n0 into n1 is the last link added each time
        chain = [f"c{i}" for i in range(7)]
        store.append([Envelope("prod-otlp", "event_stream", c, "INFO", "x", now, {},
                               {"service": c}, now) for c in chain])
        store.set_service("c0", "setup", merged_into="c1")
        ok = []
        for i in range(1, 5):
            ok.append((await cx.patch(f"/api/services/c{i}", json={"merged_into": f"c{i + 1}"})).status_code)
        eq("a chain of up to five links is allowed", ok, [200, 200, 200, 200])
        r = await cx.patch("/api/services/c5", json={"merged_into": "c6"})
        ck("a sixth link would hide the far end, so it is refused and says so",
           r.status_code == 400 and "more than 5 services" in r.json()["detail"], r.text)
        eq("c5 stays unmerged", store.service_settings().get("c5", {}).get("merged_into"), None)
        eq("the five links still resolve", sre.canonical("c0", store.service_settings()), "c5")
        r = await cx.patch("/api/services/c6", json={"merged_into": "c0"})
        ck("a merge into the start of the chain is refused: that one is merged on",
           r.status_code == 400 and "already merged into c1" in r.json()["detail"], r.text)
        eq("c6 stays unmerged", store.service_settings().get("c6", {}).get("merged_into"), None)

        print("== who may write ==")
        rk = (await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()["secret"]
        pid = (await cx.post("/api/projects", json={"template": "custom", "name": "Alpha",
                                                    "objects": []})).json()["id"]
        pk = (await cx.post(f"/api/projects/{pid}/keys", json={"name": "agent"})).json()["secret"]
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        snapshot = (store.service_settings(), store.get_setting("sre.service_labels"))
        for label, method, path, body in (
                ("PATCH", "PATCH", "/api/services/service-a", {"hidden": True}),
                ("PUT", "PUT", "/api/settings/service-labels", {"prod-otlp": None})):
            eq(f"a read key cannot {label}", (await cx.request(
                method, path, json=body, headers={"Authorization": f"Bearer {rk}"})).status_code, 403)
            eq(f"a project key cannot {label}", (await cx.request(
                method, path, json=body, headers={"Authorization": f"Bearer {pk}"})).status_code, 403)
            eq(f"no credential: 401 on {label}", (await anon.request(method, path, json=body)).status_code, 401)
        eq("none of those wrote anything",
           (store.service_settings(), store.get_setting("sre.service_labels")), snapshot)
        eq("an admin key can PUT", (await cx.put("/api/settings/service-labels", json={},
           headers={"Authorization": f"Bearer {ak}"})).status_code, 200)

        print("== PUT /api/settings/service-labels ==")
        eq("an empty mapping", (await cx.put("/api/settings/service-labels", json={})).json(), {})
        eq("the four and the chain's two ends are listed", len(names(await cx.get("/api/services"))), 6)
        r = await cx.put("/api/settings/service-labels", json={"prod-otlp": "namespace"})
        eq("it answers the stored mapping", (r.status_code, r.json()), (200, {"prod-otlp": "namespace"}))
        eq("and stores it as the setting", json.loads(store.get_setting("sre.service_labels")),
           {"prod-otlp": "namespace"})
        eq("the label that names a service changed: the services are the label's values",
           [(i["name"], i["events_24h"]) for i in (await cx.get("/api/services")).json()["services"]
            if i["name"] != "docs-site"], [("default", 5)])
        r = await cx.put("/api/settings/service-labels", json={"prod-otlp": None})
        eq("null: that source holds no services", names(await cx.get("/api/services")), ["docs-site"])
        r = await cx.put("/api/settings/service-labels", json={"github": "repo"})
        eq("it replaces the mapping, it does not add to it", (r.json(), sorted(
            names(await cx.get("/api/services")))), ({"github": "repo"}, [
                "c5", "c6", "docs-site", "edge-proxy", "example-org/service-a", "service-a",
                "service-a-receiver"]))
        # a service named like a repo has a slash in it: GET and PATCH take the whole name
        r = await cx.get("/api/services/example-org/service-a")
        eq("a name with a slash: GET", (r.status_code, r.json().get("name")), (200, "example-org/service-a"))
        eq("the same name with the slash encoded", (await cx.get(
            "/api/services/example-org%2Fservice-a")).json().get("name"), "example-org/service-a")
        r = await cx.patch("/api/services/example-org/service-a", json={"display": "Repo A"})
        eq("a name with a slash: PATCH", (r.status_code, r.json().get("name"), r.json().get("display")),
           (200, "example-org/service-a", "Repo A"))
        eq("and it is stored under the whole name", store.service_settings()["example-org/service-a"]["display"],
           "Repo A")
        r = await cx.get("/api/services/example-org/nothing-here")
        eq("an unknown name with a slash is 404 saying so", (r.status_code, "no service named" in r.text),
           (404, True))
        r = await cx.patch("/api/services/example-org/nothing-here", json={"hidden": True})
        eq("and so is a PATCH for it", (r.status_code, "no service named" in r.text), (404, True))
        r = await cx.put("/api/settings/service-labels", json={})
        eq("an empty mapping puts the defaults back", sorted(names(await cx.get("/api/services"))),
           ["c5", "c6", "docs-site", "edge-proxy", "service-a", "service-a-receiver"])
        # a stored label that is not a label name (written by hand): that source keeps its default
        for bad in ({"prod-otlp": "bad label"}, {"prod-otlp": 7}, {"prod-otlp": "a'; --", "github": ["x"]},
                    [], "text"):
            store.set_setting("sre.service_labels", json.dumps(bad))
            r = await cx.get("/api/services")
            eq(f"a stored label of {bad!r}: the list still answers, as with no override",
               (r.status_code, sorted(names(r)) if r.status_code == 200 else r.text[:80]),
               (200, ["c5", "c6", "docs-site", "edge-proxy", "service-a", "service-a-receiver"]))
            eq("and so does one service", (await cx.get("/api/services/service-a")).status_code, 200)
        store.set_setting("sre.service_labels", json.dumps({"prod-otlp": "bad label", "github": None}))
        eq("a null entry beside a bad one: the list still answers",
           (await cx.get("/api/services")).status_code, 200)
        await cx.put("/api/settings/service-labels", json={})
        before = store.get_setting("sre.service_labels")
        events = store.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        for label, body, want in (
                ("a list", [], "JSON object"),
                ("a source the cell does not have", {"nope": "service"}, "no source named nope"),
                ("one good and one unknown source", {"prod-otlp": "service", "nope": None}, "nope"),
                ("a label with a space", {"prod-otlp": "my label"}, "letters, digits and underscores"),
                ("a label that is SQL", {"prod-otlp": "a'; DROP TABLE events; --"}, "letters, digits"),
                ("an empty label", {"prod-otlp": ""}, "or null"),
                ("a label that is a number", {"prod-otlp": 5}, "or null"),
                ("a label with a dot", {"prod-otlp": "a.b"}, "letters, digits")):
            r = await cx.put("/api/settings/service-labels", json=body)
            ck(f"{label}: 400 saying why", r.status_code == 400 and want in r.json()["detail"],
               f"{r.status_code} {r.text}")
        eq("none of them changed the setting", store.get_setting("sre.service_labels"), before)
        eq("the events are still there", store.con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
           events)

        sre.cache_clock = time.monotonic
    print("== who is recorded with no auth at all ==")
    # a cell with auth off has no credential: the change is made by "console"
    daemon.AUTH_TOKEN = ""
    os.environ.pop("TARES_AUTH_TOKEN", None)
    daemon.DB_PATH = os.path.join(TMP, "open.duckdb")
    app2 = daemon.make_app()
    async with app2.router.lifespan_context(app2):
        cx2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t")
        s2 = app2.state.store
        build(s2, datetime.now(timezone.utc))
        app2.state.runtime.reload_catalog()
        r = await cx2.patch("/api/services/service-a", json={"hidden": True})
        eq("PATCH on an open cell", r.status_code, 200)
        eq("the change is by console", s2.service_settings()["service-a"]["updated_by"], "console")
        eq("PUT works on an open cell", (await cx2.put(
            "/api/settings/service-labels", json={"docs-site": "project"})).status_code, 200)


def cache():
    print("== the 30-second cache ==")
    path = os.path.join(tempfile.mkdtemp(dir=TMP), "c.duckdb")
    store = Store(path)
    NOW = datetime.now(timezone.utc)
    build(store, NOW)
    cat = catalog_from_db(store)
    clock = Clock()
    sre.cache_clock = clock
    spy = Computes()
    sre._compute = spy
    try:
        first = sre.list_services(store, cat)
        eq("the first read computes", spy.n, 1)
        eq("the same read within the time does not", (sre.list_services(store, cat), spy.n),
           (first, 1))
        sre.list_services(store, cat, include_hidden=True)
        sre.get_service(store, cat, "service-a")
        sre.get_service(store, cat, "docs-site")
        eq("hidden or not, and one service, share the one compute", spy.n, 1)

        t = Envelope("prod-otlp", "event_stream", "service-a", "INFO", "late line",
                     NOW, {}, {"service": "service-a"}, NOW)
        store.append([t])
        clock.t += 29.9
        eq("29.9 seconds on, the answer is still the cached one (a new line is not in it)",
           (sre.list_services(store, cat)[0]["events_24h"], spy.n), (6, 1))
        clock.t += 0.2
        eq("30.1 seconds on, it is computed again and has the line",
           (sre.list_services(store, cat)[0]["events_24h"], spy.n), (7, 2))
        eq("and that is kept again", (sre.list_services(store, cat)[0]["events_24h"], spy.n), (7, 2))

        sre.list_services(store, cat, NOW)
        sre.list_services(store, cat, NOW)
        sre.get_service(store, cat, "service-a", NOW)
        eq("a call with its own now is never served from the cache, and never fills it", spy.n, 5)

        n = spy.n
        store.set_service("docs-site", "t", hidden=True)
        sre.list_services(store, cat)
        eq("store.set_service drops it, whoever calls it", spy.n, n + 1)
        eq("and the new answer is the one kept", (len(sre.list_services(store, cat)), spy.n), (3, n + 1))

        n = spy.n
        store.clear_services_cache()
        sre.list_services(store, cat)
        eq("clear_services_cache drops it", spy.n, n + 1)

        # a changed source shows at once: a new source whose events name services, then its removal
        n = spy.n
        store.upsert_catalog_source("edge-logs", "event_stream", "docker_logs", "1m",
                                    {"labels": [{"name": "service", "primary": True}]})
        cat2 = catalog_from_db(store)
        sre.list_services(store, cat2)
        eq("a source that holds services was added: computed again", spy.n, n + 1)
        eq("an unchanged catalog keeps it", (sre.list_services(store, cat2) and spy.n), n + 1)
        n = spy.n
        sre.list_services(store, cat)
        eq("the old catalog is a different key: computed again", spy.n, n + 1)
        sre.list_services(store, cat2)   # this catalog's entry is the one kept now
        store.set_setting("sre.service_labels", json.dumps({"prod-otlp": "namespace"}))
        n = spy.n
        eq("a changed label setting shows with no wait (docs-site is hidden)", (
            [i["name"] for i in sre.list_services(store, cat2)], spy.n), (["default"], n + 1))
        store.set_setting("sre.service_labels", None)
        sre.list_services(store, cat2)

        # a write that lands while a compute is running: that compute must not be kept
        store.clear_services_cache()
        n = spy.n
        spy.during = lambda: store.set_service("service-a", "t", display="During")
        sre.list_services(store, cat2)
        got = {i["name"]: i for i in sre.list_services(store, cat2)}
        eq("a write during a compute: the next read computes again", spy.n, n + 2)
        eq("and shows the write", got["service-a"]["display"], "During")

        # each store has its own entry
        other = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "o.duckdb"))
        n = spy.n
        eq("another store is empty, not a copy of this one's", (sre.list_services(
            other, catalog_from_db(other)), spy.n), ([], n + 1))
        ck("and this one's is still its own", len(sre.list_services(store, cat2)) == 3)
    finally:
        sre._compute = spy.real
        sre.cache_clock = time.monotonic


def counts():
    print("== the event scan groups by span or not ==")
    store = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "w.duckdb"))
    NOW = datetime.now(timezone.utc)
    build(store, NOW)
    store.append([Envelope("prod-otlp", kind, "service-a", "INFO", "x", NOW, {}, {"service": "service-a"}, NOW)
                  for kind in ("log", "event_stream", "other_kind")])
    rows = store.service_window_counts({"prod-otlp": "service"}, NOW - timedelta(minutes=10),
                                       NOW - timedelta(hours=24))
    mine = [r for r in rows if r["value"] == "service-a"]
    eq("event types are span or none", sorted({str(r["event_type"]) for r in mine}), ["None", "span"])
    eq("one row for the logs whatever their raw event type, one for spans, one for metrics",
       len(mine), 3)
    eq("and the logs row has all of them", sum(
        r["n24"] for r in mine if r["event_type"] is None and not r["is_metric"]) >= 3, True)


async def cache_over_http():
    print("== the cache over HTTP: every write drops it ==")
    from tares import daemon
    daemon.AUTH_TOKEN = ROOT
    os.environ["TARES_AUTH_TOKEN"] = ROOT
    daemon.DB_PATH = os.path.join(TMP, "cache.duckdb")
    app = daemon.make_app()
    H = {"Authorization": f"Bearer {ROOT}"}
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)
        store, runtime = app.state.store, app.state.runtime
        # the daemon's watch loop (TR-743) reads the services once at start; let that look end
        # before the build, so it does not count as a compute of the test
        await asyncio.sleep(1)
        build(store, datetime.now(timezone.utc))
        runtime.reload_catalog()
        store.clear_services_cache()
        clock = Clock()
        sre.cache_clock = clock
        spy = Computes()
        sre._compute = spy
        try:
            await cx.get("/api/services")
            await cx.get("/api/services?hidden=1")
            await cx.get("/api/services/service-a")
            await cx.get("/api/services/edge-proxy")
            eq("two list calls and two single reads: one compute", spy.n, 1)
            r = await cx.patch("/api/services/service-a", json={"display": "Service A"})
            eq("a PATCH drops it: its own answer is fresh", (r.json()["display"], spy.n), ("Service A", 2))
            n = spy.n
            r = await cx.get("/api/services")
            eq("and what it computed is kept for the next read", (spy.n, [
                i["display"] for i in r.json()["services"] if i["name"] == "service-a"]),
               (n, ["Service A"]))
            r = await cx.patch("/api/services/service-a", json={"colour": "red"})
            eq("a refused PATCH does not drop it", (r.status_code, spy.n), (400, n))
            r = await cx.patch("/api/services/nope", json={"hidden": True})
            eq("nor a PATCH for an unknown name", (r.status_code, spy.n), (404, n))
            await cx.get("/api/services")
            n = spy.n
            # the same label for the same source: the cache key does not change, so only the
            # PUT's own drop can make the next read compute again
            r = await cx.put("/api/settings/service-labels", json={"docs-site": "project"})
            await cx.get("/api/services")
            eq("a PUT drops it", (r.status_code, spy.n), (200, n + 1))
            n = spy.n
            r = await cx.put("/api/settings/service-labels", json={"docs-site": "no good"})
            await cx.get("/api/services")
            eq("a refused PUT does not", (r.status_code, spy.n), (400, n))
        finally:
            sre._compute = spy.real
            sre.cache_clock = time.monotonic


def main():
    asyncio.run(writes())
    cache()
    counts()
    asyncio.run(cache_over_http())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
