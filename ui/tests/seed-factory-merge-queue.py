"""Seed a scratch factory Tares for factory-merge-queue.e2e.cjs: a project's Merge queue page. Invented names only.
Writes straight to the cell's database, so no daemon runs here. The caller is the cell's own person (`local`).
  p-wait   a pull request waiting for CI, one next, one queued behind it, with a merged list
  p-ask    L3-review: one waiting for your yes, one for someone else, one whose commit moved, one answered yes, one answered no
  p-block  blocked by repo rules, blocked with no GitHub connection (a second repo), sent back twice and five times
  p-wd     L3-review: a question for you that was withdrawn (the person approves it through the approve route)
  p-act    L3-review: three questions for you, to answer yes, answer no and fail (p-act2 the same, for the other width)
  p-many   eight queued pull requests (the page shows five)
  p-cap    six queued and, last in line, one blocked: the page shows the blocked one and four of the queued
  p-empty  nothing in the queue
  p-other  a project that is not a software factory project
Prints a JSON map of names to ids on the last line.
usage: python seed-factory-merge-queue.py <data-dir>"""
import json, os, sys
from datetime import timedelta

sys.path.insert(0, os.getcwd())
from tares import factory as factory_mod  # noqa: E402
from tares import merge_queue  # noqa: E402
from sqlalchemy import update  # noqa: E402
from tares.session_store.tables import merge_queue as merge_queue_table  # noqa: E402
from tares.store import Store, now_utc  # noqa: E402

home = sys.argv[1]
store = Store(home)
ids = {}
API, WEB = "example-org/api", "example-org/web"
lee = store.create_person("Lee Okafor", email="lee@example.com")
dana = store.create_person("Dana Whitfield", email="dana@example.com", github="dana-w")
n = {"pos": 0}


def ago(**kw):
    return now_utc() - timedelta(**kw)


def project(name, level="L4-ship", kind="software_factory"):
    uid = f"uc_{name.replace('-', '')}"
    store.create_project(uid, "custom", name, {}, goal="A project for the merge queue page.")
    store.set_project_kind(uid, kind)
    store.set_handover(uid, [{"path": "/w/a", "name": name}], level, None, now_utc(), person_id=lee["id"])
    ids[name] = uid
    return uid


def pull(uid, repo, num, ident, title, branch, state="queued", reason=None, head=None, ci="success",
         checks=None, builder=None, depends=None, reviewed=True):
    n["pos"] += 1
    head = head or f"{num:07x}".ljust(40, "0")
    tid = store.create_ticket(uid, "tares", title, identifier=ident, repo=repo, position=float(n["pos"]),
                              assignee_person_id=builder, depends_on=depends)
    store.update_ticket(tid, stage="review", pr={
        "url": f"https://github.com/{repo}/pull/{num}", "number": num, "repo": repo, "head": head,
        "base": "main", "branch": branch, "state": "open", "title": title, "ci": ci, "ci_checks": checks or []})
    if reviewed:
        store.add_review(uid, tid, f"https://github.com/{repo}/pull/{num}", head, "pass", "Ran the tests", "",
                         "crew-reviewer", [], {}, factory_mod.finding_kind, now_utc())
    return tid, store.merge_queue_add(repo, "main", num, head, tid, uid, state=state, reason=reason)


def question(uid, tid, row, to, mins):
    d = store.add_desk_item(uid, "Merge it?", "", ["Yes, merge", "No, hold"], "", "", "", [tid], True,
                            "merge-queue", [], ago(minutes=mins), to_person_id=to)
    store.merge_queue_update(row["id"], desk_item=d["id"])
    return d


def answer(uid, d, tid, words, choice, mins):
    store.add_decision(uid, d["id"], d["question"], words, choice, False, "project", False, None, False, [tid],
                       ago(minutes=mins), via="console", person_id=lee["id"])
    store.close_desk_item(d["id"], "answered", None, now_utc())


def back(row, **kw):
    with store.sessions_db.begin() as c:
        c.execute(update(merge_queue_table).where(merge_queue_table.c.id == row["id"]).values(updated_at=ago(**kw)))


# ── p-wait ──
u = project("p-wait")
t1, r1 = pull(u, API, 217, "ORD-41", "Round prices to the order currency", "ord-41-price-rounding",
              state="waiting_ci", ci="pending")
