"""Seed a scratch factory Tares for factory-ticket.e2e.cjs (TR-760): the ticket page as a cell with some
history shows it. Invented names only. Writes straight to the cell's database, so no daemon runs here.
  project-y (building): six tickets and a milestone;
    T1 merged: one passing review, a pull request that merged, short working notes, blocks T2;
    T2 changes asked: four review rounds (a finding fixed in a later round, one still open), twelve
       checks (one failing), three calls and a decision, fourteen lines of history, about a hundred
       lines of working notes, a builder whose session opens in a terminal, depends on T1, blocks T3;
    T3 blocked: a review that blocked, held by the lead (no terminal), depends on T2;
    T4 to do: nothing yet (every section empty);
    T5 in Linear: in progress, a link out, no pull request;
    T6 a long title and a long unbroken word, for narrow screens;
  project-w (not a factory project): its tickets answer 404;
  project-z (a factory project still at its setup, a draft): its tickets answer 404 too.
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-ticket.py <data-dir>"""
import json, os, sys
from datetime import timedelta

sys.path.insert(0, os.getcwd())
from tares import factory as factory_mod  # noqa: E402
from tares.store import Store, now_utc  # noqa: E402

home = sys.argv[1]
store = Store(os.path.join(home, "tares.duckdb"))
ids = {}
REPO = "example-org/docs-site"
BUILDER, LEAD, REVIEWER = "project-y-build-m1", "crew-orchestrator", "crew-reviewer"


def ago(**kw):
    return now_utc() - timedelta(**kw)


def project(name, factory=True):
    uid = f"uc_{name.replace('-', '')}"
    store.create_project(uid, "custom", name, {}, goal="Publish 1,000 answers to common questions on the docs site.")
    if factory:
        store.set_project_kind(uid, "software_factory")
    ids[name] = uid
    return uid


def ticket(uid, title, **fields):
    owner = fields.pop("owner", "tares")
    ms, deps = fields.pop("milestone", None), fields.pop("depends_on", None)
    tid = store.create_ticket(uid, owner, title, milestone=ms, depends_on=deps)
    if fields:
        store.update_ticket(tid, **fields)
    return tid


