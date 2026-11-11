"""The spec session creates its milestones and tickets in Linear or GitHub Issues the same way (TR-851).

One set of MCP calls (link a project to Linear or GitHub, write_milestone, write_ticket) makes the
same plan in either: a milestone, a ticket, and a second ticket that depends on the first, and the
Tares rows to match. A fake Linear and a fake GitHub (the ones of test_tickets_linear and
test_tickets_github, trimmed) hold what was made, so the test counts what is really there.

Covered, for each of Linear and GitHub: linking through the PUT route; write_milestone and
write_ticket (the MCP tools, against the daemon in-process) make exactly one milestone and two
issues, the second blocked by the first; writing them all again makes nothing new, no extra
relation; the system's own sync afterwards leaves one Tares row per issue, matching the ref the
integration gave (external id, owner, identifier, url, dependencies, milestone); a sync that runs
before the row is adopted still leaves one row; a ticket made over the daemon's route; refusals
(a status, an assignee or a position on a new ticket, an unknown milestone, an unknown dependency)
make nothing in Linear or GitHub; a milestone written twice is one.

Run: python tests/test_spec_tickets_create.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-spec-tickets-create-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-spec-tickets"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import github_tickets as gt
from tares import linear
from tares.integrations.builtin import tickets_github as tg

logging.getLogger("httpx").setLevel(logging.WARNING)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── a fake Linear: the project, its teams, its milestones and issues, relations ────────────────
FIX = os.path.join(HERE, "fixtures", "linear")
with open(os.path.join(FIX, "project.json")) as f:
    PROJECT = json.load(f)
with open(os.path.join(FIX, "viewer.json")) as f:
    VIEWER = json.load(f)
GOOD_KEY = "lin_api_good"
LS: dict = {}


def lin_reset():
    LS.clear()
    LS.update(milestones=[], issues=[], relations=[], teams=[{"id": "tm1", "name": "Factory"}],
              serial=0, creates=[])


def lin_served(iss):
    return {**iss, "inverseRelations": {"nodes": [
        {"type": r["type"], "issue": {"id": r["from"]}} for r in LS["relations"]
        if r["to"] == iss["id"]]}}


def lin_mutate(q, v):
    inp = v.get("input") or {}
    op = next(n for n in ("issueRelationCreate", "projectMilestoneCreate", "issueCreate") if n in q)
    LS["creates"].append(op)
    if op == "issueRelationCreate":
        LS["relations"].append({"type": inp["type"], "from": inp["issueId"], "to": inp["relatedIssueId"]})
        return httpx.Response(200, json={"data": {op: {"success": True}}})
    if op == "projectMilestoneCreate":
        n = len(LS["milestones"]) + 1
        ms = {"id": f"m{n}", "name": inp["name"], "description": inp.get("description"),
              "sortOrder": n * 1000.0}
        LS["milestones"].append(ms)
        return httpx.Response(200, json={"data": {op: {"success": True, "projectMilestone": ms}}})
    LS["serial"] += 1
    n = LS["serial"]
    iss = lin_new_issue(f"n{n}", f"FAC-{3 + n}", inp["title"], inp.get("description"),
                        inp.get("projectMilestoneId"))
    return httpx.Response(200, json={"data": {op: {"success": True, "issue": iss}}})


def lin_new_issue(iid, ident, title, desc=None, ms=None):
    iss = {"id": iid, "identifier": ident, "title": title,
           "url": f"https://linear.app/acme/issue/{ident}", "sortOrder": 10.0 + len(LS["issues"]),
           "updatedAt": "2026-11-10T11:00:00Z", "description": desc,
           "state": {"name": "Todo", "type": "unstarted"}}
    if ms:
        iss["projectMilestone"] = {"id": ms}
    LS["issues"].append(iss)
    return iss


def lin_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != GOOD_KEY:
        return httpx.Response(400, json={"errors": [{"message": "Authentication required"}]})
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": VIEWER}})
    if "issues(" in q and "filter:" in q:
        nodes = [i for i in LS["issues"] if i["title"] == v["title"]]
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "issues(" in q:
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": [lin_served(i) for i in LS["issues"]],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "projectMilestones" in q:
        return httpx.Response(200, json={"data": {"project": {"projectMilestones": {
            "nodes": LS["milestones"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "teams(first" in q:
        return httpx.Response(200, json={"data": {"project": {"id": PROJECT["id"],
                                                               "teams": {"nodes": LS["teams"]}}}})
    if "inverseRelations" in q and "issue(id" in q:
        rels = [{"type": r["type"], "issue": {"id": r["from"]}} for r in LS["relations"]
                if r["to"] == v["id"]]
        return httpx.Response(200, json={"data": {"issue": {"id": v["id"], "inverseRelations": {
            "nodes": rels, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if q.lstrip().startswith("mutation") and "$input" in q:
        return lin_mutate(q, v)
    if "project(id" in q:
        return httpx.Response(200, json={"data": {"project": PROJECT}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


linear._transport = httpx.MockTransport(lin_handler)

# ── a fake GitHub: the repo, its labels, milestones, issues and blocked-by links ───────────────
REPO = "acme/app"
LABEL = "tares:spec"
GS: dict = {}


def gh_reset():
    GS.clear()
    GS.update(repo={"full_name": REPO, "permissions": {"push": True}}, issues=[], milestones=[],
              labels={LABEL.lower()}, blocked={}, creates=[])


def gh_issue(n, title, milestone=None, labels=(LABEL,)):
    return {"number": n, "node_id": f"I_{n}", "title": title, "body": "", "state": "open",
            "state_reason": None, "labels": [{"name": x} for x in labels], "milestone": milestone,
            "html_url": f"https://github.com/{REPO}/issues/{n}"}


def gh_graphql(request):
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    if "addBlockedBy" in q:
        GS["creates"].append("addBlockedBy")
        GS["blocked"].setdefault(v["issue"], []).append(v["blocker"])
        return httpx.Response(200, json={"data": {"addBlockedBy": {"clientMutationId": None}}})
    if "node(id" in q:
        return httpx.Response(200, json={"data": {"node": {"id": v["id"], "blockedBy": {"nodes": [
            {"id": b} for b in GS["blocked"].get(v["id"], [])]}}}})
    want = [i for i in GS["issues"] if v["label"].lower() in [x["name"].lower() for x in i["labels"]]]
    ms = [{"id": x["node_id"], "number": x["number"], "title": x["title"], "description": ""}
          for x in GS["milestones"]]
    nodes = [{"id": i["node_id"], "number": i["number"], "title": i["title"], "url": i["html_url"],
              "state": "OPEN", "stateReason": None, "labels": {"nodes": list(i["labels"])},
              "milestone": {"id": i["milestone"]["node_id"]} if i["milestone"] else None,
              "assignees": {"nodes": []},
              "blockedBy": {"nodes": [{"id": b} for b in GS["blocked"].get(i["node_id"], [])]}}
             for i in want]
    return httpx.Response(200, json={"data": {"repository": {
        "nameWithOwner": REPO, "url": f"https://github.com/{REPO}",
        "milestones": {"nodes": ms, "pageInfo": {"hasNextPage": False}},
        "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})


def gh_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization", "").removeprefix("Bearer ") != "tok-a":
        return httpx.Response(401, json={"message": "Bad credentials"})
    path, method = request.url.path, request.method
    if path == "/graphql":
        return gh_graphql(request)
    base = f"/repos/{REPO}"
    if not path.startswith(base):
        return httpx.Response(404, json={"message": "Not Found"})
    rel = path[len(base):]
    params = dict(request.url.params)
    body = json.loads(request.content) if request.content else None
    if rel == "" and method == "GET":
        return httpx.Response(200, json=GS["repo"])
    if rel == "/issues" and method == "GET":
        want = params.get("labels", "").lower()
        items = [i for i in GS["issues"] if not want or want in [x["name"].lower() for x in i["labels"]]]
        page, per = int(params.get("page", 1)), int(params.get("per_page", 30))
        return httpx.Response(200, json=items[(page - 1) * per: page * per])
    if rel == "/issues" and method == "POST":
        GS["creates"].append("issue")
        n = len(GS["issues"]) + 1
        ms = next((x for x in GS["milestones"] if x["number"] == body.get("milestone")), None)
        i = gh_issue(n, body["title"], milestone=ms, labels=tuple(body.get("labels") or ()))
        i["body"] = body.get("body") or ""
        GS["issues"].append(i)
        return httpx.Response(201, json=i)
    if re.fullmatch(r"/labels/(.+)", rel) and method == "GET":
        if rel.split("/", 2)[2].lower() in GS["labels"]:
            return httpx.Response(200, json={"name": rel.split("/", 2)[2]})
        return httpx.Response(404, json={"message": "Not Found"})
    if rel == "/labels" and method == "POST":
        GS["labels"].add(body["name"].lower())
        return httpx.Response(201, json={"name": body["name"]})
    if rel == "/milestones" and method == "GET":
        page, per = int(params.get("page", 1)), int(params.get("per_page", 30))
        return httpx.Response(200, json=[{**x, "state": "open"} for x in GS["milestones"]][
            (page - 1) * per: page * per])
    if rel == "/milestones" and method == "POST":
        GS["creates"].append("milestone")
        n = len(GS["milestones"]) + 1
        ms = {"number": n, "node_id": f"MS_{n}", "title": body["title"], "state": "open",
              "description": body.get("description") or "",
              "html_url": f"https://github.com/{REPO}/milestone/{n}"}
        GS["milestones"].append(ms)
        return httpx.Response(201, json=ms)
    return httpx.Response(404, json={"message": "Not Found"})


async def _no_sleep(seconds):
    return None

gt._transport = httpx.MockTransport(gh_handler)
tg._sleep = _no_sleep


# ── what differs between the two, so one scenario runs against both ────────────────────────────
class Linear:
    kind = "linear"
    link_fields = {"project": "https://linear.app/acme/project/invoicing-ab12cd34ef56"}

    @staticmethod
    def reset(store):
        lin_reset()
        linear._save_connection(store, {"kind": "api_key", "token": GOOD_KEY,
                                        "account": {"name": "Ada", "org": "Acme"}})

    @staticmethod
    def milestones():
        return [x["name"] for x in LS["milestones"]]

    @staticmethod
    def issues():
        return [i["title"] for i in LS["issues"]]

    @staticmethod
    def ext_id(title):
        return next(i["id"] for i in LS["issues"] if i["title"] == title)

    @staticmethod
    def ms_ext_id(name):
        return next(x["id"] for x in LS["milestones"] if x["name"] == name)

    @staticmethod
    def blockers(title):
        me = Linear.ext_id(title)
        return sorted(next(i["title"] for i in LS["issues"] if i["id"] == r["from"])
                      for r in LS["relations"] if r["to"] == me and r["type"] == "blocks")

    @staticmethod
    def relations():
        return len(LS["relations"])

    @staticmethod
    def made():
        return len(LS["creates"])

    @staticmethod
    def make_ms_directly(name):
        n = len(LS["milestones"]) + 1
        LS["milestones"].append({"id": f"m{n}", "name": name, "description": "", "sortOrder": n * 1000.0})

    @staticmethod
    def make_directly(title, ms=None):
        lin_new_issue(f"d{len(LS['issues']) + 1}", f"FAC-{90 + len(LS['issues'])}", title,
                      ms=Linear.ms_ext_id(ms) if ms else None)

    @staticmethod
    async def sync(store, uid):
        await linear.sync_project(store, uid)


class Github:
    kind = "github"
    link_fields = {"repo": REPO, "issue_label": LABEL}

    @staticmethod
    def reset(store):
        gh_reset()
        store.upsert_github_credential("cred-a", "tok-a")

    @staticmethod
    def milestones():
        return [x["title"] for x in GS["milestones"]]

    @staticmethod
    def issues():
        return [i["title"] for i in GS["issues"]]

    @staticmethod
    def ext_id(title):
        return next(i["node_id"] for i in GS["issues"] if i["title"] == title)

    @staticmethod
    def ms_ext_id(name):
        return next(x["node_id"] for x in GS["milestones"] if x["title"] == name)

    @staticmethod
    def blockers(title):
        me = Github.ext_id(title)
        return sorted(next(i["title"] for i in GS["issues"] if i["node_id"] == b)
                      for b in GS["blocked"].get(me, []))

    @staticmethod
    def relations():
        return sum(len(v) for v in GS["blocked"].values())

    @staticmethod
    def made():
        return len(GS["creates"])

    @staticmethod
    def make_ms_directly(name):
        n = len(GS["milestones"]) + 1
        GS["milestones"].append({"number": n, "node_id": f"MS_{n}", "title": name, "state": "open",
                                 "description": "", "html_url": f"https://github.com/{REPO}/milestone/{n}"})

    @staticmethod
    def make_directly(title, ms=None):
        GS["issues"].append(gh_issue(len(GS["issues"]) + 1, title,
                                     milestone=next(x for x in GS["milestones"] if x["title"] == ms)
                                     if ms else None))

    @staticmethod
    async def sync(store, uid):
        await gt.sync_project(store, uid)


def rows(store, uid):
    return {t["title"]: t for t in store.list_tickets(uid)}


def row_count(store, uid):
    """Raw, on the state store (store.sc: DuckDB, or Postgres), under its read lock: the rows, and
    how many carry an external id twice."""
    with store._sread:
        n = store.sc.execute("SELECT count(*) FROM tickets WHERE project = ?", [uid]).fetchone()[0]
        dup = store.sc.execute("SELECT count(*) - count(DISTINCT external_id) FROM tickets "
                               "WHERE project = ? AND external_id IS NOT NULL", [uid]).fetchone()[0]
    return n, dup


async def scenario(b, app, cx, store):
    k = b.kind
    print(f"== {k}: link, milestone, two tickets ==")
    b.reset(store)
    uid = json.loads(await m.create_project(f"Invoices in {k}", "Turn invoices into rows"))["id"]
    r = await cx.put(f"/api/projects/{uid}/tickets/link",
                     json={"integration": k, "link_fields": b.link_fields})
    ck(f"{k}: linking answers 200 and starts empty", r.status_code == 200 and r.json()["tickets"] == [], r.text)
    ck(f"{k}: nothing was made by linking", b.made() == 0 and b.issues() == [] and b.milestones() == [])

    ms = json.loads(await mf.write_milestone("Alpha", "The first cut",
                                             [{"check": "make e2e", "expect": "passes"}], project=uid))
    ck(f"{k}: write_milestone makes one milestone there", b.milestones() == ["Alpha"], b.milestones())
    ck(f"{k}: and answers with its Tares row, checks kept", ms["name"] == "Alpha" and ms["goal"] == "The first cut"
       and ms["checks"] == [{"check": "make e2e", "expect": "passes"}], ms)
    mrow = store.get_milestone(uid, "Alpha")
    ck(f"{k}: the Tares milestone is {k}'s, with its external id",
       mrow["owner"] == k and mrow["external_id"] == b.ms_ext_id("Alpha"), mrow)

    a = json.loads(await m.write_ticket("Parse invoices", milestone="Alpha", repo="acme/app", project=uid))
    bt = json.loads(await m.write_ticket("Store invoices", milestone="Alpha", depends_on=[a["id"]], project=uid))
    ck(f"{k}: exactly one milestone and two issues", b.milestones() == ["Alpha"]
       and sorted(b.issues()) == ["Parse invoices", "Store invoices"], (b.milestones(), b.issues()))
    ck(f"{k}: the second is blocked by the first, the first by nothing",
       b.blockers("Store invoices") == ["Parse invoices"] and b.blockers("Parse invoices") == [],
       (b.blockers("Store invoices"), b.blockers("Parse invoices")))
    ck(f"{k}: one relation in all", b.relations() == 1, b.relations())
    ck(f"{k}: the answers carry the milestone, the dependency and the repo",
       a["milestone_name"] == "Alpha" and a["repo"] == "acme/app"
       and bt["depends_on"] == [a["label"]], (a, bt))
    r1 = rows(store, uid)
    ck(f"{k}: two Tares rows, owned by {k}, matching the refs",
       len(r1) == 2 and all(t["owner"] == k for t in r1.values())
       and r1["Parse invoices"]["external_id"] == b.ext_id("Parse invoices")
       and r1["Store invoices"]["external_id"] == b.ext_id("Store invoices")
       and all(t["identifier"] and t["url"] for t in r1.values()), r1)
    ck(f"{k}: the label of a ticket is its identifier", a["label"] == r1["Parse invoices"]["identifier"], a)
    ck(f"{k}: the row has its dependency and milestone",
       r1["Store invoices"]["depends_on"] == [r1["Parse invoices"]["id"]]
       and r1["Parse invoices"]["milestone"] == mrow["id"] and r1["Store invoices"]["milestone"] == mrow["id"], r1)
    made = b.made()

    print(f"== {k}: the same writes again ==")
    ms2 = json.loads(await mf.write_milestone("Alpha", "The first cut", project=uid))
    a2 = json.loads(await m.write_ticket("Parse invoices", milestone="Alpha", repo="acme/app", project=uid))
    b2 = json.loads(await m.write_ticket("Store invoices", milestone="Alpha", depends_on=[a["id"]], project=uid))
    ck(f"{k}: still one milestone and two issues", b.milestones() == ["Alpha"] and len(b.issues()) == 2,
       (b.milestones(), b.issues()))
    ck(f"{k}: no extra relation, and nothing created at all", b.relations() == 1 and b.made() == made,
       (b.relations(), b.made(), made))
    ck(f"{k}: the same Tares rows and ids answer", a2["id"] == a["id"] and b2["id"] == bt["id"]
       and ms2["id"] == ms["id"] and row_count(store, uid) == (2, 0)
       and len(store.list_milestones(uid)) == 1, (a2, b2, row_count(store, uid)))
    ck(f"{k}: the checks written first are still there", store.get_milestone(uid, "Alpha")["checks"]
       == [{"check": "make e2e", "expect": "passes"}], store.get_milestone(uid, "Alpha"))

    print(f"== {k}: the system's own sync afterwards ==")
    await b.sync(store, uid)
    r3 = rows(store, uid)
    ck(f"{k}: one Tares row per issue, the same ones", row_count(store, uid) == (2, 0)
       and r3["Parse invoices"]["id"] == a["id"] and r3["Store invoices"]["id"] == bt["id"], (row_count(store, uid), r3))
    ck(f"{k}: external id is the ref's, owner is {k}, identifier and url set",
       all(r3[t]["external_id"] == b.ext_id(t) and r3[t]["owner"] == k and r3[t]["identifier"]
           and r3[t]["url"] for t in r3), r3)
    ck(f"{k}: dependencies and milestone survive the sync",
       r3["Store invoices"]["depends_on"] == [a["id"]] and r3["Parse invoices"]["depends_on"] == []
       and r3["Store invoices"]["milestone"] == mrow["id"], r3)
    ck(f"{k}: one milestone row, still the adopted one with its checks",
       [x["id"] for x in store.list_milestones(uid)] == [mrow["id"]]
       and store.get_milestone(uid, "Alpha")["checks"] == [{"check": "make e2e", "expect": "passes"}],
       store.list_milestones(uid))
    await b.sync(store, uid)
    await m.write_ticket("Store invoices", milestone="Alpha", depends_on=[a["id"]], project=uid)
    ck(f"{k}: a second sync and a write after it change nothing", row_count(store, uid) == (2, 0)
       and b.relations() == 1 and len(b.issues()) == 2, (row_count(store, uid), b.relations()))

    print(f"== {k}: a sync that runs before the row is adopted ==")
    b.make_directly("Email invoices")          # made in the tracker, a sync reads it first
    await b.sync(store, uid)
    ck(f"{k}: the sync gave it a row", row_count(store, uid) == (3, 0) and "Email invoices" in rows(store, uid))
    pre = rows(store, uid)["Email invoices"]["id"]
    c = json.loads(await m.write_ticket("Email invoices", milestone="Alpha", depends_on=[bt["id"]], project=uid))
    ck(f"{k}: write_ticket finds the issue and the row: one of each", b.issues().count("Email invoices") == 1
       and row_count(store, uid) == (3, 0) and c["id"] == pre, (c, row_count(store, uid), b.issues()))
    rc = rows(store, uid)["Email invoices"]
    ck(f"{k}: the row took the milestone and dependency, and the issue got the link",
       rc["milestone"] == mrow["id"] and rc["depends_on"] == [bt["id"]]
       and b.blockers("Email invoices") == ["Store invoices"], (rc, b.blockers("Email invoices")))

    print(f"== {k}: a ticket over the daemon's route ==")
    r = await cx.post(f"/api/projects/{uid}/tickets",
                      json={"title": "Chart invoices", "milestone": "Alpha", "depends_on": [c["id"]]})
    out = r.json()
    ck(f"{k}: POST /tickets creates it in {k} and answers its row",
       r.status_code == 201 and "Chart invoices" in b.issues() and out["owner"] == k
       and out["identifier"] and out["url"] and out["depends_on"] == [c["label"]]
       and rows(store, uid)["Chart invoices"]["external_id"] == b.ext_id("Chart invoices"), (r.status_code, r.text))
    r = await cx.post(f"/api/projects/{uid}/milestones", json={"name": "Beta", "goal": "Reports"})
    ck(f"{k}: POST /milestones creates it in {k}", r.status_code == 201 and b.milestones() == ["Alpha", "Beta"],
       (r.status_code, r.text, b.milestones()))
    r = await cx.post(f"/api/projects/{uid}/milestones", json={"name": "beta", "goal": "Reports"})
    ck(f"{k}: and the same name in another case finds it", r.status_code in (200, 201) and b.milestones() == ["Alpha", "Beta"]
       and len(store.list_milestones(uid)) == 2, (r.status_code, r.text, b.milestones()))
    await b.sync(store, uid)
    ck(f"{k}: a sync after that keeps four rows, two milestones", row_count(store, uid) == (4, 0)
       and len(store.list_milestones(uid)) == 2, (row_count(store, uid), store.list_milestones(uid)))

    print(f"== {k}: refusals make nothing ==")
    before = (list(b.issues()), list(b.milestones()), b.made(), b.relations(), row_count(store, uid))

    def same():
        return (list(b.issues()), list(b.milestones()), b.made(), b.relations(), row_count(store, uid)) == before
    for label, kw, code in (("a status", {"status": "done"}, "409"), ("an assignee", {"assignee": "a@x.com"}, "409"),
                            ("a position", {"position": 2.0}, "409"),
                            ("an unknown milestone", {"milestone": "Nowhere"}, "404"),
                            ("an unknown dependency", {"depends_on": ["T99"]}, "4")):
        out = await m.write_ticket("Refused ticket", project=uid, **kw)
        ck(f"{k}: {label} on a new ticket is refused ({code}xx), and nothing was made",
           out.startswith("error " + code) and same(), (out, before))
    r = await cx.post(f"/api/projects/{uid}/tickets", json={"title": "Refused ticket", "status": "done"})
    ck(f"{k}: the route says 409 for a status, and names {k}", r.status_code == 409
       and ("Linear" if k == "linear" else "GitHub") in r.text and same(), (r.status_code, r.text))
    r = await cx.post(f"/api/projects/{uid}/tickets", json={"title": "   "})
    ck(f"{k}: an empty title is refused and nothing was made", r.status_code == 400 and same(), (r.status_code, r.text))
    return uid


async def milestone_scenario(b, cx, store):
    k = b.kind
    print(f"== {k}: milestones that exist before write_milestone does ==")
    b.reset(store)
    uid = json.loads(await m.create_project(f"Milestones in {k}", "Where each milestone starts"))["id"]
    early = json.loads(await mf.write_milestone("Draft", "Written before linking",
                                                [{"check": "make lint", "expect": "passes"}], project=uid))
    r = await cx.put(f"/api/projects/{uid}/tickets/link",
                     json={"integration": k, "link_fields": b.link_fields})
    ck(f"{k}: a Tares milestone written before linking stays, owned by Tares", r.status_code == 200
       and store.get_milestone(uid, "Draft")["owner"] == "tares" and b.milestones() == [],
       (r.status_code, r.text, b.milestones()))
    again = json.loads(await mf.write_milestone("Draft", project=uid))
    row = store.get_milestone(uid, "Draft")
    ck(f"{k}: writing it again makes it in {k} once and the same Tares row takes it over",
       b.milestones() == ["Draft"] and again["id"] == early["id"] and len(store.list_milestones(uid)) == 1
       and row["owner"] == k and row["external_id"] == b.ms_ext_id("Draft"), (b.milestones(), again, row))
    ck(f"{k}: the goal and checks written before are kept", row["goal"] == "Written before linking"
       and row["checks"] == [{"check": "make lint", "expect": "passes"}], row)
    json.loads(await mf.write_milestone("Draft", project=uid))
    ck(f"{k}: and once more changes nothing", b.milestones() == ["Draft"] and len(store.list_milestones(uid)) == 1,
       (b.milestones(), store.list_milestones(uid)))

    b.make_ms_directly("Shared")       # made in the tracker by someone, not read into Tares yet
    made = b.made()
    got = json.loads(await mf.write_milestone("Shared", "Agreed with the team",
                                              [{"check": "make e2e", "expect": "passes"}], project=uid))
    row = store.get_milestone(uid, "Shared")
    ck(f"{k}: a milestone that is already in {k} is found, not made again", b.milestones() == ["Draft", "Shared"]
       and b.made() == made, (b.milestones(), b.made(), made))
    ck(f"{k}: and it has one Tares row of {k}'s, with the goal and checks", len(store.list_milestones(uid)) == 2
       and row["owner"] == k and row["external_id"] == b.ms_ext_id("Shared")
       and got["id"] == row["id"] and row["goal"] == "Agreed with the team"
       and row["checks"] == [{"check": "make e2e", "expect": "passes"}], (got, row))
    await b.sync(store, uid)
    ck(f"{k}: a sync afterwards keeps two milestone rows", len(store.list_milestones(uid)) == 2
       and store.get_milestone(uid, "Shared")["id"] == row["id"], store.list_milestones(uid))
    return uid


async def race_scenario(b, cx, store):
    """A sync that has read the Tares rows when write_ticket / write_milestone adopt the same
    issue and milestone, and inserts afterwards: it must find them, not add a second row."""
    k = b.kind
    print(f"== {k}: a sync racing a write ==")
    b.reset(store)
    uid = json.loads(await m.create_project(f"Race in {k}", "A sync and a write at once"))["id"]
    r = await cx.put(f"/api/projects/{uid}/tickets/link",
                     json={"integration": k, "link_fields": b.link_fields})
    ck(f"{k}: linked", r.status_code == 200, r.text)
    b.make_ms_directly("Beta")
    b.make_directly("Race issue", ms="Beta")       # in the tracker, in the milestone, not read yet
    adopted = {}

    def after_first_read(name, fire):
        orig = getattr(store, name)
        state = {"fired": False}

        def wrapper(*a, **kw):
            out = orig(*a, **kw)
            if not state["fired"]:
                state["fired"] = True
                fire()
            return out
        setattr(store, name, wrapper)
        return name, orig

    def adopt_milestone():
        adopted["ms"] = store.adopt_milestone(uid, k, b.ms_ext_id("Beta"), "Beta", "Written by the session",
                                              [{"check": "make e2e", "expect": "passes"}])

    def adopt_ticket():
        adopted["t"] = store.adopt_ticket(uid, k, b.ext_id("Race issue"), "Race issue", "todo", "RACE-1",
                                          "https://example.test/race-1", repo="acme/app")
    hooks = [after_first_read("list_milestones", adopt_milestone), after_first_read("list_tickets", adopt_ticket)]
    try:
        await b.sync(store, uid)
    finally:
        for name, orig in hooks:
            setattr(store, name, orig)
    ck(f"{k}: both adoptions ran between the sync's read and its insert", set(adopted) == {"ms", "t"}, adopted)

    def check(label):
        t = [x for x in store.list_tickets(uid) if x["title"] == "Race issue"]
        ms = [x for x in store.list_milestones(uid) if x["name"] == "Beta"]
        ck(f"{k}: {label}: one row for the issue, the one the write made", len(t) == 1
           and t[0]["id"] == adopted["t"] and t[0]["owner"] == k and t[0]["external_id"] == b.ext_id("Race issue")
           and row_count(store, uid) == (1, 0) and t[0]["repo"] == "acme/app", (t, row_count(store, uid)))
        ck(f"{k}: {label}: one row for the milestone, the one the write made, with its goal and checks",
           len(store.list_milestones(uid)) == 1 and len(ms) == 1 and ms[0]["id"] == adopted["ms"]
           and ms[0]["owner"] == k and ms[0]["external_id"] == b.ms_ext_id("Beta")
           and ms[0]["goal"] == "Written by the session"
           and ms[0]["checks"] == [{"check": "make e2e", "expect": "passes"}], ms)
        ck(f"{k}: {label}: the ticket sits in that milestone", t and t[0]["milestone"] == adopted["ms"], t)
    check("after the sync")
    await b.sync(store, uid)
    check("after a second sync")
    return uid


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = lambda timeout=10: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root)
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root)
        lin_uid = await scenario(Linear, app, cx, store)
        gh_uid = await scenario(Github, app, cx, store)
        await cx.delete(f"/api/projects/{lin_uid}/linear")    # a repo and a label serve one project
        await cx.delete(f"/api/projects/{gh_uid}/github")
        lin_uid = await milestone_scenario(Linear, cx, store)
        await cx.delete(f"/api/projects/{lin_uid}/linear")
        gh_uid = await milestone_scenario(Github, cx, store)
        await cx.delete(f"/api/projects/{gh_uid}/github")
        lin_uid = await race_scenario(Linear, cx, store)
        await cx.delete(f"/api/projects/{lin_uid}/linear")
        await race_scenario(Github, cx, store)

        print("== a Tares project is untouched ==")
        lin_reset()
        gh_reset()
        pid = json.loads(await m.create_project("Plain plan"))["id"]
        t = json.loads(await m.write_ticket("Local ticket", status="in_progress", project=pid))
        ms = json.loads(await mf.write_milestone("Local", "Stays here", [], project=pid))
        ck("a ticket and a milestone of a Tares project are made in Tares, with a status and a number",
           t["label"] == "T1" and t["status"] == "in_progress" and ms["name"] == "Local"
           and store.get_milestone(pid, "Local")["owner"] == "tares" and Linear.made() == 0 and Github.made() == 0, (t, ms))


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
