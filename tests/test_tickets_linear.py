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

TR-847 adds create and update, and the shared rate limit wait. Covered: create of a milestone and of
a ticket (title, body, milestone, team), find before create (twice gives one, any state, an exact
title only: inner spaces, case and a different spelling are other titles, both sides cut at the
ends, the oldest of two, a page 2 hit, junk in the answer ignored), milestone lookup over pages,
the relations (blocks, from the dependency to the new issue, read back by sync; a retry adds only
the missing ones; an existing one, a repeat, an empty entry and the issue itself are skipped; a
ticket of Tares or a wrong Linear id is refused before anything is made), a project with no team,
several teams, no link and a gone project, refused items, Linear refusing or failing a write, 40
tickets with one 429 in the middle, update (title, order, milestone, clearing it, several at once,
unknown fields and bad values refused with nothing sent, no fields no request), and the wait: 429
and RATELIMITED answers, Retry-After, X-RateLimit-Requests-Reset, the default, the floor and the
cap, giving up after RATE_RETRIES, no retry of other errors or a 5xx, only the limited request
sent again, and sync, set_status, create and update all waiting through the same helper.

Run: python tests/test_tickets_linear.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

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
from tares.integrations.types import Status, TicketRef
from tares.store import Store

FIX = os.path.join(HERE, "fixtures", "linear")


def fixture(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)


GOOD_KEY = "lin_api_good"
PROJECT = fixture("project")
STATE: dict = {}


SLEEPS: list = []


async def fake_sleep(seconds):
    SLEEPS.append(seconds)


linear._sleep = fake_sleep


def reset(team="team_full"):
    STATE.clear()
    SLEEPS.clear()
    STATE.update(team=fixture(team), issues=fixture("issues"), calls=[], mode="ok", more_pages=False,
                 on_fetch=None, no_issue=False, valid={GOOD_KEY},
                 # TR-847: what the fake Linear holds and what was asked of it
                 milestones=[], relations=[], teams=[{"id": "tm1", "name": "Factory"}], page_size=50,
                 rate=None, limited=0, fail={}, loose=False, junk=[], gone=set(), no_cursor=False,
                 ops=[], bodies=[], serial=0)


def page_of(nodes, v):
    """One page of `nodes` the way Linear pages: the cursor is the offset."""
    size, off = STATE["page_size"], int(v.get("after") or 0)
    more = off + size < len(nodes)
    return {"nodes": nodes[off:off + size],
            "pageInfo": {"hasNextPage": more or STATE["no_cursor"],
                         "endCursor": str(off + size) if more else None}}


def served(iss):
    """An issue as the sync query reads it: with the relations that point at it."""
    return {**iss, "inverseRelations": {"nodes": [
        {"type": r["type"], "issue": {"id": r["from"]}} for r in STATE["relations"]
        if r["to"] == iss["id"]]}}


def squash(text):
    return " ".join(str(text).split()).lower()


def mutate(q, v):
    inp = v.get("input") or {}
    op = next(n for n in ("issueRelationCreate", "projectMilestoneCreate", "issueCreate",
                          "issueUpdate") if n in q)
    STATE["ops"].append((op, v.get("id"), dict(inp)))
    f = STATE["fail"].get(op)
    if f and f["count"] != 0:
        if f["skip"] > 0:
            f["skip"] -= 1
        else:
            f["count"] -= 1 if f["count"] > 0 else 0
            if f["kind"] == "error":
                return httpx.Response(200, json={"errors": [{"message": "boom"}]})
            if f["kind"] == "empty":
                return httpx.Response(200, json={"data": {op: {"success": True}}})
            if f["kind"] == "false_with":   # says no, but hands back something that has an id
                return httpx.Response(200, json={"data": {op: {
                    "success": False, "projectMilestone": {"id": "ghost", "name": inp.get("name")},
                    "issue": {"id": "ghost", "title": inp.get("title")}}}})
            return httpx.Response(200, json={"data": {op: {"success": False}}})
    if op == "issueRelationCreate":
        STATE["relations"].append({"type": inp["type"], "from": inp["issueId"],
                                   "to": inp["relatedIssueId"]})
        return httpx.Response(200, json={"data": {op: {"success": True}}})
    if op == "projectMilestoneCreate":
        n = len(STATE["milestones"]) + 1
        m = {"id": f"m{n}", "name": inp["name"], "description": inp.get("description"),
             "sortOrder": n * 1000.0}
        STATE["milestones"].append(m)
        return httpx.Response(200, json={"data": {op: {"success": True, "projectMilestone": m}}})
    if op == "issueCreate":
        if inp["projectId"] != PROJECT["id"] or inp["teamId"] not in [t.get("id") for t in STATE["teams"]]:
            return httpx.Response(200, json={"errors": [{"message": "wrong team or project"}]})
        STATE["serial"] += 1
        n = STATE["serial"]
        iss = {"id": f"n{n}", "identifier": f"FAC-{3 + n}", "title": inp["title"],
               "url": f"https://linear.app/acme/issue/FAC-{3 + n}", "sortOrder": 10.0 + n,
               "updatedAt": "2026-11-10T11:00:00Z", "createdAt": f"2026-11-10T11:{n // 60:02d}:{n % 60:02d}Z",
               "description": inp.get("description"), "state": {"name": "Todo", "type": "unstarted"}}
        if inp.get("projectMilestoneId"):
            iss["projectMilestone"] = {"id": inp["projectMilestoneId"]}
        STATE["issues"].append(iss)
        return httpx.Response(200, json={"data": {op: {"success": True, "issue": iss}}})
    iss = next((i for i in STATE["issues"] if i["id"] == v["id"]), None)
    if iss is None:
        return httpx.Response(200, json={"errors": [{"message": "Entity not found: Issue"}]})
    if "title" in inp:
        iss["title"] = inp["title"]
    if "sortOrder" in inp:
        iss["sortOrder"] = inp["sortOrder"]
    if "projectMilestoneId" in inp:
        if inp["projectMilestoneId"] is None:
            iss.pop("projectMilestone", None)
        else:
            iss["projectMilestone"] = {"id": inp["projectMilestoneId"]}
    return httpx.Response(200, json={"data": {op: {"success": True, "issue": {"id": iss["id"]}}}})


def handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != linear.API_URL:
        return httpx.Response(404)
    if request.headers.get("authorization") not in STATE["valid"]:
        return httpx.Response(400, json={"errors": [{"message": "Authentication required"}]})
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    STATE["bodies"].append(body)
    rate = STATE["rate"]
    if rate and rate["left"] != 0 and (rate.get("match") or "") in q:
        if rate["left"] > 0:
            rate["left"] -= 1
        STATE["limited"] += 1
        style, headers = rate["style"], rate.get("headers") or {}
        if style == "429":
            return httpx.Response(429, headers=headers, json={"errors": [{"message": "slow down"}]})
        if style == "5xx":
            return httpx.Response(503, headers=headers, text="unavailable")
        if style == "plain400":
            return httpx.Response(400, headers=headers, text="not json at all")
        if style == "list400":
            return httpx.Response(400, headers=headers, json=[1])
        if style == "errors400":       # errors that are not rate limit ones, in the shapes Linear might send
            return httpx.Response(rate.get("status", 400), headers=headers,
                                  json={"errors": rate["errors"]})
        return httpx.Response(rate.get("status", 400), headers=headers, json={"errors": [{
            "message": "Rate limit exceeded", "extensions": {"code": rate.get("code", "RATELIMITED")}}]})
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": fixture("viewer")}})
    if "issues(" in q and "filter:" in q:       # the lookup of an issue by its title
        if "issues" in STATE["gone"] or v["id"] != PROJECT["id"]:
            return httpx.Response(200, json={"data": {"project": None}})
        if STATE["loose"]:      # a server that matches more loosely than exactly
            nodes = [i for i in STATE["issues"] if squash(i["title"]) == squash(v["title"])]
        else:
            nodes = [i for i in STATE["issues"] if i["title"] == v["title"]]
        return httpx.Response(200, json={"data": {"project": {"issues": page_of(nodes + STATE["junk"], v)}}})
    if "issues(" in q:
        if STATE["on_fetch"]:
            STATE["on_fetch"]()
            STATE["on_fetch"] = None
        more = STATE["more_pages"]
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": [served(i) for i in STATE["issues"]], "pageInfo": {
                "hasNextPage": more, "endCursor": "c1" if more else None}}}}})
    if "projectMilestones" in q:
        if "milestones" in STATE["gone"]:
            return httpx.Response(200, json={"data": {"project": None}})
        return httpx.Response(200, json={"data": {"project": {
            "projectMilestones": page_of(STATE["milestones"], v)}}})
    if "teams(first" in q:
        if "teams" in STATE["gone"]:
            return httpx.Response(200, json={"data": {"project": None}})
        return httpx.Response(200, json={"data": {"project": {"id": PROJECT["id"],
                                                               "teams": {"nodes": STATE["teams"]}}}})
    if "inverseRelations" in q and "issue(id" in q:       # the relations that point at one issue
        if not any(i["id"] == v["id"] for i in STATE["issues"]):
            return httpx.Response(200, json={"data": {"issue": None}})
        rels = [{"type": r["type"], "issue": {"id": r["from"]}} for r in STATE["relations"]
                if r["to"] == v["id"]]
        return httpx.Response(200, json={"data": {"issue": {"id": v["id"], "inverseRelations": page_of(rels, v)}}})
    if "issue(id" in q and "team {" not in q:       # a bare Linear id or identifier, asked of Linear
        if v["id"].startswith("null-"):
            return httpx.Response(200, json={"data": {"issue": None}})
        iss = next((i for i in STATE["issues"] if v["id"] in (i["id"], i["identifier"])), None)
        if iss is None:
            return httpx.Response(200, json={"errors": [{"message": "Entity not found: Issue"}]})
        return httpx.Response(200, json={"data": {"issue": {"id": iss["id"]}}})
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
    if q.lstrip().startswith("mutation") and "$input" in q:
        return mutate(q, v)
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


SEEN: list = []       # every message a refused call gave, to check none holds the credential


def fails(coro, exc):
    try:
        run(coro)
    except exc as e:
        SEEN.append(str(e))
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
ck("link says it is not built yet", fails(b.link("p1", {}), NotImplementedError))
ck("nothing was written by the refused calls", STATE["calls"] == []
   and s.list_pending_writebacks("linear") == [])

# ── TR-847 ────────────────────────────────────────────────────────────────────────────────────
def ops(name):
    return [o for o in STATE["ops"] if o[0] == name]


def asks(text):
    """How many requests so far had this text in their query."""
    return sum(1 for b_ in STATE["bodies"] if text in b_["query"])


def limited(headers=None, style="429", left=1, match="", **more):
    STATE["rate"] = {"left": left, "style": style, "headers": headers or {}, "match": match, **more}


def failing(op, kind="false", count=1, skip=0):
    STATE["fail"][op] = {"kind": kind, "count": count, "skip": skip}


def viewer(store):
    return run(linear.query(store, "query { viewer { id name } }"))


async def collect(gen):
    return [n async for n in gen]


def quiet_world(team="team_full"):
    """A world with the request log emptied, so a test counts only its own requests."""
    s_, b_ = world(team)
    STATE["bodies"].clear()
    return s_, b_


def reset_ms(seconds):
    return str(int((time.time() + seconds) * 1000))


def make(b_, **item):
    return run(b_.create("p1", [], [item]))[0]


print("== the wait for Linear's rate limit: how long ==")
cases = [
    ("Retry-After says 7", {"Retry-After": "7"}, 7.0),
    ("no header at all is 10", {}, 10.0),
    ("Retry-After 0 is raised to the 1 second floor", {"Retry-After": "0"}, 1.0),
    ("a negative Retry-After is raised to 1", {"Retry-After": "-5"}, 1.0),
    ("a huge Retry-After is cut to MAX_WAIT", {"Retry-After": "3600"}, 60.0),
    ("a reset an hour away is cut to MAX_WAIT", {"X-RateLimit-Requests-Reset": reset_ms(3600)}, 60.0),
    ("a reset in the past is raised to 1", {"X-RateLimit-Requests-Reset": reset_ms(-5000)}, 1.0),
    ("Retry-After is not a number: no header, 10", {"Retry-After": "soon"}, 10.0),
    ("Retry-After is nan: no header, 10", {"Retry-After": "nan"}, 10.0),
    ("a reset that is not a number: 10", {"X-RateLimit-Requests-Reset": "tomorrow"}, 10.0),
    ("Retry-After wins over the reset", {"Retry-After": "3", "X-RateLimit-Requests-Reset": reset_ms(30)}, 3.0),
    ("a bad Retry-After falls to the reset", {"Retry-After": "soon",
                                              "X-RateLimit-Requests-Reset": reset_ms(30)}, "reset"),
]
for label, hdr, want in cases:
    s, b = quiet_world()
    limited(headers=hdr)
    viewer(s)
    got = SLEEPS[0] if SLEEPS else None
    right = (28 <= got <= 30.5) if want == "reset" else got == want
    ck(f"{label}: one wait, then the same request goes through",
       len(SLEEPS) == 1 and right and STATE["limited"] == 1 and len(STATE["bodies"]) == 2, (SLEEPS, want))
s, b = quiet_world()
limited(headers={"X-RateLimit-Requests-Reset": reset_ms(22)})
viewer(s)
ck("X-RateLimit-Requests-Reset is epoch milliseconds: waits until then", 20 <= SLEEPS[0] <= 22.5, SLEEPS)
s, b = quiet_world()
limited(style="graphql", status=200)
viewer(s)
ck("a RATELIMITED error in a 200 answer waits too, the default 10",
   SLEEPS == [10.0] and len(STATE["bodies"]) == 2, SLEEPS)
s, b = quiet_world()
limited(style="graphql", status=400, headers={"Retry-After": "4"})
viewer(s)
ck("a RATELIMITED error in a 400 answer waits Retry-After", SLEEPS == [4.0], SLEEPS)

print("== what counts as a rate limit ==")
R = lambda status, body=None, text=None: (httpx.Response(status, text=text) if text is not None
                                          else httpx.Response(status, json=body))
RL = {"extensions": {"code": "RATELIMITED"}}
ck("HTTP 429 is one, whatever the body", linear._rate_limited(R(429, {})) and linear._rate_limited(
   R(429, text="slow down")))
ck("a RATELIMITED error code is one, in a 400 and a 200",
   linear._rate_limited(R(400, {"errors": [RL]})) and linear._rate_limited(R(200, {"errors": [RL]})))
ck("the code in lower case, or after another error, is one",
   linear._rate_limited(R(200, {"errors": [{"extensions": {"code": "ratelimited"}}]}))
   and linear._rate_limited(R(200, {"errors": [{"message": "x"}, RL]})))
ck("a plain answer is not", not linear._rate_limited(R(200, {"data": {}})))
ck("a 5xx is not one", not linear._rate_limited(R(503, text="unavailable"))
   and not linear._rate_limited(R(500, {"errors": [{"message": "x"}]})))
