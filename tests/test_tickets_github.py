"""GitHub Issues as the tickets integration (TR-846).

Runs against a fake GitHub (httpx.MockTransport answering its REST and GraphQL endpoints), never the
live API. Covered: tickets/github is registered (built-in) with its three connection fields;
check_connection with a credential that can write, one that cannot (push false, no permissions, null
permissions), a missing repo, 404, 401, no credential, a bad name, an unreachable GitHub and the
rate limit; sync retries the waiting writes first and returns the count of GitHub tickets; set_status
for every status and every state of the issue (done closes the issue directly and never twice, to do
reopens and takes the label off, in progress adds the label and creates it when the repo has none,
in review keeps it); a failed write never raises and leaves a pending_writeback row, the sync does not
flip the status back while the row exists (also when the row appears during the sync), each sync
retries, the fifth failure adds one desk item, a success clears the row and an issue that shows the
status already ends the wait; create (one milestone or one ticket, found before made, open or closed,
across pages, the label made once, "blocked by" links once per dependency, the milestone checked
first); update (only the fields given, a milestone by title, a missing one is a plain error); the
rate limit (waited out, capped, a clear error when it stays, never for a plain 403); empty and
missing cases (no store, no link, no credential, unknown project, a ticket that is not GitHub's).

Run: python tests/test_tickets_github.py
"""
import asyncio
import json
import os
import re
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


from tares import github_tickets as gt
from tares.envelope import now_utc
from tares.integrations import registry
from tares.integrations.builtin import tickets_github as tg
from tares.integrations.builtin.tickets_github import TicketsGithub
from tares.integrations.types import Status, TicketRef
from tares.store import Store

REPO = "acme/app"
API = "https://api.github.com"
LABEL = "tares:p"
STATE: dict = {}
SLEEPS: list = []


def issue(n, title, state="open", reason=None, labels=(LABEL,), milestone=None, pr=False):
    i = {"number": n, "node_id": f"I_{n}", "title": title, "body": "", "state": state,
         "state_reason": reason, "labels": [{"name": l} for l in labels], "milestone": milestone,
         "html_url": f"https://github.com/{REPO}/issues/{n}"}
    if pr:
        i["pull_request"] = {"url": "x"}
    return i


def milestone(n, title, state="open"):
    return {"number": n, "node_id": f"MS_{n}", "title": title, "description": "", "state": state,
            "html_url": f"https://github.com/{REPO}/milestone/{n}"}


def reset():
    SLEEPS.clear()
    STATE.clear()
    STATE.update(
        tokens={"tok-a", "tok-b"}, repo={"full_name": REPO, "permissions": {"push": True}},
        repo_status=200, issues=[], milestones=[], labels={LABEL.lower(), "in progress"},
        blocked={}, calls=[], mode="ok",        # ok | down | longerr | boom (the connection fails)
        limit={"left": 0, "status": 403, "headers": {"retry-after": "2"}},   # left -1: always
        label_race=False, dup_link_error=False, on_gql=None)


async def fake_sleep(seconds):
    SLEEPS.append(seconds)

tg._sleep = fake_sleep


def _gql_issue(i):
    return {"id": i["node_id"], "number": i["number"], "title": i["title"], "url": i["html_url"],
            "state": i["state"].upper(), "stateReason": (i["state_reason"] or "").upper() or None,
            "labels": {"nodes": list(i["labels"])},
            "milestone": {"id": i["milestone"]["node_id"]} if i["milestone"] else None,
            "assignees": {"nodes": []},
            "blockedBy": {"nodes": [{"id": b} for b in STATE["blocked"].get(i["node_id"], [])]}}


def _page(items, params):
    per, page = int(params.get("per_page", 30)), int(params.get("page", 1))
    return items[(page - 1) * per: page * per]


def _find(n):
    return next((i for i in STATE["issues"] if i["number"] == n), None)


