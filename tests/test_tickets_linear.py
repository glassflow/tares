"""Linear as the tickets integration (TR-845).

Runs against a fake Linear (httpx.MockTransport, answers from tests/fixtures/linear/*.json), never
the live API. Covered: tickets/linear is registered (built-in and entry point name) with its
connection field; check_connection with a good key, a refused key, the saved connection and no key
at all; the status mapping for a team with In Review, one with In Progress but no In Review, one
with neither name, and one with no state for done; set_status moves the issue with issueUpdate;
a failed write (Linear down, refusing, no state for the status, no issue) never raises and leaves
a pending_writeback row; the stage does not flip back while the row exists, including a row written
while a sync is running; each sync retries the row; the fifth failure adds one desk item, not
more; a success clears the row; sync returns the count of Linear tickets and keeps the TR-1024
guards (a truncated and an empty fetch change nothing); a ticket that left the project or was
deleted loses its row; a bare integration, a Tares ticket, an unknown status and empty inputs are
refused.

Run: python tests/test_tickets_linear.py
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


from tares import linear
from tares.envelope import now_utc
from tares.integrations import registry
from tares.integrations.builtin.tickets_linear import TicketsLinear, pick_state
from tares.integrations.types import Status
from tares.store import Store

FIX = os.path.join(HERE, "fixtures", "linear")


def fixture(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)


GOOD_KEY = "lin_api_good"
PROJECT = fixture("project")
STATE: dict = {}


def reset(team="team_full"):
    STATE.clear()
    STATE.update(team=fixture(team), issues=fixture("issues"), calls=[], mode="ok", more_pages=False,
                 on_fetch=None, no_issue=False, valid={GOOD_KEY})


def handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != linear.API_URL:
        return httpx.Response(404)
    if request.headers.get("authorization") not in STATE["valid"]:
        return httpx.Response(400, json={"errors": [{"message": "Authentication required"}]})
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": fixture("viewer")}})
    if "issues(" in q:
        if STATE["on_fetch"]:
            STATE["on_fetch"]()
            STATE["on_fetch"] = None
        more = STATE["more_pages"]
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": STATE["issues"], "pageInfo": {"hasNextPage": more,
                                                   "endCursor": "c1" if more else None}}}}})
    if "projectMilestones" in q:
        return httpx.Response(200, json={"data": {"project": {"projectMilestones": {"nodes": []}}}})
    # the two calls that write or read a ticket's team: Linear down is a 503 on these only
    if q.lstrip().startswith("mutation") or "team {" in q:
        if STATE["mode"] == "down":
            return httpx.Response(503, text="unavailable")
        if STATE["mode"] == "html":
            return httpx.Response(200, text="<html>down for maintenance</html>")
        if STATE["mode"] == "array":
            return httpx.Response(200, json=[1, 2])
        if STATE["mode"] == "longerr":
            return httpx.Response(200, json={"errors": [{"message": "boom " * 400}]})
    if q.lstrip().startswith("mutation"):
        STATE["calls"].append((v["id"], v["state"]))
        if STATE["mode"] == "refuse":
            return httpx.Response(200, json={"data": {"issueUpdate": {"success": False}}})
        state = next(s for s in STATE["team"]["states"]["nodes"] if s["id"] == v["state"])
        for iss in STATE["issues"]:
            if iss["id"] == v["id"]:
                iss["state"] = {"id": state["id"], "name": state["name"], "type": state["type"]}
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
    if "team {" in q:
        if STATE["no_issue"]:
            return httpx.Response(200, json={"data": {"issue": None}})
        iss = next((i for i in STATE["issues"] if i["id"] == v["id"]), {})
        now = dict(iss.get("state") or {})
        if now and not now.get("id"):       # Linear always says which state; the fixtures give the name
            now["id"] = next((x["id"] for x in STATE["team"]["states"]["nodes"]
                              if x["name"] == now.get("name")), None)
        return httpx.Response(200, json={"data": {"issue": {"id": v["id"], "state": now,
                                                             "team": STATE["team"]}}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


linear._transport = httpx.MockTransport(handler)
run = asyncio.run


def fails(coro, exc):
    try:
        run(coro)
    except exc as e:
        return str(e) or True
    except Exception:  # noqa: BLE001
        return False
    return False


tmp = tempfile.mkdtemp(prefix="tares-tickets-linear-")
_n = [0]


def world(team="team_full"):
    """A store with a project linked to the fake Linear project, and its three tickets synced."""
    reset(team)
    _n[0] += 1
    s = Store(os.path.join(tmp, f"t{_n[0]}.duckdb"))
    s.create_project("p1", "blank", "Invoices", {})
    s.set_project_linear("p1", {"id": PROJECT["id"], "name": PROJECT["name"]})
    linear._save_connection(s, {"kind": "api_key", "token": GOOD_KEY,
                                "account": {"name": "Ada", "org": "Acme"}})
    b = TicketsLinear(s)
    run(b.sync("p1"))
    return s, b


def tk(s, ident):
    return s.get_ticket("p1", ident)


def status_events(s):
    return s.con.execute("SELECT count(*) FROM events WHERE source = 'linear' AND "
                         "event_type = 'ticket_status'").fetchone()[0]


print("== installed ==")
ck("tickets/linear is registered", registry.get("tickets", "linear") is TicketsLinear)
ck("it is a built-in", "tares.integrations.builtin.tickets_linear:TicketsLinear" in registry.BUILTINS)
ck("its connection field is one secret", [(f.name, f.kind, f.required)
   for f in TicketsLinear.settings_schema] == [("api_key", "secret", False)])

print("== the status mapping ==")
full = fixture("team_full")["states"]["nodes"]
plain = fixture("team_plain")["states"]["nodes"]
prog = fixture("team_progress_only")["states"]["nodes"]
nodone = fixture("team_no_done")["states"]["nodes"]
ids = lambda states, st: (pick_state(states, st) or {}).get("id")
ck("with In Review: todo, in progress, in review, done are four states",
   [ids(full, x) for x in (Status.todo, Status.in_progress, Status.in_review, Status.done)]
   == ["s-todo", "s-prog", "s-review", "s-done"])
ck("without In Review: in review is the in progress state",
   ids(plain, Status.in_review) == ids(plain, Status.in_progress))
ck("with no state named In Progress, in progress is the first started state by position",
   ids(plain, Status.in_progress) == "p-doing", ids(plain, Status.in_progress))
ck("a state named In Progress wins over an earlier started one (any case)",
   ids(prog, Status.in_progress) == "q-prog" and ids(prog, Status.in_review) == "q-prog")
ck("todo is the first unstarted state, done the first completed",
   ids(plain, Status.todo) == "p-todo" and ids(plain, Status.done) == "p-shipped")
ck("a team with no completed state has none for done", ids(nodone, Status.done) is None)
ck("no states at all gives none, and an empty or missing list is fine",
   pick_state([], Status.todo) is None and pick_state([{"id": "x"}], Status.done) is None)

print("== check_connection ==")
s, b = world()
ok, msg = run(b.check_connection({"api_key": GOOD_KEY}))
ck("a good key: connected, as whom", ok and "Ada" in msg and "Acme" in msg, (ok, msg))
ok, msg = run(b.check_connection({"api_key": "lin_api_bad"}))
ck("a refused key says so", not ok and "did not accept" in msg, (ok, msg))
ok, msg = run(b.check_connection({}))
ck("no key: the saved connection is tried", ok and "Ada" in msg, (ok, msg))
ok, msg = run(TicketsLinear().check_connection({}))
ck("no key and no store: asks for a key, never raises", not ok and "key" in msg, (ok, msg))
ok, msg = run(TicketsLinear().check_connection({"api_key": GOOD_KEY}))
ck("a key with no store is still tested", ok, (ok, msg))
ok, msg = run(b.check_connection(None))
ck("None settings count as no key", ok, (ok, msg))
STATE["valid"].clear()
ok, msg = run(b.check_connection({}))
ck("a saved connection Linear refuses is a no, not a raise", not ok and msg, (ok, msg))

print("== sync ==")
s, b = world()
ck("the three tickets came in, Linear's", sorted(t["identifier"] for t in s.list_tickets("p1"))
   == ["FAC-1", "FAC-2", "FAC-3"] and all(t["owner"] == "linear" for t in s.list_tickets("p1")))
s.create_ticket("p1", "tares", "Mine")
n = run(b.sync({"id": "p1"}))
ck("sync returns the count of Linear tickets, not Tares ones, and takes a project dict", n == 3, n)
ck("the event of each change is on the timeline", s.con.execute(
   "SELECT count(*) FROM events WHERE source = 'linear'").fetchone()[0] == 3)
STATE["issues"][0] = {**STATE["issues"][0], "state": {"name": "Done", "type": "completed"}}
run(b.sync("p1"))
ck("a change in Linear is read", tk(s, "FAC-1")["status"] == "done")
ck("a project not linked to Linear has nothing to sync", run(TicketsLinear(Store(
   os.path.join(tmp, "unlinked.duckdb"))).sync("nope")) == 0)

print("== the guards stay (TR-1024) ==")
s, b = world()
before = s.list_tickets("p1")
real, linear.MAX_ISSUES = linear.MAX_ISSUES, 2
STATE["more_pages"] = True
msg = fails(b.sync("p1"), linear.LinearError)
ck("a truncated fetch raises and says why", msg and "more than 2 issues" in msg, msg)
ck("no ticket changed, none canceled", s.list_tickets("p1") == before)
linear.MAX_ISSUES = real
STATE["more_pages"] = False
STATE["issues"] = []
msg = fails(b.sync("p1"), linear.LinearError)
ck("an empty fetch raises and says why", msg and "no issues" in msg, msg)
ck("no ticket changed, none canceled (empty)", s.list_tickets("p1") == before)

print("== set_status ==")
s, b = world()
t1 = tk(s, "FAC-1")
for want, state_id in ((Status.in_progress, "s-prog"), (Status.in_review, "s-review"),
                        (Status.done, "s-done"), (Status.todo, "s-todo")):
    STATE["calls"].clear()
    r = run(b.set_status(t1, want))
    ck(f"{want.value} moves FAC-1 to its state in Linear", r is None
       and STATE["calls"] == [("i1", state_id)], STATE["calls"])
ck("a string status works as well as the enum", run(b.set_status(t1, "in_progress")) is None
   and STATE["calls"][-1] == ("i1", "s-prog"))
ck("a success leaves no pending row", s.list_pending_writebacks("linear") == [])
s2, b2 = world("team_plain")
STATE["calls"].clear()
run(b2.set_status(tk(s2, "FAC-2"), Status.in_review))
ck("a team without In Review: in review is the in progress state", STATE["calls"] == [("i2", "p-doing")])

print("== a failure leaves a pending row, and the stage does not flip back ==")
s, b = world()
t1 = tk(s, "FAC-1")
s.update_ticket(t1["id"], status="in_progress", stage="doing", why="started in Tares")   # what the caller did
STATE["mode"] = "down"
r = run(b.set_status(t1, Status.in_progress))
row = s.get_pending_writeback(t1["id"], "linear")
ck("a failed write does not raise", r is None)
ck("it leaves one row: the status wanted, one attempt, the error",
   row and row["status"] == "in_progress" and row["attempts"] == 1
   and "503" in (row["last_error"] or "") and row["integration"] == "linear", row)
ck("the ticket is as the caller left it", tk(s, "FAC-1")["status"] == "in_progress")
base = status_events(s)
STATE["mode"] = "ok"
STATE["on_fetch"] = None
for iss in STATE["issues"]:
    if iss["id"] == "i1":
        iss["state"] = {"name": "Todo", "type": "unstarted"}   # Linear still shows todo
STATE["mode"] = "down"
run(b.sync("p1"))
ck("a sync with Linear still saying todo does not flip it back",
   tk(s, "FAC-1")["status"] == "in_progress" and tk(s, "FAC-1")["stage"] == "doing")
ck("and says nothing on the timeline about a move that did not happen", status_events(s) == base)
ck("the sync retried: two attempts now",
   s.get_pending_writeback(t1["id"], "linear")["attempts"] == 2)
STATE["issues"][0] = {**STATE["issues"][0], "title": "Parse every invoice"}
run(b.sync("p1"))
ck("other fields still follow Linear while the row waits", tk(s, "FAC-1")["title"] == "Parse every invoice"
   and tk(s, "FAC-1")["status"] == "in_progress")
STATE["mode"] = "ok"
STATE["calls"].clear()
n = run(b.sync("p1"))
ck("when Linear works again the sync lands the write, once", STATE["calls"] == [("i1", "s-prog")], STATE["calls"])
ck("the row is gone", s.list_pending_writebacks("linear") == [])
ck("the ticket is in progress, and Linear agrees", tk(s, "FAC-1")["status"] == "in_progress"
   and STATE["issues"][0]["state"]["type"] == "started")
ck("sync still returns the Linear ticket count", n == 3, n)
run(b.sync("p1"))
ck("and the next sync has nothing to retry", STATE["calls"] == [("i1", "s-prog")])

print("== a row written while a sync is running ==")
s, b = world()
t2 = tk(s, "FAC-2")
s.update_ticket(t2["id"], status="in_progress", why="started in Tares")
STATE["on_fetch"] = lambda: s.pending_writeback_fail(t2["id"], "linear", "in_progress", "boom", now_utc())
base = status_events(s)
run(b.sync("p1"))
ck("the row, written after the sync began reading, still protects the stage",
   tk(s, "FAC-2")["status"] == "in_progress", tk(s, "FAC-2")["status"])
ck("the row is still there for the retry", s.get_pending_writeback(t2["id"], "linear") is not None)
ck("no status event for the move that was not applied", status_events(s) == base)
ck("update_ticket says it left the ticket alone", s.update_ticket(
   t2["id"], status="todo", unless_pending=True) is False and tk(s, "FAC-2")["status"] == "in_progress")
ck("without the flag it writes as before", s.update_ticket(t2["id"], status="todo") is True
   and tk(s, "FAC-2")["status"] == "todo")
s.update_ticket(t2["id"], status="in_progress")

print("== other ways a write fails ==")
for label, mode, no_issue, team, want, text in (
        ("Linear refuses the move", "refuse", False, "team_full", Status.done, "did not move"),
        ("the team has no state for done", "ok", False, "team_no_done", Status.done, "no state for done"),
        ("Linear has no such issue", "ok", True, "team_full", Status.todo, "no issue")):
    s, b = world(team)
    STATE["mode"], STATE["no_issue"] = mode, no_issue
    t = tk(s, "FAC-3")
    r = run(b.set_status(t, want))
    row = s.get_pending_writeback(t["id"], "linear")
    ck(f"{label}: no raise, a row that says why", r is None and row and text in row["last_error"],
       row)
STATE["valid"].clear()
s, b = world()
STATE["valid"].clear()
run(b.set_status(tk(s, "FAC-1"), Status.done))
ck("a refused credential is a pending row as well", s.get_pending_writeback(
   tk(s, "FAC-1")["id"], "linear")["last_error"].startswith("Linear did not accept"))

print("== Linear already shows the status: the wait ends ==")
s, b = world()
t1 = tk(s, "FAC-1")
s.update_ticket(t1["id"], status="in_progress", why="started in Tares")
STATE["mode"] = "down"
run(b.set_status(t1, Status.in_progress))
for _ in range(4):
    run(b.sync("p1"))
ck("five failures, a row, one desk item", s.get_pending_writeback(t1["id"], "linear")["attempts"] == 5
   and len(s.list_desk(project="p1")) == 1)
STATE["mode"] = "ok"
STATE["issues"][0]["state"] = {"name": "In Progress", "type": "started"}   # moved by hand in Linear
STATE["calls"].clear()
run(b.sync("p1"))
ck("the next sync writes nothing: Linear shows in progress already", STATE["calls"] == [], STATE["calls"])
ck("the row is gone", s.get_pending_writeback(t1["id"], "linear") is None)
ck("Tares follows Linear again", tk(s, "FAC-1")["status"] == "in_progress")

s, b = world()
STATE["issues"][0]["state"] = {"name": "In Progress", "type": "started"}
STATE["calls"].clear()
run(b.set_status(tk(s, "FAC-1"), Status.in_review))
ck("in review needs the In Review state: an In Progress issue is moved",
   STATE["calls"] == [("i1", "s-review")], STATE["calls"])
STATE["issues"][0]["state"] = {"name": "In Review", "type": "started"}
STATE["calls"].clear()
run(b.set_status(tk(s, "FAC-1"), Status.in_review))
ck("an issue in In Review is left alone", STATE["calls"] == [], STATE["calls"])
STATE["issues"][0]["state"] = {"name": "Done", "type": "completed"}
run(b.set_status(tk(s, "FAC-1"), Status.done))
ck("done: an issue in Done is left alone", STATE["calls"] == [], STATE["calls"])
s2, b2 = world("team_plain")
STATE["issues"][1]["state"] = {"name": "Doing", "type": "started"}
STATE["calls"].clear()
run(b2.set_status(tk(s2, "FAC-2"), Status.in_review))
ck("a team without In Review: an issue in its started state is left alone", STATE["calls"] == [],
   STATE["calls"])

s, b = world("team_no_done")
t3 = tk(s, "FAC-3")
run(b.set_status(t3, Status.done))
for _ in range(4):
    run(b.sync("p1"))
row = s.get_pending_writeback(t3["id"], "linear")
item = s.list_desk(project="p1")[0]
ck("a team with no state for done keeps its row, and the desk says to add the state",
   row and row["attempts"] == 5 and "add the missing state" in item["recommendation"], (row, item))

print("== a move is never skipped for the wrong state (F12) ==")
s, b = world()
t1 = tk(s, "FAC-1")
STATE["issues"][0]["state"] = {"name": "In Review", "type": "started"}
STATE["calls"].clear()
r = run(b.set_status(t1, Status.in_progress))
ck("changes requested: an issue in In Review is moved to In Progress", STATE["calls"] == [("i1", "s-prog")]
   and r is None, STATE["calls"])
ck("and the write landed: no row", s.get_pending_writeback(t1["id"], "linear") is None)
s2, b2 = world("team_plain")
STATE["issues"][1]["state"] = {"name": "Testing", "type": "started"}
STATE["calls"].clear()
run(b2.set_status(tk(s2, "FAC-2"), Status.in_progress))
ck("a first write moves an issue sitting in another state of the same type",
   STATE["calls"] == [("i2", "p-doing")], STATE["calls"])
# a retry: the person moved the issue by hand while the write was failing
s2, b2 = world("team_plain")
t2 = tk(s2, "FAC-2")
STATE["mode"] = "down"
run(b2.set_status(t2, Status.in_progress))
STATE["mode"] = "ok"
STATE["issues"][1]["state"] = {"name": "Testing", "type": "started"}
STATE["calls"].clear()
run(b2.sync("p1"))
ck("a retry accepts another state of the same type: nothing is written, the row is gone",
   STATE["calls"] == [] and s2.get_pending_writeback(t2["id"], "linear") is None, STATE["calls"])
s, b = world()
t1 = tk(s, "FAC-1")
STATE["mode"] = "down"
run(b.set_status(t1, Status.in_progress))
STATE["mode"] = "ok"
STATE["issues"][0]["state"] = {"name": "In Review", "type": "started"}
STATE["calls"].clear()
run(b.sync("p1"))
ck("a retry never counts In Review as in progress: the write is made",
   STATE["calls"] == [("i1", "s-prog")] and s.get_pending_writeback(t1["id"], "linear") is None,
   STATE["calls"])

print("== a pick that is not a started state ==")
odd = [{"id": "x1", "name": "In Progress", "type": "backlog", "position": 1},
       {"id": "x2", "name": "Doing", "type": "started", "position": 2}]
ck("a state named In Progress of another type is not picked",
   ids(odd, Status.in_progress) == "x2" and ids(odd, Status.in_review) == "x2")

print("== a failure of any kind is kept, never raised ==")
for mode, text in (("html", "not JSON"), ("array", "cannot read"), ("longerr", "boom")):
    s, b = world()
    STATE["mode"] = mode
    t = tk(s, "FAC-2")
    r = fails(b.set_status(t, Status.done), Exception)
    row = s.get_pending_writeback(t["id"], "linear")
    ck(f"a {mode} answer: set_status does not raise, the row says why",
       r is False and row and text in row["last_error"], (r, row))
    ck(f"a {mode} answer: retry_pending does not raise and lands nothing",
       run(b.retry_pending("p1")) == 0)
    ck(f"a {mode} answer: sync does not abort", run(b.sync("p1")) == 3)
s, b = world()
async def boom(*a, **k):
    raise RuntimeError("secret detail")
b._move = boom
t = tk(s, "FAC-1")
run(b.set_status(t, Status.done))
row = s.get_pending_writeback(t["id"], "linear")
ck("any other error is kept as a short line, not its text", row and row["last_error"]
   == "the move failed: RuntimeError", row)

print("== a long error is cut, and the desk item still lands ==")
s, b = world()
STATE["mode"] = "longerr"
t = tk(s, "FAC-1")
run(b.set_status(t, Status.done))
for _ in range(4):
    run(b.sync("p1"))
row = s.get_pending_writeback(t["id"], "linear")
ck("the row keeps at most 300 characters of the error", row and len(row["last_error"]) <= 300, row)
ck("the fifth failure still put one item on the desk", len(s.list_desk(project="p1")) == 1
   and row["attempts"] == 5)
s, b = world()
STATE["mode"] = "down"
def nope(*a, **k):
    raise RuntimeError("desk is broken")
b._desk = nope
t = tk(s, "FAC-1")
run(b.set_status(t, Status.done))
for _ in range(4):
    run(b.sync("p1"))
ck("a desk that fails does not fail the write: the row counts five",
   s.get_pending_writeback(t["id"], "linear")["attempts"] == 5)

print("== who the desk item is for ==")
s, b = world()
lead = s.create_person("Lena Lead")["id"]
doer = s.create_person("Dan Doer")["id"]
s.set_project_lead("p1", lead)
s.update_ticket(tk(s, "FAC-2")["id"], assignee_person_id=doer)
STATE["mode"] = "down"
run(b.set_status(tk(s, "FAC-1"), Status.done))
run(b.set_status(tk(s, "FAC-2"), Status.done))
for _ in range(4):
    run(b.sync("p1"))
who = {("FAC-1" in d["question"]): d["to_person_id"] for d in s.list_desk(project="p1")}
ck("a ticket with an assignee: the item is for them", who.get(False) == doer, who)
ck("a ticket with none: the item is for the project lead", who.get(True) == lead, who)

print("== a new status wanted starts the count again ==")
s, b = world()
t1 = tk(s, "FAC-1")
STATE["mode"] = "down"
run(b.set_status(t1, Status.in_progress))
run(b.set_status(t1, Status.in_progress))
ck("the same status counts up", s.get_pending_writeback(t1["id"], "linear")["attempts"] == 2)
run(b.set_status(t1, Status.done))
row = s.get_pending_writeback(t1["id"], "linear")
ck("a different status replaces the row and starts at one", row["status"] == "done"
   and row["attempts"] == 1, row)

print("== a Tares ticket's row is never retried ==")
s, b = world()
mine = s.get_ticket("p1", s.create_ticket("p1", "tares", "Mine", external_id="i1"))   # an id Linear knows
s.pending_writeback_fail(mine["id"], "linear", "done", "x", now_utc())
STATE["calls"].clear()
run(b.retry_pending("p1"))
ck("no call to Linear, and the row is dropped", STATE["calls"] == []
   and s.get_pending_writeback(mine["id"], "linear") is None)

print("== the desk hears after five failures, once ==")
s, b = world()
t1 = tk(s, "FAC-1")
STATE["mode"] = "down"
run(b.set_status(t1, Status.in_review))
counts = [len(s.list_desk(project="p1"))]
for _ in range(4):
    run(b.sync("p1"))
    counts.append(len(s.list_desk(project="p1")))
ck("attempts 1 to 4: no desk item; attempt 5: one", counts == [0, 0, 0, 0, 1], counts)
ck("the row counts five", s.get_pending_writeback(t1["id"], "linear")["attempts"] == 5)
item = s.list_desk(project="p1")[0]
ck("it names the ticket, the status and Linear's error",
   "FAC-1" in item["question"] and "in review" in item["question"]
   and "503" in (item["context"] or ""), item)
ck("it is not blocking, and has no em dash", not item["blocking"]
   and "\u2014" not in json.dumps(item, default=str), item)
run(b.sync("p1"))
run(b.sync("p1"))
ck("more failures add no second item", len(s.list_desk(project="p1")) == 1
   and s.get_pending_writeback(t1["id"], "linear")["attempts"] == 7)
STATE["mode"] = "ok"
run(b.sync("p1"))
ck("a success afterwards clears the row", s.get_pending_writeback(t1["id"], "linear") is None)

print("== the row goes with its ticket ==")
s, b = world()
t3 = tk(s, "FAC-3")
STATE["mode"] = "down"
run(b.set_status(t3, Status.done))
ck("a pending row", s.get_pending_writeback(t3["id"]) is not None)
STATE["issues"] = [i for i in STATE["issues"] if i["id"] != "i3"]
run(b.sync("p1"))     # Linear still refuses the write (but can be read), so the row is not retried away
ck("the ticket left the project: canceled, and its row is gone",
   tk(s, "FAC-3")["status"] == "canceled" and s.get_pending_writeback(t3["id"]) is None)
STATE["mode"] = "ok"
STATE["mode"] = "down"
t2 = tk(s, "FAC-2")
run(b.set_status(t2, Status.done))
s.delete_ticket(t2["id"])
ck("a deleted ticket loses its row", s.get_pending_writeback(t2["id"]) is None)
t1 = tk(s, "FAC-1")
run(b.set_status(t1, Status.done))
ck("another row", len(s.list_pending_writebacks("linear", project="p1")) == 1)
s.delete_project("p1")
ck("a deleted project loses its rows", s.list_pending_writebacks("linear") == [])
s, b = world()
s.pending_writeback_fail(s.create_ticket("p1", "tares", "Not Linear's"), "linear", "done", "x", now_utc())
s.pending_writeback_fail("tk_gone", "linear", "done", "x", now_utc())
ck("an orphan row is listed for the project only through its ticket",
   len(s.list_pending_writebacks("linear", project="p1")) == 1)
ck("a row whose ticket is not a Linear one is dropped on the next sync, no raise",
   run(b.sync("p1")) == 3 and s.list_pending_writebacks("linear", project="p1") == [])

print("== the rows of one project and one integration ==")
s, b = world()
s.create_project("p2", "blank", "Other", {})
s.pending_writeback_fail(tk(s, "FAC-1")["id"], "linear", "done", "x", now_utc())
other = s.create_ticket("p2", "linear", "Elsewhere", external_id="z9", identifier="OTH-1")
s.pending_writeback_fail(other, "linear", "done", "y", now_utc())
s.pending_writeback_fail(tk(s, "FAC-2")["id"], "github", "done", "z", now_utc())
ck("the list can be narrowed by project", [r["ticket"] for r in s.list_pending_writebacks(
   "linear", project="p1")] == [tk(s, "FAC-1")["id"]])
ck("and by integration", len(s.list_pending_writebacks("github")) == 1
   and len(s.list_pending_writebacks("linear")) == 2 and len(s.list_pending_writebacks()) == 3)
ck("clear takes one integration or all of a ticket", s.pending_writeback_clear(
   tk(s, "FAC-2")["id"], "linear") is False and s.pending_writeback_clear(
   tk(s, "FAC-2")["id"]) is True and s.get_pending_writeback(tk(s, "FAC-2")["id"]) is None)

print("== refused calls ==")
s, b = world()
t1 = tk(s, "FAC-1")
ck("a bare integration refuses sync", fails(TicketsLinear().sync("p1"), RuntimeError))
ck("and set_status", fails(TicketsLinear().set_status(t1, Status.done), RuntimeError))
ck("an unknown status is refused", fails(b.set_status(t1, "blocked"), ValueError))
ck("None is not a status", fails(b.set_status(t1, None), ValueError))
ck("None is not a ticket", fails(b.set_status(None, Status.done), ValueError))
ck("an empty dict is not a ticket", fails(b.set_status({}, Status.done), ValueError))
ck("a bare id is not a ticket", fails(b.set_status(t1["id"], Status.done), ValueError))
ck("a ticket that is not there", fails(b.set_status({"id": "tk_nope", "project": "p1"},
                                                    Status.done), KeyError))
mine = s.create_ticket("p1", "tares", "Mine")
ck("a Tares ticket is refused: it does not live in Linear",
   fails(b.set_status(s.get_ticket("p1", mine), Status.done), ValueError))
ck("None and '' are not projects", fails(b.sync(None), ValueError) and fails(b.sync(""), ValueError)
   and fails(b.sync({}), ValueError))
ck("create, update and link say they are not built yet",
   fails(b.create("p1", [], []), NotImplementedError)
   and fails(b.update(t1, {}), NotImplementedError)
   and fails(b.link("p1", {}), NotImplementedError))
ck("nothing was written by the refused calls", STATE["calls"] == []
   and s.list_pending_writebacks("linear") == [])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
