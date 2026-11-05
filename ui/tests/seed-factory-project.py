"""Seed a scratch factory Tares for factory-project.e2e.cjs (TR-734): the project page as a cell with some
history shows it. Invented names only. Writes straight to the cell's database, so no daemon runs here.
  project-y (building, waiting on you): seven tickets moving, two merged, a milestone to check, a pull
      request to review, a question and a permission request on the desk, two open calls, decisions, docs
      and two builders (so two commands to open their sessions);
  project-z (ready to close): every ticket merged, no builder, one decision;
  project-x (not started): a starting prompt and no tickets;
  project-p (building, no milestones): two tickets, one moving;
  project-d (done): finished, every ticket merged;
  project-w (not a factory project): its page answers 404.
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-project.py <data-dir>"""
import json, os, sys
from datetime import timedelta

sys.path.insert(0, os.getcwd())
from tares import factory as factory_mod  # noqa: E402
from tares.store import Store, now_utc  # noqa: E402

home = sys.argv[1]
store = Store(os.path.join(home, "tares.duckdb"))
ids = {}
REPO = "example-org/docs-site"


def ago(**kw):
    return now_utc() - timedelta(**kw)


def project(name, goal, factory=True):
    uid = f"uc_{name.replace('-', '')}"
    store.create_project(uid, "custom", name, {}, goal=goal)
    if factory:
        store.set_project_kind(uid, "software_factory")
    ids[name] = uid
    return uid


def ticket(uid, title, **fields):
    ms, deps = fields.pop("milestone", None), fields.pop("depends_on", None)
    tid = store.create_ticket(uid, "tares", title, milestone=ms, depends_on=deps)
    if fields:
        store.update_ticket(tid, **fields)
    return tid


def hand(uid, name):
    store.set_handover(uid, [{"path": f"/w/{name}", "name": REPO}], None, None, now_utc())


def pr(n, **kw):
    return {"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO, "number": n, "head": "aaaaaaa1111", **kw}


# project-y: the rich one
y = project("project-y", "Publish 1,000 answers to common questions on the docs site.")
hand(y, "project-y")
m1 = store.create_milestone(y, "M1: The first articles", "Ten articles are live.",
                            [{"check": "Open the docs home page", "expect": "Ten articles are listed"},
                             {"check": "Search for a title", "expect": "The article is the first result"}])
m2 = store.create_milestone(y, "M2: The search page", "Search finds every article.", [])
m3 = store.create_milestone(y, "M3: The count", "The home page shows how many answers exist.", [])
for n in (1, 2):
    ticket(y, f"Write article {n}", milestone=m1, stage="merged", status="done", pr=pr(n, state="closed", merged=True))
t_rev1 = ticket(y, "Write article 3", milestone=m1, stage="review", status="in_progress", holder="project-y-build-m1",
                pr=pr(13, state="open", ci="success"))
ticket(y, "Write article 4", milestone=m2, stage="review", status="in_progress", holder="project-y-build-m1",
       pr=pr(14, state="open", ci="pending"))
ticket(y, "Add the search box", milestone=m2, stage="changes", status="in_progress", holder="project-y-build-m2",
       pr=pr(15, state="open", ci="failure"))
for n in (5, 6, 7):
    ticket(y, f"Add result page {n}", milestone=m2, stage="doing", status="in_progress", holder="project-y-build-m2")
ticket(y, "Wait for the logo", milestone=m3, stage="blocked", status="in_progress", holder="project-y-build-m2",
       stage_reason="waiting for a logo")
t_todo = ticket(y, "Show the answer count on the home page", milestone=m3)
ticket(y, "Add the count to the footer", milestone=m3, depends_on=[t_todo])
store.add_review(y, t_rev1, None, "aaaaaaa1111", "pass", "ran the tests", None, "crew-reviewer", [], {},
                 factory_mod.finding_kind, now_utc())
for sid, station in (("sess-y1", "project-y-build-m1"), ("sess-y2", "project-y-build-m2")):
    store.set_session_station(sid, station, "builder", "crew-orchestrator", y, now_utc())
for kind, title in (("start", "Start"), ("spec", "Spec"), ("plan", "Plan"), ("agents", "Rules"), ("memory", "Memory")):
    store.create_doc(y, kind, title, f"# {title}\n\nText.", by="Alex")
store.add_assumption(y, "Which date format?", "ISO dates", "It sorts as text.", "", [t_rev1], "project-y-build-m1",
                     "builder", ago(days=2))
store.add_assumption(y, "Which title length?", "Sixty characters", "It fits a result row.", "", [], "project-y-build-m2",
                     "builder", ago(days=1))
for q, w, when in (("Use tabs or spaces?", "Tabs", ago(days=3)), ("Which licence?", "MIT", ago(days=2)),
                   ("Which host?", "The docs site", ago(days=1)), ("Which theme?", "The light one", ago(hours=2))):
    store.add_decision(y, "", q, w, w, False, "project", False, None, False, [], when)
store.add_desk_item(y, "Which gateway should the crew use?", "", [], "The first", "It is the cheaper one.", "", [],
                    False, "crew-orchestrator", [], ago(hours=3), "crew-orchestrator", None)
store.add_desk_item(y, "Permission", "", [], "", "", "", [], False, "project-y-build-m1", [], ago(hours=1),
                    "project-y-build-m1", "gh pr merge 13 --squash")

# project-z: every ticket merged
z = project("project-z", "Prepare the workshop for the whole team.")
hand(z, "project-z")
mz = store.create_milestone(z, "M1: The slides", "The slides are ready.", [])
for n in (1, 2, 3):
    ticket(z, f"Write part {n}", milestone=mz, stage="merged", status="done")
store.create_doc(z, "start", "Start", "Build it.")
store.add_decision(z, "", "Which room?", "The big one", "big", False, "project", False, None, False, [], ago(days=1))

# project-k: M1 merged, the crew passed its check, one check is for the person; M2 still open
k = project("project-k", "Show who has each pull request.")
hand(k, "project-k")
mk1 = store.create_milestone(k, "M1: Three lists", "Reviews shows three lists.", [
    {"check": "pytest tests/test_reviews.py", "expect": "0 failed"},
    {"check": "Open Reviews", "expect": "Three lists, each row says who has it", "for": "person"},
    {"check": "Open Home", "expect": "The same pull requests wait for you", "for": "person"}])
ticket(k, "Split the review list", milestone=mk1, stage="merged", status="done")
store.record_milestone_check(k, mk1, 0, "pass", "12 passed", "crew", "crew-orchestrator", None, now_utc())
mk2 = store.create_milestone(k, "M2: Later", "Later.", [])
ticket(k, "Later work", milestone=mk2)

# project-x: a starting prompt only
x = project("project-x", "Let customers connect their own tools, with a setup page for each.")
store.create_doc(x, "start", "Start", "Build it.")

# project-p: no milestones
p = project("project-p", "Tidy the styles across the console.")
hand(p, "project-p")
ticket(p, "Move the colours to tokens", stage="doing", status="in_progress", holder="project-p-build-m1")
ticket(p, "Remove the old classes")

# project-d: finished
d = project("project-d", "Move the blog to the new site.")
hand(d, "project-d")
for n in (1, 2):
    ticket(d, f"Move post {n}", stage="merged", status="done")
store.finish_handover(d, "done", None, "Alex", now_utc())
store.update_project(d, status="done")

# not a factory project
project("project-w", "Not a factory project", factory=False)
print(json.dumps(ids))