def graphql(request):
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    STATE["calls"].append({"m": "POST", "p": "graphql", "b": v})
    if STATE["on_gql"]:
        hook, STATE["on_gql"] = STATE["on_gql"], None
        hook()
    if "addBlockedBy" in q:
        have = STATE["blocked"].setdefault(v["issue"], [])
        if v["blocker"] in have and STATE["dup_link_error"]:
            return httpx.Response(200, json={"errors": [{"message": "Issue is already blocked by it"}]})
        have.append(v["blocker"])
        return httpx.Response(200, json={"data": {"addBlockedBy": {"clientMutationId": None}}})
    if "node(id" in q:
        return httpx.Response(200, json={"data": {"node": {"id": v["id"], "blockedBy": {"nodes": [
            {"id": b} for b in STATE["blocked"].get(v["id"], [])]}}}})
    want = [i for i in STATE["issues"] if "pull_request" not in i
            and v["label"].lower() in [l["name"].lower() for l in i["labels"]]]
    ms = [{"id": m["node_id"], "number": m["number"], "title": m["title"], "description": ""}
          for m in STATE["milestones"]]
    return httpx.Response(200, json={"data": {"repository": {
        "nameWithOwner": REPO, "url": f"https://github.com/{REPO}",
        "milestones": {"nodes": ms, "pageInfo": {"hasNextPage": False}},
        "issues": {"nodes": [_gql_issue(i) for i in want],
                   "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})


def handler(request: httpx.Request) -> httpx.Response:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if STATE["mode"] == "boom":
        raise httpx.ConnectError("connection refused", request=request)
    if token not in STATE["tokens"]:
        return httpx.Response(401, json={"message": "Bad credentials"})
    lim = STATE["limit"]
    if lim["left"] != 0:
        lim["left"] -= 1 if lim["left"] > 0 else 0
        return httpx.Response(lim["status"], headers=lim["headers"], json={"message": "slow down"})
    path, method = request.url.path, request.method
    if path == "/graphql":
        return graphql(request)
    base = f"/repos/{REPO}"
    if not path.startswith(base):
        return httpx.Response(404, json={"message": "Not Found"})
    rel = path[len(base):]
    params = dict(request.url.params)
    body = json.loads(request.content) if request.content else None
    STATE["calls"].append({"m": method, "p": rel, "b": body, "q": params})
    if rel == "" and method == "GET":
        if STATE["repo_status"] != 200:
            return httpx.Response(STATE["repo_status"], json={"message": "nope"})
        return httpx.Response(200, json=STATE["repo"])
    if method != "GET" and STATE["mode"] == "down":
        return httpx.Response(503, text="unavailable")
    if method != "GET" and STATE["mode"] == "longerr":
        return httpx.Response(422, json={"errors": [{"message": "boom " * 400}]})
    m = re.fullmatch(r"/issues/(\d+)", rel)
    if m:
        i = _find(int(m.group(1)))
        if i is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "PATCH":
            for k in ("state", "state_reason", "title", "body"):
                if k in body:
                    i[k] = body[k]
            if "milestone" in body:
                i["milestone"] = next((x for x in STATE["milestones"]
                                       if x["number"] == body["milestone"]), None)
        return httpx.Response(200, json=i)
    m = re.fullmatch(r"/issues/(\d+)/labels(?:/(.+))?", rel)
    if m:
        i = _find(int(m.group(1)))
        if method == "POST":
            i["labels"] += [{"name": n} for n in body["labels"]]
            return httpx.Response(200, json=i["labels"])
        if method == "DELETE":
            before = len(i["labels"])
            i["labels"] = [l for l in i["labels"] if l["name"] != m.group(2)]
            if len(i["labels"]) == before:
                return httpx.Response(404, json={"message": "Label does not exist"})
            return httpx.Response(200, json=i["labels"])
    if rel == "/issues" and method == "GET":
        want = params.get("labels", "").lower()
        items = [i for i in STATE["issues"]
                 if (not want or want in [l["name"].lower() for l in i["labels"]])
                 and params.get("state", "open") in ("all", i["state"])]
        return httpx.Response(200, json=_page(sorted(items, key=lambda i: i["number"]), params))
    if rel == "/issues" and method == "POST":
        n = max([i["number"] for i in STATE["issues"]] + [0]) + 1
        ms = next((x for x in STATE["milestones"] if x["number"] == body.get("milestone")), None)
        i = issue(n, body["title"], labels=tuple(body.get("labels") or ()), milestone=ms)
        i["body"] = body.get("body") or ""
        STATE["issues"].append(i)
        return httpx.Response(201, json=i)
    m = re.fullmatch(r"/labels/(.+)", rel)
    if m and method == "GET":
        if m.group(1).lower() in STATE["labels"] and not STATE["label_race"]:
            return httpx.Response(200, json={"name": m.group(1)})
        return httpx.Response(404, json={"message": "Not Found"})
    if rel == "/labels" and method == "POST":
        if body["name"].lower() in STATE["labels"] or STATE["label_race"]:
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [
                {"resource": "Label", "code": "already_exists", "field": "name"}]})
        STATE["labels"].add(body["name"].lower())
        return httpx.Response(201, json={"name": body["name"]})
    if rel == "/milestones" and method == "GET":
        items = [m for m in STATE["milestones"] if params.get("state", "open") in ("all", m["state"])]
        return httpx.Response(200, json=_page(items, params))
    if rel == "/milestones" and method == "POST":
        n = max([m["number"] for m in STATE["milestones"]] + [0]) + 1
        ms = milestone(n, body["title"])
        ms["description"] = body.get("description") or ""
        STATE["milestones"].append(ms)
        return httpx.Response(201, json=ms)
    return httpx.Response(404, json={"message": "Not Found"})


gt._transport = httpx.MockTransport(handler)
run = asyncio.run


def fails(coro, exc=Exception):
    try:
        run(coro)
    except exc as e:
        return str(e) or True
    except Exception:  # noqa: BLE001
        return False
    return False


def attempt(label, coro):
    """The result of a call that should work; a raise is a failed check, not a crash."""
    try:
        return run(coro)
    except Exception as e:  # noqa: BLE001
        ck(label, False, f"raised {type(e).__name__}: {e}")
        return None


def calls(method=None, path=None):
    return [c for c in STATE["calls"] if (method is None or c["m"] == method)
            and (path is None or c["p"] == path)]


tmp = tempfile.mkdtemp(prefix="tares-tickets-github-")
_n = [0]


def newstore():
    _n[0] += 1
    return Store(os.path.join(tmp, f"t{_n[0]}.duckdb"))


def world(link=None, seed=True):
    """A store with a project linked to the fake repo and the three seeded issues synced:
    GH-1 open, GH-2 open with the in progress label, GH-3 closed."""
    reset()
    s = newstore()
    s.upsert_github_credential("cred-a", "tok-a")
    s.create_project("p1", "custom", "p", {"objects": []})
    s.set_project_github("p1", link if link is not None else {
        "repo": REPO, "issue_label": LABEL, "url": f"https://github.com/{REPO}",
        "credential": "cred-a"})
    b = TicketsGithub(s)
    if seed:
        STATE["milestones"].append(milestone(1, "Alpha"))
        STATE["issues"] += [issue(1, "Parse invoices"),
                            issue(2, "Store invoices", labels=(LABEL, "in progress")),
                            issue(3, "Email invoices", state="closed", reason="completed")]
        run(b.sync("p1"))
        STATE["calls"].clear()
    return s, b


def tk(s, ident):
    return s.get_ticket("p1", ident)


def status_events(s):
    return s.con.execute("SELECT count(*) FROM events WHERE source = 'github_issues' AND "
                         "event_type = 'ticket_status'").fetchone()[0]


def labels_of(n):
    return [l["name"] for l in _find(n)["labels"]]


print("== installed ==")
ck("tickets/github is registered", registry.get("tickets", "github") is TicketsGithub)
ck("it is a built-in", "tares.integrations.builtin.tickets_github:TicketsGithub" in registry.BUILTINS)
ck("its name and title", (TicketsGithub.name, TicketsGithub.title) == ("github", "GitHub Issues"))
ck("its fields are the repo, the credential and the issue label", [
    (f.name, f.kind, f.required) for f in TicketsGithub.settings_schema]
   == [("repo", "text", True), ("credential", "text", False), ("issue_label", "text", False)])

print("== check_connection ==")
s, b = world()
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("a credential that can push: connected, says it can write issues", ok and "write issues" in msg
   and REPO in msg, (ok, msg))
ok, msg = run(b.check_connection({"repo": f"https://github.com/{REPO}.git"}))
ck("a pasted address works, and with no credential named each stored one is tried", ok, (ok, msg))
STATE["repo"] = {"full_name": REPO, "permissions": {"push": False, "pull": True}}
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("push false: no, and plain words that it cannot write issues",
   not ok and "cannot write issues" in msg, (ok, msg))
for label, perms in (("empty permissions", {}), ("null permissions", None), ("no permissions key", "gone")):
    STATE["repo"] = {"full_name": REPO}
    if perms != "gone":
        STATE["repo"]["permissions"] = perms
    ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
    ck(f"{label}: no, never a raise", not ok and "cannot write issues" in msg, (ok, msg))
STATE["repo"] = {"full_name": None, "permissions": {"push": True}}
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("a null full_name still passes", ok and REPO in msg, (ok, msg))
STATE["repo"] = {"full_name": REPO, "permissions": {"push": True}}
STATE["repo_status"] = 404
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("404: cannot find the repo", not ok and "cannot find" in msg, (ok, msg))
STATE["repo_status"] = 500
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("another status is a no that names it", not ok and "500" in msg, (ok, msg))
STATE["repo_status"] = 200
s.upsert_github_credential("cred-bad", "tok-bad")
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-bad"}))
ck("401: the credential is not accepted, named", not ok and "did not accept" in msg
   and "cred-bad" in msg, (ok, msg))
ok, msg = run(b.check_connection({"repo": REPO}))
ck("no credential named: the stored ones are tried and one works", ok, (ok, msg))
ok, msg = run(b.check_connection({"repo": REPO, "credential": "nope"}))
ck("a credential that is not stored is a plain no", not ok and "not found" in msg, (ok, msg))
ok, msg = run(TicketsGithub(newstore()).check_connection({"repo": REPO}))
ck("no credential stored at all: say to add one", not ok and "No GitHub credential" in msg, (ok, msg))
for bad in ({}, None, {"repo": ""}, {"repo": "justaname"}):
    ok, msg = run(b.check_connection(bad))
    ck(f"repo missing or malformed ({bad}): asks for owner/name", not ok and "owner/name" in msg, (ok, msg))
ok, msg = run(TicketsGithub().check_connection({"repo": REPO}))
ck("no store: a no, never a raise", not ok and msg, (ok, msg))
STATE["limit"] = {"left": -1, "status": 403, "headers": {"retry-after": "5"}}
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("rate limited: a no that says so", not ok and "rate limit" in msg, (ok, msg))
STATE["limit"]["left"] = 0
STATE["mode"] = "boom"
ok, msg = run(b.check_connection({"repo": REPO, "credential": "cred-a"}))
ck("GitHub unreachable: a no, never a raise", not ok and "Could not reach" in msg, (ok, msg))
STATE["mode"] = "ok"

print("== sync ==")
s, b = world()
ck("the three tickets came in, GitHub's", sorted(t["identifier"] for t in s.list_tickets("p1"))
   == ["GH-1", "GH-2", "GH-3"] and all(t["owner"] == "github" for t in s.list_tickets("p1")))
s.create_ticket("p1", "tares", "Mine")
n = run(b.sync({"id": "p1"}))
ck("sync returns the count of GitHub tickets, not Tares ones, and takes a project dict", n == 3, n)
ck("the events of the first sync are on the timeline", s.con.execute(
   "SELECT count(*) FROM events WHERE source = 'github_issues'").fetchone()[0] == 3)
_find(1)["state"] = "closed"
run(b.sync("p1"))
ck("a change in GitHub is read", tk(s, "GH-1")["status"] == "done")
ck("a project not linked to GitHub has nothing to sync", run(TicketsGithub(newstore()).sync("nope")) == 0)
s, b = world()
STATE["mode"] = "down"
run(b.set_status(tk(s, "GH-1"), Status.in_progress))
STATE["mode"] = "ok"
STATE["calls"].clear()
run(b.sync("p1"))
order = [c["p"] for c in STATE["calls"]]
ck("sync lands the waiting write before it reads", "/issues/1/labels" in order and "graphql" in order
   and order.index("/issues/1/labels") < order.index("graphql"), order)
before = s.list_tickets("p1")
STATE["issues"] = []
ck("(the TR-1024 guard of github_tickets stays) an empty fetch raises", fails(b.sync("p1"),
   gt.GitHubTicketsError) is not False)
ck("and no ticket changed", s.list_tickets("p1") == before)

print("== set_status: done ==")
s, b = world()
r = run(b.set_status(tk(s, "GH-1"), Status.done))
ck("done closes the issue directly, as completed", r is None and _find(1)["state"] == "closed"
   and _find(1)["state_reason"] == "completed"
   and [c["b"] for c in calls("PATCH", "/issues/1")] == [{"state": "closed", "state_reason": "completed"}],
   calls("PATCH"))
ck("a success leaves no pending row", s.list_pending_writebacks("github") == [])
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-1"), Status.done))
ck("done on a closed issue writes nothing", calls("PATCH") == [] and calls("POST") == [], STATE["calls"])
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-3"), "done"))
ck("a string status works, and an issue closed already is left alone", calls("PATCH") == [])
_find(3)["state_reason"] = "not_planned"
run(b.set_status(tk(s, "GH-3"), Status.done))
ck("a closed issue is not reopened or rewritten whatever its reason", calls("PATCH") == []
   and _find(3)["state_reason"] == "not_planned")
run(b.set_status(tk(s, "GH-2"), Status.done))
ck("done on an issue with the label closes it, with one write", _find(2)["state"] == "closed"
   and len(calls("PATCH", "/issues/2")) == 1 and calls("DELETE") == [])

print("== set_status: todo ==")
s, b = world()
run(b.set_status(tk(s, "GH-3"), Status.todo))
ck("todo reopens a closed issue", _find(3)["state"] == "open" and
   [c["b"] for c in calls("PATCH", "/issues/3")] == [{"state": "open"}])
ck("and it had no label to remove", calls("DELETE") == [])
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-2"), Status.todo))
ck("todo on an open issue with the label takes the label off", "in progress" not in labels_of(2)
   and LABEL in labels_of(2) and len(calls("DELETE")) == 1 and calls("PATCH") == [], STATE["calls"])
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-1"), Status.todo))
ck("todo on an open issue without it writes nothing", calls("PATCH") + calls("POST") + calls("DELETE") == [])
_find(3)["state"] = "closed"
_find(3)["labels"].append({"name": "in progress"})
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-3"), Status.todo))
ck("a closed issue with the label: reopened and the label taken off", _find(3)["state"] == "open"
   and "in progress" not in labels_of(3), STATE["calls"])

print("== set_status: in progress and in review ==")
s, b = world()
STATE["labels"].discard("in progress")
run(b.set_status(tk(s, "GH-1"), Status.in_progress))
made = [c["b"] for c in calls("POST", "/labels")]
ck("in progress creates the label the repo lacks, then adds it", made and made[0]["name"] == "in progress"
   and "in progress" in labels_of(1) and "in progress" in STATE["labels"], STATE["calls"])
order = [(c["m"], c["p"]) for c in STATE["calls"]]
ck("the label is made before it is added", order.index(("POST", "/labels"))
   < order.index(("POST", "/issues/1/labels")), order)
s, b = world()
run(b.set_status(tk(s, "GH-1"), Status.in_progress))
ck("with the label in the repo it is only added", calls("POST", "/labels") == []
   and "in progress" in labels_of(1))
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-1"), Status.in_progress))
ck("an issue that shows in progress already is left alone", calls("POST") + calls("PATCH") == [])
s, b = world()
STATE["label_race"] = True
r = run(b.set_status(tk(s, "GH-1"), Status.in_progress))
ck("a label made a moment ago by someone else (422) counts as there", "in progress" in labels_of(1)
   and s.list_pending_writebacks("github") == [], STATE["calls"])
s, b = world()
run(b.set_status(tk(s, "GH-3"), Status.in_progress))
ck("in progress on a closed issue reopens it and adds the label", _find(3)["state"] == "open"
   and "in progress" in labels_of(3), STATE["calls"])
s, b = world()
run(b.set_status(tk(s, "GH-1"), Status.in_review))
ck("in review adds the label to an issue without it", "in progress" in labels_of(1))
STATE["calls"].clear()
run(b.set_status(tk(s, "GH-2"), Status.in_review))
ck("in review keeps the label an issue has: nothing written", calls("POST") + calls("PATCH") + calls("DELETE") == []
   and "in progress" in labels_of(2))
run(b.set_status(tk(s, "GH-3"), Status.in_review))
ck("in review on a closed issue reopens it, labeled", _find(3)["state"] == "open" and "in progress" in labels_of(3))
ck("none of it left a row", s.list_pending_writebacks("github") == [])

print("== a failure leaves a pending row, and the status does not flip back ==")
s, b = world()
t1 = tk(s, "GH-1")
s.update_ticket(t1["id"], status="in_progress", stage="doing", why="started in Tares")
STATE["mode"] = "down"
r = run(b.set_status(t1, Status.in_progress))
row = s.get_pending_writeback(t1["id"], "github")
ck("a failed write does not raise", r is None)
ck("it leaves one row: the status wanted, one attempt, the error",
   row and row["status"] == "in_progress" and row["attempts"] == 1
   and "503" in (row["last_error"] or "") and row["integration"] == "github", row)
ck("the ticket is as the caller left it", tk(s, "GH-1")["status"] == "in_progress")
base = status_events(s)
run(b.sync("p1"))
ck("a sync with GitHub still showing it open does not flip it back",
   tk(s, "GH-1")["status"] == "in_progress" and tk(s, "GH-1")["stage"] == "doing")
ck("and says nothing on the timeline about a move that did not happen", status_events(s) == base)
ck("the sync retried: two attempts now", s.get_pending_writeback(t1["id"], "github")["attempts"] == 2)
_find(1)["title"] = "Parse every invoice"
run(b.sync("p1"))
ck("other fields still follow GitHub while the row waits", tk(s, "GH-1")["title"] == "Parse every invoice"
   and tk(s, "GH-1")["status"] == "in_progress")
STATE["mode"] = "ok"
STATE["calls"].clear()
n = run(b.sync("p1"))
ck("when GitHub works again the sync lands the write, once", len(calls("POST", "/issues/1/labels")) == 1, STATE["calls"])
ck("the row is gone", s.list_pending_writebacks("github") == [])
ck("the ticket is in progress, and GitHub agrees", tk(s, "GH-1")["status"] == "in_progress"
   and "in progress" in labels_of(1))
ck("sync still returns the GitHub ticket count", n == 3, n)
run(b.sync("p1"))
ck("and the next sync has nothing to retry", len(calls("POST", "/issues/1/labels")) == 1)

print("== a row written while a sync is running ==")
s, b = world()
t2 = tk(s, "GH-2")
s.update_ticket(tk(s, "GH-1")["id"], status="in_progress", why="started in Tares")
t1 = tk(s, "GH-1")
base = status_events(s)
STATE["on_gql"] = lambda: s.pending_writeback_fail(t1["id"], "github", "in_progress", "boom", now_utc())
STATE["mode"] = "down"
# the row appears after the sync's retry has run and while GitHub is still being read
async def sync_only():
    return await gt.sync_project(s, "p1")
envs = run(sync_only())
ck("the row, written after the sync began reading, still protects the status",
   tk(s, "GH-1")["status"] == "in_progress", tk(s, "GH-1")["status"])
ck("the row is still there for the retry", s.get_pending_writeback(t1["id"], "github") is not None)
ck("no status event for the move that was not applied", not any(
   e.event_type == "ticket_status" and e.key_value == "GH-1" for e in envs))
ck("update_ticket says it left the ticket alone", s.update_ticket(
   t1["id"], status="todo", unless_pending=True) is False and tk(s, "GH-1")["status"] == "in_progress")

print("== other ways a write fails ==")
s, b = world()
STATE["mode"] = "longerr"
t = tk(s, "GH-1")
run(b.set_status(t, Status.done))
row = s.get_pending_writeback(t["id"], "github")
ck("a 422 is kept with what GitHub said, cut to 300 characters", row and "GitHub refused it" in row["last_error"]
   and len(row["last_error"]) <= 300, row)
s, b = world()
STATE["mode"] = "boom"
t = tk(s, "GH-1")
r = run(b.set_status(t, Status.done))
row = s.get_pending_writeback(t["id"], "github")
ck("GitHub unreachable: no raise, a row that says so", r is None and row and "cannot reach GitHub" in row["last_error"], row)
s, b = world()
t = tk(s, "GH-1")
STATE["issues"].remove(_find(1))
run(b.set_status(t, Status.done))
row = s.get_pending_writeback(t["id"], "github")
ck("GitHub has no such issue: a row that says why", row and "has no issue" in row["last_error"], row)
s, b = world()
STATE["tokens"].clear()
run(b.set_status(tk(s, "GH-1"), Status.done))
ck("a refused credential is a pending row as well", s.get_pending_writeback(
   tk(s, "GH-1")["id"], "github")["last_error"].startswith("GitHub did not accept"))
s, b = world()
STATE["limit"] = {"left": -1, "status": 429, "headers": {"retry-after": "1"}}
run(b.set_status(tk(s, "GH-1"), Status.done))
ck("a rate limit that does not clear is a pending row that says so", "rate limit" in (
   s.get_pending_writeback(tk(s, "GH-1")["id"], "github") or {}).get("last_error", ""))
s, b = world()
t = s.get_ticket("p1", s.create_ticket("p1", "github", "Odd", "todo", 9.0, external_id="I_odd", identifier="ABC-1"))
run(b.set_status(t, Status.done))
ck("a ticket with no issue number is a pending row that says to sync", "no GitHub issue number" in (
   s.get_pending_writeback(t["id"], "github") or {}).get("last_error", ""))
s, b = world()
async def boom(*a, **k):
    raise RuntimeError("secret detail")
b._move = boom
t = tk(s, "GH-1")
run(b.set_status(t, Status.done))
row = s.get_pending_writeback(t["id"], "github")
ck("any other error is kept as a short line, not its text", row and row["last_error"] == "the move failed: RuntimeError", row)

print("== the fifth failure reaches the desk ==")
s, b = world()
t1 = tk(s, "GH-1")
STATE["mode"] = "down"
run(b.set_status(t1, Status.in_progress))
for _ in range(3):
    run(b.sync("p1"))
ck("four failures: a row, nothing on the desk", s.get_pending_writeback(t1["id"], "github")["attempts"] == 4
   and s.list_desk(project="p1") == [])
run(b.sync("p1"))
items = s.list_desk(project="p1")
ck("the fifth failure adds one desk item that names the ticket and GitHub", len(items) == 1
   and "GH-1" in items[0]["question"] and "GitHub" in items[0]["question"], items)
run(b.sync("p1"))
run(b.sync("p1"))
ck("further failures add no more", len(s.list_desk(project="p1")) == 1
   and s.get_pending_writeback(t1["id"], "github")["attempts"] == 7)
STATE["mode"] = "ok"
STATE["calls"].clear()
_find(1)["labels"].append({"name": "in progress"})      # moved by hand in GitHub
run(b.sync("p1"))
ck("when GitHub shows the status already, nothing is written and the wait ends", [c for c in calls("POST") if c["p"] != "graphql"] == []
   and calls("PATCH") == [] and s.get_pending_writeback(t1["id"], "github") is None, STATE["calls"])
ck("Tares follows GitHub again", tk(s, "GH-1")["status"] == "in_progress")
s, b = world()
STATE["mode"] = "down"
t = tk(s, "GH-1")
def nope(*a, **k):
    raise RuntimeError("desk is broken")
b._desk = nope
run(b.set_status(t, Status.done))
for _ in range(4):
    run(b.sync("p1"))
ck("a desk that fails does not fail the write: the row counts five",
   s.get_pending_writeback(t["id"], "github")["attempts"] == 5)
s, b = world()
lead = s.create_person("Lena Lead")["id"]
doer = s.create_person("Dan Doer")["id"]
s.set_project_lead("p1", lead)
s.update_ticket(tk(s, "GH-2")["id"], assignee_person_id=doer)
STATE["mode"] = "down"
run(b.set_status(tk(s, "GH-1"), Status.done))
run(b.set_status(tk(s, "GH-2"), Status.done))
for _ in range(4):
    run(b.sync("p1"))
who = {i["question"].split(" ")[0]: i.get("to_person_id")
       for i in s.list_desk(project="p1")}
ck("the desk item goes to the assignee, else the lead", who.get("GH-1") == lead and who.get("GH-2") == doer, who)

print("== retry_pending ==")
s, b = world()
STATE["mode"] = "down"
run(b.set_status(tk(s, "GH-1"), Status.done))
run(b.set_status(tk(s, "GH-2"), Status.done))
STATE["mode"] = "ok"
ck("retry_pending returns how many landed", run(b.retry_pending("p1")) == 2
   and s.list_pending_writebacks("github") == [] and _find(1)["state"] == "closed")
s, b = world()
STATE["mode"] = "down"
t = tk(s, "GH-1")
run(b.set_status(t, Status.done))
s.update_ticket_owner(t["id"], "tares")
STATE["mode"] = "ok"
ck("a row whose ticket left GitHub is cleared, nothing written", run(b.retry_pending({"id": "p1"})) == 0
   and s.list_pending_writebacks("github") == [] and _find(1)["state"] == "open")
ck("an unknown project has nothing to retry", run(b.retry_pending("nope")) == 0)
s, b = world()
STATE["issues"] = [i for i in STATE["issues"] if i["number"] != 1]
STATE["mode"] = "down"
run(b.set_status(tk(s, "GH-1"), Status.done))
ck("a ticket that left the label loses its row on the sync", run(b.sync("p1")) >= 0
   and s.list_pending_writebacks("github") == [])

print("== a ticket or call that is refused ==")
s, b = world()
tares_id = s.create_ticket("p1", "tares", "Mine")
mine = s.get_ticket("p1", tares_id)
ck("a Tares ticket is refused", "does not live in GitHub" in str(fails(b.set_status(mine, Status.done), ValueError)))
noext = s.get_ticket("p1", s.create_ticket("p1", "github", "No id", "todo", 7.0))
ck("a GitHub ticket with no external id is refused", fails(b.set_status(noext, Status.done), ValueError) is not False)
ck("an unknown status is refused, naming the choices", "in_review" in str(fails(b.set_status(tk(s, "GH-1"), "blocked"), ValueError)))
ck("a bare id is refused", fails(b.set_status("GH-1", Status.done), ValueError) is not False)
ck("a ticket of no project is refused", fails(b.set_status({"id": "x"}, Status.done), ValueError) is not False)
ck("a ticket that is not there is refused", fails(b.set_status({"id": "zzz", "project": "p1"}, Status.done), KeyError) is not False)
ck("a bare integration has no store to work with", fails(TicketsGithub().set_status(tk(s, "GH-1"), Status.done), RuntimeError) is not False
   and fails(TicketsGithub().sync("p1"), RuntimeError) is not False)
ck("sync wants a project", fails(b.sync(""), ValueError) is not False)
_before = s.get_project_github("p1")
ck("link with no repo to name is refused, the link is untouched", fails(b.link("p1", {}), Exception) is not False
   and s.get_project_github("p1") == _before)
ck("nothing was written by the refused calls", calls("PATCH") + calls("POST") + calls("DELETE") == []
   and s.list_pending_writebacks("github") == [])
s, b = world(link={}, seed=False)
ck("a project with no GitHub link: set_status keeps a row only for a ticket, create says to link",
   "not linked" in str(fails(b.create("p1", [], [{"title": "x"}]), gt.GitHubTicketsError)))
s, b = world(link={"repo": REPO, "issue_label": LABEL}, seed=False)
s.delete_github_credential("cred-a") if hasattr(s, "delete_github_credential") else None
ck("no stored credential: create says to add one", "no stored GitHub credential" in str(
    fails(b.create("p1", [], [{"title": "x"}]), gt.GitHubTicketsError)))
ck("an unknown project", fails(b.create("nope", [], [{"title": "x"}]), KeyError) is not False)

print("== create: a ticket ==")
s, b = world()
refs = attempt("a ticket in a milestone GitHub has is created", b.create(
    "p1", [], [{"title": "Add retries", "body": "Do it", "milestone": "Alpha"}])) or [None]
new = _find(4)
ck("one ticket gives one issue with the project label, body and milestone", len(refs) == 1 and new
   and new["title"] == "Add retries" and [l["name"] for l in new["labels"]] == [LABEL]
   and new["body"] == "Do it" and new["milestone"]["title"] == "Alpha", new)
ck("the ref is the issue's node id, url and status", len(refs) == 1 and refs[0] == TicketRef("I_4", f"https://github.com/{REPO}/issues/4", "todo"), refs)
ck("the label existed: it was not made again", calls("POST", "/labels") == [])
refs2 = attempt("create again", b.create("p1", [], [{"title": "Add retries", "body": "Do it", "milestone": "Alpha"}]))
ck("create twice gives ONE issue and the same ref", len(STATE["issues"]) == 4 and refs2 == refs, (len(STATE["issues"]), refs2))
_find(4)["state"] = "closed"
_find(4)["state_reason"] = "completed"
refs3 = run(b.create("p1", [], [{"title": "Add retries"}]))
ck("a closed issue is found too: no second one, and its status is done", len(STATE["issues"]) == 4
   and refs3[0].id == "I_4" and refs3[0].status == "done", refs3)
ck("a title that only starts the same is not the same issue", run(b.create("p1", [], [{"title": "Add"}]))[0].id == "I_5")
ck("a title is exact: spaces collapse, case does not", run(b.create("p1", [], [{"title": "add retries"}]))[0].id == "I_6"
   and run(b.create("p1", [], [{"title": "Add   retries"}]))[0].id == "I_4")
STATE["issues"].append(issue(7, "Same title", labels=("other",)))
ck("an issue of another label with the title is not the project's", run(b.create("p1", [], [{"title": "Same title"}]))[0].id == "I_8")
STATE["issues"].append(issue(9, "A pull request", pr=True))
ck("a pull request with the title is skipped", run(b.create("p1", [], [{"title": "A pull request"}]))[0].id == "I_10")
n_before = len(STATE["issues"])
run(b.create("p1", [], [{"title": "Add retries"}]))
ck("no new issue on a find", len(STATE["issues"]) == n_before)
s, b = world()
STATE["issues"] += [issue(n, f"Bulk {n}") for n in range(10, 115)]
ref = run(b.create("p1", [], [{"title": "Bulk 114"}]))[0]
ck("an issue on the second page of the listing is found", ref.id == "I_114" and len(STATE["issues"]) == 108
   and any(c["q"].get("page") == "2" for c in calls("GET", "/issues")), ref)
s, b = world(seed=False)
ck("an empty issue list creates the first one", run(b.create("p1", [], [{"title": "First"}]))[0].id == "I_1")
s, b = world(seed=False)
STATE["labels"].clear()
ref = run(b.create("p1", [], [{"title": "First"}]))[0]
ck("an empty repo: the missing project label is made, then the issue", ref.id == "I_1"
   and [c["b"]["name"] for c in calls("POST", "/labels")] == [LABEL] and LABEL.lower() in STATE["labels"], STATE["calls"])
run(b.create("p1", [], [{"title": "Second"}]))
ck("the label is made once", len(calls("POST", "/labels")) == 1)
s, b = world(seed=False)
STATE["labels"].clear()
STATE["label_race"] = True
ck("a label made at the same moment (422 already_exists) is fine", run(b.create("p1", [], [{"title": "Raced"}]))[0].id == "I_1")
s, b = world(link={"repo": REPO, "credential": "cred-a"}, seed=False)
STATE["labels"].clear()
run(b.create("p1", [], [{"title": "Default"}]))
ck("a link with no issue label uses tares:<project>", labels_of(1) == [LABEL] and LABEL in [c["b"]["name"] for c in calls("POST", "/labels")])
s, b = world(link={"repo": REPO, "issue_label": "factory:x", "credential": "cred-a"}, seed=False)
run(b.create("p1", [], [{"title": "Custom"}]))
ck("the link's own issue label is used", labels_of(1) == ["factory:x"])
s, b = world(link={"repo": REPO, "issue_label": LABEL}, seed=False)
ck("a link with no remembered credential tries the stored ones", run(b.create("p1", [], [{"title": "Any"}]))[0].id == "I_1")

print("== create: blocked by ==")
s, b = world()
t1, t2 = tk(s, "GH-1"), tk(s, "GH-2")
attempt("a ticket with dependencies is created", b.create("p1", [], [{"title": "Needs two", "depends_on": [t1["id"], "GH-2", "I_77"]}]))
links = [c["b"] for c in calls("POST", "graphql") if c["b"].get("issue")]
ck("addBlockedBy once per dependency, in order, with node ids", [(c["issue"], c["blocker"]) for c in links]
   == [("I_4", "I_1"), ("I_4", "I_2"), ("I_4", "I_77")] and STATE["blocked"].get("I_4") == ["I_1", "I_2", "I_77"], links)
STATE["calls"].clear()
run(b.create("p1", [], [{"title": "Needs two", "depends_on": [t1["id"], "GH-2", "I_77"]}]))
ck("a retry finds the issue and adds no link again", [c for c in calls("POST", "graphql") if c["b"].get("issue")] == []
   and STATE["blocked"].get("I_4") == ["I_1", "I_2", "I_77"])
run(b.create("p1", [], [{"title": "Needs two", "depends_on": ["GH-1", "GH-2", "I_88"]}]))
ck("a retry with one more dependency adds only that one", STATE["blocked"]["I_4"] == ["I_1", "I_2", "I_77", "I_88"])
STATE["dup_link_error"] = True
STATE["blocked"]["I_4"] = []
STATE["calls"].clear()
run(b.create("p1", [], [{"title": "Needs two", "depends_on": ["GH-1"]}]))
STATE["blocked"]["I_4"] = ["I_1"]
ck("an already linked dependency GitHub reports is ignored", run(b.create("p1", [], [{
    "title": "Needs two", "depends_on": ["GH-1"]}]))[0].id == "I_4")
STATE["blocked"]["I_4"] = []
STATE["calls"].clear()
ref = run(b.create("p1", [], [{"title": "Fresh with dup", "depends_on": ["GH-1", "GH-1"]}]))[0]
ck("a dependency named twice is linked once", STATE["blocked"][ref.id] == ["I_1"])
ref = run(b.create("p1", [], [{"title": "No deps"}]))[0]
ck("no dependency, no GraphQL call", ref.id not in STATE["blocked"])
dep_ref = TicketRef("I_1", "u", "todo")
ref = run(b.create("p1", [], [{"title": "From refs", "depends_on": [dep_ref, {"id": "I_2"}, "", None]}]))[0]
ck("a TicketRef or a dict with an id is a dependency too, empty ones are skipped", STATE["blocked"][ref.id] == ["I_1", "I_2"], STATE["blocked"])
STATE["dup_link_error"] = False

print("== create: a milestone ==")
s, b = world()
ref = run(b.create("p1", [{"name": "Beta", "goal": "Second slice"}], []))[0]
ck("a milestone is made with its goal as the description", ref == TicketRef("MS_2", f"https://github.com/{REPO}/milestone/2", "")
   and STATE["milestones"][-1]["description"] == "Second slice", ref)
ck("create twice gives ONE milestone", run(b.create("p1", [{"name": "Beta"}], []))[0] == ref
   and len(STATE["milestones"]) == 2)
ck("an open milestone of the same title is found", run(b.create("p1", [{"name": "Alpha"}], []))[0].id == "MS_1"
   and len(STATE["milestones"]) == 2)
ck("a title only starting the same is another milestone", run(b.create("p1", [{"name": "Alph"}], []))[0].id == "MS_3")
STATE["milestones"] += [milestone(n, f"Bulk {n}") for n in range(10, 115)]
ref = run(b.create("p1", [{"name": "Bulk 114"}], []))[0]
ck("a milestone on the second page is found", ref.id == "MS_114" and len(STATE["milestones"]) == 108
   and any(c["q"].get("page") == "2" for c in calls("GET", "/milestones")), ref)
s, b = world(seed=False)
ck("no milestones yet: the first is made", run(b.create("p1", [{"name": "One"}], []))[0].id == "MS_1")

print("== create: refused ==")
s, b = world()
ck("two items in one call are refused", "one milestone or one ticket" in str(fails(b.create(
   "p1", [{"name": "a"}], [{"title": "b"}]), ValueError)))
ck("no item is refused", fails(b.create("p1", [], []), ValueError) is not False
   and fails(b.create("p1", None, None), ValueError) is not False)
ck("a ticket without a title is refused", "needs a title" in str(fails(b.create("p1", [], [{"title": "  "}]), ValueError)))
ck("a milestone without a name is refused", "needs a name" in str(fails(b.create("p1", [{}], []), ValueError)))
ck("a title that is too long is refused", "at most 256" in str(fails(b.create("p1", [], [{"title": "x" * 300}]), ValueError)))
before = len(STATE["issues"])
msg = fails(b.create("p1", [], [{"title": "Lost", "milestone": "Nope"}]), ValueError)
ck("a milestone GitHub does not have is a plain error and no issue is made", "no open milestone" in str(msg)
   and "Nope" in str(msg) and len(STATE["issues"]) == before, msg)
STATE["mode"] = "down"
msg = fails(b.create("p1", [], [{"title": "Down"}]), gt.GitHubTicketsError)
ck("GitHub refusing a create raises, and nothing is kept for a retry", msg and "503" in str(msg)
   and s.list_pending_writebacks("github") == [], msg)
fails(b.create("p1", [{"name": "Down"}], []), gt.GitHubTicketsError)
ck("a milestone create raises too", "503" in str(fails(b.create("p1", [{"name": "Down"}], []), gt.GitHubTicketsError)))
STATE["mode"] = "ok"
ck("a bare integration cannot create", fails(TicketsGithub().create("p1", [], [{"title": "x"}]), RuntimeError) is not False)

print("== update ==")
s, b = world()
STATE["milestones"].append(milestone(2, "Beta"))
t1 = tk(s, "GH-1")
run(b.update(t1, {"title": "Renamed"}))
ck("only the title is sent when only the title is given", [c["b"] for c in calls("PATCH", "/issues/1")] == [{"title": "Renamed"}]
   and _find(1)["title"] == "Renamed")
STATE["calls"].clear()
run(b.update(t1, {"body": "New text"}))
ck("the body alone", [c["b"] for c in calls("PATCH")] == [{"body": "New text"}] and _find(1)["body"] == "New text")
STATE["calls"].clear()
run(b.update(t1, {"milestone": "Beta"}))
ck("a milestone is found by its title and sent as its number", [c["b"] for c in calls("PATCH")] == [{"milestone": 2}]
   and _find(1)["milestone"]["title"] == "Beta")
STATE["calls"].clear()
run(b.update(t1, {"title": "Both", "milestone": "Alpha", "body": "b", "order": 5}))
ck("several fields in one write, and order is ignored", [c["b"] for c in calls("PATCH")] == [
    {"title": "Both", "milestone": 1, "body": "b"}] , STATE["calls"])
STATE["calls"].clear()
run(b.update(t1, {"milestone": ""}))
ck("an empty milestone takes the issue out of its milestone", [c["b"] for c in calls("PATCH")] == [{"milestone": None}]
   and _find(1)["milestone"] is None)
STATE["calls"].clear()
run(b.update(t1, {}))
run(b.update(t1, None))
run(b.update(t1, {"order": 3}))
ck("nothing to change, nothing sent", calls("PATCH") == [])
msg = fails(b.update(t1, {"milestone": "Nowhere"}), ValueError)
ck("a missing milestone is a plain error and nothing is sent", "no open milestone" in str(msg) and "Nowhere" in str(msg)
   and calls("PATCH") == [], msg)
ck("a field GitHub cannot change is refused", "status" in str(fails(b.update(t1, {"status": "done"}), ValueError)))
ck("an empty title is refused", fails(b.update(t1, {"title": ""}), ValueError) is not False)
ck("a Tares ticket is refused", fails(b.update(s.get_ticket("p1", s.create_ticket("p1", "tares", "M")), {"title": "x"}), ValueError) is not False)
STATE["mode"] = "down"
ck("GitHub refusing an update raises", "503" in str(fails(b.update(t1, {"title": "Again"}), gt.GitHubTicketsError)))
STATE["mode"] = "ok"

print("== the rate limit ==")
s, b = world()
STATE["limit"] = {"left": 2, "status": 403, "headers": {"retry-after": "7"}}
ref = (attempt("a 403 with Retry-After is waited out", b.create("p1", [], [{"title": "Slow"}])) or [None])[0]
ck("a 403 with Retry-After is waited out, then it works, one issue", ref is not None and ref.id == "I_4"
   and len([i for i in STATE["issues"] if i["title"] == "Slow"]) == 1, (ref, SLEEPS))
ck("it waited the time GitHub named, once per limited answer", SLEEPS == [7.0, 7.0], SLEEPS)
SLEEPS.clear()
STATE["limit"] = {"left": 1, "status": 429, "headers": {"retry-after": "3"}}
attempt("a limited create", b.create("p1", [], [{"title": "Slow 2"}]))
ck("a 429 is waited out too", SLEEPS == [3.0], SLEEPS)
SLEEPS.clear()
STATE["limit"] = {"left": 1, "status": 403, "headers": {"x-ratelimit-remaining": "0"}}
attempt("a limited create", b.create("p1", [], [{"title": "Slow 3"}]))
ck("a 403 with no requests left is waited out, for a minute at most", len(SLEEPS) == 1 and 1 <= SLEEPS[0] <= tg.MAX_WAIT, SLEEPS)
SLEEPS.clear()
STATE["limit"] = {"left": 1, "status": 403, "headers": {"retry-after": "900"}}
attempt("a limited create", b.create("p1", [], [{"title": "Slow 4"}]))
ck("a long wait is capped", SLEEPS == [float(tg.MAX_WAIT)], SLEEPS)
SLEEPS.clear()
STATE["limit"] = {"left": -1, "status": 403, "headers": {"retry-after": "2"}}
n_issues = len(STATE["issues"])
msg = fails(b.create("p1", [], [{"title": "Never"}]), gt.GitHubTicketsError)
ck("a limit that stays is a clear error after a few tries", "rate limit" in str(msg) and len(SLEEPS) == tg.RATE_RETRIES
   and len(STATE["issues"]) == n_issues, (msg, SLEEPS))
SLEEPS.clear()
STATE["limit"] = {"left": 1, "status": 403, "headers": {}}
msg = fails(b.create("p1", [], [{"title": "Plain 403"}]), gt.GitHubTicketsError)
ck("a plain 403 is not a rate limit: no waiting, and it says the credential cannot write", SLEEPS == []
   and "cannot write" in str(msg), (msg, SLEEPS))
STATE["limit"] = {"left": 0, "status": 403, "headers": {}}

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
