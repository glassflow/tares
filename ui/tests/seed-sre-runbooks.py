"""Seed a scratch AI SRE Tares for sre-runbooks.e2e.cjs (TR-753): the Runbooks and memory page as a cell
with some history shows it. Invented names only.
The cell is the shared one of tests/fixtures/sre_cell.py (a watcher and an investigator in one project,
services edge-proxy, service-a, docs-site, service-a-receiver, and extra-svc-01 to extra-svc-03). On top:
  edge-proxy   five runbook lines by Alex, Kim and Lee (3 days to 1 month back), two memory notes,
               read by the investigator 4 times this week
  extra-svc-01 a 500 character line and a short one, read by the investigator 12 times this week
  extra-svc-02 one runbook line
  service-a    memory only (two notes), no runbook
  docs-site    nothing
The runbooks are written straight into the cell's store, so their authors and days are fixed. The memory
notes go in through a daemon's own /remember route (started on the scratch data dir with
TARES_EDITION=sre, stopped again). Prints a JSON map on the last line.
usage: python seed-sre-runbooks.py <data-dir> <port> <mcp-port> <ui-dist>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import timedelta

home, port, mcp, dist = sys.argv[1:5]
repo = os.getcwd()
sys.path.insert(0, repo)
sys.path.insert(0, os.path.join(repo, "tests"))

from fixtures.sre_cell import PROJECT, seed_cell          # noqa: E402
from tares.envelope import now_utc                        # noqa: E402
from tares.screens import sre_runbooks                    # noqa: E402
from tares.store import Store                             # noqa: E402

UID = "prj-" + PROJECT
LONG_LINE = ("The checkout service depends on the payments gateway, the inventory cache and the fraud scorer, and "
             "when any of the three slows down the symptom is a burst of 502 errors from edge-proxy. Before "
             "restarting anything, check the gateway status page, then the cache hit rate, then the scorer queue "
             "depth; if all three look normal, the cause is almost always a bad deploy of checkout-v2, so roll it "
             "back first and investigate afterwards. Do not restart the cache during business hours.")
assert len(LONG_LINE) <= 500, len(LONG_LINE)

EDGE = [("Check /health on the load balancer before restarting edge-proxy.", "Alex", 3),
        ("The checkout-v2 cluster retries 3 times; a burst of 502s after a deploy is usually the retry budget.", "Kim", 14),
        ("Rolling back is safe: the last 5 deploys had no migrations.", "Alex", 14),
        ("Page the payments team only when 5xx stays above 2% for 10 minutes.", "Kim", 1),
        ("Ignore the nightly 02:00 spike: it is the log rotation.", "Lee", 30)]

made = seed_cell(home, "sre", watcher=True, investigator=True, services=3)

store = Store(os.path.join(home, "tares.duckdb"))
try:
    today = now_utc().date()

    def runbook(service, lines):
        body = f"# Runbook for {service}\n\n" + "".join(
            f"- {t} (by {who}, {(today - timedelta(days=d)).isoformat()})\n" for t, who, d in lines)
        store.upsert_skill(UID, sre_runbooks.skill_name(service), sre_runbooks._description(service), body,
                           updated_by=lines[-1][1])

    def reads(service, n, agent="investigator"):
        for i in range(n):
            rid = f"run_{service}_{i}"
            store.start_agent_run(rid, agent, "watch-prod", "", service, "h")
            store.finish_agent_run(rid, "ok")
            store.set_run_skills(rid, [sre_runbooks.skill_name(service)])

    runbook("edge-proxy", EDGE)
    runbook("extra-svc-01", [(LONG_LINE, "Kim", 7), ("Roll back the last deploy first.", "Alex", 3)])
    runbook("extra-svc-02", [("Restart the job runner before the failover.", "Lee", 1)])
    reads("edge-proxy", 4)
    reads("extra-svc-01", 12)
finally:
    store.con.close()

env = {**os.environ, "PYTHONPATH": repo, "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "sre", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"


def remember(key, content):
    r = urllib.request.Request(base + "/remember", json.dumps({"key": key, "content": content}).encode(),
                               {"content-type": "application/json"})
    try:
        urllib.request.urlopen(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"/remember -> {e.code} {e.read().decode()[:300]}")


# the daemon of this checkout, started from the interpreter that runs this script (no installed `tares` needed)
daemon = subprocess.Popen([sys.executable, "-c", "from tares.cli import main; main()", "up", "--port", port, "--data-dir", home], env=env, cwd=repo,
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
    remember("edge-proxy", "A restart of edge-proxy cleared a 502 burst on 28 Oct; the cause was a stale upstream list.")
    time.sleep(0.3)
    remember("edge-proxy", "p95 latency above 800 ms usually follows a cache flush.")
    remember("service-a", "A restart of service-a cleared a burst of failed jobs on 28 Oct; the cause was a stale worker list.")
    time.sleep(0.3)
    remember("service-a", "Job latency above 800 ms usually follows a cache flush.")
    time.sleep(0.8)      # the notes are written after the route answers
finally:
    daemon.terminate()
    try:
        daemon.wait(timeout=15)
    except subprocess.TimeoutExpired:
        daemon.kill()

print(json.dumps({"services": made["services"], "project": UID}))
