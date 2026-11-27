"""Project keys (TR-335, TR-336).

A project key reads ONE project; every other route answers 403. The sweep walks every route the
daemon serves, so a route added later without a decision for project keys shows up here as a
failure rather than as a hole. Then: reads, stats, catalog and project listings narrowed to the
project; its timeline only; revoking the key; the MCP tools; and the same tools on an open
instance, where they pick the project by name. (Skills, findings and subscribing to a project
went with Tares agents and triggers, TR-1810.)

Run: .venv/bin/python tests/test_project_keys.py
"""
import asyncio
import json
import os
import tempfile

# a folder per run: the session store is a file next to the database, and in a shared folder one
# checkout's newer schema would stop every other checkout's run
TMP = tempfile.mkdtemp(prefix="tares-project-keys-")
DB = os.path.join(TMP, "keys.duckdb")
OPEN_DB = os.path.join(TMP, "keys-open.duckdb")
ROOT = "root-token-project-keys"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = os.path.join(TMP, "catalog.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx
from fastapi.routing import APIRoute

from tares.config import FINDINGS_SOURCE

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def source(name):
    return {"name": name, "connector": "webhook", "poll": "5s",
            "config": {"event_type": "log", "text_template": "{msg}",
                       "labels": [{"name": "service", "field": "service", "primary": True}]}}


COND = {"aggregate": "count", "predicate": "> 0", "window": "5m"}


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def fill(path: str, vals: dict) -> str:
    for k, v in vals.items():
        path = path.replace("{" + k + "}", v)
    return path


async def main():
    from tares import daemon
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        store = app.state.store

        async def mk(kind, body, code=201):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == code, (kind, r.status_code, r.text)
            return r.json()

        a = (await mk("projects", {"template": "custom", "name": "Alpha", "objects": []}))["id"]
        b = (await mk("projects", {"template": "custom", "name": "Beta", "objects": []}))["id"]
        await mk("sources", {**source("evt_a"), "project": a})
        await mk("sources", {**source("evt_b"), "project": b})
        # the triggers are catalog rows only now (no route makes one, nothing evaluates them)
        for name, src, proj in (("watch_a", "evt_a", a), ("watch_b", "evt_b", b)):
            store.upsert_catalog_trigger(name, [src], COND, {}, "1h", key_field="service")
            store.put_in_project("trigger", name, proj)
        app.state.runtime.reload_catalog()

        print("== creating project keys ==")
        r = await cx.post(f"/api/projects/{a}/keys", json={"name": "claude-code"})
        ck("POST /api/projects/{uid}/keys makes a project key",
           r.status_code == 201 and r.json()["scopes"] == ["read"]
           and r.json()["project"] == a and r.json()["secret"].startswith("nvf_"), r.text)
        key_a, kid_a = r.json()["secret"], r.json()["id"]
        r = await cx.post("/api/keys", json={"name": "cursor", "project": "Alpha"})
        ck("POST /api/keys with project (a name) makes one too",
           r.status_code == 201 and r.json()["project"] == a
           and r.json()["scopes"] == ["read"], r.text)
        kid_a2 = r.json()["id"]
        r = await cx.post("/api/keys", json={"name": "b-agent", "project": b, "scopes": ["read"]})
        ck("a read-only project key", r.status_code == 201 and r.json()["scopes"] == ["read"], r.text)
        key_b_ro = r.json()["secret"]
        ck("a project key with the admin scope is refused",
           (await cx.post("/api/keys", json={"name": "x", "project": a,
                                              "scopes": ["admin"]})).status_code == 400)
        ck("the findings scope is gone (TR-1810): a project key asking for it is refused",
           (await cx.post("/api/keys", json={"name": "x", "project": a, "scopes": ["findings"]})).status_code == 400)
        ck("an unknown project is refused",
           (await cx.post("/api/keys", json={"name": "x", "project": "nope"})).status_code == 400)
        r = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
        read_key = r.json()["secret"]
        lk = (await cx.get(f"/api/projects/{a}/keys")).json()["keys"]
        ck("the project lists its keys, no secrets",
           {k["name"] for k in lk} == {"claude-code", "cursor"} and "secret" not in json.dumps(lk, default=str))
        ck("a read key cannot list a project's keys",
           (await cx.get(f"/api/projects/{a}/keys", headers=H(read_key))).status_code == 403)
        ck("a project key cannot make keys",
           (await cx.post(f"/api/projects/{a}/keys", json={"name": "y"}, headers=H(key_a))).status_code == 403)
        who = (await cx.get("/api/whoami", headers=H(key_a))).json()
        ck("whoami names the key's project", who.get("project") == a
           and who.get("project_name") == "Alpha", str(who))

        # something to read: events in both projects' sources
        await cx.post("/ingest/evt_a", json={"service": "checkout", "msg": "alpha 500"})
        await cx.post("/ingest/evt_b", json={"service": "checkout", "msg": "beta 500"})

        print("== every route, as a project key ==")
        vals = {"uid": b, "name": "evt_b", "handle": "source:evt_b", "token": "evt_b",
                "kid": kid_a2, "sid": "s", "dispatch_id": "d", "run_id": "r",
                "provider_id": "anthropic", "key": "custom", "path": "x"}
        # reachable without any credential: their own authentication or nothing to protect
        public = {("GET", "/health"), ("GET", "/metrics"), ("GET", "/ingest/{token}"),
                  ("POST", "/api/slack/events"), ("GET", "/{path:path}"),
                  ("GET", "/derive"),   # a GET outside /api is the console's; this one says views are gone
                  # GitHub sends the browser back here: gated by a signed state / proof of ownership
                  ("GET", "/api/integrations/github/apps/callback"),
                  ("GET", "/api/integrations/github/apps/installed")}
        # allowed, and narrowed to the key's project by the handler
        scoped = {("GET", "/api/whoami"), ("POST", "/read"), ("GET", "/catalog"),
                  ("GET", "/catalog/{handle}"), ("GET", "/api/projects")}
        bad = []
        n = 0
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods):
                key = (method, route.path)
                if key in public:
                    continue
                path = fill(route.path.replace("{path:path}", "{path}"), vals)
                r = await cx.request(method, path, headers=H(key_a),
                                     **({"json": {}} if method in ("POST", "PUT") else {}))
                n += 1
                if key in scoped:
                    if key == ("GET", "/catalog/{handle}"):
                        if r.status_code != 403:   # another project's source
                            bad.append((method, path, r.status_code))
                    elif r.status_code in (401, 403):
                        bad.append((method, path, r.status_code))
                elif r.status_code != 403:
                    bad.append((method, path, r.status_code, r.text[:80]))
        ck(f"{n} route/method pairs: 403 unless allowed for the key's own project", not bad,
           str(bad))
        # its OWN project: only reading it
        own_ok = {("GET", "/api/projects/{uid}"), ("GET", "/api/projects/{uid}/timeline"),
                  ("POST", "/api/projects/{uid}/stats"),
                  # the goal-first page's reads
                  ("GET", "/api/projects/{uid}/outline"), ("GET", "/api/projects/{uid}/health"),
                  # docs and tickets (TR-403): what a session building the project reads
                  ("GET", "/api/projects/{uid}/docs"), ("GET", "/api/projects/{uid}/docs/{doc_id}"),
                  ("GET", "/api/projects/{uid}/tickets"),
                  ("GET", "/api/projects/{uid}/tickets/{ref}"),
                  ("GET", "/api/projects/{uid}/sessions"),
                  ("GET", "/api/projects/{uid}/sessions/{sid}"),
                  ("GET", "/api/projects/{uid}/pickup"),
                  # the factory's plan (TR-411)
                  ("GET", "/api/projects/{uid}/milestones"),
                  # what an accepted milestone delivered (TR-1645)
                  ("GET", "/api/projects/{uid}/milestones/{ref}/summary"),
                  # the crew that works on it (TR-422)
                  ("GET", "/api/projects/{uid}/crew"),
                  # the crew's messages about a ticket (TR-429)
                  ("GET", "/api/projects/{uid}/tickets/{ref}/messages"),
                  # what builders assumed (TR-433)
                  ("GET", "/api/projects/{uid}/assumptions"),
                  # the person's decisions on it (TR-451)
                  ("GET", "/api/projects/{uid}/decisions")}
        bad, n = [], 0
        for route in app.routes:
            if not isinstance(route, APIRoute) or "{uid}" not in route.path:
                continue
            for method in sorted(route.methods):
                if (method, route.path) in own_ok:
                    continue
                path = fill(route.path, {**vals, "uid": a, "name": "triage"})
                r = await cx.request(method, path, headers=H(key_a),
                                     **({"json": {}} if method in ("POST", "PUT") else {}))
                n += 1
                if r.status_code != 403:
                    bad.append((method, path, r.status_code))
        ck(f"{n} writes and settings of its own project: 403", not bad, str(bad))
        ck("it is still there", (await cx.get(f"/api/projects/{a}")).status_code == 200)
        r = await cx.post(f"/api/projects/{b}/stats", json={"by": "service"}, headers=H(key_a))
        ck("the 403 says what the key may do",
           r.status_code == 403 and "only reads project Alpha" in r.text, r.text)

        print("== another project's resources, by id and by name ==")
        for label, method, path, body in (
                ("timeline", "GET", f"/api/projects/{b}/timeline", None),
                ("stats", "POST", f"/api/projects/{b}/stats", {"by": "service"}),
                ("the project", "GET", f"/api/projects/{b}", None),
                ("the project by name", "GET", "/api/projects/Beta", None),
                ("the source", "GET", "/catalog/source:evt_b", None),
                ("the trigger", "GET", "/catalog/trigger:watch_b", None),
                ("the shared findings source", "GET", f"/catalog/source:{FINDINGS_SOURCE}", None),
                ("an unknown source", "GET", "/catalog/source:nope", None),
                ("read naming the project", "POST", "/read", {"selector": {"service": "checkout"}, "project": b}),
                ("read naming it by name", "POST", "/read", {"selector": {"service": "checkout"}, "project": "Beta"}),
                ("read naming its source", "POST", "/read", {"selector": {"service": "checkout"}, "sources": ["evt_b"]}),
                ("read naming an unknown source", "POST", "/read", {"selector": {"service": "checkout"}, "sources": ["nope"]}),
                ("sources list", "GET", "/api/sources", None),
                ("a source's events", "GET", "/api/sources/evt_b/events", None),
                ("remember", "POST", "/remember", {"key": "x", "content": "y"}),
                ("ingest", "POST", "/ingest/evt_a", {"service": "x"}),
                ("otlp", "POST", "/v1/logs", {}),
                ("catalog export", "GET", "/api/catalog/export", None)):
            r = await cx.request(method, path, headers=H(key_a),
                                 **({"json": body} if body is not None else {}))
            ck(f"{label}: 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

        print("== what the key reads is its project ==")
        r = await cx.post("/read", json={"selector": {"service": "checkout"}, "window": "1h"},
                          headers=H(key_a))
        rows = r.json()["rows"]
        srcs = {x["source"] for x in rows}
        texts = " ".join(x["text"] for x in rows)
        ck("/read without a project reads the key's project",
           r.status_code == 200 and "evt_a" in srcs and "evt_b" not in srcs, str(srcs))
        ck("its rows are Alpha's, not Beta's", "alpha 500" in texts and "beta 500" not in texts,
           texts[:300])
        r2 = await cx.post("/read", json={"selector": {"service": "checkout"}, "window": "1h",
                                          "project": "Alpha"}, headers=H(key_a))
        # a row's "offset" is whole seconds since its event, so two reads a second apart differ by it
        same = lambda xs: [{k: v for k, v in x.items() if k != "offset"} for x in xs]
        ck("naming its own project by name works",
           r2.status_code == 200 and same(r2.json()["rows"]) == same(rows), r2.text[:300])
        st = (await cx.post(f"/api/projects/{a}/stats", json={"by": "service", "window": "1h"},
                            headers=H(key_a))).json()["stats"]
        ck("stats over its sources: 1 log event for checkout, not Beta's",
           "checkout | 1 |" in st, st)
        cat = (await cx.get("/catalog", headers=H(key_a))).json()
        ck("/catalog lists its sources, triggers and project only",
           {s["name"] for s in cat["sources"]} == {"evt_a"}
           and [t["name"] for t in cat["triggers"]] == ["watch_a"]
           and [p["id"] for p in cat["projects"]] == [a], json.dumps(cat))
        d = await cx.get("/catalog/source:evt_a", headers=H(key_a))
        ck("describing its own source works", d.status_code == 200
           and all(e["to"] == "trigger:watch_a" for e in d.json()["lineage"]), d.text[:200])
        ps = (await cx.get("/api/projects", headers=H(key_a))).json()["projects"]
        ck("/api/projects lists its project only, without template params",
           [p["id"] for p in ps] == [a] and "params" not in ps[0]
           and ps[0]["triggers"] == ["watch_a"], str(ps))
        g = await cx.get(f"/api/projects/{a}", headers=H(key_a))
        ck("GET its project", g.status_code == 200 and g.json()["name"] == "Alpha", g.text[:200])
        tl = await cx.get(f"/api/projects/{a}/timeline", headers=H(key_a))
        ck("its timeline", tl.status_code == 200 and isinstance(tl.json()["threads"], list), tl.text[:300])

        print("== the MCP tools, as the project key ==")
        from tares import mcp_server as m

        def as_key(tok):
            m.TARESD = "http://t"
            m._cx = lambda timeout=10: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(tok))

        as_key(key_a)
        lp = json.loads(await m.list_projects())
        ck("list_projects: its project", [p["id"] for p in lp] == [a], str(lp))
        ck("naming another project is refused",
           "only" in await m.project_timeline(project="Beta"))
        ck("read", "alpha 500" in await m.read({"service": "checkout"}, window="1h")
           and "beta 500" not in await m.read({"service": "checkout"}, window="1h"))
        ck("stats", "checkout" in await m.stats("service", window="1h"))
        tl = json.loads(await m.project_timeline(limit=5))
        ck("project_timeline", isinstance(tl.get("threads"), list), str(tl)[:200])
        ck("an admin tool is refused", "only reads project" in await m.list_sources())

        print("== revoking the key ==")
        ck("revoke", (await cx.delete(f"/api/keys/{kid_a}")).status_code == 200)
        ck("the key no longer works",
           (await cx.get("/api/whoami", headers=H(key_a))).status_code == 401)
        ck("the project's keys list drops it",
           kid_a not in {k["id"] for k in (await cx.get(f"/api/projects/{a}/keys")).json()["keys"]})

        print("== deleting the project revokes its keys ==")
        r = await cx.delete(f"/api/projects/{b}", params={"delete_sources": "none"})
        ck("Beta deleted", r.status_code == 200, r.text)
        ck("Beta's key stops working",
           (await cx.get("/api/whoami", headers=H(key_b_ro))).status_code == 401)
        await cx.aclose()

    print("== an open instance: the tools pick the project by name ==")
    daemon.AUTH_TOKEN = ""
    daemon.DB_PATH = OPEN_DB
    app2 = daemon.make_app()
    async with app2.router.lifespan_context(app2):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t")
        await cx.post("/api/projects", json={"template": "custom", "name": "One", "objects": []})
        await cx.post("/api/projects", json={"template": "custom", "name": "Two", "objects": []})
        from tares import mcp_server as m
        m._cx = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                                     base_url="http://t")
        ck("with several projects the tools ask which one",
           "name the project" in await m.project_timeline(), await m.project_timeline())
        ck("naming it works", "threads" in await m.project_timeline(project="One"))
        await cx.aclose()

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