ck("a 400 that is not JSON is not", not linear._rate_limited(R(400, text="not json")))
ck("a JSON body that is not a dict is not", not linear._rate_limited(R(200, [RL])))
ck("errors that are not a list are not", not linear._rate_limited(R(200, {"errors": "RATELIMITED"}))
   and not linear._rate_limited(R(200, {"errors": RL})) and not linear._rate_limited(R(200, {"errors": None})))
ck("errors that are not dicts are not", not linear._rate_limited(R(200, {"errors": [1, "RATELIMITED", None]})))
ck("an empty errors list is not", not linear._rate_limited(R(200, {"errors": []})))
ck("extensions that are not a dict, another code, or no code are not",
   not linear._rate_limited(R(200, {"errors": [{"extensions": "RATELIMITED"}]}))
   and not linear._rate_limited(R(200, {"errors": [{"extensions": {"code": "FORBIDDEN"}}]}))
   and not linear._rate_limited(R(200, {"errors": [{"extensions": {"code": None}}]}))
   and not linear._rate_limited(R(200, {"errors": [{"extensions": {}}]})))
ck("the header names are read in any case", linear._wait_of(httpx.Headers({"retry-after": "4"})) == 4.0
   and linear._wait_of(httpx.Headers({"RETRY-AFTER": "5"})) == 5.0 and linear._wait_of({}) == 10.0)

print("== the wait: when it gives up, and what it does not retry ==")
s, b = quiet_world()
limited(left=-1)
msg = fails(linear.query(s, "query { viewer { id } }"), linear.LinearError)
ck("after RATE_RETRIES waits it raises a plain LinearError", msg and "rate limiting" in msg
   and linear.RATE_RETRIES == 3, msg)
ck("that is 3 waits and 4 requests, never more", SLEEPS == [10.0] * 3 and len(STATE["bodies"]) == 4,
   (SLEEPS, len(STATE["bodies"])))
ck("the message does not hold the credential", GOOD_KEY not in (msg or ""))
s, b = quiet_world()
linear.RATE_RETRIES = 1
limited(left=-1)
fails(linear.query(s, "query { viewer { id } }"), linear.LinearError)
linear.RATE_RETRIES = 3
ck("RATE_RETRIES is the number of retries", len(SLEEPS) == 1 and len(STATE["bodies"]) == 2, SLEEPS)
s, b = quiet_world()
limited(left=-1, match="issueCreate")
fails(b.create("p1", [], [{"title": "Never made"}]), linear.LinearError)
ck("rate limited for good on the create: the issue is not made", ops("issueCreate") == []
   and len(STATE["issues"]) == 3 and asks("issueCreate") == 4)
s, b = quiet_world()
limited(left=-1, match="issues(")
msg = fails(b.create("p1", [], [{"title": "Never made"}]), linear.LinearError)
ck("rate limited for good on the lookup: no create after it", msg and ops("issueCreate") == []
   and asks("issueCreate") == 0 and len(STATE["issues"]) == 3, msg)
for label, kw in (("a 5xx", {"style": "5xx"}),
                  ("a GraphQL error in a 400", {"style": "errors400", "errors": [{"message": "boom"}]}),
                  ("a GraphQL error in a 200", {"style": "errors400", "status": 200,
                                                "errors": [{"message": "boom"}]}),
                  ("a GraphQL error that is a bare string", {"style": "errors400", "status": 200,
                                                              "errors": "boom"}),
                  ("an error with another code", {"style": "errors400", "status": 200, "errors": [
                      {"message": "no", "extensions": {"code": "FORBIDDEN"}}]}),
                  ("a 400 that is not JSON", {"style": "plain400"}),
                  ("a JSON list", {"style": "list400"})):
    s, b = quiet_world()
    limited(**kw)
    msg = fails(linear.query(s, "query { viewer { id } }"), linear.LinearError)
    ck(f"{label} is not retried: one request, no wait, a LinearError", msg and len(STATE["bodies"]) == 1
       and SLEEPS == [], (msg, SLEEPS))
for label, errs in (("a bare string", "boom"), ("one error that is not in a list", {"message": "boom"}),
                    ("a list with a string in it", ["boom"]), ("a list with an error that has no message",
                                                               [{"extensions": {}}, {"message": "boom"}])):
    s, b = quiet_world()
    limited(style="errors400", status=200, errors=errs)
    msg = fails(linear.query(s, "query { viewer { id } }"), linear.LinearError)
    ck(f"errors given as {label} are read whole into one message", msg and msg.startswith("Linear: ")
       and "boom" in msg and "b; o" not in msg, msg)
s, b = quiet_world()
limited(style="graphql", status=200, match="issueCreate")
ref = make(b, title="Limited once")
ck("a rate limit on a mutation retries only that request", ref.id == "n1" and asks("issueCreate") == 2
   and asks("filter:") == 1 and asks("teams(first") == 1 and len(ops("issueCreate")) == 1
   and len(STATE["issues"]) == 4 and SLEEPS == [10.0], (asks("issueCreate"), asks("filter:"), SLEEPS))
s, b = quiet_world()
limited(headers={"Retry-After": "2"}, match="issues(")
ck("sync waits out a 429 and goes on", run(b.sync("p1")) == 3 and SLEEPS == [2.0]
   and STATE["limited"] == 1)
s, b = quiet_world()
limited(headers={"Retry-After": "2"}, match="mutation")
run(b.set_status(tk(s, "FAC-1"), Status.in_progress))
ck("set_status waits out a 429: written once, nothing pending", STATE["calls"] == [("i1", "s-prog")]
   and SLEEPS == [2.0] and s.list_pending_writebacks("linear") == [], (STATE["calls"], SLEEPS))

print("== paging ==")
MS = lambda n: [{"id": f"m{i}", "name": f"Stage {i}"} for i in range(1, n + 1)]
s, b = quiet_world()
STATE["milestones"] = MS(5)
STATE["page_size"] = 2
got = run(collect(linear.pages(s, linear._MILESTONES, {"id": PROJECT["id"]}, ("project", "projectMilestones"))))
ck("pages hands over page after page until Linear says no more", [len(g) for g in got] == [2, 2, 1]
   and asks("projectMilestones") == 3, [len(g) for g in got])
ck("fetch_milestones gathers every page, in Linear's order",
   [m["id"] for m in run(linear.fetch_milestones(s, PROJECT["id"]))] == ["m1", "m2", "m3", "m4", "m5"])
STATE["milestones"] = []
ck("an empty list is one empty page and no milestones", run(collect(linear.pages(
    s, linear._MILESTONES, {"id": PROJECT["id"]}, ("project", "projectMilestones")))) == [[]]
   and run(linear.fetch_milestones(s, PROJECT["id"])) == [])
STATE["gone"] = {"milestones"}
ck("a missing connection gives nothing", run(collect(linear.pages(
    s, linear._MILESTONES, {"id": PROJECT["id"]}, ("project", "projectMilestones")))) == [])
msg = fails(collect(linear.pages(s, linear._MILESTONES, {"id": PROJECT["id"]},
                                 ("project", "projectMilestones"), missing="it is gone")), linear.LinearError)
ck("a missing connection with a message raises it", msg == "it is gone", msg)
STATE["gone"] = set()
STATE["milestones"], STATE["no_cursor"] = MS(1), True
msg = fails(collect(linear.pages(s, linear._MILESTONES, {"id": PROJECT["id"]},
                                 ("project", "projectMilestones"))), linear.LinearError)
