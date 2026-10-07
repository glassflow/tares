"""Project keys and joining a project (TR-335, TR-336).

A project key reads ONE project and records findings in it; every other route answers 403. The
sweep walks every route the daemon serves, so a route added later without a decision for project
keys shows up here as a failure rather than as a hole. Then: reads, stats, catalog and project
listings narrowed to the project; skills, timeline and findings of the project only; a webhook
subscription to the whole project (a trigger added after subscribing included) delivering the
firing with `project`, the dispatch id and the runs it started; an external finding recorded and
shown in the timeline as an external run; revoking the key; the MCP tools; and the same tools on
an open instance, where they pick the project by name.

The model loop is replaced by a stand-in that concludes at once, so no provider is needed.

Run: .venv/bin/python tests/test_project_keys.py
"""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/tmp/tares-project-keys.duckdb"
OPEN_DB = "/tmp/tares-project-keys-open.duckdb"
ROOT = "root-token-project-keys"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = "/tmp/tares-project-keys.catalog.yaml"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
for _p in (DB, DB + ".wal", OPEN_DB, OPEN_DB + ".wal", os.environ["TARES_CATALOG"]):
    if os.path.exists(_p):
        os.remove(_p)

import httpx
from fastapi.routing import APIRoute

from tares.builtin_agents import AgentRunner
from tares.config import FINDINGS_SOURCE

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def fake_run(self, agent, trigger_name, key, payload, run_id, dispatch_id=None):
    text = f"{agent['name']} looked at {key}"
    await self._record(agent, trigger_name, key, text, verdict="done", run_id=run_id,
                       dispatch_id=dispatch_id)
    self.store.finish_agent_run(run_id, "ok", rounds=1, finding=text, outcome="finding",
                                verdict="done")
    return "ok", None


AgentRunner._run = fake_run

