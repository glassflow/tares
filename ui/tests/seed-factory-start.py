"""Seed a scratch factory Tares for the Start checklist e2e (TR-756): a cell part of the way through
its setup. Invented names only.
  GitHub connected as example-org; one access key (laptop) that has been used;
  no crew stations, no Linear, no project and so no spec, no GitHub Issues link.
So the Start block shows two of five steps done: sign in and Connect GitHub.
Phase 1 writes the GitHub connection into the database, phase 2 starts a daemon with
TARES_EDITION=factory and no token and makes the key through its API, then stops it, and phase 3
marks the key used. The e2e runs its own daemon on the same data.
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-start.py <data-dir> [<port> <mcp-port> <ui-dist> <tares-bin>]
The port defaults to TARES_E2E_PORT, else 8875; the MCP port to the port plus one; the console
build to ui/dist; the binary to `tares`."""
import json, os, subprocess, sys, time, urllib.error, urllib.request

args = sys.argv[1:]
home = args[0]
port = args[1] if len(args) > 1 else os.environ.get("TARES_E2E_PORT", "8875")
mcp = args[2] if len(args) > 2 else str(int(port) + 1)
dist = args[3] if len(args) > 3 else os.path.join(os.getcwd(), "ui", "dist")
tares = args[4] if len(args) > 4 else "tares"
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "TARES_LOGIN_URL",
          "TARES_GITHUB_CONNECT_URL", "TARES_LINEAR_CONNECT_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"
sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

DB = os.path.join(home, "tares.duckdb")

# phase 1: the GitHub connection
store = Store(DB)
store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
store.con.close()


def call(path, body=None, method=None):
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"content-type": "application/json"}, method=method)
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


# phase 2: the key
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
    made = call("/api/keys", {"name": "laptop", "scopes": ["read"]})
    out["key"] = made["id"]
    out["key_secret"] = made["secret"]
finally:
    daemon.terminate()
    daemon.wait()

# phase 3: the key has been used
store = Store(DB)
store.touch_api_key(out["key"])
store.con.close()
print(json.dumps(out))
