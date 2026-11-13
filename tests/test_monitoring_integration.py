"""The monitoring slot over the tracing settings, and Claude Code telemetry for crew stations (TR-866).

One in-process cell with auth on. Covered:
 - off, rius and otlp are installed for the monitoring slot, with their settings;
 - one storage: a choice made through the integrations API shows in /api/settings/tracing, and one made
   through that route is what the slot reads; off keeps the stored values so switching back needs nothing
   typed again; no response holds a secret value;
 - station_env: the Claude Code telemetry variables for the endpoint, the endpoint without a user, query
   or fragment, the resource attributes percent-encoded, {} while off or for another provider, and never a
   key or a header value in any variable;
 - station_settings: the header helper only while there are headers to send;
 - GET /api/factory/monitoring/headers: a station gets the headers, no-store; anyone else 403; {} while off;
 - the helper script (claude-plugin/scripts/otel_headers.py) against a real local server: it sends the
   crew token and the station labels, prints the headers, and prints {} for no token, no address, a refusal,
   a bad answer or a server that is down;
 - the connection test: ok, refused (401), an error status, an address that is not http, nothing listening,
   and no secret in any message;
 - null and empty inputs: no settings, an empty endpoint, a bad scheme, no labels, a blank label.

Run: python tests/test_monitoring_integration.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-monitoring-")
os.environ["TARES_DB"] = os.path.join(_TMP, "mon.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-monitoring"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
for k in list(os.environ):
    if k.startswith("TARES_TRACING_"):
        del os.environ[k]

import httpx

from tares.integrations import registry
from tares.integrations.builtin import monitoring_tares as mon

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {str(detail)[:300]}"))


KEY = "SECRET-key-mon-1"          # gitleaks:allow, planted to look for
HDR = "SECRET-hdr-mon-2"          # gitleaks:allow
CREW = "crew-token-mon-3"         # gitleaks:allow
SECRETS = [KEY, HDR, CREW]
ALL = []                          # every response text of the run
HITS = []                         # what the fake collector and fake Tares saw


class Handler(BaseHTTPRequestHandler):
    status = 200
    body = b"{}"

    def _go(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        HITS.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    do_GET = do_POST = _go

    def log_message(self, *a):
        pass


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def helper(env_extra):
    """Run the helper script as Claude Code would, in a clean environment; its stdout and stderr."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": _TMP}
    env.update(env_extra)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "claude-plugin", "scripts", "otel_headers.py")],
                       capture_output=True, text=True, env=env, timeout=30)
    return r


