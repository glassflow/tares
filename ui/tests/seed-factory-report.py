"""Seed a scratch factory Tares for report.e2e.cjs (TR-1667, TR-1668). Invented names only.
Tickets merged in whole weeks before this one, so the counts hold on any day (in brackets: hours
from start to merge, "-" a ticket never marked doing):
  project-a (Ada leads it): 3 merged 1 week ago [2, 3, 4], 1 merged 20 weeks ago [2];
    1 week ago one of them passed review in a second round and one in its first;
    a milestone accepted 1 week ago and one 5 weeks ago;
  project-b (Ada leads it): 2 merged 2 weeks ago [26, -], 4 merged 6 weeks ago [1, 1, 1, 1];
    2 weeks ago one passed its first review; 30 weeks ago an open ticket was sent back;
    a milestone accepted 5 weeks ago;
  project-c (Bo leads it): one open ticket, nothing merged.
So 8 weeks hold 9 merged tickets, 4 weeks 5, and the last 20 weeks and more 10.
For the Time section (TR-1669): three of the merged tickets also passed through review, changes and
blocked, and two answered blocking questions held them up, so 8 weeks hold 39 hours of stage time,
5 and a half of them waiting. Stuck now: seven open blocking questions the viewer may read (six of
project-a and project-b, one of no project) and one of no project addressed to Ada; seven open
tickets of project-a and project-b in a stage. project-c has neither.
Starts a daemon on the scratch data dir with TARES_EDITION=factory (so the cell settles to the
factory edition), stops it, then writes the people, projects, tickets, reviews and milestones
straight into the database and rolls them up into the weekly figures. Prints a JSON map of names to
ids, with the Mondays used, on the last line; "questions" and "tickets" in it map the age in hours
of each stuck candidate to its label (D7, T12).
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
from tares import factory, report  # noqa: E402
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


def wednesday(back, hour=12):
    """The Wednesday `back` weeks before this week's, at `hour` UTC."""
    day = monday - timedelta(weeks=back) + timedelta(days=2)
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def merged(uid, back, *took):
    """A ticket of `uid` for each of `took`, merged `back` weeks ago, that many hours after it was
    taken up (None: it was never marked doing)."""
    tids = []
    for hours in took:
        tid = store.create_ticket(uid, "tares", "Add a page")
        if hours is not None:
            store.add_ticket_history(tid, uid, "stage", "doing", None, None, None,
                                     wednesday(back) - timedelta(hours=hours))
        store.add_ticket_history(tid, uid, "stage", "merged", None, None, None, wednesday(back))
        tids.append(tid)
    return tids


def reviewed(uid, tid, back, verdict, hour=10):
    store.add_review(uid, tid, "7", "abc1234", verdict, "read the diff", "", "crew-reviewer", [], {},
                     factory.finding_kind, wednesday(back, hour))


def accepted(uid, back):
    mid = store.create_milestone(uid, f"Step {back}")
    store.accept_milestone(uid, mid, "Ada", None, wednesday(back))


a = project("project-a", "Ada")
b = project("project-b", "Ada")
c = project("project-c", "Bo")
first, second, _ = merged(a, 1, 2, 3, 4)
merged(a, 20, 2)
third, _ = merged(b, 2, 26, None)
merged(b, 6, 1, 1, 1, 1)
reviewed(a, first, 1, "changes")
reviewed(a, first, 1, "pass", hour=11)
reviewed(a, second, 1, "pass")
reviewed(b, third, 2, "pass")
reviewed(b, store.create_ticket(b, "tares", "Not merged"), 30, "changes")
for uid, back in ((a, 1), (a, 5), (b, 5)):
    accepted(uid, back)
store.create_ticket(c, "tares", "Write the plan")

# ---- the Time section
now = datetime.now(timezone.utc).replace(microsecond=0)


def moved(uid, tid, stage, at):
    store.add_ticket_history(tid, uid, "stage", stage, None, None, None, at)


def ask(uid, text, at, tid=None, closed=None, to=None):
    """A blocking desk question of `uid` (None: of no project) asked at `at`, naming `tid`, and
    answered at `closed` when given. Returns its label."""
    named = [factory.label(store.get_ticket(uid, tid))] if tid else []
    d = store.add_desk_item(uid, text, "", [], "", "", "", named, True, "builder", [], at,
                            to_person_id=to)
    if closed:
        store.close_desk_item(d["id"], "answered", None, closed)
    return d["label"]


# stages between taken up and merged: start to merge, and so Delivered, stays as it was
end = wednesday(1)
moved(a, first, "review", end - timedelta(minutes=60))
for stage, minutes in (("review", 120), ("changes", 90), ("review", 60)):
    moved(a, second, stage, end - timedelta(minutes=minutes))
for stage, hours in (("blocked", 20), ("doing", 10)):
    moved(b, third, stage, wednesday(2) - timedelta(hours=hours))
ask(a, "Is the page right?", end - timedelta(minutes=45), first, closed=end - timedelta(minutes=15))
ask(b, "Which key signs it?", wednesday(2) - timedelta(hours=18), third,
    closed=wednesday(2) - timedelta(hours=13))

LONG = "build_artifacts_of_the_nightly_pipeline_for_the_whole_workspace_and_every_cache_it_left_behind"
ids["questions"] = {hours: ask(uid, text, now - timedelta(hours=hours), to=to) for hours, uid, text, to in (
    (200, None, "For Ada alone, of no project", ids["Ada"]),
    (100, None, "May the crew rotate the signing key?", "local"),
    (80, a, "Which database should the page read?", None),
    (55, b, "May the crew drop the old table?", None),
    (30, a, f"May the crew delete {LONG}?", None),
    (10, b, "Which colour marks a late ticket?", None),
    (5, a, "Should the list start closed?", None),
    (3, b, "Is one retry enough?", None))}
ids["tickets"] = {}
for hours, uid, stage, title in (
        (190, a, "doing", "Move the sync"), (150, b, "review", "Read the logs"),
        (120, a, "changes", f"Remove {LONG}"), (90, b, "blocked", "Rotate the key"),
        (50, a, "doing", "Show the totals"), (20, b, "doing", "Sort the list"),
        (5, a, "review", "Name the columns")):
    tid = store.create_ticket(uid, "tares", title)
    moved(uid, tid, stage, now - timedelta(hours=hours))
    store.update_ticket(tid, stage=stage)
    ids["tickets"][hours] = factory.label(store.get_ticket(uid, tid))
report.rebuild_all(store)
store.con.close()

ids["monday"] = monday.isoformat()
ids["old_monday"] = (monday - timedelta(weeks=20)).isoformat()
print(json.dumps(ids))