ck("another page promised with no cursor is refused, saying so", msg and "no way to read" in msg, msg)
STATE["no_cursor"] = False
STATE["milestones"], STATE["page_size"] = MS(5), 1
linear.MAX_PAGES = 3
msg = fails(collect(linear.pages(s, linear._MILESTONES, {"id": PROJECT["id"]},
                                 ("project", "projectMilestones"))), linear.LinearError)
msg2 = fails(b.create("p1", [{"name": "Stage 9"}], []), linear.LinearError)
linear.MAX_PAGES = 100
ck("a list longer than MAX_PAGES pages is refused, not cut", msg and "more than 3 pages" in msg, msg)
ck("and a create on such a list makes nothing", msg2 and ops("projectMilestoneCreate") == [], msg2)

print("== create a milestone ==")
s, b = quiet_world()
r = run(b.create("p1", [{"name": "Alpha", "goal": "The first cut"}], []))
ck("a milestone is made: one ref, its Linear id, no url or status", len(r) == 1 and isinstance(r[0], TicketRef)
   and (r[0].id, r[0].url, r[0].status) == ("m1", "", ""), r)
ck("it holds the name and the goal as its description, in the Linear project",
   ops("projectMilestoneCreate")[0][2] == {"projectId": PROJECT["id"], "name": "Alpha",
                                           "description": "The first cut"}, ops("projectMilestoneCreate"))
again = run(b.create("p1", [{"name": "Alpha", "goal": "other goal"}], []))
ck("the same name again finds it: one milestone, same id", again[0].id == "m1" and len(ops(
    "projectMilestoneCreate")) == 1 and len(STATE["milestones"]) == 1)
ck("the name is cut at both ends before it is looked up and sent",
   run(b.create("p1", [{"name": "  Alpha "}], []))[0].id == "m1"
   and run(b.create("p1", [{"name": " Beta "}], []))[0].id == "m2"
   and STATE["milestones"][1]["name"] == "Beta")
ck("no goal, an empty goal and a None goal send no description", all(
    "description" not in o[2] for o in ops("projectMilestoneCreate")[1:])
   and run(b.create("p1", [{"name": "Gamma", "goal": None}], []))[0].id == "m3"
   and run(b.create("p1", [{"name": "Delta", "goal": "  "}], []))[0].id == "m4"
   and all("description" not in o[2] for o in ops("projectMilestoneCreate")[1:]))
ck("a name that differs in case is another milestone", run(b.create("p1", [{"name": "alpha"}], []))[0].id == "m5")
ck("a name with two spaces inside is another milestone",
   run(b.create("p1", [{"name": "Stage  one"}], []))[0].id == "m6"
   and run(b.create("p1", [{"name": "Stage one"}], []))[0].id == "m7")
STATE["milestones"].append({"id": "m8", "name": "  Padded  "})
ck("a name padded in Linear is found by its cut name", run(b.create("p1", [{"name": "Padded"}], []))[0].id == "m8")
ck("a milestone Linear gave no id is not a match, a new one is made",
   (STATE["milestones"].append({"name": "Nameless id"}) or True)
   and run(b.create("p1", [{"name": "Nameless id"}], []))[0].id == "m10")
ck("a milestone entry that is not a dict is skipped", (STATE["milestones"].insert(0, "junk") or True)
   and run(b.create("p1", [{"name": "Alpha"}], []))[0].id == "m1")
s, b = quiet_world()
STATE["milestones"] = [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}, {"id": "m3", "name": "Three"}]
STATE["page_size"] = 1
ck("a milestone on page 3 is found, nothing is made", run(b.create("p1", [{"name": "Three"}], []))[0].id == "m3"
   and ops("projectMilestoneCreate") == [] and asks("projectMilestones") == 3)
STATE["page_size"] = 50
for label, kind in (("refuses", "false"), ("answers success with no milestone", "empty"),
                    ("says no but names a milestone", "false_with")):
    s, b = quiet_world()
    failing("projectMilestoneCreate", kind)
    msg = fails(b.create("p1", [{"name": "Alpha"}], []), linear.LinearError)
    ck(f"Linear {label}: a LinearError, nothing left behind", msg and "did not create the milestone" in msg
       and STATE["milestones"] == [] and s.list_pending_writebacks("linear") == [], msg)
s, b = quiet_world()
failing("projectMilestoneCreate", "error")
ck("Linear failing the milestone write raises what it said", "boom" in (fails(
    b.create("p1", [{"name": "Alpha"}], []), linear.LinearError) or ""))
ck("the retry makes it", run(b.create("p1", [{"name": "Alpha"}], []))[0].id == "m1" and len(STATE["milestones"]) == 1)

print("== create a ticket ==")
s, b = quiet_world()
r = run(b.create("p1", [], [{"title": "Add refunds", "body": "Refund a paid invoice"}]))
ck("a ticket is made: one ref with its Linear id, url and status", len(r) == 1 and isinstance(r[0], TicketRef)
   and (r[0].id, r[0].url, r[0].status) == ("n1", "https://linear.app/acme/issue/FAC-4", "todo"), r)
ck("it goes to the Linear project and its team, with title and body, and no milestone",
   ops("issueCreate")[0][2] == {"teamId": "tm1", "projectId": PROJECT["id"], "title": "Add refunds",
                                "description": "Refund a paid invoice"}, ops("issueCreate"))
ck("the project may be given as its dict", run(b.create({"id": "p1"}, [], [{"title": "Add refunds"}]))[0].id == "n1")
ck("the same title again finds it: one issue, same id", len(ops("issueCreate")) == 1 and len(STATE["issues"]) == 4)
ck("a lone dict as the tickets is one ticket", run(b.create("p1", None, {"title": "Add refunds"}))[0].id == "n1"
   and len(STATE["issues"]) == 4)
ck("the title is cut at both ends, and so is the body",
   make(b, title="  Cut me  ", body="  text \n").id == "n2"
   and ops("issueCreate")[1][2]["title"] == "Cut me" and ops("issueCreate")[1][2]["description"] == "text")
ck("the same cut title finds it", make(b, title="Cut me").id == "n2" and make(b, title=" Cut me").id == "n2")
ck("body None, empty and missing send no description, and `description` is taken for the body",
   make(b, title="No body", body=None).id == "n3" and make(b, title="Empty body", body="").id == "n4"
   and make(b, title="Blank body", body="  ").id == "n5" and make(b, title="Gone body").id == "n6"
   and all("description" not in o[2] for o in ops("issueCreate")[2:])
   and make(b, title="Described", description="via description").id == "n7"
   and ops("issueCreate")[-1][2]["description"] == "via description")
ck("a milestone None or empty sends none", make(b, title="No stage", milestone=None).id == "n8"
   and make(b, title="Blank stage", milestone="  ").id == "n9"
   and all("projectMilestoneId" not in o[2] for o in ops("issueCreate")[-2:]))
ck("it was made once each: 3 + 9 issues", len(STATE["issues"]) == 12)

print("== find before create: what is the same title ==")
s, b = quiet_world()
STATE["issues"] += [{"id": "a1", "identifier": "FAC-7", "title": "Pay  twice", "createdAt": "2026-11-01T00:00:00Z",
                     "state": {"name": "Todo", "type": "unstarted"}},
                    {"id": "a2", "identifier": "FAC-8", "title": "Done thing", "createdAt": "2026-11-01T00:00:00Z",
                     "url": "https://linear.app/acme/issue/FAC-8", "state": {"name": "Done", "type": "completed"}},
                    {"id": "a3", "identifier": "FAC-9", "title": "Dropped thing", "createdAt": "2026-11-01T00:00:00Z",
                     "state": {"name": "Canceled", "type": "canceled"}}]
