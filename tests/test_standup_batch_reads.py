"""The crew's standup reads all its projects together (TR-1633) and answers what it answered when it
read them one at a time.

The batch build is held against the same report built from one project's reads at a time
(list_tickets, list_milestones, list_desk, get_project, get_handover), for a cell of one person and
a cell of several: a project with work, one finished in the range, one with no tickets, no
milestones and no desk items, tickets and desk items with fields left empty, a hidden project and a
cell with no projects at all. Tickets, milestones and desk items that tie on every column they are
ordered by come out in one order from both reads. The batch build calls none of the one-project
reads, and reads nothing of a hidden project's tickets.

Run: .venv/bin/python tests/test_standup_batch_reads.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tares import factory_owners, standup
from tares.factory_api import project_progress
from tares.session_store.tables import milestones as milestones_table
from tares.session_store.tables import tickets as tickets_table
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
SINCE, UNTIL = NOW - timedelta(days=1), NOW


def ago(**kw):
    return NOW - timedelta(**kw)


def new_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(prefix="tares-standup-batch-"), "s.duckdb"))


def batch(store, hide=()):
    return standup.facts(store, SINCE, UNTIL,
                         lambda uid, tickets, ms: project_progress(store, uid, tickets, ms), hide=hide)


def one_by_one(store, hide=()):
    """The same report from one project's reads at a time."""
    names = {p["id"]: p["name"] for p in store.list_projects()}
    history: dict = {}
    for c in store.history_between(SINCE, UNTIL):
        history.setdefault(c["project"], []).append(c)
    uids = [(u, None) for u in store.handed_over_projects(finished=False, taken=False)]
    for u in store.handed_over_projects(finished=True):
        at = store.get_handover(u)["finished"]["at"]
        if SINCE <= at < UNTIL:
            uids.append((u, at))
    out = [standup.project_facts(factory_owners.Owners(store), store.get_project(u),
                                 store.list_tickets(u), store.list_desk("open", u),
                                 history.get(u, []), SINCE, UNTIL, project_progress(store, u), at)
           for u, at in uids if u in names and u not in hide]
    out.sort(key=lambda p: (not p["moved"], -len(p["done"]), p["name"].lower()))
    return {"team": not store.only_local_person(), "since": SINCE, "until": UNTIL,
            "projects": [p for p in out if p["moved"]],
            "quiet": [{"id": p["id"], "name": p["name"], "progress": p["progress"],
                       "blocked": len(p["blocked"]), "needs_you": len(p["needs_you"])}
                      for p in out if not p["moved"]]}


class Spy:
    """The store's reads the batch build makes, with their arguments."""

    NAMES = ("list_tickets", "list_milestones", "get_handover", "get_project", "list_desk",
             "tickets_by_project", "milestones_by_project")

    def __init__(self, store):
        self.store, self.calls = store, {n: [] for n in self.NAMES}

    def __enter__(self):
        for n in self.NAMES:
            def wrapped(*a, _n=n, _real=getattr(self.store, n), **kw):
                self.calls[_n].append(a)
                return _real(*a, **kw)
            setattr(self.store, n, wrapped)
        return self

    def __exit__(self, *exc):
        for n in self.NAMES:
            delattr(self.store, n)


def project(store, uid, name, *, handed=True, finished=None):
    store.create_project(uid, "custom", name, {}, goal=f"Goal of {name}")
    store.set_project_kind(uid, "software_factory")
    if handed:
        store.set_handover(uid, [{"path": f"/tmp/{uid}", "name": f"example-org/{uid}"}], None, None,
                           ago(days=5))
    if finished is not None:
        store.finish_handover(uid, "done", None, "crew", finished)


def ticket(store, uid, title, stage=None, milestone=None, moved=True, **fields):
    tid = store.create_ticket(uid, "tares", title, milestone=milestone)
    if stage:
        store.update_ticket(tid, stage=stage, **fields)
        if moved:
            store.add_ticket_history(tid, uid, "stage", stage, None, "crew", "builder", ago(hours=3))
    return tid


def ask(store, uid, question, *, blocking=False, at=None, **kw):
    return store.add_desk_item(uid, question, "", [], "", "", "", [], blocking, "", [],
                               at or ago(hours=2), **kw)


