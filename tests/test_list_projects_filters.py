"""list_projects filters (TR-1503) and done_ticket_facts: a finished project's row from counts.

    python tests/test_list_projects_filters.py
With the Postgres harness: TARES_TEST_PG=<dsn> PYTHONPATH=tests/pg_harness:. python tests/test_list_projects_filters.py
"""
import json
import os
import sys
import tempfile
from datetime import timedelta

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tares import factory as factory_mod  # noqa: E402
from tares.envelope import now_utc  # noqa: E402
from tares.screens.factory_projects import done_row_for, row_for  # noqa: E402
from tares.store import Store  # noqa: E402

FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp()
store = Store(os.path.join(tmp, "t"))
now = now_utc()


def ids(rows):
    return [p["id"] for p in rows]


def make(uid, name, status="active", kind="software_factory", age=0):
    store.create_project(uid, "custom", name, {}, status=status)
    if kind:
        store.set_project_kind(uid, kind)
    with store.sessions_db.begin() as c:    # a distinct, increasing created_at so the order is the one asked for
        c.execute(text("UPDATE usecases SET created_at = :t WHERE id = :i"),
                  {"t": now - timedelta(days=100 - age), "i": uid})


# ── list_projects ──
make("a", "A", "active", "software_factory", 1)
make("b", "B", "done", "software_factory", 2)
make("c", "C", "later", "software_factory", 3)
make("d", "D", "active", None, 4)
make("e", "E", "draft", "other", 5)
with store.sessions_db.begin() as c:
    c.execute(text("UPDATE usecases SET status = NULL WHERE id = 'e'"))

allp = store.list_projects()
ck("default gives every project, oldest first", ids(allp) == ["a", "b", "c", "d", "e"], ids(allp))
ck("kind", ids(store.list_projects(kind="software_factory")) == ["a", "b", "c"])
ck("kind with no match", store.list_projects(kind="nope") == [])
ck("status_in", ids(store.list_projects(status_in=("done", "later"))) == ["b", "c"])
ck("status_not_in keeps a project with no status",
   ids(store.list_projects(status_not_in=("done", "later"))) == ["a", "d", "e"])
ck("ids", ids(store.list_projects(ids=["c", "a", "zz"])) == ["a", "c"])
ck("empty ids gives none", store.list_projects(ids=[]) == [])
ck("ids as a set", ids(store.list_projects(ids={"b"})) == ["b"])
ck("exclude_ids", ids(store.list_projects(exclude_ids={"a", "d"})) == ["b", "c", "e"])
ck("empty exclude_ids and None add no condition",
   ids(store.list_projects(exclude_ids=set())) == ids(allp)
   and ids(store.list_projects(exclude_ids=None)) == ids(allp))
ck("combined", ids(store.list_projects(kind="software_factory", status_not_in=("done", "later"),
                                       exclude_ids=["zz"])) == ["a"])
ck("combined with ids", ids(store.list_projects(ids=["a", "b", "d"], kind="software_factory",
                                                status_not_in=("done",))) == ["a"])
ck("a filtered row equals the same row from the full list",
   store.list_projects(ids=["b"])[0] == next(p for p in allp if p["id"] == "b"))
ck("old filter in Python gives the same as the SQL one",
   store.list_projects(kind="software_factory", exclude_ids={"b"})
   == [p for p in allp if p.get("kind") == "software_factory" and p["id"] not in {"b"}])
try:
    store.list_projects("software_factory")
    ck("arguments are keyword only", False)
except TypeError:
    ck("arguments are keyword only", True)

# ── done_ticket_facts against the real tickets ──
CASES = {}


def project(uid, title="P"):
    make(uid, title, "done", "software_factory", 10 + len(CASES))
    CASES[uid] = []


def ticket(uid, title, status="todo", stage=None, repo=None, position=None, deps=None, pr=None,
           who=None):
    tid = store.create_ticket(uid, "tares", title, status=status, position=position, repo=repo,
                              depends_on=deps, assignee_person_id=who)
    extra = {}
    if stage:
        extra["stage"] = stage
    if pr:
        extra["pr"] = pr
    if extra:
        store.update_ticket(tid, **extra)
    CASES[uid].append(tid)
    return tid


def set_created(tid, at):
    from sqlalchemy import update
    from tares.session_store.tables import tickets as tickets_t
    with store.sessions_db.begin() as c:
        c.execute(update(tickets_t).where(tickets_t.c.id == tid).values(created_at=at))


