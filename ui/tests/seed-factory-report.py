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
For the Review section (TR-1670): the three reviewed merged tickets have a pull request in
example-org/web and their reviews hold seven findings, one of them fixed. "Missing test" and "error
handling" are each on all three tickets, security on one. A rule is accepted for missing test; the
one for error handling is left open. In 8 weeks 2 of 3 tickets passed their first review, with 1.3
review rounds per ticket.
For the Cost section (TR-1671): in the last two weeks $23.00 was spent and one reply has no price,
$17.00 of it 1 week ago. Three merged tickets have a cost ($10.00, $7.00 and $3.00 beside the reply
with no price), the first two of them in the milestone accepted 1 week ago; $3.00 is tied to no
ticket, $2.25 of it of no project.
For the Merges section (TR-1672): project-a is handed over at L4-ship. Its two reviewed merged
tickets went through the merge queue 1 week ago (one at L3-review, approved by Ada, one at L4-ship),
project-b's reviewed one 2 weeks ago, merged outside Tares, and one more of project-b has a merged
queue row with no record, so it counts today. "merged" lists their labels, newest first.
Starts a daemon on the scratch data dir with TARES_EDITION=factory (so the cell settles to the
factory edition), stops it, then writes the people, projects, tickets, reviews and milestones
straight into the database, starts the daemon once more to read the ids of the rule offers, and
rolls everything up into the report's figures. Prints a JSON map of names to ids, with the Mondays
used, on the last line; "questions" and "tickets" in it map the age in hours of each stuck candidate
to its label (D7, T12).
usage: python seed-factory-report.py <data-dir> <port> <mcp-port> <ui-dist> <tares-bin>"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

home, port, mcp, dist, tares = sys.argv[1:6]
env = {**os.environ, "PYTHONPATH": os.getcwd(), "TARES_HOME": home, "TARES_PORT": port, "TARES_MCP_PORT": mcp,
       "TARES_UI_DIST": dist, "TARES_EDITION": "factory", "TARES_OTLP_GRPC_PORT": "off"}