def tied(store, uid):
    """Three tickets, milestones and desk items of `uid` that tie on every column they are ordered
    by but the last: written so the order they were written in is not the order they must come out."""
    when = ago(days=2)
    for mid, name in (("ms_tie_c", "M c"), ("ms_tie_a", "M a"), ("ms_tie_b", "M b")):
        with store.sessions_db.begin() as conn:
            conn.execute(milestones_table.insert().values(
                id=mid, project=uid, owner="tares", external_id=None, name=name, goal="",
                checks=[], position=1, created_at=when, updated_at=when))
    for number, mid in ((3, "ms_tie_c"), (1, "ms_tie_a"), (2, "ms_tie_b")):
        tid = ticket(store, uid, f"Ticket number {number}", "review", milestone=mid)
        with store.sessions_db.begin() as conn:
            conn.execute(tickets_table.update().where(tickets_table.c.id == tid)
                         .values(number=number, position=1, created_at=when))
    base = 900
    for offset in (3, 1, 2):
        d = ask(store, uid, f"Question {offset}", at=when)
        store.sc.execute("UPDATE desk_items SET number = ? WHERE id = ?", [base + offset, d["id"]])


def seed(store):
    project(store, "uc_busy", "Busy")
    m1 = store.create_milestone("uc_busy", "M1", "First", [])
    store.create_milestone("uc_busy", "M2", "Nothing in it", [])
    ticket(store, "uc_busy", "Merged one", "merged", milestone=m1, status="done")
    ticket(store, "uc_busy", "Being built", "doing", milestone=m1, holder="busy-build")
    ticket(store, "uc_busy", "Started earlier", "doing", moved=False)
    ticket(store, "uc_busy", "Blocked with no reason", "blocked", milestone=m1)
    ticket(store, "uc_busy", "Changes asked", "changes",
           pr={"repo": "example-org/uc_busy", "number": 4})
    store.create_ticket("uc_busy", "tares", "Nothing set")     # no stage, holder, pr, url or milestone
    ask(store, "uc_busy", "A later blocking one", blocking=True, at=ago(hours=1))
    ask(store, "uc_busy", "An older one", at=ago(days=3))
    ask(store, "uc_busy", "May I run it", command="make release", station="crew-releaser")

    project(store, "uc_tied", "Tied")
    tied(store, "uc_tied")
    project(store, "uc_empty", "Empty")                        # no tickets, milestones or desk items
    project(store, "uc_done", "Done today", finished=ago(hours=4))
    ticket(store, "uc_done", "The last one", "shipped")
    project(store, "uc_olddone", "Done last week", finished=ago(days=7))
    ticket(store, "uc_olddone", "Long ago", "shipped", moved=False)
    project(store, "uc_mine", "Not handed over", handed=False)
    ticket(store, "uc_mine", "In a person's session", "doing")
    ask(store, "uc_mine", "Not in any standup")
    project(store, "uc_secret", "Secret")
    ticket(store, "uc_secret", "Quiet work", "review")
    ask(store, "uc_secret", "A secret question")
    ask(store, None, "A question about no project")


def by_id(report, uid):
    return next(p for p in report["projects"] if p["id"] == uid)


def same(label, store, hide=()):
    got, want = batch(store, hide), one_by_one(store, hide)
    ck(label, got == want, f"\n    batch: {got}\n    one by one: {want}")
    return got