def ru(r):
    ALL.append(r.text)
    return r


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk(station=None, token=TOKEN):
        hdr = {"Authorization": f"Bearer {token}"}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": "builder"})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    async with app.router.lifespan_context(app):
        async with mk() as root, mk("crew-build-x") as st:
            print("== what is installed ==")
            got = {c.name: c for c in registry.available("monitoring")}
            ck("off, rius and otlp are installed", {"off", "rius", "otlp"} <= set(got), list(got))
            ck("off has no settings", got["off"].settings_schema == [])
            ck("otlp wants an address; headers and key are secrets",
               {f.name: (f.kind, f.required) for f in got["otlp"].settings_schema}
               == {"endpoint": ("url", True), "headers": ("secret", False), "api_key": ("secret", False)},
               got["otlp"].settings_schema)
            ck("rius has a key and an optional address",
               {f.name: (f.kind, f.required) for f in got["rius"].settings_schema}
               == {"api_key": ("secret", False), "endpoint": ("url", False)})
            ck("the address help says running stations pick a change up on restart",
               "next restart" in got["otlp"].settings_schema[0].help)

            print("== one storage ==")
            r = ru(await root.get("/api/factory/integrations"))
            slot = next(s for s in r.json()["slots"] if s["slot"] == "monitoring")
            ck("a fresh cell: monitoring is the default and off is listed",
               slot["effective"]["integration"] == "off" and "off" in [a["name"] for a in slot["available"]],
               slot["effective"])
            r = ru(await root.put("/api/factory/integrations/monitoring", json={
                "name": "otlp",
                "settings": {"endpoint": "https://collector.example.com:4318/", "headers": f"x-token={HDR}",
                             "api_key": KEY}}))
            ck("choosing otlp is 200", r.status_code == 200, r.text[:300])
            ck("the answer has no secret", not any(s in r.text for s in SECRETS), r.text[:300])
            t = ru(await root.get("/api/settings/tracing")).json()
            ck("the old tracing route shows the same choice",
               t["provider"] == "otlp" and t["enabled"] and t["active"]
               and t["endpoint"] == "https://collector.example.com:4318", t)
            r = ru(await root.get("/api/factory/integrations"))
            slot = next(s for s in r.json()["slots"] if s["slot"] == "monitoring")
            ck("the slot reads otlp as what applies", slot["effective"]["integration"] == "otlp", slot["effective"])
            ck("no list response holds a secret", not any(s in r.text for s in SECRETS))

            print("== station_env ==")
            otlp = registry.get("monitoring", "otlp")(store)
            labels = {"station": "crew-build-m5-api", "role": "builder", "project": "my project, one=1",
                      "ticket": "TR-1"}
            env = otlp.station_env(labels)
            ck("telemetry on, all three exporters otlp over http/protobuf",
               env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
               and all(env[f"OTEL_{s}_EXPORTER"] == "otlp" for s in ("METRICS", "LOGS", "TRACES"))
               and env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf", env)
            ck("the endpoint is the stored one, base only",
               env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://collector.example.com:4318", env)
            ck("resource attributes carry station, role and project, encoded, and not the ticket",
               env["OTEL_RESOURCE_ATTRIBUTES"]
               == "tares.station=crew-build-m5-api,tares.role=builder,tares.project=my%20project%2C%20one%3D1",
               env.get("OTEL_RESOURCE_ATTRIBUTES"))
            ck("no key or header value in any variable", not any(s in v for v in env.values() for s in SECRETS), env)
            ck("no OTEL_EXPORTER_OTLP_HEADERS variable", "OTEL_EXPORTER_OTLP_HEADERS" not in env, list(env))
            ck("rius is not in use, so its station_env is empty",
               registry.get("monitoring", "rius")(store).station_env(labels) == {})
            ck("exporter_config has the headers (the cell's own use)",
               otlp.exporter_config()["headers"].get("x-token") == HDR)
            ck("station_settings names the header helper", otlp.station_settings() == {
                "otelHeadersHelper": "tares hook otel-headers"}, otlp.station_settings())
            ck("a bare class has nothing to give", registry.get("monitoring", "otlp")().station_env(labels) == {}
               and registry.get("monitoring", "otlp")().station_settings() == {})
            ck("no labels: no attributes variable",
               "OTEL_RESOURCE_ATTRIBUTES" not in otlp.station_env({})
               and "OTEL_RESOURCE_ATTRIBUTES" not in otlp.station_env(None)
               and "OTEL_RESOURCE_ATTRIBUTES" not in otlp.station_env({"station": "  ", "role": ""}))

            print("== the endpoint is cleaned ==")
            for raw, want in (("https://u:p@host.example.com:4318/x/?token=abc#f", "https://host.example.com:4318/x"),
                              ("http://h:4318", "http://h:4318"), ("", ""), (None, ""), ("ftp://h", ""),
                              ("http://", ""), ("not a url", ""), ("https://[::1]:4318/p", "https://[::1]:4318/p")):
                ck(f"clean_endpoint({raw!r})", mon.clean_endpoint(raw) == want, mon.clean_endpoint(raw))
            ck("resource_attributes of nothing is empty",
               mon.resource_attributes({}) == "" and mon.resource_attributes(None) == "")

            print("== off keeps what was stored ==")
            r = ru(await root.put("/api/factory/integrations/monitoring", json={"name": "off"}))
            ck("off is 200", r.status_code == 200, r.text[:300])
            t = ru(await root.get("/api/settings/tracing")).json()
            ck("tracing is switched off, the provider stays", t["enabled"] is False and t["provider"] == "otlp", t)
            ck("while off: no env, no settings, no config",
               otlp.station_env(labels) == {} and otlp.station_settings() == {} and otlp.exporter_config() == {})
            r = ru(await root.put("/api/factory/integrations/monitoring", json={
                "name": "otlp", "settings": {"endpoint": "https://collector.example.com:4318/"}}))
            ck("back to otlp with only the address: 200", r.status_code == 200, r.text[:300])
            ck("the stored headers and key are kept (a blank secret keeps it)",
               otlp.exporter_config()["headers"].get("x-token") == HDR
               and store.get_setting("tracing_api_key") == KEY, otlp.exporter_config())

            print("== the other route is read by the slot ==")
            await root.put("/api/settings/tracing", json={"provider": "rius", "enabled": True, "api_key": KEY,
                                                           "endpoint": "", "headers": ""})
            slot = next(s for s in ru(await root.get("/api/factory/integrations")).json()["slots"]
                        if s["slot"] == "monitoring")
            ck("a change on the old route shows as rius in the slot", slot["effective"]["integration"] == "rius",
               slot["effective"])
            rius = registry.get("monitoring", "rius")(store)
            ck("rius uses its own ingest address", rius.station_env({}).get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
               .startswith("https://"), rius.station_env({}))
            ck("rius sends a key, so the helper is named", rius.station_settings() == {
                "otelHeadersHelper": "tares hook otel-headers"}, rius.station_settings())

            print("== bad writes ==")
            for label, body in (("no settings", {"name": "otlp"}),
                                ("settings null", {"name": "otlp", "settings": None}),
                                ("an empty address", {"name": "otlp", "settings": {"endpoint": ""}}),
                                ("an unknown field", {"name": "otlp", "settings": {"endpoint": "https://c", "x": 1}})):
                r = ru(await root.put("/api/factory/integrations/monitoring", json=body))
                ck(f"otlp with {label} is 422", r.status_code == 422, (r.status_code, r.text[:200]))
            r = ru(await root.put("/api/factory/integrations/monitoring", json={"name": "rius", "settings": None}))
            ck("rius with no settings is allowed (the key may come from the environment)", r.status_code == 200, r.text[:200])

            print("== the headers route ==")
            await root.put("/api/factory/integrations/monitoring", json={
                "name": "otlp", "settings": {"endpoint": "https://collector.example.com:4318",
                                                    "headers": f"x-token={HDR}"}})
            r = await st.get("/api/factory/monitoring/headers")
            ck("a station gets the headers", r.status_code == 200 and r.json() == {
                "headers": {"x-token": HDR}}, r.text)
            ck("no-store", "no-store" in r.headers.get("cache-control", ""), dict(r.headers))
            r = await root.get("/api/factory/monitoring/headers")
            ck("a caller that is not a station gets 403", r.status_code == 403, (r.status_code, r.text[:120]))
            await root.put("/api/factory/integrations/monitoring", json={"name": "off"})
            r = await st.get("/api/factory/monitoring/headers")
            ck("off: {} headers", r.status_code == 200 and r.json() == {"headers": {}}, r.text)

            print("== the connection test ==")
            srv, base = serve()
            Handler.status = 200
            r = ru(await root.post("/api/factory/integrations/monitoring/test", json={
                "name": "otlp", "settings": {"endpoint": base, "headers": f"x-token={HDR}", "api_key": KEY}}))
            ck("an internal address is refused for the test, like every outbound call", r.status_code == 422
               and "internal" in r.text and not HITS, (r.status_code, r.text[:200]))
            os.environ["TARES_WEBHOOK_ALLOW_PRIVATE"] = "1"
            r = ru(await root.post("/api/factory/integrations/monitoring/test", json={
                "name": "otlp", "settings": {"endpoint": base, "headers": f"x-token={HDR}", "api_key": KEY}}))
            ck("an endpoint that answers is ok", r.status_code == 200 and r.json().get("ok") is True, r.text[:300])
            hit = HITS[-1]
            ck("the test posted to /v1/traces with the headers",
               hit["path"] == "/v1/traces" and hit["headers"].get("x-token") == HDR
               and hit["headers"].get("authorization") == f"Bearer {KEY}", hit)
            Handler.status = 401
            r = ru(await root.post("/api/factory/integrations/monitoring/test", json={
                "name": "otlp", "settings": {"endpoint": base, "headers": f"x-token={HDR}", "api_key": KEY}}))
            ck("a 401 is a refusal", r.json().get("ok") is False and "refused" in r.json().get("message", ""), r.text[:300])
            Handler.status = 500
            r = ru(await root.post("/api/factory/integrations/monitoring/test", json={
                "name": "otlp", "settings": {"endpoint": base, "headers": f"x-token={HDR}", "api_key": KEY}}))
            ck("a 500 is an error", r.json().get("ok") is False and "500" in r.json().get("message", ""), r.text[:300])
            Handler.status = 415
            r = ru(await root.post("/api/factory/integrations/monitoring/test", json={
                "name": "otlp", "settings": {"endpoint": base, "headers": f"x-token={HDR}", "api_key": KEY}}))
            ck("a 415 to an empty body still means a collector is there", r.json().get("ok") is True, r.text[:300])
            srv.shutdown()
            r = ru(await root.post("/api/factory/integrations/monitoring/test", json={
                "name": "otlp", "settings": {"endpoint": base, "headers": f"x-token={HDR}", "api_key": KEY}}))
            ck("nothing listening is not ok, and the message has no secret",
               r.json().get("ok") is False and KEY not in r.text, r.text[:300])
            direct = await registry.get("monitoring", "otlp")().check_connection({"endpoint": "ftp://h"})
            ck("a non-http address is refused before any call", direct[0] is False and "http" in direct[1], direct)
            direct = await registry.get("monitoring", "otlp")().check_connection({})
            ck("no address is asked for", direct[0] is False and "address" in direct[1], direct)
            direct = await registry.get("monitoring", "otlp")().check_connection(None)
            ck("settings None do not crash", direct[0] is False, direct)

            ck("no response of the run holds a secret", not any(s in t for t in ALL for s in SECRETS),
               [s for s in SECRETS if any(s in t for t in ALL)])

    print("== the helper script ==")
    srv, base = serve()
    Handler.status = 200
    Handler.body = json.dumps({"headers": {"x-token": HDR, "authorization": "Bearer abc", "n": 1, "e": ""}}).encode()
    cred = os.path.join(_TMP, "cred")
    with open(cred, "w") as fh:
        fh.write(CREW + "\n")
    env = {"TARES_FACTORY_URL": base, "TARES_FACTORY_CRED_FILE": cred, "FACTORY_STATION": "crew-build-x",
           "FACTORY_ROLE": "builder", "FACTORY_PARENT": "crew-orchestrator"}
    HITS.clear()
    r = helper(env)
    ck("it prints only the string headers, as one JSON line",
       json.loads(r.stdout) == {"x-token": HDR, "authorization": "Bearer abc"} and r.stdout.count("\n") == 1, r.stdout)
    ck("it printed nothing on stderr", r.stderr == "", r.stderr)
    hit = HITS[-1]
    ck("it asked the headers route with the crew token and the station labels",
       hit["path"] == "/api/factory/monitoring/headers" and hit["headers"].get("authorization") == f"Bearer {CREW}"
       and hit["headers"].get("x-tares-station") == "crew-build-x" and hit["headers"].get("x-tares-role") == "builder"
       and hit["headers"].get("x-tares-parent") == "crew-orchestrator", hit)
    for label, extra in (("no token file", {"TARES_FACTORY_CRED_FILE": os.path.join(_TMP, "nope")}),
                         ("no address", {"TARES_FACTORY_URL": ""}),
                         ("an empty token file", {"TARES_FACTORY_CRED_FILE": os.devnull})):
        r = helper({**env, **extra})
        ck(f"{label}: {{}}", r.stdout.strip() == "{}" and r.returncode == 0, (r.stdout, r.returncode))
    Handler.status = 403
    r = helper(env)
    ck("a refusal: {}", r.stdout.strip() == "{}" and r.returncode == 0, r.stdout)
    Handler.status = 200
    Handler.body = b"not json"
    ck("a bad answer: {}", helper(env).stdout.strip() == "{}")
    Handler.body = b'{"headers": ["a"]}'
    ck("headers that are not an object: {}", helper(env).stdout.strip() == "{}")
    srv.shutdown()
    ck("a server that is down: {}", helper(env).stdout.strip() == "{}")
    r = subprocess.run([sys.executable, "-c", "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()",
                        "hook", "otel-headers", "--dry-run"],
                       capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT})
    ck("`tares hook otel-headers --dry-run` finds the script",
       r.returncode == 0 and r.stdout.strip().endswith("scripts/otel_headers.py"), (r.returncode, r.stdout, r.stderr[-200:]))

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