back(r1, minutes=3)
pull(u, API, 219, "ORD-43", "Add currency to the order export", "ord-43-export-currency", builder=dana["id"])
pull(u, API, 221, "ORD-44", "Add a column to the export", "ord-44-column", builder=lee["id"])
pull(u, API, 222, "ORD-45", "Make the order export keep the currency of every line when a customer pays in two currencies and the shop rounds each line on its own", "ord-45-" + "a-very-long-branch-name-" * 4 + "end", builder=lee["id"])
t, r = pull(u, API, 214, "ORD-38", "Fix tax total on refunds", "ord-38-tax", state="merged")
store.add_ticket_history(t, u, "stage", "merged", None, "merge-queue", None, now_utc())
back(r, minutes=9)

# ── p-ask ──
u = project("p-ask", level="L3-review")
t, r = pull(u, API, 301, "ASK-1", "Backport price rounding to 2.4", "ask-1-backport", state="waiting_approval", builder=lee["id"])
question(u, t, r, "local", 14)
t, r = pull(u, API, 302, "ASK-2", "Backport tax rounding to 2.4", "ask-2-backport-tax", state="waiting_approval", builder=dana["id"])
question(u, t, r, dana["id"], 2)
t, r = pull(u, API, 303, "ASK-3", "Backport the export column", "ask-3-export", state="waiting_approval")
question(u, t, r, "local", 30)
store.update_ticket(t, pr={"url": f"https://github.com/{API}/pull/303", "number": 303, "repo": API,
                           "head": "9" * 40, "base": "main", "branch": "ask-3-export", "state": "open",
                           "title": "Backport the export column", "ci": "success", "ci_checks": []})
t, r = pull(u, API, 304, "ASK-4", "Backport the refund fix", "ask-4-refund", state="waiting_approval", builder=lee["id"])
d = question(u, t, r, "local", 50)
answer(u, d, t, "A", "Yes, merge", 5)
t, r = pull(u, API, 305, "ASK-5", "Backport the coupon cap", "ask-5-coupon", state="blocked",
            reason="The person said: No, hold")
d = question(u, t, r, "local", 60)
answer(u, d, t, "B", "No, hold", 8)

# ── p-block ──
u = project("p-block")
pull(u, API, 311, "BLK-1", "Use one rounding rule everywhere", "blk-1-one-rule", state="blocked",
     reason="GitHub will not merge it: the base branch requires two approving reviews.")
pull(u, WEB, 88, "BLK-2", "Show the shipping estimate", "blk-2-shipping", state="blocked",
     reason=merge_queue.no_write_reason(WEB))
t, r = pull(u, API, 312, "BLK-3", "Move the price rules into one file", "blk-3-price-rules", state="sent_back",
            reason="conflicts with PR 218 in app/pricing.py")
for _ in range(2):
    store.add_ticket_history(t, u, "send_back", "sent back", "a conflict", "merge-queue", None, now_utc())
t, r = pull(u, API, 313, "BLK-4", "Split the pricing module", "blk-4-split", state="sent_back",
            reason="the checks failed twice on main")
for _ in range(5):
    store.add_ticket_history(t, u, "send_back", "sent back", "the checks", "merge-queue", None, now_utc())

# ── p-wd, p-wd2 ──  (the second is the one a test clicks, so the first stays as it was seeded)
for k, name in enumerate(("p-wd", "p-wd2")):
    u = project(name, level="L3-review")
    t, r = pull(u, API, 341 + k, "WDR-1", "Backport the invoice footer", "wdr-1-footer", state="blocked",
                reason="The question was withdrawn", builder=lee["id"])
    d = question(u, t, r, "local", 20)
    store.close_desk_item(d["id"], "withdrawn", None, now_utc())

# ── p-act, p-act2 ──  (one per screen width: an answer changes the cell, so each width answers its own)
for k, name in enumerate(("p-act", "p-act2")):
    u = project(name, level="L3-review")
    for i, what in enumerate(("yes", "no", "fail")):
        t, r = pull(u, API, 321 + 10 * k + i, f"ACT-{i + 1}", f"Answer {what} for this pull request", f"act-{i + 1}-{what}",
                    state="waiting_approval", builder=lee["id"])
        question(u, t, r, "local", 10 + i)

# ── p-many ──
u = project("p-many")
for i in range(8):
    pull(u, API, 371 + i, f"MNY-{i + 1}", f"Queued pull request number {i + 1}", f"mny-{i + 1}-queued")

# ── p-cap ──
u = project("p-cap")
for i in range(6):
    pull(u, API, 351 + i, f"CAP-{i + 1}", f"Queued pull request number {i + 1}", f"cap-{i + 1}-queued")
pull(u, API, 360, "CAP-7", "Last in line but blocked by the repo rules", "cap-7-blocked", state="blocked",
     reason="GitHub will not merge it: the base branch requires two approving reviews.")

project("p-empty")
project("p-other", kind="custom")
print(json.dumps(ids))