def main():
    print("== no projects ==")
    store = new_store()
    with Spy(store) as spy:
        report = batch(store)
    ck("no projects: nothing moved and nothing quiet",
       report == {"team": False, "since": SINCE, "until": UNTIL, "projects": [], "quiet": []}, report)
    ck("no projects: the desk is not read", spy.calls["list_desk"] == [], spy.calls["list_desk"])
    same("no projects: the same as one at a time", store)

    print("== a cell of one person ==")
    seed(store)
    report = same("the same as one at a time", store)
    ck("the projects in the report",
       {p["id"] for p in report["projects"]} == {"uc_busy", "uc_tied", "uc_done", "uc_secret"}
       and [q["id"] for q in report["quiet"]] == ["uc_empty"], report)
    empty = report["quiet"][0] if report["quiet"] else {}
    ck("the project with nothing has no tickets, blocks or questions",
       (empty.get("progress") or {}).get("total") == 0 and empty.get("blocked") == 0
       and empty.get("needs_you") == 0, empty)
    busy = by_id(report, "uc_busy")
    ck("the blocking question first, then the oldest",
       [d["question"] for d in busy["needs_you"]] == ["A later blocking one", "An older one", "May I run it"]
       and [d["permission"] for d in busy["needs_you"]] == [False, False, True], busy["needs_you"])
    ck("a blocked ticket with no reason says an empty one",
       [x["reason"] for x in busy["blocked"]] == [""], busy["blocked"])
    ck("the ticket with nothing set counts in the progress only",
       busy["progress"]["total"] == 6 and busy["progress"]["done"] == 1
       and busy["progress"]["milestone"] == {"name": "M1", "done": 1, "total": 3}
       and not any(x["title"] == "Nothing set" for k in ("done", "started", "doing", "review", "blocked")
                   for x in busy[k]), busy)

    print("== ties ==")
    tied_p = by_id(report, "uc_tied")
    ck("tied tickets come by their number",
       [x["title"] for x in tied_p["review"]] == [f"Ticket number {n}" for n in (1, 2, 3)],
       tied_p["review"])
    ck("tied milestones come by their id", (tied_p["progress"]["milestone"] or {}).get("name") == "M a",
       tied_p["progress"])
    ck("tied desk items come by their number",
       [d["question"] for d in tied_p["needs_you"]] == [f"Question {n}" for n in (1, 2, 3)],
       tied_p["needs_you"])
    ids = ["uc_busy", "uc_tied", "uc_empty"]
    tk, ms, desk = store.tickets_by_project(ids), store.milestones_by_project(ids), store.list_desk("open")
    ck("the batch reads give each project the one-project read, in its order",
       all(tk[u] == store.list_tickets(u) and ms[u] == store.list_milestones(u)
           and [d for d in desk if d["project"] == u] == store.list_desk("open", u) for u in ids),
       {u: ([t["number"] for t in tk[u]], [m["id"] for m in ms[u]]) for u in ids})

    print("== the reads ==")
    with Spy(store) as spy:
        report = batch(store, hide={"uc_secret"})
    one = {n: len(spy.calls[n]) for n in ("list_tickets", "list_milestones", "get_handover", "get_project")}
    ck("no read of one project at a time", not any(one.values()), one)
    ck("the open desk is read once, for all projects", spy.calls["list_desk"] == [("open",)],
       spy.calls["list_desk"])
    asked = [set(a[0]) for n in ("tickets_by_project", "milestones_by_project") for a in spy.calls[n]]
    ck("tickets and milestones are read once each", len(asked) == 2, spy.calls)
    ck("a hidden project's tickets and milestones are not read",
       all("uc_secret" not in a and "uc_busy" in a for a in asked), asked)
    ck("nothing of a hidden project is in the report", "Secret" not in str(report)
       and "secret question" not in str(report) and "Quiet work" not in str(report), report)
    same("with a hidden project: the same as one at a time", store, hide={"uc_secret"})

    print("== a cell of several people ==")
    lead = store.create_person("Kim", "kim@example.com")["id"]
    member = store.create_person("Ana", "ana@example.com")["id"]
    other = store.create_person("Bo", "bo@example.com")["id"]
    store.set_project_lead("uc_busy", lead)
    store.set_project_lead("uc_secret", lead)
    store.set_project_scope("uc_secret", "private")
    store.add_project_member("uc_secret", member)
    ask(store, "uc_busy", "For Ana", to_person_id=member)
    ask(store, "uc_secret", "For the member", to_person_id=member)
    ask(store, "uc_secret", "For someone not on it", to_person_id=other)
    report = same("several people: the same as one at a time", store)
    ck("the report says it is a team's", report["team"] is True, report["team"])
    who = {d["question"]: d["for"] for p in report["projects"] for d in p["needs_you"]}
    ck("each question names whom it waits for",
       who.get("For Ana") == ["Ana"] and who.get("An older one") == ["Kim"]
       and who.get("For the member") == ["Ana"] and who.get("For someone not on it") == ["Kim"], who)
    report = same("several people, the private project hidden: the same as one at a time", store,
                  hide={"uc_secret"})
    ck("the hidden private project is nowhere in it",
       "uc_secret" not in str(report) and "For the member" not in str(report), report)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