r = make(b, title="Pay twice")
ck("a title with two spaces inside is NOT the one with one", r.id == "n1" and len(ops("issueCreate")) == 1, r)
ck("and the other way round: one space asked, two stored", make(b, title="Pay  twice").id == "a1"
   and len(ops("issueCreate")) == 1)
ck("a different case is another title", make(b, title="parse invoices").id == "n2")
ck("the same title in another punctuation is another", make(b, title="Parse invoices.").id == "n3")
ck("an issue in Done is found, with its status", (lambda x: (x.id, x.status, x.url))(make(b, title="Done thing"))
   == ("a2", "done", "https://linear.app/acme/issue/FAC-8"))
ck("an issue in Canceled is found too, not made again", (lambda x: (x.id, x.status))(make(b, title="Dropped thing"))
   == ("a3", "canceled"))
ck("none of those made a second one", len(ops("issueCreate")) == 3, len(ops("issueCreate")))
STATE["loose"] = True
ck("a server that matches loosely: a client side compare still refuses a different case",
   make(b, title="PARSE INVOICES").id == "n4")
STATE["issues"].append({"id": "a4", "identifier": "FAC-20", "title": "Wide  gap", "createdAt": "2026-11-02T00:00:00Z",
                        "state": {"name": "Todo", "type": "unstarted"}})
ck("and refuses a double space the server let through", make(b, title="Wide gap").id == "n5")
STATE["issues"].append({"id": "a5", "identifier": "FAC-21", "title": "Padded title ", "createdAt": "2026-11-02T00:00:00Z",
                        "state": {"name": "Todo", "type": "unstarted"}})
ck("a title padded in Linear is the one asked without the padding", make(b, title="Padded title").id == "a5")
STATE["loose"] = False
STATE["issues"] += [{"id": "d2", "identifier": "FAC-30", "title": "Twin", "createdAt": "2026-11-05T00:00:00Z",
                     "state": {"name": "Todo", "type": "unstarted"}},
                    {"id": "d1", "identifier": "FAC-31", "title": "Twin", "createdAt": "2026-11-03T00:00:00Z",
                     "state": {"name": "Todo", "type": "unstarted"}},
                    {"id": "d3", "identifier": "FAC-32", "title": "Twin"}]
ck("of two with the title the oldest wins, one with no date last", make(b, title="Twin").id == "d1")
STATE["issues"] = [i for i in STATE["issues"] if i["id"] != "d1"]
ck("without that one, the next oldest", make(b, title="Twin").id == "d2")
STATE["issues"] = [i for i in STATE["issues"] if i["id"] != "d2"]
ck("a dateless one still wins when it is the only one", make(b, title="Twin").id == "d3")
STATE["junk"] = [{"id": "j1", "title": "Other"}, {"title": "Fresh"}, "text", None, 7]
n0 = len(ops("issueCreate"))
ck("junk in Linear's answer (other titles, no id, not dicts) is ignored", make(b, title="Fresh").id == "n6"
   and len(ops("issueCreate")) == n0 + 1)
STATE["junk"] = []
STATE["issues"] += [{"id": "p2a", "identifier": "FAC-40", "title": "On two", "createdAt": "2026-11-04T00:00:00Z",
                     "state": {"name": "Todo", "type": "unstarted"}}]
STATE["loose"], STATE["page_size"] = True, 1
STATE["issues"] = [{"id": "p1a", "identifier": "FAC-39", "title": "On  two", "createdAt": "2026-11-01T00:00:00Z",
                    "state": {"name": "Todo", "type": "unstarted"}}] + STATE["issues"]
STATE["bodies"].clear()
n0 = len(ops("issueCreate"))
ck("the match is on page 2 (page 1 holds a near miss): found, no duplicate made",
   make(b, title="On two").id == "p2a" and len(ops("issueCreate")) == n0 and asks("filter:") == 2, asks("filter:"))
STATE["loose"], STATE["page_size"] = False, 50
STATE["issues"] = [i for i in STATE["issues"] if i["id"] != "p1a"]
STATE["gone"] = {"issues"}
msg = fails(b.create("p1", [], [{"title": "Whatever"}]), linear.LinearError)
ck("a Linear project that is gone is a plain LinearError, nothing made", msg and "is gone" in msg
   and len(ops("issueCreate")) == n0, msg)
STATE["gone"] = set()

print("== create a ticket in a milestone ==")
s, b = quiet_world()
STATE["milestones"] = [{"id": "m1", "name": "Alpha"}, {"id": "m2", "name": "Beta"}]
r = make(b, title="In alpha", milestone="Alpha")
ck("the ticket goes into the milestone found by name", ops("issueCreate")[0][2]["projectMilestoneId"] == "m1" and r.id == "n1")
make(b, title="In beta", milestone="  Beta ")
ck("the milestone name is cut at both ends", ops("issueCreate")[1][2]["projectMilestoneId"] == "m2")
msg = fails(b.create("p1", [], [{"title": "Lost", "milestone": "Gamma"}]), linear.LinearError)
ck("a milestone that is not there is a plain LinearError and no issue", msg and "no milestone 'Gamma'" in msg
   and len(ops("issueCreate")) == 2 and len(STATE["issues"]) == 5, msg)
ck("the milestone name is exact: another case is refused", fails(b.create(
    "p1", [], [{"title": "Lost", "milestone": "alpha"}]), linear.LinearError) and len(ops("issueCreate")) == 2)
STATE["milestones"] = [{"id": f"m{i}", "name": f"Stage {i}"} for i in range(1, 4)]
STATE["page_size"] = 1
make(b, title="Third", milestone="Stage 3")
ck("a milestone on page 3 is found for the ticket", ops("issueCreate")[2][2]["projectMilestoneId"] == "m3")
STATE["page_size"] = 50
ck("a found issue keeps its own milestone: a retry with another name sends no create or update",
   make(b, title="Third", milestone="Stage 1").id == "n3" and len(ops("issueCreate")) == 3
   and ops("issueUpdate") == [])

print("== dependencies become blocks relations ==")


def rel_reads():
    return sum(1 for b_ in STATE["bodies"] if "inverseRelations" in b_["query"] and "issues(" not in b_["query"])


def pairs():
    return sorted((r["from"], r["to"]) for r in STATE["relations"])


s, b = quiet_world()
STATE["issues"].append({"id": "z1", "identifier": "FAC-9", "title": "Outside", "createdAt": "2026-11-01T00:00:00Z",
                        "state": {"name": "Todo", "type": "unstarted"}})
t1, t2, t3 = tk(s, "FAC-1"), tk(s, "FAC-2"), tk(s, "FAC-3")
r = make(b, title="Needs three", depends_on=[t1, TicketRef("i2"), "FAC-3"])
ck("a ticket dict, a ref and an identifier of the project each give a relation", pairs() == [
    ("i1", r.id), ("i2", r.id), ("i3", r.id)], pairs())
ck("each is a blocks relation from the dependency (issueId) to the new issue (relatedIssueId)",
   ops("issueRelationCreate")[0][2] == {"issueId": "i1", "relatedIssueId": r.id, "type": "blocks"}
   and {o[2]["type"] for o in ops("issueRelationCreate")} == {"blocks"})
ck("the way sync reads it back: the new issue is blocked by the three", sorted(linear.blockers_of(
    served(next(i for i in STATE["issues"] if i["id"] == r.id)))) == ["i1", "i2", "i3"])
ck("a fresh issue does not read relations first", rel_reads() == 0)
r2 = make(b, title="Dict and id forms", depends_on=[{"id": "i1"}, TicketRef(t2["id"]), t3["id"]])
ck("a dict with an id, a ref holding the Tares id and a Tares id all resolve",
   [p for p in pairs() if p[1] == r2.id] == [("i1", r2.id), ("i2", r2.id), ("i3", r2.id)], pairs())
