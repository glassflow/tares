"""The AI SRE demo as a project (TR-176): template shape, the six demo objects, adopting an
existing unowned demo catalog, and the inject/clear actions against a fake api-server.
Run: .venv/bin/python tests/test_ai_sre_demo.py   (no Docker needed)
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-ai-sre-demo-test.duckdb"
CATALOG = "/tmp/tares-ai-sre-demo-test.catalog.yaml"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = CATALOG

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


# a fake api-server that records what /demo/inject receives
INJECTED = []
AUTHS = []   # Authorization headers seen by the fake hosted stack


class FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/demo/inject":
            INJECTED.append(body.get("scenario"))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()


class FakeStack(BaseHTTPRequestHandler):
    """The hosted demo stack in one handler: Prometheus, Loki and api-server probe endpoints,
    plus /demo/inject, all recording the Authorization header they were called with."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        AUTHS.append(self.headers.get("authorization"))
        if self.path.startswith(("/api/v1/query", "/loki/api/v1/labels", "/api/stats")):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        AUTHS.append(self.headers.get("authorization"))
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/demo/inject":
            INJECTED.append(body.get("scenario"))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()


async def main():
    for p in (DB, DB + ".wal", CATALOG):
        if os.path.exists(p):
            os.remove(p)
    srv = HTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api_url = f"http://127.0.0.1:{srv.server_port}"

    from tares.projects import get_template
    from tares.projects.base import ProjectError
    r = get_template("ai_sre_demo")

    print("== template ==")
    d = r.describe()
    check("tagged demo", d["tags"] == ["demo"])
    check("setup steps and actions advertised", len(d["setup"]) == 3 and
          [a["name"] for a in d["actions"]] == ["inject", "clear"])
    p = r.validate({})
    check("defaults", p["prometheus_url"] == "http://localhost:9090" and
          p["container"] == "tares-demo-api-server", json.dumps(p))
    try:
        r.validate({"prometheus_url": "localhost:9090"}); check("bad url rejected", False)
    except ProjectError:
        check("bad url rejected", True)
    plan = r.plan(r.validate({"prometheus_url": "http://prom:9090/"}))
    names = [(o.kind, o.name) for o in plan]
    check("the five demo objects by their catalog names",
          names == [("source", "demo_metrics"), ("source", "demo_logs"), ("source", "demo_alerts"),
                    ("trigger", "incident"), ("agent", "incident-first-look")], str(names))
    check("the trigger reads all three sources, keyed by service",
          plan[3].spec["sources"] == ["demo_logs", "demo_metrics", "demo_alerts"]
          and plan[3].spec["key_field"] == "service" and "view" not in plan[3].spec, str(plan[3].spec))
    check("prometheus url applied without trailing slash",
          plan[0].spec["config"]["url"] == "http://prom:9090" and plan[2].spec["config"]["url"] == "http://prom:9090")
    check("agent prompt has no em dash", "—" not in plan[4].spec["prompt"])

    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:
            print("== adopt an existing demo catalog ==")
            # a user who imported catalog.demo.yaml first: same names, unowned -> adopted, not duplicated
            demo_yaml = open(os.path.join(os.path.dirname(__file__), "..", "demo", "catalog.demo.yaml")).read()
            rr = await cx.post("/api/catalog/import", json={"yaml": demo_yaml, "mode": "merge"})
            check("demo catalog imported", rr.status_code == 200, rr.text[:200])
            before = {s["name"] for s in (await cx.get("/api/sources")).json()}

            print("== create ==")
            rr = await cx.post("/api/projects", json={"template": "ai_sre_demo", "name": "AI SRE demo",
                                                     "params": {"api_server_url": api_url}})
            check("create -> 201", rr.status_code == 201, rr.text[:300])
            inst = rr.json(); uid = inst["id"]
            after = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("no duplicate sources: adopted the existing ones", after == before, str(after ^ before))
            srcs = {s["name"]: s for s in (await cx.get("/api/sources")).json()}
            check("the existing demo sources are in the project",
                  all(uid in srcs[n]["projects"] for n in ("demo_metrics", "demo_logs", "demo_alerts")))
            agents = (await cx.get("/api/agents/builtin")).json()["agents"]
            a = next(x for x in agents if x["name"] == "incident-first-look")
            check("agent owned and on the incident trigger", a["owned_by"] == uid and a["trigger"] == "incident")

            print("== actions ==")
            rr = await cx.post(f"/api/projects/{uid}/actions/inject", json={"scenario": "latency"})
            check("inject latency -> ok", rr.status_code == 200 and rr.json().get("scenario") == "latency", rr.text[:200])
            rr = await cx.post(f"/api/projects/{uid}/actions/inject", json={"scenario": "clear"})
            check("inject rejects clear as a scenario", rr.status_code == 400, rr.text[:200])
            rr = await cx.post(f"/api/projects/{uid}/actions/clear", json={})
            check("clear -> ok", rr.status_code == 200 and rr.json().get("scenario") == "clear", rr.text[:200])
            rr = await cx.post(f"/api/projects/{uid}/actions/nope", json={})
            check("unknown action -> 400", rr.status_code == 400, rr.text[:200])
            check("fake api-server received latency then clear", INJECTED == ["latency", "clear"], str(INJECTED))
            log = (await cx.get(f"/api/projects/{uid}/summary")).json()["log"]
            check("actions logged", any(l["action"] == "action:inject" for l in log) and
                  any(l["action"] == "action:clear" for l in log), str([l["action"] for l in log]))

            print("== summary ==")
            s = (await cx.get(f"/api/projects/{uid}/summary")).json()
            panel = (s.get("panels") or [{}])[0]
            check("summary has the demo stack panel and runs",
                  panel.get("title") == "Demo stack" and "runs" in s and "triggers" in s,
                  json.dumps({k: s.get(k) for k in ("panels",)})[:300])

            print("== unreachable api-server ==")
            rr = await cx.put(f"/api/projects/{uid}", json={"params": {"api_server_url": "http://127.0.0.1:1"}})
            check("update ok", rr.status_code == 200, rr.text[:200])
            rr = await cx.post(f"/api/projects/{uid}/actions/inject", json={"scenario": "error_spike"})
            check("inject with unreachable api-server -> 400 with a named reason",
                  rr.status_code == 400 and "could not reach" in rr.text, rr.text[:200])

            print("== delete ==")
            rr = await cx.delete(f"/api/projects/{uid}", params={
                "delete_sources": "demo_metrics,demo_logs,demo_alerts"})
            check("delete -> 200", rr.status_code == 200, rr.text[:200])
            after = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("demo sources gone", not ({"demo_metrics", "demo_logs", "demo_alerts"} & after), str(after))
            check("the trigger and agent went with the project",
                  not any(t["name"] == "incident" for t in (await cx.get("/api/triggers")).json()))

    srv.shutdown()

    # ── hosted mode: the same template against a hosted demo stack (TR-194) ────
    print("== hosted mode ==")
    stack = HTTPServer(("127.0.0.1", 0), FakeStack)
    threading.Thread(target=stack.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{stack.server_port}"
    os.environ.update({"TARES_DEMO_PROMETHEUS_URL": base, "TARES_DEMO_LOKI_URL": base,
                       "TARES_DEMO_API_SERVER_URL": base,
                       "TARES_DEMO_USERNAME": "demo", "TARES_DEMO_PASSWORD": "pw"})
    try:
        d = r.describe()
        check("hosted defaults come from the env",
              d["params"]["prometheus_url"]["default"] == base
              and d["params"]["loki_url"]["default"] == base, json.dumps(d["params"])[:300])
        check("hosted form hides the container field", "container" not in d["params"],
              str(list(d["params"])))
        check("hosted setup has no docker step and keeps the key step",
              "docker" not in json.dumps(d["setup"]).lower()
              and [s.get("check") for s in d["setup"]] == ["detect", "anthropic_key"],
              json.dumps(d["setup"])[:300])
        check("hosted facts say the stack is shared and nothing to install",
              "shared" in json.dumps(d["facts"]) and "docker" not in json.dumps(d["facts"]),
              json.dumps(d["facts"])[:300])
        check("hosted inject action says it is shared",
              "shared" in d["actions"][0]["intro"], d["actions"][0]["intro"][:200])

        hp = r.validate({})
        hplan = {o.key: o for o in r.plan(hp)}
        check("hosted plan reads logs from Loki",
              hplan["logs"].spec["connector"] == "loki"
              and hplan["logs"].spec["config"]["query"] == '{service="api-server"}',
              json.dumps(hplan["logs"].spec)[:300])
        check("hosted sources carry the shared credential",
              all(hplan[k].spec["config"].get("username") == "demo"
                  and hplan[k].spec["config"].get("password") == "pw"
                  for k in ("metrics", "logs", "alerts")), json.dumps(hplan["logs"].spec)[:300])

        det = await r.detect(None, None)
        check("hosted detect probes the stack, no docker",
              set(det["found"]) == {"prometheus_url", "loki_url", "api_server_url"}
              and not det["missing"], json.dumps(det)[:300])

        AUTHS.clear(); INJECTED.clear()
        out = r.run_action({"params": hp}, "inject", {"scenario": "error_spike"}, None, None)
        check("hosted inject reaches the stack with the credential",
              INJECTED == ["error_spike"] and AUTHS and AUTHS[-1] and AUTHS[-1].startswith("Basic "),
              f"injected={INJECTED} auths={AUTHS[-1:]}")
        check("inject message unchanged", "alert fires" in out["message"], out["message"])
    finally:
        for k in list(os.environ):
            if k.startswith("TARES_DEMO_"):
                del os.environ[k]
        stack.shutdown()

    # regression: with the env cleared, local mode is back untouched
    d = r.describe()
    check("local mode restored after env cleared",
          d["params"]["prometheus_url"]["default"] == "http://localhost:9090"
          and "docker compose" in json.dumps(d["setup"])
          and r.plan(r.validate({}))[1].spec["connector"] == "docker_logs")
    check("local form does not show the Loki field", "loki_url" not in d["params"],
          str(list(d["params"])))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
