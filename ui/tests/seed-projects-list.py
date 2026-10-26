"""Seed a scratch Tares for projects-list.e2e.cjs (TR-689): the Projects list as a busy cell shows it.
16 projects: software factory ones with tickets in every state (done, in progress, blocked, to do),
ordinary ones with and without objects, a draft, a paused and a failing one, long names, long goals,
done and active. Starts a daemon on the scratch data dir, creates the projects through its API,
stops it, then writes statuses, tickets and timestamps straight into the database. Prints a JSON
map of project name to id on the last line.
usage: python seed-projects-list.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp, "TARES_UI_DIST": dist}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"):
    env.pop(k, None)
base = f"http://127.0.0.1:{port}"

LONG = ("Close the loop on every alert the on-call engineer used to triage by hand: reproduce it, "
        "find the commit, open the fix, and flag anything that needs a person before it ships")

# name, goal, factory?, (done, in progress, blocked, to do), status, project updated (days ago),
# newest ticket activity (minutes ago, None = the tickets are as old as the project)
SPECS = [
    ("Rius: Admin delete user", "Admins delete a user from rius-admin; every org they created or administer goes with all its workspaces", True, (32, 0, 0, 0), "done", 6, None),
    ("Rius Bugs Auto", "The crew works every Todo rius-bugs-triage bug in Linear Rius Bugs: reproduce, fix, PR, flag human-review ones", True, (6, 0, 0, 0), "active", 5, None),
    ("Rius: Cursor Codex Gaps", "Close RIUS-1414: make the Rius plugin's Cursor support fully supported and verified in live Cursor", True, (4, 1, 2, 4), "active", 11, None),
    ("vetme-desktop", "macOS app running vetme interviews, mocks and Sharpen on the user's own Claude Code or Codex", True, (188, 3, 1, 15), "active", 13, 9),
    ("Rius: Silent Err 0", "Silent Errors 1: a per-run findings store and detector framework in argus-core, findings on the run page", True, (18, 0, 0, 0), "done", 11, None),
    ("Tests auto fix", "A continuous loop that finds flaky tests in argus-core CI and fixes them with test-only changes, one PR per test", True, (11, 0, 0, 0), "active", 5, None),
    ("silent-errors-context-rot", "Rius finds runs where an agent silently degrades from context pressure (window nearly full, one tool result too big)", True, (26, 2, 0, 4), "active", 6, None),
    ("link-click-counter", "Count every successful redirect of a short link and return the count at GET /links/{code}/stats", True, (2, 0, 0, 0), "done", 6, None),
    ("Challenger workflow", "Get a second opinion on Claude Code's plans and commits, and a summary of each session", True, (0, 0, 0, 0), "active", 6, None),
    ("🔄 Tares Bugs Auto Triage & Fix", "The crew fixes every bug filed in the Linear project Tares Bugs (team Tares): one builder per bug, a regression test, a PR", True, (5, 1, 0, 0), "active", 2, 4),
    ("Claude Code auto-update (TR-669)", "The crew's watch moves idle stations onto a new Claude Code version like a plugin update, with crew-wide pauses", True, (1, 1, 0, 0), "active", 2, None),
    ("Factory optimizations", "Make the crew cheaper per merged PR: idle wake-ups, plan-limit pacing, a kit freeze, doctor language servers", True, (10, 0, 0, 0), "done", 0, 17),
    ("fb-90a1", "Spec: unsubscribe from Rius alert emails", True, (0, 0, 0, 9), "active", 0, 17),
    ("Checkout latency watch", LONG, False, None, "paused", 20, None),
    ("Billing webhooks", "Catch failed Stripe webhooks before the customer notices", False, None, "error", 30, None),
    ("New idea", "", False, None, "draft", 1, None),
]


def post(path, body):
    r = urllib.request.Request(base + path, json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:300]}")


daemon = subprocess.Popen([tares, "up", "--port", port, "--data-dir", home], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ids = {}
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("daemon did not start")
    for name, goal, factory, *_ in SPECS:
        body = {"template": "custom", "name": name, "objects": [], "goal": goal or None}
        if factory:
            body["kind"] = "software_factory"
        ids[name] = post("/api/projects", body)["id"]
finally:
    daemon.terminate()
    daemon.wait()

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
con = store.con
for name, goal, factory, counts, status, days, fresh in SPECS:
    uid = ids[name]
    con.execute("UPDATE usecases SET status = ?, updated_at = now() - to_days(?) WHERE id = ?", [status, days, uid])
    if not counts:
        continue
    done, doing, blocked, todo = counts
    kinds = ["done"] * done + ["doing"] * doing + ["blocked"] * blocked + ["todo"] * todo
    for n, k in enumerate(kinds):
        tid = store.create_ticket(uid, "tares", f"Ticket {n + 1}", {"done": "done", "doing": "in_progress"}.get(k, "todo"))
        if k == "blocked":
            store.update_ticket(tid, status="in_progress", stage="blocked", stage_reason="waiting on a decision")
        con.execute("UPDATE tickets SET updated_at = now() - to_days(?) WHERE id = ?", [days, tid])
    if fresh is not None:
        con.execute("UPDATE tickets SET updated_at = now() - to_minutes(?) WHERE id = "
                    "(SELECT id FROM tickets WHERE project = ? ORDER BY position LIMIT 1)", [fresh, uid])
con.close()
print(json.dumps(ids))
