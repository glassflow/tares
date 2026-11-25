"""Seed a scratch factory Tares for report.e2e.cjs (TR-1667). Invented names only.
Tickets merged in whole weeks before this one, so the counts hold on any day:
  project-a (Ada leads it): 3 merged 1 week ago, 1 merged 20 weeks ago;
  project-b (Ada leads it): 2 merged 2 weeks ago, 4 merged 6 weeks ago;
  project-c (Bo leads it): one open ticket, nothing merged.
So 8 weeks hold 9 merged tickets, 4 weeks 5, and the last 20 weeks and more 10.
Starts a daemon on the scratch data dir with TARES_EDITION=factory (so the cell settles to the
factory edition), stops it, then writes the people, projects and tickets straight into the database
and rolls them up into the weekly figures. Prints a JSON map of names to ids, with the Mondays used,
on the last line.
usage: python seed-factory-report.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

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
from tares import report  # noqa: E402
from tares.store import Store  # noqa: E402

store = Store(os.path.join(home, "tares.duckdb"))
today = datetime.now(timezone.utc).date()
monday = today - timedelta(days=today.weekday())
ids = {name: store.create_person(name, f"{name.lower()}@example.com")["id"] for name in ("Ada", "Bo")}


def project(name, lead):
    uid = "uc_report_" + name[-1]
    store.create_project(uid, "custom", name, {}, goal="g", lead_person_id=ids[lead])
    store.set_project_kind(uid, "software_factory")
    ids[name] = uid
    return uid


def merged(uid, back, n):
    """`n` tickets of `uid`, merged on the Wednesday `back` weeks before this week's."""
    day = monday - timedelta(weeks=back) + timedelta(days=2)
    when = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    for _ in range(n):
        tid = store.create_ticket(uid, "tares", "Add a page")
        store.add_ticket_history(tid, uid, "stage", "doing", None, None, None, when - timedelta(hours=2))
        store.add_ticket_history(tid, uid, "stage", "merged", None, None, None, when)


a = project("project-a", "Ada")
b = project("project-b", "Ada")
c = project("project-c", "Bo")
merged(a, 1, 3)
merged(a, 20, 1)
merged(b, 2, 2)
merged(b, 6, 4)
store.create_ticket(c, "tares", "Write the plan")
report.rebuild_all(store)
store.con.close()

ids["monday"] = monday.isoformat()
ids["old_monday"] = (monday - timedelta(weeks=20)).isoformat()
print(json.dumps(ids))