def pr(n, **kw):
    return {"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO, "number": n, "head": "aaaaaaa1111", **kw}


y = project("project-y")
store.set_handover(y, [{"path": "/w/project-y", "name": REPO}], None, None, now_utc())
store.set_session_station("sess-y1", BUILDER, "builder", LEAD, y, now_utc())
store.set_session_station("sess-lead", LEAD, "orchestrator", "", None, now_utc())
store.set_session_station("sess-rev", REVIEWER, "reviewer", LEAD, None, now_utc())
m1 = store.create_milestone(y, "M1: The first articles", "Ten articles are live.", [])

short_notes = "## Progress\n\n- The header is built.\n- It shows on every page.\n"
doc1 = store.create_doc(y, "working", "Working notes: T1", short_notes, by="Alex")
long_notes = ["# Goal", "", "Add the footer to every page, with the last link kept.", "", "## Progress", ""]
long_notes += [f"- Step {n}: moved one link into the footer and checked the page." for n in range(1, 91)]
long_notes += ["", "## A table", "", "| Page | Footer |", "| --- | --- |", "| Home | yes |", "| Search | yes |", "",
               "## How to try it", "", "```", "npm test -- footer", "```", "",
               "See [the style guide](https://example.com/style) and ![a picture](https://example.com/p.png).",
               "", "The last line."]
doc2 = store.create_doc(y, "working", "Working notes: T2", "\n".join(long_notes), by="Alex")

t1 = ticket(y, "Add the header", milestone=m1, stage="merged", status="done", holder=BUILDER,
            pr=pr(11, state="closed", merged=True), working_doc=doc1)
t2 = ticket(y, "Add the footer", milestone=m1, stage="changes", status="in_progress", holder=BUILDER,
            pr=pr(12, state="open", ci="failure", head="ccccccc3333"), depends_on=[t1], working_doc=doc2)
t3 = ticket(y, "Add the sitemap", milestone=m1, stage="blocked", status="in_progress", holder=LEAD, depends_on=[t2])
t4 = ticket(y, "Tidy the readme")
t5 = ticket(y, "Add the search box", owner="linear", milestone=m1, stage="doing", status="in_progress")
store.update_ticket(t5, identifier="PY-5", url="https://linear.example.com/project-y/PY-5")
t6 = ticket(y, "Make the very long title of this ticket wrap on a narrow screen without scrolling sideways, "
               "and keep going until it is much longer than a row", stage="doing", status="in_progress")

# T1: one passing review, two checks, a few lines of history
store.add_review(y, t1, None, "aaaaaaa1111", "pass", "ran the tests, opened the page", None, REVIEWER, [], {},
                 factory_mod.finding_kind, ago(days=5))
store.add_check(y, t1, "npm test", "8 passed, 0 failed", "aaaaaaa1111", "", BUILDER, "sess-y1", ago(days=6))
store.add_check(y, t1, "npm run build", "ok", "aaaaaaa1111", "", BUILDER, "sess-y1", ago(days=6))
store.add_ticket_history(t1, y, "stage", "doing", None, BUILDER, "builder", ago(days=7))
store.add_ticket_history(t1, y, "stage", "review", None, BUILDER, "builder", ago(days=6))
store.add_ticket_history(t1, y, "stage", "merged", None, "github", "github", ago(days=5))

# T2: four rounds, a finding fixed in round 3, one open
store.add_review(y, t2, None, "aaaaaaa1111", "changes", "ran the tests", "did not open the build in a browser", REVIEWER,
                 [{"severity": "high", "file": "src/footer.ts", "line": 12, "blocking": True, "text": "The footer drops the last link"},
                  {"severity": "low", "file": "src/footer.css", "line": 0, "blocking": False, "text": "A colour could be a token"}],
                 {}, factory_mod.finding_kind, ago(days=4))
store.add_review(y, t2, None, "bbbbbbb2222", "changes", "ran the tests again", None, REVIEWER,
                 [{"severity": "high", "file": "src/footer.ts", "line": 40, "blocking": True, "text": "The link has no label"}],
                 {"F1": "open"}, factory_mod.finding_kind, ago(days=3))
store.add_review(y, t2, None, "ccccccc3333", "changes", "ran the tests a third time", None, REVIEWER, [],
                 {"F1": "fixed", "F3": "open"}, factory_mod.finding_kind, ago(days=2))
store.add_review(y, t2, None, "ccccccc3333", "changes", "ran the tests a fourth time", None, REVIEWER, [],
                 {"F3": "open"}, factory_mod.finding_kind, ago(days=1))
for n in range(12):
    result = "2 failed, 10 passed" if n == 3 else f"{n + 5} passed, 0 failed"
    store.add_check(y, t2, f"npm test -- footer-{n}", result, "ccccccc3333", "", BUILDER, "sess-y1", ago(hours=30 - n))
for n, (field, value, reason, who, role) in enumerate(
        [("stage", "doing", None, BUILDER, "builder"), ("stage", "review", None, BUILDER, "builder"),
         ("stage", "changes", "asked for changes", REVIEWER, "reviewer"), ("holder", BUILDER, None, LEAD, "orchestrator"),
         ("stage", "review", "new commits pushed after the verdict", "github", "github"),
         ("stage", "changes", "asked for changes", REVIEWER, "reviewer"), ("title", "Add the footer", None, LEAD, "orchestrator")]
        + [("stage", "doing" if n % 2 else "review", None, BUILDER, "builder") for n in range(7)]):
    store.add_ticket_history(t2, y, field, value, reason, who, role, ago(days=9, hours=-n * 3))
a1 = store.add_assumption(y, "Which date format?", "ISO dates", "It sorts as text.", "", [t2], BUILDER, "builder", ago(days=2))
a2 = store.add_assumption(y, "Which logger?", "The standard one", "No new package.", "", [t2], BUILDER, "builder", ago(days=1))
store.decide_assumption(a2["id"], "kept", None, None, now_utc())
a3 = store.add_assumption(y, "Which font?", "The system one", "It is fast.", "", [t2], BUILDER, "builder", ago(days=1))
store.decide_assumption(a3["id"], "overturned", "Use the brand font", None, now_utc())
store.add_decision(y, "", "Tabs or spaces?", "Tabs", "tabs", False, "project", False, None, False, ["T2"], ago(days=3))

# T3: a review that blocked
store.add_review(y, t3, None, "aaaaaaa1111", "block", "read the diff", None, REVIEWER, [], {}, factory_mod.finding_kind, ago(days=1))

# T6: a long unbroken word in the notes for narrow screens
doc6 = store.create_doc(y, "working", "Working notes: T6", "x" * 220 + "\n", by="Alex")
store.update_ticket(t6, working_doc=doc6)
store.add_check(y, t6, "python " + "a" * 150 + ".py", "b" * 180, "aaaaaaa1111", "", BUILDER, "sess-y1", ago(hours=1))

# not a factory project
pw = project("project-w", factory=False)
ticket(pw, "Not a factory ticket")

# a factory project that is still a draft
pz = project("project-z")
store.update_project(pz, status="draft")
ticket(pz, "A ticket in a draft")
print(json.dumps(ids))
