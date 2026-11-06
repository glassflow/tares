"""Seed a scratch factory Tares for factory-github.e2e.cjs: a project's GitHub page. Invented names only.
Writes straight to the cell's database, so no daemon runs here.
  project-g: two repos; pull requests open (CI green, CI failing), a draft, merged (with size and
      who merged), closed without merging, and one ticket with no pull request;
  project-n: tickets, none with a pull request.
Every pull request is marked read just now, so the page shows it as seeded.
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-github.py <data-dir>"""
import json, os, sys
from datetime import timedelta

sys.path.insert(0, os.getcwd())
from tares import factory as factory_mod  # noqa: E402
from tares.store import Store, now_utc  # noqa: E402

home = sys.argv[1]
store = Store(os.path.join(home, "tares.duckdb"))
ids = {}
API, WEB = "example-org/api", "example-org/web"


def ago(**kw):
    return (now_utc() - timedelta(**kw)).isoformat()


def project(name, goal):
    uid = f"uc_{name.replace('-', '')}"
    store.create_project(uid, "custom", name, {}, goal=goal)
    store.set_project_kind(uid, "software_factory")
    ids[name] = uid
    return uid


def ticket(uid, title, **fields):
    tid = store.create_ticket(uid, "tares", title)
    if fields.get("pr"):        # the crew's branch is named for the ticket
        fields["pr"]["branch"] = f"factory/{factory_mod.label(store.get_ticket(uid, tid))}"
        fields["pr"]["head"] = f"{fields['pr']['number']:04d}abcdef"
    if fields:
        store.update_ticket(tid, **fields)
    return tid


def pr(repo, n, title, state="open", **kw):
    base = {"url": f"https://github.com/{repo}/pull/{n}", "repo": repo, "number": n, "title": title,
            "head": f"{n:04d}abcdef", "branch": f"factory/T{n}", "base": "main", "state": state,
            "merged": False, "author": "tares-crew", "checked_at": now_utc().isoformat(), "error": None}
    return {**base, **kw}


g = project("project-g", "Let customers export their invoices as CSV.")
store.set_handover(g, [{"path": "/w/api", "name": "api"}, {"path": "/w/web", "name": "web"}], None, None, now_utc())
ticket(g, "Add the export endpoint", stage="merged", status="done",
       pr=pr(API, 41, "Add GET /invoices/export", state="closed", merged=True, merged_at=ago(days=2),
             merged_by="kim", created_at=ago(days=3), closed_at=ago(days=2), ci="success",
             commits=4, changed_files=7, additions=312, deletions=18))
ticket(g, "Stream large exports", stage="merged", status="done",
       pr=pr(API, 44, "Stream exports over 10k rows", state="closed", merged=True, merged_at=ago(hours=5),
             merged_by="alex", created_at=ago(days=1), closed_at=ago(hours=5), ci="success",
             commits=2, changed_files=3, additions=96, deletions=41))
t3 = ticket(g, "Limit exports per minute", stage="review", status="in_progress",
       pr=pr(API, 47, "Rate limit the export endpoint", created_at=ago(hours=3), ci="success",
             commits=1, changed_files=2, additions=58, deletions=4))
t4 = ticket(g, "Quote fields with commas", stage="changes", status="in_progress",
       pr=pr(API, 48, "Quote CSV fields that hold commas", created_at=ago(hours=1), ci="failure",
             commits=3, changed_files=2, additions=40, deletions=12))
ticket(g, "Try a background job for exports", status="canceled",
       pr=pr(API, 43, "Export invoices in a background job", state="closed", created_at=ago(days=2),
             closed_at=ago(days=1), commits=5, changed_files=9, additions=420, deletions=30))
ticket(g, "Export button on the invoices page", stage="merged", status="done",
       pr=pr(WEB, 112, "Add an Export CSV button", state="closed", merged=True, merged_at=ago(days=1),
             merged_by="kim", created_at=ago(days=2), closed_at=ago(days=1), ci="success",
             commits=3, changed_files=4, additions=128, deletions=9))
ticket(g, "Show export progress", stage="doing", status="in_progress",
       pr=pr(WEB, 115, "Show a progress bar while exporting", created_at=ago(minutes=40), draft=True,
             ci="pending", commits=1, changed_files=2, additions=33, deletions=2))
ticket(g, "Write the export docs")

store.add_review(g, t3, None, "0047abcdef", "pass", "ran the tests", None, "crew-reviewer", [], {},
                 factory_mod.finding_kind, now_utc())
store.add_review(g, t4, None, "0048abcdef", "changes", "ran the tests", None, "crew-reviewer",
                 [{"severity": "major", "file": "api/export.py", "line": 40, "blocking": True, "text": "A field with a quote breaks the row"}], {}, factory_mod.finding_kind, now_utc())

n = project("project-n", "Tidy the settings page.")
ticket(n, "Group the settings")

print(json.dumps(ids))