# ── a stub webhook: the external agent's endpoint ─────────────────────────────
HOOK_PORT = 18977
received: list[dict] = []


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        received.append({"path": self.path, "body": json.loads(self.rfile.read(n) or b"{}")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def source(name):
    return {"name": name, "connector": "webhook", "poll": "5s",
            "config": {"event_type": "log", "text_template": "{msg}",
                       "labels": [{"name": "service", "field": "service", "primary": True}]}}


COND = {"aggregate": "count", "predicate": "> 0", "window": "5m"}


async def wait_for(fn, tries=100):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.05)
    return fn()


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def fill(path: str, vals: dict) -> str:
    for k, v in vals.items():
        path = path.replace("{" + k + "}", v)
    return path


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hook = f"http://127.0.0.1:{HOOK_PORT}/hook/s3cr3t-path"

    from tares import daemon
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H(ROOT))
        store = app.state.store
        runner = app.state.agents
        runner.attach_loop()

        async def mk(kind, body, code=201):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == code, (kind, r.status_code, r.text)
            return r.json()

        a = (await mk("projects", {"template": "custom", "name": "Alpha", "objects": []}))["id"]
        b = (await mk("projects", {"template": "custom", "name": "Beta", "objects": []}))["id"]
        await mk("sources", {**source("evt_a"), "project": a})
        await mk("sources", {**source("evt_b"), "project": b})
        await mk("triggers", {"name": "watch_a", "project": a, "sources": ["evt_a"],
                              "key_field": "service", "condition": COND, "cooldown": "1h"})
        await mk("triggers", {"name": "watch_b", "project": b, "sources": ["evt_b"],
                              "key_field": "service", "condition": COND, "cooldown": "1h"})
        for name, trig, proj in (("agent_a", "watch_a", a), ("agent_b", "watch_b", b)):
            await mk("agents/builtin", {"name": name, "trigger": trig, "prompt": "look",
                                        "project": proj})
            assert (await cx.post(f"/api/agents/builtin/{name}/enable")).status_code == 200
        await mk(f"projects/{a}/skills", {"name": "triage", "description": "How to triage.",
                                          "body": "# Triage\nlook at errors"})
        await mk(f"projects/{b}/skills", {"name": "secret-sauce", "description": "Beta only.",
                                          "body": "beta instructions"})
        await mk("mcp-servers", {"name": "mcp_b", "url": "http://127.0.0.1:1/mcp", "project": b})

        print("== creating project keys ==")
        r = await cx.post(f"/api/projects/{a}/keys", json={"name": "claude-code"})
        ck("POST /api/projects/{uid}/keys makes a project key",
           r.status_code == 201 and r.json()["scopes"] == ["findings", "read"]
           and r.json()["project"] == a and r.json()["secret"].startswith("nvf_"), r.text)
        key_a, kid_a = r.json()["secret"], r.json()["id"]
        r = await cx.post("/api/keys", json={"name": "cursor", "project": "Alpha"})
        ck("POST /api/keys with project (a name) makes one too",
           r.status_code == 201 and r.json()["project"] == a
           and r.json()["scopes"] == ["findings", "read"], r.text)
        key_a2, kid_a2 = r.json()["secret"], r.json()["id"]
        r = await cx.post("/api/keys", json={"name": "b-agent", "project": b, "scopes": ["read"]})
        ck("a read-only project key", r.status_code == 201 and r.json()["scopes"] == ["read"], r.text)
        key_b_ro = r.json()["secret"]
        ck("a project key with the admin scope is refused",
           (await cx.post("/api/keys", json={"name": "x", "project": a,
                                              "scopes": ["admin"]})).status_code == 400)
        ck("the findings scope without a project is refused",
           (await cx.post("/api/keys", json={"name": "x", "scopes": ["findings"]})).status_code == 400)
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

        # something to read: events in both projects' sources, a Tares agent finding in each
        await cx.post("/ingest/evt_a", json={"service": "checkout", "msg": "alpha 500"})
        await cx.post("/ingest/evt_b", json={"service": "checkout", "msg": "beta 500"})
        await wait_for(lambda: len([r for n in ("agent_a", "agent_b")
                                    for r in store.list_agent_runs(n) if r["status"] == "ok"]) == 2)

        print("== every route, as a project key ==")
        sid_root = (await cx.post("/subscribe", json={"trigger": "watch_b",
                                                       "url": "http://127.0.0.1:1/x"})).json()["subscription_id"]
        vals = {"uid": b, "name": "evt_b", "handle": "source:evt_b", "token": "evt_b",
                "kid": kid_a2, "sid": sid_root, "dispatch_id": "d", "run_id": "r",
                "provider_id": "anthropic", "key": "custom", "path": "x"}
        # reachable without any credential: their own authentication or nothing to protect
        public = {("GET", "/health"), ("GET", "/metrics"), ("GET", "/ingest/{token}"),
                  ("POST", "/api/slack/events"), ("GET", "/{path:path}"),
                  ("GET", "/derive"),   # a GET outside /api is the console's; this one says views are gone
                  # GitHub sends the browser back here: gated by a signed state / proof of ownership
                  ("GET", "/api/integrations/github/apps/callback"),
                  ("GET", "/api/linear/oauth/callback"),   # gated by its signed state + PKCE
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
        # its OWN project: only reading it, recording findings and its own subscription
        own_ok = {("GET", "/api/projects/{uid}"), ("GET", "/api/projects/{uid}/timeline"),
                  ("GET", "/api/projects/{uid}/skills"), ("GET", "/api/projects/{uid}/skills/{name}"),
                  ("GET", "/api/projects/{uid}/findings"), ("POST", "/api/projects/{uid}/findings"),
                  ("POST", "/api/projects/{uid}/stats"), ("POST", "/api/projects/{uid}/subscribe"),
                  ("DELETE", "/api/projects/{uid}/subscribe/{sid}"),
                  # the goal-first page's reads
                  ("GET", "/api/projects/{uid}/results"),
                  ("GET", "/api/projects/{uid}/results/{run_id}"),
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
        r = await cx.post(f"/api/projects/{b}/findings", json={"entity": "x", "finding": "y"},
                          headers=H(key_a))
        ck("the 403 says what the key may do",
           r.status_code == 403 and "only reads project Alpha and records findings" in r.text, r.text)

        print("== another project's resources, by id and by name ==")
        for label, method, path, body in (
                ("timeline", "GET", f"/api/projects/{b}/timeline", None),
                ("skills", "GET", f"/api/projects/{b}/skills", None),
                ("a skill", "GET", f"/api/projects/{b}/skills/secret-sauce", None),
                ("findings", "GET", f"/api/projects/{b}/findings", None),
                ("record a finding", "POST", f"/api/projects/{b}/findings", {"entity": "x", "finding": "y"}),
                ("stats", "POST", f"/api/projects/{b}/stats", {"by": "service"}),
                ("subscribe", "POST", f"/api/projects/{b}/subscribe", {"url": hook}),
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
                ("agent runs", "GET", "/api/agents/builtin/agent_b/runs", None),
                ("mcp servers", "GET", "/api/mcp-servers", None),
                ("remember", "POST", "/remember", {"key": "x", "content": "y"}),
                ("ingest", "POST", "/ingest/evt_a", {"service": "x"}),
                ("otlp", "POST", "/v1/logs", {}),
                ("trigger subscribe", "POST", "/subscribe", {"trigger": "watch_a", "url": hook}),
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
        ck("its findings include its own agent's, not Beta's",
           "agent_a looked at checkout" in texts and "agent_b" not in texts, texts[:300])
        r2 = await cx.post("/read", json={"selector": {"service": "checkout"}, "window": "1h",
                                          "project": "Alpha"}, headers=H(key_a))
        ck("naming its own project by name works", r2.status_code == 200 and r2.json()["rows"] == rows)
        r = await cx.post(f"/api/projects/{a}/stats", json={"by": "agent", "window": "1h"},
                          headers=H(key_a))
        st = r.json().get("stats", "")
        ck("stats count the project's rows only",
           r.status_code == 200 and "agent_a" in st and "agent_b" not in st, st)
        st = (await cx.post(f"/api/projects/{a}/stats", json={"by": "service", "window": "1h"},
                            headers=H(key_a))).json()["stats"]
        ck("stats over its sources: 1 log event plus 1 finding for checkout",
           "checkout | 2 |" in st, st)
        cat = (await cx.get("/catalog", headers=H(key_a))).json()
        ck("/catalog lists its sources, triggers and project only",
           {s["name"] for s in cat["sources"]} == {"evt_a", FINDINGS_SOURCE}
           and [t["name"] for t in cat["triggers"]] == ["watch_a"]
           and [p["id"] for p in cat["projects"]] == [a], json.dumps(cat))
        d = await cx.get("/catalog/source:evt_a", headers=H(key_a))
        ck("describing its own source works", d.status_code == 200
           and all(e["to"] == "trigger:watch_a" for e in d.json()["lineage"]), d.text[:200])
        ps = (await cx.get("/api/projects", headers=H(key_a))).json()["projects"]
        ck("/api/projects lists its project only, without template params",
           [p["id"] for p in ps] == [a] and "params" not in ps[0]
           and ps[0]["skills"] == ["triage"] and ps[0]["triggers"] == ["watch_a"], str(ps))
        g = await cx.get(f"/api/projects/{a}", headers=H(key_a))
        ck("GET its project", g.status_code == 200 and g.json()["name"] == "Alpha", g.text[:200])
        sk = (await cx.get(f"/api/projects/{a}/skills", headers=H(key_a))).json()
        ck("its skills", [s["name"] for s in sk] == ["triage"], str(sk))
        body = (await cx.get(f"/api/projects/{a}/skills/triage", headers=H(key_a))).json()
        ck("a skill's body", body.get("body", "").startswith("# Triage"), str(body))
        ck("a skill write is refused",
           (await cx.put(f"/api/projects/{a}/skills/triage", json={"body": "x"},
                         headers=H(key_a))).status_code == 403)
        tl = await cx.get(f"/api/projects/{a}/timeline", headers=H(key_a))
        ck("its timeline", tl.status_code == 200 and tl.json()["threads"]
           and all(t["trigger"] == "watch_a" for t in tl.json()["threads"]), tl.text[:300])

        print("== joining: a subscription to the whole project ==")
        r = await cx.post(f"/api/projects/{a}/subscribe", json={"url": "slack://channel/C1"},
                          headers=H(key_a))
        ck("only an http(s) URL can join", r.status_code == 400, r.text)
        for internal in ("http://169.254.169.254/latest/meta-data/", hook, "http://10.0.0.5/x"):
            r = await cx.post(f"/api/projects/{a}/subscribe", json={"url": internal}, headers=H(key_a))
            ck(f"a project key cannot join with an internal address ({internal.split('/')[2]})",
               r.status_code == 400 and "internal" in r.text, r.text)
        os.environ["TARES_WEBHOOK_ALLOW_PRIVATE"] = "1"   # the stub webhook below is on loopback
        r = await cx.post(f"/api/projects/{a}/subscribe", json={"url": hook}, headers=H(key_a))
        ck("the key subscribes to its project", r.status_code == 200 and r.json()["project"] == a, r.text)
        sid = r.json()["subscription_id"]
        r = await cx.post(f"/api/projects/{a}/subscribe", json={"url": hook}, headers=H(key_a))
        ck("subscribing again returns the same subscription", r.json()["subscription_id"] == sid)
        ck("a read key cannot subscribe a project",
           (await cx.post(f"/api/projects/{a}/subscribe", json={"url": hook},
                          headers=H(read_key))).status_code == 403)
        # a trigger added AFTER the subscription
        await mk("triggers", {"name": "late_a", "project": a, "sources": ["evt_a"],
                              "key_field": "service", "condition": COND, "cooldown": "1h"})
        received.clear()
        await cx.post("/ingest/evt_a", json={"service": "payments", "msg": "alpha again"})
        await cx.post("/ingest/evt_b", json={"service": "payments", "msg": "beta again"})
        got = await wait_for(lambda: {x["body"]["trigger"] for x in received} >= {"watch_a", "late_a"})
        trig = {x["body"]["trigger"] for x in received}
        ck("the webhook gets every trigger of the project, the late one included",
           got and trig == {"watch_a", "late_a"}, str(trig))
        w = next((x["body"] for x in received if x["body"]["trigger"] == "watch_a"), {})
        runs = store.list_agent_runs("agent_a")
        ck("the body carries project, dispatch id and the runs it started",
           w.get("project") == a and w.get("dispatch_id") and w.get("key") == "payments"
           and w.get("run_ids") and w["run_ids"][0] in {x["id"] for x in runs}
           and "alpha again" in w.get("payload", ""), json.dumps(w)[:400])
        ck("nothing of Beta was delivered", not any(x["body"].get("project") == b for x in received))
        ea = (await cx.get(f"/api/projects/{a}/external-agents")).json()["agents"]
        ck("External agents: masked URL, key name, last delivery",
           len(ea) == 1 and ea[0]["key_name"] == "claude-code" and "s3cr3t" not in ea[0]["url"]
           and ea[0]["last_delivery"] and ea[0]["last_delivery"]["ok"] is True, str(ea))
        ck("a project key cannot list the external agents",
           (await cx.get(f"/api/projects/{a}/external-agents", headers=H(key_a))).status_code == 403)
        tl = (await cx.get(f"/api/projects/{a}/timeline", headers=H(key_a))).json()
        targets = [d["target"] for t in tl["threads"] for d in t["deliveries"] if d["kind"] == "webhook"]
        ck("the key sees webhook targets masked in the timeline",
           targets and not any("s3cr3t" in x for x in targets), str(targets))
        sid2 = (await cx.post(f"/api/projects/{a}/subscribe", json={"url": hook + "2"},
                              headers=H(key_a2))).json()["subscription_id"]
        ck("a key cannot remove another key's subscription",
           (await cx.delete(f"/api/projects/{a}/subscribe/{sid2}", headers=H(key_a))).status_code == 403)
        ck("it removes its own",
           (await cx.delete(f"/api/projects/{a}/subscribe/{sid2}", headers=H(key_a2))).status_code == 200)

        print("== an external finding ==")
        r = await cx.post(f"/api/projects/{a}/findings",
                          json={"entity": "payments", "finding": "Root cause: a bad deploy.",
                                "verdict": "RCA"}, headers=H(key_a))
        ck("recorded", r.status_code == 201 and r.json()["agent"] == "claude-code"
           and r.json()["verdict"] == "rca", r.text)
        ext_run = r.json()["run_id"]
        ck("a bad verdict is refused",
           (await cx.post(f"/api/projects/{a}/findings", json={"entity": "x", "finding": "y",
                                                                "verdict": "two words"},
                          headers=H(key_a))).status_code == 400)
        ck("an empty finding is refused",
           (await cx.post(f"/api/projects/{a}/findings", json={"entity": "x"},
                          headers=H(key_a))).status_code == 400)
        ck("a read-only project key cannot record one",
           (await cx.post(f"/api/projects/{b}/findings", json={"entity": "x", "finding": "y"},
                          headers=H(key_b_ro))).status_code == 403)
        ck("a plain read key cannot record one",
           (await cx.post(f"/api/projects/{a}/findings", json={"entity": "x", "finding": "y"},
                          headers=H(read_key))).status_code == 403)
        tl = (await cx.get(f"/api/projects/{a}/timeline", headers=H(key_a))).json()
        ext = [t for t in tl["threads"] if t["id"] == ext_run]
        ck("in the timeline: a thread of kind run, external, by the key's name",
           len(ext) == 1 and ext[0]["kind"] == "run" and ext[0]["external"] is True
           and ext[0]["runs"][0]["agent"] == "claude-code" and ext[0]["runs"][0]["external"]
           and ext[0]["runs"][0]["verdict"] == "rca" and ext[0]["entity"] == "payments",
           json.dumps(ext, default=str)[:400])
        ck("not in Beta's timeline",
           ext_run not in json.dumps((await cx.get(f"/api/projects/{b}/timeline")).json(), default=str))
        fs = (await cx.get(f"/api/projects/{a}/findings?entity=payments", headers=H(key_a))).json()["findings"]
        ck("listed with the project's findings, marked external",
           any(f["run_id"] == ext_run and f["external"] for f in fs)
           and all(f["entity"] == "payments" for f in fs), str(fs))
        fs = (await cx.get(f"/api/projects/{a}/findings?agent=agent_a", headers=H(key_a))).json()["findings"]
        ck("findings narrowed by agent", fs and all(f["agent"] == "agent_a" and not f["external"] for f in fs))
        rows = (await cx.post("/read", json={"selector": {"service": "payments"}, "window": "1h"},
                              headers=H(key_a))).json()["rows"]
        ck("the finding is on the entity's timeline for the project's reads",
           any("bad deploy" in x["text"] for x in rows), str(rows)[:300])
        rows_b = (await cx.post("/read", json={"selector": {"service": "payments"}, "window": "1h",
                                               "project": b})).json()["rows"]
        ck("and not in Beta's reads", not any("bad deploy" in x["text"] for x in rows_b))
        # a key named like a Tares agent would mix its findings into that agent's runs
        kb = await cx.post(f"/api/projects/{a}/keys", json={"name": "agent_a"})
        r = await cx.post(f"/api/projects/{a}/findings", json={"entity": "x", "finding": "y"},
                          headers=H(kb.json()["secret"]))
        ck("a key named like a Tares agent cannot record findings", r.status_code == 409, r.text)

        print("== the MCP tools, as the project key ==")
        from tares import mcp_server as m

        def as_key(tok):
            m.TARESD = "http://t"
            m._cx = lambda timeout=10: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(tok))

        as_key(key_a)
        lp = json.loads(await m.list_projects())
        ck("list_projects: its project", [p["id"] for p in lp] == [a], str(lp))
        ck("list_skills: implied project", json.loads(await m.list_skills())[0]["name"] == "triage")
        ck("get_skill", (await m.get_skill("triage")).startswith("# Triage"))
        ck("naming another project is refused",
           "only" in await m.list_skills(project="Beta"))
        ck("read", "alpha 500" in await m.read({"service": "checkout"}, window="1h")
           and "beta 500" not in await m.read({"service": "checkout"}, window="1h"))
        ck("stats", "checkout" in await m.stats("service", window="1h"))
        out = json.loads(await m.record_finding("checkout", "Looks fine now.", verdict="resolved"))
        ck("record_finding", out.get("ok") and out.get("agent") == "claude-code", str(out))
        lf = json.loads(await m.list_findings(entity="checkout"))
        ck("list_findings", any(f["finding"] == "Looks fine now." and f["external"] for f in lf), str(lf))
        tl = json.loads(await m.project_timeline(limit=5))
        ck("project_timeline", any(t.get("external") for t in tl["threads"]))
        j = json.loads(await m.join_project(hook + "/mcp"))
        ck("join_project", j.get("project") == a and j.get("subscription_id", "").startswith("sub_"), str(j))
        ck("an admin tool is refused", "only reads project" in await m.list_sources())

        print("== revoking the key ==")
        ck("revoke", (await cx.delete(f"/api/keys/{kid_a}")).status_code == 200)
        ck("the key no longer works",
           (await cx.get("/api/whoami", headers=H(key_a))).status_code == 401)
        subs = store.list_project_subscriptions(a)
        ck("its subscriptions are gone with it",
           not any(s["created_by"] == f"key:{kid_a}" for s in subs), str(subs))
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
           "name the project" in await m.list_skills(), await m.list_skills())
        ck("naming it works", await m.list_skills(project="One") == "[]")
        out = json.loads(await m.record_finding("svc", "noted", project="Two"))
        ck("record_finding on an open instance: agent is 'external agent'",
           out.get("agent") == "external agent", str(out))
        await cx.aclose()
    server.shutdown()

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