q0 = asks("issue(id: $id) { id }")
r3 = make(b, title="Outside dep", depends_on=["FAC-9", "z1", "FAC-9"])
ck("a Linear id given twice is asked of Linear once (two ids, two lookups)", asks("issue(id: $id) { id }") - q0 == 2,
   asks("issue(id: $id) { id }") - q0)
ck("a Linear identifier or id Tares has no ticket for is asked of Linear, its own id used, once",
   [p for p in pairs() if p[1] == r3.id] == [("z1", r3.id)], pairs())
rr = make(b, title="Same dep many ways", depends_on=[t1, t1["id"], "FAC-1", "i1", {"id": "i1"}])
ck("a ticket, its id, its identifier, its Linear id and repeats are one relation",
   [p for p in pairs() if p[1] == rr.id] == [("i1", rr.id)], pairs())
n0 = len(ops("issueRelationCreate"))
r4 = make(b, title="Blanks", depends_on=["", None, "  "])
ck("empty strings and None entries are skipped: the issue is made, no relation",
   r4.id and len(ops("issueRelationCreate")) == n0)
for label, deps in (("None", None), ("an empty list", []), ("a tuple", ("i1",)), ("a lone string", "i1"),
                    ("a lone dict", {"id": "i1"}), ("a lone ref", TicketRef("i1"))):
    n0 = len(ops("issueRelationCreate"))
    r5 = make(b, title=f"Deps as {label}", depends_on=deps)
    want = 0 if deps in (None, []) else 1
    ck(f"depends_on as {label} gives {want} relation", len(ops("issueRelationCreate")) == n0 + want and r5.id)
before_issues, before_rels = len(STATE["issues"]), len(ops("issueRelationCreate"))
for label, deps, exc in (("a number", 5, ValueError), ("a ref with no id", [TicketRef("")], ValueError),
                         ("a dict with no id", [{}], ValueError), ("a dict with an empty id", [{"id": ""}], ValueError),
                         ("a dict with None as its id", [{"id": None}], ValueError)):
    msg = fails(b.create("p1", [], [{"title": "Refused deps", "depends_on": deps}]), exc)
    ck(f"depends_on as {label} is refused with a message", msg and "depend" in msg, msg)
ck("and nothing was made", len(STATE["issues"]) == before_issues and len(ops("issueRelationCreate")) == before_rels)
for label, deps in (("a made up id", ["nope"]), ("an id Linear answers null for", ["null-1"]),
                    ("one good and one made up", ["i1", "nope"])):
    n_i = len(ops("issueCreate"))
    msg = fails(b.create("p1", [], [{"title": "Wrong dep", "depends_on": deps}]), linear.LinearError)
    ck(f"{label}: a LinearError naming it, and nothing made", msg and "has no issue" in msg
       and len(ops("issueCreate")) == n_i and len(STATE["issues"]) == before_issues, msg)
limited(style="errors400", status=200, left=-1, match="issue(id: $id) { id } }", errors=[{"message": "boom"}])
n_i = len(ops("issueCreate"))
msg = fails(b.create("p1", [], [{"title": "Check fails", "depends_on": ["zz"]}]), linear.LinearError)
ck("another failure of that check is raised as it was, not turned into 'no issue'", msg and "boom" in msg
   and "has no issue" not in msg and len(ops("issueCreate")) == n_i, msg)
STATE["rate"] = None
mine = s.create_ticket("p1", "tares", "Mine")
bare = s.create_ticket("p1", "linear", "Linear but not synced")
for label, dep in (("a ticket Tares owns", mine), ("a Linear ticket with no Linear id yet", bare)):
    msg = fails(b.create("p1", [], [{"title": "Bad dep", "depends_on": [dep]}]), ValueError)
    ck(f"{label} cannot be a dependency", msg and "does not live in Linear" in msg
       and len(STATE["issues"]) == before_issues, msg)
n0 = len(ops("issueRelationCreate"))
me = make(b, title="Self reference")
make(b, title="Self reference", depends_on=[me, me.id, TicketRef(me.id)])
ck("an issue is never made to depend on itself", len(ops("issueRelationCreate")) == n0
   and not any(p[0] == p[1] for p in pairs()))

print("== a retry finishes only what is missing ==")
s, b = quiet_world()
r = make(b, title="Rel", depends_on=["i1"])
ck("one relation made", pairs() == [("i1", r.id)] and rel_reads() == 0)
STATE["bodies"].clear()
make(b, title="Rel", depends_on=["i1"])
ck("an existing relation is skipped: no issueRelationCreate the second time, and it read them",
   len(ops("issueRelationCreate")) == 1 and rel_reads() == 1 and len(ops("issueCreate")) == 1)
make(b, title="Rel", depends_on=["i1", "i2"])
ck("a found issue still gets the relation it lacks, only that one", pairs() == [("i1", r.id), ("i2", r.id)]
   and len(ops("issueRelationCreate")) == 2 and ops("issueRelationCreate")[1][2]["issueId"] == "i2")
s, b = quiet_world()
failing("issueRelationCreate", "error", count=1, skip=1)
msg = fails(b.create("p1", [], [{"title": "Half", "depends_on": ["i1", "i2", "i3"]}]), linear.LinearError)
ck("the second relation fails: raised, what Linear said, the issue and the first relation exist",
   msg and "boom" in msg and len(ops("issueCreate")) == 1 and pairs() == [("i1", "n1")], (msg, pairs()))
r = make(b, title="Half", depends_on=["i1", "i2", "i3"])
ck("the retry adds only the two missing: one issue, three relations, four relation writes in all",
   r.id == "n1" and len(ops("issueCreate")) == 1 and pairs() == [("i1", "n1"), ("i2", "n1"), ("i3", "n1")]
   and [o[2]["issueId"] for o in ops("issueRelationCreate")] == ["i1", "i2", "i2", "i3"], ops("issueRelationCreate"))
ck("a failure leaves no pending row", s.list_pending_writebacks("linear") == [])
s, b = quiet_world()
failing("issueRelationCreate", "false")
msg = fails(b.create("p1", [], [{"title": "Refused rel", "depends_on": ["i1"]}]), linear.LinearError)
ck("Linear refusing a relation is a LinearError naming the title and the blocker, no credential",
   msg and "'Refused rel'" in msg and "i1" in msg and GOOD_KEY not in msg, msg)
ck("and the retry makes it", make(b, title="Refused rel", depends_on=["i1"]).id == "n1" and pairs() == [("i1", "n1")])
s, b = quiet_world()
STATE["page_size"] = 1
make(b, title="Many", depends_on=["i1", "i2", "i3"])
STATE["bodies"].clear()
make(b, title="Many", depends_on=["i1", "i2", "i3"])
ck("existing relations are read over every page: nothing is added twice", len(ops("issueRelationCreate")) == 3
   and rel_reads() == 3, (len(ops("issueRelationCreate")), rel_reads()))
ck("an issue Linear does not know cannot have its relations read",
   fails(b._blocked_by("nope"), linear.LinearError))
for label, kind in (("refuses", "false"), ("answers success with no issue", "empty"),
                    ("says no but names an issue", "false_with")):
    s, b = quiet_world()
    failing("issueCreate", kind)
    msg = fails(b.create("p1", [], [{"title": "Not made"}]), linear.LinearError)
    ck(f"Linear {label} the issue: a LinearError, no issue, no pending row", msg and "did not create the issue" in msg
       and len(STATE["issues"]) == 3 and s.list_pending_writebacks("linear") == [], msg)
