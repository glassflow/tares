"""Seed a scratch factory Tares for factory-tickets.e2e.cjs (TR-806): a cell with enough tickets to
page, group and cap. Invented names only.
  project-x: 31 tickets and three milestones (1. Foundations, 2. Speed, 3. Launch).
    T1 to T12 merged, T13 in progress (a builder, a pull request), T14 in review (the reviewer),
    T15 changes asked (the lead), T16 blocked by T13, T17 to T30 to do (14), T31 canceled.
    T1 is the newest change, T31 the oldest (20 minutes apart).
  project-y: T1 in progress (a builder), T2 to do, T3 merged; milestone "Faster lists".
  a project whose name carries markup: one ticket to do whose title carries markup.
  project-w: no tickets.
Starts a daemon on the scratch data dir with TARES_EDITION=factory (so the cell settles to the
factory edition), stops it, then writes projects, tickets and milestones straight into the database
and backdates them. Prints a JSON map of names to ids on the last line.
usage: python seed-factory-tickets.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.request
from datetime import timedelta

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)

daemon = subprocess.Popen([tares, "up", "--port", port, "--data-dir", home], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("daemon did not start")
finally:
    daemon.terminate()
    daemon.wait()

sys.path.insert(0, os.getcwd())
from tares.store import Store, now_utc  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
ids = {}
REPO = "example-org/service-a"


def project(name, goal):
    uid = "uc_" + "".join(c for c in name.lower() if c.isalnum())[:14] + str(len(ids))
    store.create_project(uid, "custom", name, {}, goal=goal)
    store.set_project_kind(uid, "software_factory")
    ids[name] = uid
    return uid


def ticket(uid, title, minutes, *, milestone=None, stage=None, status=None, holder=None, depends_on=None, pr=None):
    tid = store.create_ticket(uid, "tares", title, milestone=milestone, repo=REPO, depends_on=depends_on)
    fields = {k: v for k, v in (("stage", stage), ("status", status), ("holder", holder), ("pr", pr)) if v is not None}
    if fields:
        store.update_ticket(tid, **fields)
    with store._swrite:
        store.sc.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", [now_utc() - timedelta(minutes=minutes), tid])
    return tid


def pr(n, ci="success"):
    return {"url": f"https://github.com/{REPO}/pull/{n}", "number": n, "repo": REPO, "head": "bbbbbbb2222", "ci": ci,
            "title": f"PR {n}"}


x = project("project-x", "Give the console an edition for each kind of team.")
m1 = store.create_milestone(x, "1. Foundations")
m2 = store.create_milestone(x, "Speed")
m3 = store.create_milestone(x, "Launch")
BUILD = "project-x-build-m2"
t = {}
for n in range(1, 13):
    t[n] = ticket(x, f"Merged step {n}", n * 20, milestone=m1 if n <= 6 else m2, stage="merged")
t[13] = ticket(x, "Speed up the list", 13 * 20, milestone=m2, stage="doing", status="in_progress", holder=BUILD, pr=pr(113, "pending"))
t[14] = ticket(x, "Cache the counts", 14 * 20, milestone=m2, stage="review", status="in_progress", holder="crew-reviewer", pr=pr(114))
t[15] = ticket(x, "Page the answers", 15 * 20, milestone=m2, stage="changes", status="in_progress", holder="crew-orchestrator", pr=pr(115, "failure"))
t[16] = ticket(x, "Announce the speed-up", 16 * 20, milestone=m3, depends_on=[t[13]])
for n in range(17, 31):
    t[n] = ticket(x, f"Plan item {n}", n * 20, milestone=m3 if n % 2 else m2)
t[31] = ticket(x, "Use a second search provider", 31 * 20, milestone=m2, status="canceled")

y = project("project-y", "Tidy the styles across the console.")
fast = store.create_milestone(y, "Faster lists")
ticket(y, "Batch the lists", 5, milestone=fast, stage="doing", status="in_progress", holder="project-y-build-m1", pr=pr(21, "pending"))
ticket(y, "Round the counts", 70, milestone=fast)
ticket(y, "Write the guide", 90, milestone=fast, stage="merged")

pm = project("project-<i>m</i>", "A project whose name carries markup.")
ticket(pm, 'Fix <b>bold</b> <img src=x onerror="window.__pwned=1"> in the header', 3)

project("project-w", "A project with no tickets yet.")

store.con.close()
print(json.dumps(ids))
