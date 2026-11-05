"""Seed a scratch factory Tares for factory-reviews.e2e.cjs (TR-735): the pull requests in review as
a cell with some history shows them. Invented names only. The crew's autonomy is left at its default
(L3-review), so every pull request the reviewer passed on its current commit waits for the person.
  project-x: six tickets in review. T1 passed in round 2 (6 days, the only ticket of "1. Foundations",
    so it holds that milestone up), T2 passed with CI failing (3 hours), T3 passed with no CI (10
    minutes), T4 passed on an older commit (2 days), T5 changes asked (30 hours; its title and
    milestone carry markup), T6 never reviewed with CI going (8 minutes);
  project-y: T1 passed (50 hours), the only ticket of its second milestone "Faster lists";
  project-u: one ticket not in review (no pull request);
  a project whose name carries markup: one passed pull request (1 hour).
Starts a daemon on the scratch data dir with TARES_EDITION=factory (so the cell settles to the
factory edition), stops it, then writes the projects, tickets, pull requests and reviews straight
into the database and backdates them. The crew's autonomy stays L3, so 5 pull requests wait for the
person (T1, project-y's T1, T2, the markup project's, T3, oldest first) and 3 are with the crew (T4,
T5, T6). Prints a JSON map of names to ids on the last line.
usage: python seed-factory-reviews.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
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
from tares import factory as factory_mod  # noqa: E402
from tares.store import Store, now_utc  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
ids = {}
REPO = "example-org/service-a"
OLD, NEW = "aaaaaaa1111", "bbbbbbb2222"


def project(name, goal):
    uid = "uc_" + "".join(c for c in name.lower() if c.isalnum())[:14] + str(len(ids))
    store.create_project(uid, "custom", name, {}, goal=goal)
    store.set_project_kind(uid, "software_factory")
    ids[name] = uid
    return uid


def in_review(uid, title, n, *, verdict="pass", reviewed=NEW, head=NEW, ci="success", milestone=None, rounds=1, **waited):
    tid = store.create_ticket(uid, "tares", title, milestone=milestone, repo=REPO)
    store.update_ticket(tid, stage="review", pr={"url": f"https://github.com/{REPO}/pull/{n}", "number": n, "repo": REPO,
                                                 "head": head, "ci": ci, "title": f"PR {n}: {title}"})
    if verdict:
        for r in range(rounds):
            store.add_review(uid, tid, None, reviewed, "changes" if r < rounds - 1 else verdict, "ran the tests", None,
                             "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc())
    with store._swrite:
        store.sc.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", [now_utc() - timedelta(**waited), tid])
    return tid


x = project("project-x", "Give the console an edition for each kind of team.")
m1 = store.create_milestone(x, "1. Foundations")
m2 = store.create_milestone(x, "M2: <u>Speed</u>")
in_review(x, "Add the footer", 11, milestone=m1, rounds=2, days=6)
in_review(x, "Add the sitemap", 12, ci="failure", hours=3)
in_review(x, "Add the search box", 13, ci=None, minutes=10)
in_review(x, "Add the news page", 14, reviewed=OLD, days=2)
in_review(x, 'Fix <b>bold</b> <img src=x onerror="window.__pwned=1"> in the header', 15, verdict="changes", milestone=m2, hours=30)
in_review(x, "Add the archive", 16, verdict=None, ci="pending", minutes=8)

y = project("project-y", "Tidy the styles across the console.")
store.create_milestone(y, "Setup")
fast = store.create_milestone(y, "Faster lists")
in_review(y, "Batch the lists", 21, milestone=fast, hours=50)

# the crew has project-x and project-y, so their pull requests not waiting for you are the crew's
for p_ in (x, y):
    store.set_handover(p_, [{"path": "/w/a", "name": REPO}], None, None, now_utc())

# a finished project still holding a pull request in review: nobody is on it (TR-1009)
f = project("project-f", "Finished, with one pull request left in review.")
in_review(f, "Old bench script", 41, verdict=None, days=7)
store.set_handover(f, [{"path": "/w/a", "name": REPO}], None, None, now_utc())
store.update_project(f, status="done")

u = project("project-u", "Nothing in review yet.")
store.create_ticket(u, "tares", "Write the plan")

pm = project("project-<i>m</i>", "A project whose name carries markup.")
in_review(pm, "Tidy the nav", 31, hours=1)

store.con.close()
print(json.dumps(ids))
