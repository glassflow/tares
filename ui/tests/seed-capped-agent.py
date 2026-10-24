"""Seed a scratch Tares for raise-limit.e2e.cjs: a project with a `watcher` agent that has used its
one run a day, 22h48m ago, so its next run is allowed in about 1h 12m, followed by 8 runs the cap
refused (TR-671). Older than that: a lone capped run between two ok runs, which the list must keep
as a plain row (TR-678). Starts a daemon on the scratch data dir, builds the project through its API, stops
it, and writes the finished run straight into the database. Prints the project id.
usage: python seed-capped-agent.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request, uuid

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_AGENT_DAILY_CAP": "1",
       "ANTHROPIC_API_KEY": "sk-ant-scratch-not-real"}   # enabling an agent needs a provider; it is never called
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def post(path, body):
    r = urllib.request.Request(base + path, json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


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
    proj = post("/api/projects", {"template": "custom", "name": "AI SRE demo", "objects": []})["id"]
    post("/api/sources", {"name": "checkout-errors", "project": proj, "connector": "webhook",
                          "poll": "5s", "config": {"event_type": "log", "text_template": "{msg}",
                          "labels": [{"name": "service", "field": "service", "primary": True}]}})
    post("/api/triggers", {"name": "spike", "project": proj, "sources": ["checkout-errors"],
                           "key_field": "service", "cooldown": "5m",
                           "condition": {"aggregate": "count", "predicate": "> 5", "window": "5m"}})
    post("/api/agents/builtin", {"name": "watcher", "trigger": "spike", "prompt": "watch", "project": proj})
    post("/api/agents/builtin/watcher/enable", {})
finally:
    daemon.terminate()
    daemon.wait()

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
run = uuid.uuid4().hex
store.start_agent_run(run, "watcher", "spike", "d1", "checkout", "h", project=proj)
store.finish_agent_run(run, "concluded", rounds=2)
store.con.execute("UPDATE agent_runs SET started_at = now() - INTERVAL '22 hours 48 minutes' "
                  "WHERE id = ?", [run])
# older history: ok, one capped run on its own, ok; the lone one must not fold into a group of one
for n, (status, age) in enumerate([("concluded", "30 hours"), ("capped", "26 hours")]):
    old = uuid.uuid4().hex
    store.start_agent_run(old, "watcher", "spike", f"old{n}", "checkout", "h", project=proj)
    store.finish_agent_run(old, status, rounds=2) if status != "capped" else \
        store.finish_agent_run(old, status, error="cap of 1 runs in the last 24h reached for "
                                                  "this agent; raise it under Settings, Agents")
    store.con.execute("UPDATE agent_runs SET started_at = now() - INTERVAL '" + age + "' WHERE id = ?", [old])
for i in range(8):   # what a trigger firing past the cap leaves behind, one row each
    capped = uuid.uuid4().hex
    store.start_agent_run(capped, "watcher", "spike", f"d{i + 2}", "checkout", "h", project=proj)
    store.finish_agent_run(capped, "capped", error="cap of 1 runs in the last 24h reached for "
                                                   "this agent; raise it under Settings, Agents")
store.con.close()
print(proj)
