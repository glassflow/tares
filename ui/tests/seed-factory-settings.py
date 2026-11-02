"""Seed a scratch factory Tares for factory-settings.e2e.cjs (TR-738): the Settings page as a cell
that has been used shows it. Invented names only.
  GitHub connected as example-org; Claude Code reported a minute ago; Linear and Slack not connected;
  a read key and a write key; project-x with grants (one shared, one for the project, long text,
  markup, and "D79 A" that answers the 79th desk question, a real one);
  the crew settings as a new cell has them (up to merge, 3 builders, second check off, pacing off).
Phase 1 writes the desk and the GitHub connection into the database, phase 2 starts a daemon without
a token and does the rest through its API, then the daemon is stopped. The e2e then runs its own
daemon on the same data with TARES_AUTH_TOKEN set. Prints a JSON map on the last line.
usage: python seed-factory-settings.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"
sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

QUESTION = "Which branch should the next wave of pull requests target?"

# phase 1: the desk (79 questions, so the last one is a real D79) and the GitHub connection
store = Store(os.path.join(home, "tares.duckdb"))
now = datetime.now(timezone.utc)
for i in range(1, 80):
    q = QUESTION if i == 79 else f"Question number {i} for the desk?"
    item = store.add_desk_item(None, q, "", ["A", "B"], "A", "It keeps history short.", "", [], False,
                               "crew-orchestrator", [], now, None, None)
store.add_decision(None, item["id"], QUESTION, "A", "A", True, "all", True, None, True, [], now)
assert item["label"] == "D79", item["label"]
store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
store.con.close()


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
    px = call("/api/projects", {"template": "custom", "name": "project-x", "objects": [], "goal": "A project.",
                                "kind": "software_factory"})["id"]
    out["project"] = px
    call("/api/crew/settings", {"autonomy": "L3-review", "builders_max": 3, "challenger": False, "pace": False}, "PUT")
    out["read_key"] = call("/api/keys", {"name": "reader", "scopes": ["read"]})["secret"]
    call("/api/keys", {"name": "writer", "scopes": ["ingest"]})
    grants = [
        ("Alex may merge any pull request once the checks are green", {"scope": "all"}),
        ("Kim's builders may read the two access keys from the keychain inside their GET calls", {"scope": "project", "project": px}),
        ("D79 A", {"scope": "all"}),
        ("Lee may push to the project-x branch. " * 6 + "<b>bold</b> <img src=x onerror=alert(1)>", {"scope": "project", "project": px}),
    ] + [(f"Builders may delete the merged branch number {i} in example-org/service-a", {"scope": "all"}) for i in range(1, 7)]
    for words, scope in grants:
        call("/api/grants", {"words": words, **scope})
    call("/api/sources", {"name": "claude-code", "type": "ingest", "connector": "claude_code", "config": {}})
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = urllib.request.Request(base + "/ingest/claude_code", (json.dumps(
        {"type": "user", "sessionId": "s1", "cwd": "/w", "timestamp": ts,
         "message": {"role": "user", "content": "hi"}}) + "\n").encode(), {"content-type": "application/x-ndjson"})
    urllib.request.urlopen(r)
finally:
    daemon.terminate()
    daemon.wait()
print(json.dumps(out))
