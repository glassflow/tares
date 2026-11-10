"""Tares tickets as the built-in tickets integration (TR-844).

Covered: the integration is installed as tickets/tares and is the default; check_connection
needs no settings; create makes a milestone or a ticket the way the routes do, and a retry finds
the one already made; set_status writes the Tares status (in review is in progress); update
changes title, order, milestone and dependencies, and refuses a loop, a missing milestone and a
ticket another system owns; sync and link do nothing; a bare integration (no store) refuses
every write.

Run: python tests/test_tickets_tares.py
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


from tares.integrations import registry, resolve
from tares.integrations.builtin.tickets_tares import TicketsTares
from tares.integrations.types import Status, TicketRef
from tares.store import Store

run = asyncio.run


def fails(coro, exc):
    try:
        run(coro)
    except exc as e:
        return str(e) or True
    except Exception as e:  # noqa: BLE001
        return False
    return False


tmp = tempfile.mkdtemp(prefix="tares-tickets-tares-")
s = Store(os.path.join(tmp, "t.duckdb"))
s.create_project("p1", "blank", "One", {})
s.create_project("p2", "blank", "Two", {})
b = TicketsTares(s)

print("== installed ==")
ck("tickets/tares is registered", registry.get("tickets", "tares") is TicketsTares)
ck("it is a built-in", "tares.integrations.builtin.tickets_tares:TicketsTares" in registry.BUILTINS)
eff = resolve.effective("tickets", "p1", store=s)
ck("tickets resolve to tares by default, installed",
   eff.integration == "tares" and eff.reason == "" and eff.source == "default", eff)
ok, msg = run(TicketsTares().check_connection({}))
ck("check_connection needs no settings, even with no store", ok is True and msg, (ok, msg))

print("== create a milestone ==")
r = run(b.create("p1", [{"name": "M1", "goal": "first", "checks": [{"check": "it works"}]}], []))
ck("one TicketRef comes back", len(r) == 1 and isinstance(r[0], TicketRef) and r[0].id, r)
m1 = s.get_milestone("p1", "M1")
ck("the milestone is stored as tares-owned", m1 and m1["owner"] == "tares" and m1["goal"] == "first", m1)
r2 = run(b.create("p1", [{"name": "M1", "goal": "changed"}], []))
ck("a retry finds the same milestone", r2[0].id == r[0].id and len(s.list_milestones("p1")) == 1)
ck("the retry updates its goal", s.get_milestone("p1", "M1")["goal"] == "changed")
run(b.create({"id": "p1"}, [{"name": "M2"}], []))
ck("a project dict works as well as its id", s.get_milestone("p1", "M2") is not None)

print("== create a ticket ==")
t = run(b.create("p1", [], [{"title": "Build it", "milestone": "M1"}]))[0]
row = s.get_ticket("p1", t.id)
ck("the ticket is stored, tares-owned, todo, in the milestone",
   row and row["owner"] == "tares" and row["status"] == "todo" and row["milestone"] == m1["id"], row)
ck("the ref carries the status", t.status == "todo", t)
t_again = run(b.create("p1", [], [{"title": "Build it", "milestone": "M1"}]))[0]
ck("a retry returns the same ticket, no second copy",
   t_again.id == t.id and len(s.list_tickets("p1")) == 1, s.list_tickets("p1"))
other = run(b.create("p1", [], [{"title": "Build it"}]))[0]
ck("the same title in another milestone is a new ticket", other.id != t.id)
dep = run(b.create("p1", [], [{"title": "Then this", "depends_on": [t.id]}]))[0]
ck("depends_on is stored", s.get_ticket("p1", dep.id)["depends_on"] == [t.id])
ck("a missing milestone is refused",
   fails(b.create("p1", [], [{"title": "x", "milestone": "nope"}]), KeyError))
ck("a missing dependency is refused",
   fails(b.create("p1", [], [{"title": "x", "depends_on": ["T999"]}]), KeyError))
ck("an empty title is refused", fails(b.create("p1", [], [{"title": "  "}]), ValueError))
ck("an unknown status is refused",
   fails(b.create("p1", [], [{"title": "y", "status": "wat"}]), ValueError))
ck("a ticket and a milestone in one call is refused",
   fails(b.create("p1", [{"name": "M9"}], [{"title": "z"}]), ValueError))
ck("nothing in a call is refused", fails(b.create("p1", [], []), ValueError))
ck("a working doc that does not exist is refused",
   fails(b.create("p1", [], [{"title": "w", "working_doc": "doc_nope"}]), KeyError))
other_proj = run(b.create("p2", [], [{"title": "Build it"}]))[0]
ck("the same title in another project is its own ticket", other_proj.id != t.id)

print("== set_status ==")
def status(ref):
    return s.get_ticket("p1", ref.id)["status"]
run(b.set_status(row, Status.in_progress))
ck("in_progress", status(t) == "in_progress")
run(b.set_status(s.get_ticket("p1", t.id), Status.in_review))
ck("in review is written as in progress", status(t) == "in_progress")
run(b.set_status(s.get_ticket("p1", t.id), Status.done))
ck("done", status(t) == "done")
run(b.set_status(s.get_ticket("p1", t.id), "todo"))
ck("a status given as its string works", status(t) == "todo")
ck("an unknown status is refused",
   fails(b.set_status(s.get_ticket("p1", t.id), "wat"), ValueError))
ck("a bare id is refused: a ticket is a dict", fails(b.set_status(t.id, Status.done), ValueError))

print("== update ==")
tk = lambda ref: s.get_ticket("p1", ref.id)
run(b.update(tk(t), {"title": "Build it well"}))
ck("title", tk(t)["title"] == "Build it well")
run(b.update(tk(t), {"order": 42}))
ck("order", tk(t)["position"] == 42.0, tk(t)["position"])
run(b.update(tk(t), {"milestone": "M2"}))
ck("milestone by name", tk(t)["milestone"] == s.get_milestone("p1", "M2")["id"])
run(b.update(tk(t), {"milestone": ""}))
ck("an empty milestone clears it", tk(t)["milestone"] is None)
run(b.update(tk(dep), {"depends_on": []}))
ck("depends_on can be emptied", tk(dep)["depends_on"] == [])
run(b.update(tk(dep), {"depends_on": [t.id]}))
ck("depends_on can be set", tk(dep)["depends_on"] == [t.id])
msg = fails(b.update(tk(t), {"depends_on": [dep.id]}), ValueError)
ck("a dependency loop is refused, and named", msg and "loop" in str(msg), msg)
ck("the refused loop changed nothing", tk(t)["depends_on"] == [])
msg = fails(b.update(tk(t), {"depends_on": [t.id]}), ValueError)
ck("a ticket cannot depend on itself, and the answer says so",
   msg and "itself" in str(msg), msg)
ck("a missing milestone is refused", fails(b.update(tk(t), {"milestone": "nope"}), KeyError))
ck("an empty title is refused", fails(b.update(tk(t), {"title": ""}), ValueError))
ck("a ticket that is gone is refused",
   fails(b.update({"id": "tk_gone", "project": "p1"}, {"title": "x"}), KeyError))

print("== a ticket another system owns ==")
ext = s.create_ticket("p1", "linear", "From Linear", "todo", external_id="x1", identifier="ENG-1")
ck("update is refused", fails(b.update(s.get_ticket("p1", ext), {"title": "no"}), ValueError))
ck("set_status is refused", fails(b.set_status(s.get_ticket("p1", ext), Status.done), ValueError))
ck("the Linear ticket is untouched",
   s.get_ticket("p1", ext)["title"] == "From Linear" and s.get_ticket("p1", ext)["status"] == "todo")

print("== sync and link ==")
before = len(s.list_tickets("p1"))
ck("sync reads nothing in", run(b.sync("p1")) == 0 and len(s.list_tickets("p1")) == before)
ck("link links to nothing", run(b.link("p1", {"anything": "x"})) == {})

print("== no store ==")
bare = TicketsTares()
ck("create is refused", fails(bare.create("p1", [], [{"title": "x"}]), RuntimeError))
ck("set_status is refused", fails(bare.set_status(row, Status.done), RuntimeError))
ck("update is refused", fails(bare.update(row, {"title": "x"}), RuntimeError))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