project("p_merged")
ticket("p_merged", "t1", "done", "merged", "o/a", 1, who="per_sam")
ticket("p_merged", "t2", "done", "shipped", "o/b", 2, who="per_sam")
ticket("p_merged", "t3", "done", None, "o/a", 3, who="per_kim")

project("p_cancel")
ticket("p_cancel", "t1", "done", "merged", "o/a", 1, who="per_sam")
ticket("p_cancel", "t2", "canceled", None, "o/a", 2, who="per_sam")
ticket("p_cancel", "t3", "canceled", None, "o/a", 3, who="per_kim")

project("p_todo_dep")
t_a = ticket("p_todo_dep", "t1", "todo", None, "o/a", 1)
ticket("p_todo_dep", "t2", "todo", None, "o/a", 2, deps=[t_a])
ticket("p_todo_dep", "t3", "done", "merged", "o/a", 3)

project("p_merged_dep")     # a merged ticket with dependencies is still just merged
m1 = ticket("p_merged_dep", "t1", "done", "merged", "o/a", 1)
ticket("p_merged_dep", "t2", "done", "merged", "o/a", 2, deps=[m1])

project("p_mixed")          # doing, review, blocked, in progress, to do with no dependencies
ticket("p_mixed", "t1", "in_progress", "doing", "o/a", 1, who="per_sam")
ticket("p_mixed", "t2", "in_review", "review", "o/a", 2, who="per_kim")
ticket("p_mixed", "t3", "in_progress", "blocked", "o/a", 3)
ticket("p_mixed", "t4", "in_progress", None, "o/a", 4)
ticket("p_mixed", "t5", "todo", None, "o/a", 5)
ticket("p_mixed", "t6", "done", "merged", "o/a", 6)

project("p_pr_only")        # no repo on the ticket, one on its pull request
ticket("p_pr_only", "t1", "done", "merged", None, 1, pr={"repo": "o/z", "number": 4})
ticket("p_pr_only", "t2", "done", "merged", "o/a", 2)

project("p_pr_empty")       # no repo and no pull request: nothing to read a repo from
ticket("p_pr_empty", "t1", "done", "merged", None, 1)
ticket("p_pr_empty", "t2", "done", "merged", "o/a", 2)

project("p_order")          # a repo is placed by its first ticket in (position, created_at)
ta = ticket("p_order", "t1", "done", "merged", "o/a", 1)
tb = ticket("p_order", "t2", "done", "merged", "o/b", 1)
tc = ticket("p_order", "t3", "done", "merged", "o/a", 2)
td = ticket("p_order", "t4", "done", "merged", "o/c", 0.5)
set_created(ta, now - timedelta(hours=1))
set_created(tb, now - timedelta(hours=3))
set_created(tc, now - timedelta(hours=9))
set_created(td, now - timedelta(hours=2))

project("p_open_pr")        # in review with a pull request: GitHub may show it merged
ticket("p_open_pr", "t1", "in_review", "review", "o/a", 1, pr={"repo": "o/a", "number": 7})
ticket("p_open_pr", "t2", "done", "merged", "o/a", 2)

project("p_empty")

facts = store.done_ticket_facts(list(CASES))
ck("no ids gives no facts", store.done_ticket_facts([]) == {})
ck("every project has facts", set(facts) == set(CASES))


def real_repos(tickets):
    names = [str(t.get("repo") or (t.get("pr") or {}).get("repo") or "") for t in tickets]
    return list(dict.fromkeys(n for n in names if n))


tickets_of = store.tickets_by_project(list(CASES))
KEYS = ("merged", "closed", "queued", "stuck", "total")     # what factory.progress gives
for uid, tickets in tickets_of.items():
    f = facts[uid]
    prog = factory_mod.progress(tickets)
    if f["exact"]:
        ck(f"{uid}: counts equal progress",
           tuple(f[k] for k in KEYS) == tuple(prog[k] for k in KEYS),
           (f, prog))
        if not f["repo_from_pr"]:
            ck(f"{uid}: repos equal the tickets' repos", f["repos"] == real_repos(tickets),
               (f["repos"], real_repos(tickets)))
ck("a to-do ticket with dependencies is not exact", facts["p_todo_dep"]["exact"] is False)
ck("a merged ticket with dependencies is exact", facts["p_merged_dep"]["exact"] is True)
ck("an open ticket with a pull request is not exact (it may be merged)",
   facts["p_open_pr"]["exact"] is False)
