"""Seed a scratch factory Tares for factory-setup.e2e.cjs (TR-872): the Set up your factory screen as a
cell that has been partly set up shows it. Invented names only.
  tickets: Linear connected as the default (its API key saved), GitHub Issues connected for
      example-org/orders-api; project-p picks the GitHub Issues connection, project-q has no pick;
  monitoring: an OTLP endpoint connected as the default (its address and API key saved);
  every other step keeps the built-in choice, so the screen shows both "set" and "not set yet" steps.
A daemon without a token starts on the data directory, the seed goes through its API (the routes the
screen saves through), then the daemon is stopped. The e2e then starts its own on the same data.
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-setup.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def call(path, body=None, method=None):
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"content-type": "application/json"}, method=method)
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


out = {}
daemon = subprocess.Popen([tares, "up", "--port", port, "--data-dir", home], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("daemon did not start")
    for name in ("project-p", "project-q"):
        out[name] = call("/api/projects", {"template": "custom", "name": name, "objects": [], "goal": "A project.",
                                           "kind": "software_factory"})["id"]
    call("/api/factory/integrations/tickets", {"name": "linear", "settings": {"api_key": "lin_api_invented_key"}, "default": True}, "PUT")
    call("/api/factory/integrations/tickets", {"name": "github", "settings": {"repo": "example-org/orders-api"}, "default": False}, "PUT")
    call(f"/api/projects/{out['project-p']}/integrations/tickets", {"name": "github", "settings": {}}, "PUT")
    call("/api/factory/integrations/monitoring", {"name": "otlp", "settings": {
        "endpoint": "https://collector.example.org:4318", "api_key": "otlp_invented_key"}, "default": True}, "PUT")
finally:
    daemon.terminate()
    daemon.wait()
print(json.dumps(out))