s, b = quiet_world()
failing("issueCreate", "error")
ck("Linear failing the issue write raises what it said", "boom" in (fails(
    b.create("p1", [], [{"title": "Boom"}]), linear.LinearError) or ""))
ck("and the retry makes exactly one", make(b, title="Boom").id == "n1" and len(STATE["issues"]) == 4)
s, b = quiet_world()
STATE["mode"] = "down"
msg = fails(b.create("p1", [], [{"title": "While down"}]), linear.LinearError)
ck("Linear down (503) on the write: a LinearError and no pending row", msg and "503" in msg
   and s.list_pending_writebacks("linear") == [], msg)

print("== the team and the project ==")
s, b = quiet_world()
STATE["teams"] = []
msg = fails(b.create("p1", [], [{"title": "No team"}]), linear.LinearError)
ck("a Linear project with no team: a plain LinearError, no issue", msg and "no team" in msg and len(STATE["issues"]) == 3, msg)
STATE["teams"] = [{"name": "no id"}, "junk"]
ck("teams without an id count as no team", fails(b.create("p1", [], [{"title": "No team"}]), linear.LinearError)
   and len(STATE["issues"]) == 3)
STATE["teams"] = [{"id": "tmA", "name": "A"}, {"id": "tmB", "name": "B"}]
make(b, title="Two teams")
ck("with several teams the first is used", ops("issueCreate")[0][2]["teamId"] == "tmA")
STATE["teams"] = [{"name": "no id"}, {"id": "tmC", "name": "C"}]
make(b, title="Skips the bad one")
ck("a team entry with no id is skipped for the next", ops("issueCreate")[1][2]["teamId"] == "tmC")
STATE["gone"] = {"teams"}
msg = fails(b.create("p1", [], [{"title": "Teams gone"}]), linear.LinearError)
ck("a Linear project that is gone is a LinearError", msg and "is gone" in msg and len(ops("issueCreate")) == 2, msg)
STATE["gone"] = set()
s.create_project("p2", "blank", "Other", {})
for kind, args in (("a ticket", ([], [{"title": "T"}])), ("a milestone", ([{"name": "M"}], []))):
    msg = fails(b.create("p2", *args), linear.LinearError)
    ck(f"{kind} for a project not linked to Linear: a plain LinearError", msg and "not linked" in msg, msg)
s.create_project("p3", "blank", "Half linked", {})
s.set_project_linear("p3", {"name": "no id"})
ck("a link with no Linear project id counts as not linked", "not linked" in (fails(
    b.create("p3", [], [{"title": "T"}]), linear.LinearError) or ""))
ck("a project that is not there is a KeyError", fails(b.create("nope", [], [{"title": "T"}]), KeyError))
ck("a bare integration refuses create", fails(TicketsLinear().create("p1", [], [{"title": "T"}]), RuntimeError))
ck("None, an empty id and an empty dict are not projects", all(
    fails(b.create(p_, [], [{"title": "T"}]), ValueError) for p_ in (None, "", {})))

print("== create refuses what it cannot make ==")
s, b = quiet_world()
bad = [("two tickets", [], [{"title": "A"}, {"title": "B"}]), ("two milestones", [{"name": "A"}, {"name": "B"}], []),
       ("a milestone and a ticket", [{"name": "A"}], [{"title": "A"}]), ("nothing", [], []),
       ("None and None", None, None)]
for label, ms_, tks in bad:
    msg = fails(b.create("p1", ms_, tks), ValueError)
    ck(f"{label}: one milestone or one ticket per call", msg and "one milestone or one ticket" in msg, msg)
for label, ms_, tks, word in (("a ticket that is a string", [], ["A title"], "dict"), ("a ticket that is None", [], [None], "dict"),
                              ("a milestone that is a string", ["A name"], [], "dict"),
                              ("a milestone that is None", [None], [], "dict"),
                              ("a ticket with no title", [], [{"body": "x"}], "title"), ("an empty title", [], [{"title": ""}], "title"),
                              ("a None title", [], [{"title": None}], "title"), ("a blank title", [], [{"title": "   "}], "title"),
                              ("a milestone with no name", [{"goal": "x"}], [], "name"), ("an empty name", [{"name": ""}], [], "name"),
                              ("a None name", [{"name": None}], [], "name"), ("a blank name", [{"name": " \t"}], [], "name")):
    msg = fails(b.create("p1", ms_, tks), ValueError)
    ck(f"{label} is refused with a message", msg and word in msg, msg)
ck("none of them sent a request or made anything", STATE["bodies"] == [] and STATE["ops"] == [])
ck("a lone dict as the milestones is one milestone", run(b.create("p1", {"name": "Solo"}, None))[0].id == "m1")
ck("a tuple of one ticket works", run(b.create("p1", (), ({"title": "In a tuple"},)))[0].id == "n1")

print("== forty tickets, one rate limit in the middle ==")
s, b = quiet_world()
refs = []


def forty(rate_at=None):
    out = []
    for i in range(40):
        if rate_at == i:
            limited(match="issueCreate", headers={"Retry-After": "3"})
        deps = ([out[i - 1]] if i >= 1 else []) + ([out[0].id] if i >= 2 else [])
        out.append(make(b, title=f"Step {i + 1:02d}", body=f"Do step {i + 1}", depends_on=deps))
    return out


refs = forty(rate_at=19)
ck("exactly 40 issues were made: 3 + 40 in Linear, each title once", len(STATE["issues"]) == 43
   and len(ops("issueCreate")) == 40 and len({i["title"] for i in STATE["issues"]}) == 43)
ck("the dependency relations: 39 chain links and 38 to the first, no repeats", len(ops("issueRelationCreate")) == 77
   and len(STATE["relations"]) == 77 and len(set(pairs())) == 77, len(ops("issueRelationCreate")))
ck("the one 429 made one wait of 3 seconds, and the request after it was the same create",
   SLEEPS == [3.0] and STATE["limited"] == 1 and asks("issueCreate") == 41, (SLEEPS, STATE["limited"], asks("issueCreate")))
ck("the refs are in order and are the Linear ids", [r_.id for r_ in refs] == [f"n{i}" for i in range(1, 41)])
again = forty()
ck("running all forty again changes nothing: same ids, 43 issues, 77 relations, 40 creates in all",
   [r_.id for r_ in again] == [r_.id for r_ in refs] and len(STATE["issues"]) == 43 and len(ops("issueCreate")) == 40
   and len(ops("issueRelationCreate")) == 77 and SLEEPS == [3.0])

print("== what sync reads after create ==")
s, b = quiet_world()
ms = run(b.create("p1", [{"name": "Alpha", "goal": "First"}], []))[0]
a = make(b, title="Build parser", body="Parse it", milestone="Alpha")
c = make(b, title="Build store", depends_on=[a, "i1"])
n_sync = run(b.sync("p1"))
ck("sync now has the five Linear tickets: the three and the two made", n_sync == 5, n_sync)
ta, tc = s.get_ticket("p1", a.id), s.get_ticket("p1", c.id)
ck("a created issue's id is the external_id sync stores, and its url and status match",
   ta and ta["external_id"] == a.id and ta["owner"] == "linear" and ta["url"] == a.url and ta["status"] == a.status
   and ta["identifier"] == "FAC-4", ta)
ck("the dependencies read back: the second ticket depends on the first and on FAC-1",
   sorted(tc["depends_on"]) == sorted([ta["id"], tk(s, "FAC-1")["id"]]), tc["depends_on"])