ck("every other case is exact",
   all(f["exact"] for u, f in facts.items() if u not in ("p_todo_dep", "p_open_pr")), facts)
ck("a ticket with no repo and a pull request is counted", facts["p_pr_only"]["repo_from_pr"] == 1)
ck("a ticket with no repo and no pull request is not counted", facts["p_pr_empty"]["repo_from_pr"] == 0)
ck("repos by first ticket", facts["p_order"]["repos"] == ["o/c", "o/b", "o/a"],
   facts["p_order"]["repos"])
ck("an empty project", facts["p_empty"] == {"merged": 0, "closed": 0, "queued": 0, "stuck": 0,
                                            "total": 0, "repos": [], "repo_from_pr": 0,
                                            "assignees": {}, "exact": True})
# the per-assignee counts are what a team cell's "tickets yours" line reads: every ticket that is
# not canceled, by assignee (None for no one), whatever its stage
for uid, tickets in tickets_of.items():
    want = {}
    for t in tickets:
        if t["status"] != "canceled":
            want[t.get("assignee_person_id")] = want.get(t.get("assignee_person_id"), 0) + 1
    ck(f"{uid}: assignees equal the count of the tickets that are not canceled",
       facts[uid]["assignees"] == want, (facts[uid]["assignees"], want))
ck("canceled tickets are left out of assignees, no one is None",
   facts["p_cancel"]["assignees"] == {"per_sam": 1}
   and facts["p_merged"]["assignees"] == {"per_sam": 2, "per_kim": 1}
   and facts["p_mixed"]["assignees"] == {"per_sam": 1, "per_kim": 1, None: 4}, facts["p_mixed"]["assignees"])
ck("progress of the mixed project is what the groups give",
   tuple(facts["p_mixed"][k] for k in KEYS) == (1, 0, 0, 1, 6), facts["p_mixed"])
ck("a done status with no merge counts as closed, not merged",
   (facts["p_merged"]["merged"], facts["p_merged"]["closed"]) == (2, 1), facts["p_merged"])

# ── the done row from the facts equals row_for on the real tickets ──
store.set_handover("p_merged", [{"path": "/x/a", "name": "org/alpha"}], None, "t", now)
store.finish_handover("p_merged", "done", None, "t", now - timedelta(days=2))
store.set_handover("p_order", [{"path": "/x/q", "name": None}], None, "t", now)
store.set_handover("p_cancel", [{"path": "/x/a", "name": "o/a"}], None, "t", now)
store.finish_handover("p_cancel", "done", None, "t", now - timedelta(days=40))
with store.sessions_db.begin() as c:
    c.execute(text("UPDATE usecases SET updated_at = NULL WHERE id = 'p_empty'"))

handovers = store.handovers_by_project(list(CASES))
milestones = store.milestones_by_project(list(CASES))
for uid in CASES:
    p = store.get_project(uid)
    h = handovers.get(uid)
    expect = row_for(p, tickets_of[uid], h, [], [], now, milestones[uid])
    got = done_row_for(p, facts[uid], h, now)
    if got is None:
        ck(f"{uid}: no summary row only when the facts cannot give one",
           uid in ("p_todo_dep", "p_empty", "p_open_pr") or (uid == "p_pr_only" and not h), uid)
        continue
    ck(f"{uid}: the done row equals row_for", got == expect, (got, expect))
    ck(f"{uid}: the done row has the same key order", json.dumps(got) == json.dumps(expect),
       (json.dumps(got), json.dumps(expect)))
ck("a finished project with no finish time falls back",
   done_row_for(store.get_project("p_empty"), facts["p_empty"], None, now) is None)
ck("a repo from a pull request falls back without a hand-over",
   done_row_for(store.get_project("p_pr_only"), facts["p_pr_only"], None, now) is None)
ck("a repo from a pull request does not matter with the hand-over's repos",
   done_row_for(store.get_project("p_pr_only"), facts["p_pr_only"],
                {"repos": [{"name": "org/x"}], "finished": {"at": now - timedelta(days=1)}}, now)
   is not None)

store.close() if hasattr(store, "close") else None
print()
print("FAIL: " + ", ".join(FAILS) if FAILS else "all ok")
sys.exit(1 if FAILS else 0)