for k in ("TARES_AUTH_TOKEN", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    env.pop(k, None)


def with_daemon(work=lambda: None):
    """Starts a daemon on the scratch data dir, runs `work` once it answers, and stops it."""
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
        return work()
    finally:
        daemon.terminate()
        daemon.wait()


with_daemon()

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
from session_helpers import seed_station, seed_usage  # noqa: E402
from tares import factory, merge_ledger, report  # noqa: E402
from tares.store import Store  # noqa: E402

store = Store(home)
today = datetime.now(timezone.utc).date()
monday = today - timedelta(days=today.weekday())
ids = {name: store.create_person(name, f"{name.lower()}@example.com")["id"] for name in ("Ada", "Bo")}
REPO = "example-org/web"


def project(name, lead):
    uid = "uc_report_" + name[-1]
    store.create_project(uid, name, goal="g", lead_person_id=ids[lead])
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


def reviewed(uid, tid, back, verdict, hour=10, findings=(), resolved=None):
    """A review round with new findings, each (severity, blocking, text), and what became of
    earlier ones."""
    new = [{"severity": severity, "file": None, "line": None, "blocking": blocking, "text": text}
           for severity, blocking, text in findings]
    store.add_review(uid, tid, "7", "abc1234", verdict, "read the diff", "", "crew-reviewer", new,
                     resolved or {}, factory.finding_kind, wednesday(back, hour))


def pull_request(uid, tid, number):
    store.update_ticket(tid, pr={"url": f"https://github.com/{REPO}/pull/{number}", "repo": REPO,
                                 "number": number, "head": "abc1234", "base": "main",
                                 "state": "merged", "checked_at": None})


def accepted(uid, back):
    mid = store.create_milestone(uid, f"Step {back}")
    store.accept_milestone(uid, mid, "Ada", None, wednesday(back))
    return mid


a = project("project-a", "Ada")
b = project("project-b", "Ada")
c = project("project-c", "Bo")
first, second, _ = merged(a, 1, 2, 3, 4)
merged(a, 20, 2)
third, _ = merged(b, 2, 26, None)
older = merged(b, 6, 1, 1, 1, 1)
for number, (uid, tid) in enumerate(((a, first), (a, second), (b, third)), start=1):
    pull_request(uid, tid, number)
reviewed(a, first, 1, "changes", findings=(("P2", True, "No tests here"),
                                           ("P3", False, "Uncaught exception in the page")))
reviewed(a, first, 1, "pass", hour=11, resolved={"F1": "fixed", "F2": "open"})
reviewed(a, second, 1, "pass", findings=(("P2", False, "No tests here"),
                                         ("P2", False, "Uncaught exception")))
reviewed(b, third, 2, "pass", findings=(("P2", False, "No tests here"),
                                        ("P3", False, "Uncaught exception"),
                                        ("P1", False, "A secret in the log")))
reviewed(b, store.create_ticket(b, "tares", "Not merged"), 30, "changes")
step, _, _ = (accepted(uid, back) for uid, back in ((a, 1), (a, 5), (b, 5)))
for tid in (first, second):
    store.update_ticket(tid, milestone=step)
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

# ---- the Cost section: model replies, and which builder held which ticket
ids["step"] = step
ids["costed"] = [factory.label(store.get_ticket(uid, tid)) for uid, tid in ((a, second), (a, first), (b, third))]
for station, role, parent, uid in (
        ("build-a", "builder", None, a), ("help-a", "helper", "build-a", a), ("build-b", "builder", None, b),
        ("crew-reviewer", "reviewer", None, None), ("crew-orchestrator", "orchestrator", None, None),
        ("crew-releaser", "releaser", None, None)):
    seed_station(store, station, station, role, parent=parent, project=uid)
for uid, tid, station, since in ((a, first, "build-a", wednesday(1, 7)), (a, second, "build-a", wednesday(1, 7)),
                                 (b, third, "build-b", wednesday(2) - timedelta(hours=30))):
    store.add_ticket_history(tid, uid, "holder", station, None, None, None, since)
# (session, weeks back, minutes before that Wednesday's noon, cost, project the reply is stamped with)
for n, (session, back, minutes, cost, uid) in enumerate((
        ("build-a", 1, 150, 9.0, None), ("build-a", 1, 105, 6.0, None), ("help-a", 1, 80, 2.0, None),
        ("build-b", 2, 300, 3.0, None), ("build-b", 2, 240, None, None), ("crew-reviewer", 2, 180, 1.5, None),
        ("crew-orchestrator", 2, 120, 0.5, None), ("crew-releaser", 2, 120, 0.25, None),
        ("a-person", 2, 60, 0.75, b))):
    # the reply with no price needs a model with none: a daemon that starts prices the others
    seed_usage(store, session, f"reply-{n}", replied_at=wednesday(back) - timedelta(minutes=minutes),
               project=uid, cost_usd=cost,
               model="claude-opus-5-5" if cost is not None else "unlisted-model")

# ---- the Review section: two offered rules, one of them accepted
for kind, text in (("missing test", "Add a test with the change."), ("error handling", "Handle the error.")):
    store.upsert_rule_proposal(REPO, kind, None, "repo", text, [], now)


# ---- the Merges section: three merges through the queue with a record, and one without
def queued(uid, tid, number, **record):
    """A merged queue row of pull request `number`, with a ledger record when `record` is given."""
    row = store.merge_queue_add(REPO, "main", number, "abc1234".ljust(40, "0"), tid, uid)
    store.merge_queue_update(row["id"], state="merged")
    if record:
        merge_ledger.save(store, row, reviewed_sha="abc1234".ljust(40, "0"),
                          merged_sha=f"fee000{number}".ljust(40, "0"), **record)


store.set_handover(a, [{"path": "/w/web", "name": "web"}], "L4-ship", None, now, person_id=ids["Ada"])
queued(a, first, 1, level="L3-review", ci_state="success", rounds=2, outside=False,
       approver_id=ids["Ada"], approved_at=wednesday(1, 11), merged_at=wednesday(1))
queued(a, second, 2, level="L4-ship", ci_state="none", rounds=1, outside=False,
       merged_at=wednesday(1, 13))
queued(b, third, 3, level="L3-review", ci_state="success", outside=True, merged_at=wednesday(2))
queued(b, older[0], 4)     # no record: it counts at the time the queue last wrote on it, now
ids["merged"] = [factory.label(store.get_ticket(uid, tid))
                 for uid, tid in ((b, older[0]), (a, second), (a, first), (b, third))]
store.close()


def open_offers():
    url = f"http://127.0.0.1:{port}/api/rule-proposals?state=open"
    with urllib.request.urlopen(url, timeout=10) as answer:
        return json.load(answer)["proposals"]


offers = with_daemon(open_offers)
store = Store(home)
offer = next(o for o in offers if o["repo"] == REPO and o["kind"] == "missing test")
store.decide_rule_proposal(offer["id"], "accepted", offer["text"], now)
report.rebuild_all(store)
store.close()

ids["monday"] = monday.isoformat()
ids["old_monday"] = (monday - timedelta(weeks=20)).isoformat()
print(json.dumps(ids))