tm = s.get_milestone("p1", "Alpha")
ck("the milestone ref's id is the external_id of the milestone sync stores, and the ticket is in it",
   tm and tm["external_id"] == ms.id and ta["milestone"] == tm["id"], (tm, ta["milestone"]))
ck("a retry after the sync still finds the same issue", make(b, title="Build parser").id == a.id and len(ops("issueCreate")) == 2)

print("== update ==")
s, b = quiet_world()
STATE["milestones"] = [{"id": "m1", "name": "Alpha"}, {"id": "m2", "name": "Beta"}]
t1 = tk(s, "FAC-1")
ck("update of the title sends only the title, cut, in one request", run(b.update(t1, {"title": "  New name "})) is None
   and ops("issueUpdate") == [("issueUpdate", "i1", {"title": "New name"})] and len(STATE["bodies"]) == 1
   and STATE["issues"][0]["title"] == "New name", ops("issueUpdate"))
STATE["ops"].clear()
run(b.update(t1, {"order": 2.5}))
run(b.update(t1, {"order": 3}))
ck("update of the order sends sortOrder, a whole number as a float", [o[2] for o in ops("issueUpdate")]
   == [{"sortOrder": 2.5}, {"sortOrder": 3.0}] and STATE["issues"][0]["sortOrder"] == 3.0)
STATE["ops"].clear()
STATE["bodies"].clear()
run(b.update(t1, {"milestone": " Beta "}))
ck("update of the milestone looks it up by name (cut) and sends its id", ops("issueUpdate")[0][2] == {
    "projectMilestoneId": "m2"} and asks("projectMilestones") == 1 and STATE["issues"][0]["projectMilestone"] == {"id": "m2"})
for empty in (None, "", "   "):
    STATE["ops"].clear()
    STATE["bodies"].clear()
    run(b.update(t1, {"milestone": empty}))
    ck(f"a milestone of {empty!r} takes the issue out of its milestone, with no lookup",
       ops("issueUpdate")[0][2] == {"projectMilestoneId": None} and len(STATE["bodies"]) == 1
       and "projectMilestone" not in STATE["issues"][0], ops("issueUpdate"))
    STATE["issues"][0]["projectMilestone"] = {"id": "m2"}
STATE["ops"].clear()
STATE["bodies"].clear()
run(b.update(t1, {"title": "All three", "order": 7, "milestone": "Alpha"}))
ck("several fields go in one issueUpdate", len(ops("issueUpdate")) == 1 and ops("issueUpdate")[0][2] == {
    "title": "All three", "sortOrder": 7.0, "projectMilestoneId": "m1"}, ops("issueUpdate"))
STATE["ops"].clear()
STATE["bodies"].clear()
msg = fails(b.update(t1, {"status": "done", "title": "x", "assignee": "me"}), ValueError)
ck("an unknown field is refused naming the unknown ones only, and nothing is sent", msg and "assignee, status" in msg
   and "title" not in msg.split("not:")[1] and STATE["bodies"] == [], msg)
ck("a field is not unknown by its case: Title is refused", fails(b.update(t1, {"Title": "x"}), ValueError)
   and STATE["bodies"] == [])
ck("fields that are not a dict are refused", all(fails(b.update(t1, f_), ValueError) for f_ in ("title", ["title"], 5))
   and STATE["bodies"] == [])
ck("no fields: no request, no error (None and an empty dict)", run(b.update(t1, {})) is None
   and run(b.update(t1, None)) is None and STATE["bodies"] == [])
msg = fails(b.update(t1, {"milestone": "Gamma", "title": "Not sent"}), linear.LinearError)
ck("a milestone Linear does not have: a plain LinearError, and NOTHING is sent, the title either",
   msg and "no milestone 'Gamma'" in msg and ops("issueUpdate") == [], msg)
ck("the milestone name is exact: another case is refused", fails(b.update(t1, {"milestone": "alpha"}), linear.LinearError)
   and ops("issueUpdate") == [])
for label, val in (("text", "3"), ("True", True), ("None", None), ("nan", float("nan")), ("inf", float("inf")),
                   ("-inf", float("-inf")), ("a list", [1])):
    sent = len(STATE["bodies"])
    msg = fails(b.update(t1, {"order": val}), ValueError)
    ck(f"an order of {label} is refused and nothing is sent", msg == "order is a number"
       and len(STATE["bodies"]) == sent and ops("issueUpdate") == [], msg)
for label, val in (("empty", ""), ("None", None), ("blank", "  ")):
    msg = fails(b.update(t1, {"title": val}), ValueError)
    ck(f"a title that is {label} is refused", msg and "title" in msg and ops("issueUpdate") == [], msg)
STATE["ops"].clear()
failing("issueUpdate", "false")
msg = fails(b.update(t1, {"title": "Refused"}), linear.LinearError)
ck("Linear refusing the write is a LinearError that names the ticket", msg and "did not change" in msg and "FAC-1" in msg, msg)
failing("issueUpdate", "error")
ck("Linear failing the write raises what it said", "boom" in (fails(b.update(t1, {"title": "Errs"}), linear.LinearError) or ""))
ck("a failed update leaves no pending row and no change", s.list_pending_writebacks("linear") == []
   and STATE["issues"][0]["title"] == "All three")
STATE["issues"] = STATE["issues"][1:]
msg = fails(b.update(t1, {"title": "Gone issue"}), linear.LinearError)
ck("an issue gone from Linear is a LinearError", msg and "Entity not found" in msg, msg)
limited(headers={"Retry-After": "2"}, match="issueUpdate")
STATE["issues"].insert(0, {"id": "i1", "identifier": "FAC-1", "title": "Back", "state": {"name": "Todo", "type": "unstarted"}})
STATE["ops"].clear()
run(b.update(t1, {"title": "After the wait"}))
ck("update waits out a 429 and sends the write once", SLEEPS[-1] == 2.0 and len(ops("issueUpdate")) == 1
   and STATE["issues"][0]["title"] == "After the wait", (SLEEPS, ops("issueUpdate")))
mine = s.create_ticket("p1", "tares", "Mine")
bare = s.create_ticket("p1", "linear", "Not synced yet")
ck("a Tares ticket is refused", "does not live in Linear" in (fails(b.update(s.get_ticket("p1", mine), {"title": "x"}), ValueError) or ""))
ck("a Linear ticket with no Linear id yet is refused", "does not live in Linear" in (fails(
    b.update(s.get_ticket("p1", bare), {"title": "x"}), ValueError) or ""))
ck("a bare integration refuses update", fails(TicketsLinear().update(t1, {"title": "x"}), RuntimeError))
ck("None, an empty dict and a bare id are not tickets", all(fails(b.update(x, {"title": "x"}), ValueError)
                                                          for x in (None, {}, t1["id"])))
ck("a ticket that is not there is a KeyError", fails(b.update({"id": "tk_nope", "project": "p1"}, {"title": "x"}), KeyError))
ck("an unknown field is refused even for a bare integration", fails(TicketsLinear().update(t1, {"x": 1}), ValueError))
s.create_project("p2", "blank", "Other", {})
other = s.create_ticket("p2", "linear", "Elsewhere", external_id="z9", identifier="OTH-1")
ck("a milestone for a ticket of a project not linked to Linear is a LinearError", "not linked" in (fails(
    b.update(s.get_ticket("p2", other), {"milestone": "Alpha"}), linear.LinearError) or ""))

print("== nothing leaks and nothing is left pending ==")
ck("no refused call left a pending_writeback row", s.list_pending_writebacks("linear") == [])
ck("no message anywhere holds the credential", SEEN and not any(GOOD_KEY in m_ or "lin_api" in m_ for m_ in SEEN), len(SEEN))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
