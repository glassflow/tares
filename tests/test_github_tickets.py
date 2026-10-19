"""GitHub Issues as a project's tickets (TR-568): the link on a project and the sync that keeps its
tickets current.

Runs against a fake GitHub (httpx.MockTransport answering like its GraphQL endpoint), never the
live API. Covers the first sync (tickets GH-n in issue order), the status rules (closed reasons,
the "in progress" issue label, reopened), GitHub milestones used by an issue (created, adopted,
ordered, unused ones left out), blocked-by as dependencies, an issue losing the issue label, the
sync changing nothing when the fetch is incomplete (repository gone, no issues while tickets are
open, a failed second page), the rate limit backoff, trying the stored credentials in turn and
remembering the one that works, the default issue label and the events on `github_issues`.

Run: .venv/bin/python tests/test_github_tickets.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from tares import github_tickets as gt
from tares.envelope import now_utc
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake GitHub ──────────────────────────────────────────────────────────
LABEL = "tares:p"


def issue(n, title, state="OPEN", reason=None, labels=(LABEL,), milestone=None, blocked_by=()):
    return {"id": f"I_{n}", "number": n, "title": title, "url": f"https://github.com/acme/app/issues/{n}",
            "state": state, "stateReason": reason, "labels": list(labels),
            "milestone": milestone, "blockedBy": list(blocked_by)}


STATE: dict = {}


def reset():
    STATE.clear()
    STATE.update({
        "tokens": {"tok-a": "ok"},          # token -> ok | not_found | forbidden | unauthorized
        "page_size": 100,
        "calls": [],                        # (token, after) for every request
        "repo_null": False,                 # repository: null, no errors
        "fail_page_2": False,               # HTTP 502 on any page after the first
        "null_page_2": False,               # repository: null on any page after the first
        "http_rate_limit": False,           # HTTP 403 with x-ratelimit-remaining: 0
        "gql_rate_limit": False,            # a RATE_LIMITED GraphQL error
        "milestones": [
            {"id": "M_alpha", "number": 2, "title": "Alpha", "description": "First  slice\nof work"},
            {"id": "M_beta", "number": 1, "title": "Beta", "description": "Second slice"},
            {"id": "M_gamma", "number": 3, "title": "Gamma", "description": "Nobody uses this"},
        ],
        "issues": [
            issue(1, "Parse invoices", milestone="M_alpha"),
            issue(2, "Store invoices", milestone="M_alpha", blocked_by=["I_1"]),
            issue(3, "Email invoices", milestone="M_beta", blocked_by=["I_1", "I_outside", "I_3"]),
            issue(4, "Retry failed emails"),
            issue(9, "Not ours", labels=("bug",)),
        ],
    })


def handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != gt.API_URL:
        return httpx.Response(404)
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    v = json.loads(request.content)["variables"]
    STATE["calls"].append((token, v.get("after")))
    mode = STATE["tokens"].get(token)
    if mode is None or mode == "unauthorized":
        return httpx.Response(401, json={"message": "Bad credentials"})
    if STATE["http_rate_limit"]:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "limit"})
    if STATE["gql_rate_limit"]:
        return httpx.Response(200, json={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit"}]})
    if mode == "not_found":
        return httpx.Response(200, json={"data": {"repository": None}, "errors": [
            {"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]})
    if mode == "forbidden":
        return httpx.Response(200, json={"data": {"repository": None}, "errors": [
            {"type": "FORBIDDEN", "message": "Resource not accessible by integration"}]})
    if STATE["repo_null"]:
        return httpx.Response(200, json={"data": {"repository": None}})
    if v.get("after") and STATE["fail_page_2"]:
        return httpx.Response(502, text="bad gateway")
    if v.get("after") and STATE["null_page_2"]:
        return httpx.Response(200, json={"data": {"repository": None}})
    want = [i for i in STATE["issues"] if v["label"].lower() in [l.lower() for l in i["labels"]]]
    start = int(v["after"]) if v.get("after") else 0
    size = STATE["page_size"]
    page = want[start:start + size]
    more = start + size < len(want)
    nodes = [{"id": i["id"], "number": i["number"], "title": i["title"], "url": i["url"],
              "state": i["state"], "stateReason": i["stateReason"],
              "labels": {"nodes": [{"name": l} for l in i["labels"]]},
              "milestone": {"id": i["milestone"]} if i["milestone"] else None,
              "blockedBy": {"nodes": [{"id": b} for b in i["blockedBy"]]}} for i in page]
    return httpx.Response(200, json={"data": {
        "rateLimit": {"remaining": 4999, "resetAt": "2026-10-19T16:00:00Z"},
        "repository": {
            "nameWithOwner": "acme/app", "url": "https://github.com/acme/app",
            "milestones": {"nodes": STATE["milestones"], "pageInfo": {"hasNextPage": False}},
            "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": more,
                                                    "endCursor": str(start + size) if more else None}}}}})


gt._transport = httpx.MockTransport(handler)


def gh(n):
    return next(i for i in STATE["issues"] if i["number"] == n)


def snapshot(store, uid):
    return (store.list_tickets(uid), [(m["id"], m["name"], m["position"]) for m in store.list_milestones(uid)])


def byid(store, uid):
    return {t["identifier"]: t for t in store.list_tickets(uid)}


async def sync(store, uid):
    try:
        return await gt.sync_project(store, uid), None
    except gt.GitHubTicketsError as e:
        return None, e


async def main():
    reset()
    store = Store(os.path.join(tempfile.mkdtemp(prefix="tares-gh-tickets-"), "t.duckdb"))
    store.upsert_github_credential("cred-a", "tok-a")
    uid = "uc_gh"
    store.create_project(uid, "custom", "Invoices", {"objects": []})
    ck("a project starts with no GitHub link, tickets with Tares", store.get_project_github(uid) is None
       and store.project_ticket_home(uid) is None)
    link = {"repo": "acme/app", "issue_label": LABEL, "url": "https://github.com/acme/app"}
    store.set_project_github(uid, link)
    ck("the link is stored and read back", store.get_project_github(uid) == link
       and store.github_projects() == [(uid, link)] and store.project_ticket_home(uid) == "github")
    # a Tares milestone written before the link, named like a GitHub milestone
    adopted = store.create_milestone(uid, "Beta", "my own goal", [{"text": "my check"}])

    print("== (a) the first sync ==")
    envs, err = await sync(store, uid)
    t = byid(store, uid)
    ck("(a) GH-1..GH-4 created, the unlabeled issue left out",
       err is None and sorted(t) == ["GH-1", "GH-2", "GH-3", "GH-4"], (err, sorted(t)))
    ck("(a) in issue number order", [x["identifier"] for x in store.list_tickets(uid)]
       == ["GH-1", "GH-2", "GH-3", "GH-4"])
    ck("(a) owner github, url and external_id from the issue",
       all(x["owner"] == "github" and x["url"] == f"https://github.com/acme/app/issues/{x['identifier'][3:]}"
           and x["external_id"] == f"I_{x['identifier'][3:]}" for x in t.values()), t)
    ck("(a) position is the issue number", [x["position"] for x in store.list_tickets(uid)]
       == [1.0, 2.0, 3.0, 4.0])
    ck("(a) every ticket starts todo", {x["status"] for x in t.values()} == {"todo"})
    lk = store.get_project_github(uid)
    ck("(a) the link records the sync and the credential",
       lk["synced_at"] and lk["error"] is None and lk["issues"] == 4 and lk["credential"] == "cred-a"
       and lk["backoff_until"] is None and lk["repo"] == "acme/app", lk)

    print("== (l) events ==")
    ck("(l) one ticket_created per ticket", sorted(e.event_type for e in envs) == ["ticket_created"] * 4)
    ck("(l) on the github_issues source with the ticket label GH-n",
       all(e.source == "github_issues" and e.source_type == "event_stream"
           and e.labels["ticket"] == e.key_value and e.key_value.startswith("GH-") for e in envs)
       and sorted(e.labels["ticket"] for e in envs) == ["GH-1", "GH-2", "GH-3", "GH-4"])
    ck("(l) project and repo labels, issue label in the payload",
       all(e.labels["project"] == "Invoices" and e.labels["repo"] == "acme/app"
           and e.payload["repo"] == "acme/app" and e.payload["issue_label"] == LABEL for e in envs))
    ck("(l) the source is made once", gt.ensure_source(store) is True and gt.ensure_source(store) is False
       and any(s["name"] == "github_issues" for s in store.list_catalog_sources()))
    ck("a second sync with nothing new emits nothing", await gt.sync_project(store, uid) == [])

    print("== (c) milestones ==")
    ms = store.list_milestones(uid)
    ck("(c) the used milestones, in GitHub number order", [m["name"] for m in ms] == ["Beta", "Alpha"], ms)
    ck("(c) owner github, external_id the GitHub id", {m["owner"] for m in ms} == {"github"}
       and {m["external_id"] for m in ms} == {"M_alpha", "M_beta"})
    alpha = next(m for m in ms if m["name"] == "Alpha")
    ck("(c) the goal comes from the description", alpha["goal"] == "First slice of work", alpha["goal"])
    beta = next(m for m in ms if m["name"] == "Beta")
    ck("(c) a same-named Tares milestone is adopted, with its goal and checks",
       beta["id"] == adopted and beta["goal"] == "my own goal" and beta["checks"] == [{"text": "my check"}]
       and beta["owner"] == "github" and beta["position"] == 1.0, beta)
    ck("(c) an unused GitHub milestone is not created", "Gamma" not in [m["name"] for m in ms])
    ck("(c) tickets point at their milestone",
       t["GH-1"]["milestone"] == alpha["id"] and t["GH-2"]["milestone"] == alpha["id"]
       and t["GH-3"]["milestone"] == beta["id"] and t["GH-4"]["milestone"] is None)

    print("== (d) blocked by ==")
    ck("(d) blocked-by between labeled issues is depends_on",
       t["GH-2"]["depends_on"] == [t["GH-1"]["id"]], t["GH-2"]["depends_on"])
    ck("(d) a blocker outside the set, and an issue blocking itself, are ignored",
       t["GH-3"]["depends_on"] == [t["GH-1"]["id"]] and t["GH-1"]["depends_on"] == [], t["GH-3"]["depends_on"])

    print("== (b) status ==")
    iss = STATE["issues"]
    gh(1).update(state="CLOSED", stateReason="COMPLETED")
    gh(2).update(state="CLOSED", stateReason="NOT_PLANNED")
    gh(3).update(state="CLOSED", stateReason="DUPLICATE")
    gh(4).update(state="CLOSED", stateReason=None)
    iss.append(issue(5, "Working on it", labels=(LABEL, "In Progress")))
    iss.append(issue(6, "Came back", state="OPEN", reason="REOPENED"))
    iss.append(issue(7, "Lowercase", labels=(LABEL, " in progress ")))
    envs, err = await sync(store, uid)
    t = byid(store, uid)
    ck("(b) closed as completed is done", t["GH-1"]["status"] == "done", t["GH-1"]["status"])
    ck("(b) closed as not planned is canceled", t["GH-2"]["status"] == "canceled", t["GH-2"]["status"])
    ck("(b) closed as duplicate is canceled", t["GH-3"]["status"] == "canceled", t["GH-3"]["status"])
    ck("(b) closed with no reason is done", t["GH-4"]["status"] == "done", t["GH-4"]["status"])
    ck("(b) open with the label In Progress is in_progress", t["GH-5"]["status"] == "in_progress")
    ck("(b) the label matches in any case", t["GH-7"]["status"] == "in_progress")
    ck("(b) reopened is todo", t["GH-6"]["status"] == "todo")
    ck("(b) status_of itself", gt.status_of({"state": "OPEN", "labels": {"nodes": [{"name": "in progress later"}]}})
       == "todo" and gt.status_of({"state": "CLOSED", "stateReason": "COMPLETED", "labels": {"nodes": [{"name": "In progress"}]}})
       == "done")
    kinds = sorted(e.event_type for e in envs)
    ck("(b) four status events and three new tickets", kinds == ["ticket_created"] * 3 + ["ticket_status"] * 4, kinds)

    print("== (e) an issue loses the issue label ==")
    gh(6)["labels"] = ["bug"]
    envs, err = await sync(store, uid)
    t = byid(store, uid)
    ck("(e) it becomes canceled, not dropped", t["GH-6"]["status"] == "canceled", t["GH-6"])
    rem = [e for e in envs if e.event_type == "ticket_removed"]
    ck("(e) and emits ticket_removed", len(rem) == 1 and rem[0].labels["ticket"] == "GH-6"
       and rem[0].text == f"GH-6 no longer carries {LABEL}" and rem[0].source == "github_issues", rem)
    envs, err = await sync(store, uid)
    ck("(e) a later sync does not announce the removal again",
       not [e for e in envs if e.event_type == "ticket_removed"], envs)
    gh(6)["labels"] = [LABEL]
    envs, err = await sync(store, uid)
    ck("(e) carrying the label again brings it back", byid(store, uid)["GH-6"]["status"] == "todo")

    print("== (f) the repository cannot be read ==")
    before = snapshot(store, uid)
    STATE["repo_null"] = True
    envs, err = await sync(store, uid)
    ck("(f) repository: null for every credential raises", isinstance(err, gt.GitHubTicketsError)
       and "no stored GitHub credential can read acme/app" in str(err), err)
    ck("(f) the error is on the link", "no stored GitHub credential" in (store.get_project_github(uid)["error"] or ""))
    ck("(f) no ticket or milestone changed", snapshot(store, uid) == before)
    STATE["repo_null"] = False
    envs, err = await sync(store, uid)
    ck("(f) the next good sync clears the error", err is None and store.get_project_github(uid)["error"] is None)

    print("== (g) no issues while tickets are open ==")
    saved_labels = [list(i["labels"]) for i in iss]
    for i in iss:
        i["labels"] = ["bug"]
    before = snapshot(store, uid)
    envs, err = await sync(store, uid)
    ck("(g) it raises and says the issue label may be renamed", isinstance(err, gt.GitHubTicketsError)
       and f"no issues carry {LABEL} in acme/app any more" in str(err) and "renamed" in str(err), err)
    ck("(g) the error is on the link", "renamed" in (store.get_project_github(uid)["error"] or ""))
    ck("(g) no ticket or milestone changed", snapshot(store, uid) == before)
    for i, labels in zip(iss, saved_labels):
        i["labels"] = labels
    envs, err = await sync(store, uid)
    ck("(g) with the label back the sync works again", err is None)

    print("== (h) a failure on page 2 ==")
    STATE["page_size"] = 2
    gh(5)["title"] = "Working on it, renamed"
    before = snapshot(store, uid)
    STATE["fail_page_2"] = True
    STATE["calls"].clear()
    envs, err = await sync(store, uid)
    ck("(h) it raises", isinstance(err, gt.GitHubTicketsError) and "502" in str(err), err)
    ck("(h) the second page was asked for", any(after for _tok, after in STATE["calls"]), STATE["calls"])
    ck("(h) nothing changed, not even what page 1 held", snapshot(store, uid) == before)
    ck("(h) the error is on the link", "502" in (store.get_project_github(uid)["error"] or ""))
    STATE["fail_page_2"] = False
    STATE["null_page_2"] = True
    envs, err = await sync(store, uid)
    ck("(h) a second page without the repository raises too", isinstance(err, gt.GitHubTicketsError), err)
    ck("(h) and changes nothing", snapshot(store, uid) == before)
    STATE["null_page_2"] = False
    envs, err = await sync(store, uid)
    ck("(h) with paging working the whole list is read", err is None
       and store.get_project_github(uid)["issues"] == 7
       and byid(store, uid)["GH-5"]["title"] == "Working on it, renamed", err)
    STATE["page_size"] = 100

    print("== (i) the rate limit ==")
    STATE["http_rate_limit"] = True
    envs, err = await sync(store, uid)
    lk = store.get_project_github(uid)
    ck("(i) it raises as rate limited", isinstance(err, gt.GitHubTicketsError) and err.rate_limited
       and "rate limit" in str(err), err)
    until = gt.datetime.fromisoformat(lk["backoff_until"])
    ck("(i) backoff_until is about 300 s ahead", timedelta(seconds=240) < until - now_utc() <= timedelta(seconds=300),
       lk["backoff_until"])
    STATE["http_rate_limit"] = False
    STATE["calls"].clear()
    ck("(i) the next sync returns [] without an HTTP call", await gt.sync_project(store, uid) == []
       and STATE["calls"] == [], STATE["calls"])
    store.set_project_github(uid, {**lk, "backoff_until": (now_utc() - timedelta(seconds=1)).isoformat()})
    envs, err = await sync(store, uid)
    ck("(i) once the time has passed it syncs and clears the backoff", err is None and STATE["calls"]
       and store.get_project_github(uid)["backoff_until"] is None)
    STATE["gql_rate_limit"] = True
    envs, err = await sync(store, uid)
    ck("(i) a RATE_LIMITED GraphQL error backs off too", isinstance(err, gt.GitHubTicketsError) and err.rate_limited
       and store.get_project_github(uid)["backoff_until"], err)
    STATE["gql_rate_limit"] = False
    store.set_project_github(uid, {**store.get_project_github(uid), "backoff_until": None})

    print("== (j) credentials tried in turn ==")
    store.upsert_github_credential("cred-b", "tok-b")
    store.upsert_github_credential("cred-ghe", "tok-ghe", api_url="https://ghe.example.com/api/v3")
    STATE["tokens"] = {"tok-a": "not_found", "tok-b": "ok", "tok-ghe": "ok"}
    STATE["calls"].clear()
    envs, err = await sync(store, uid)
    order = [tok for tok, _after in STATE["calls"]]
    ck("(j) the second credential is used when the first gets NOT_FOUND", err is None
       and order[:2] == ["tok-a", "tok-b"], (err, order))
    ck("(j) and is remembered on the link", store.get_project_github(uid)["credential"] == "cred-b")
    ck("(j) an Enterprise credential is never tried", "tok-ghe" not in order, order)
    STATE["calls"].clear()
    envs, err = await sync(store, uid)
    ck("(j) next time the remembered credential goes first and alone", err is None
       and [tok for tok, _a in STATE["calls"]] == ["tok-b"], STATE["calls"])
    STATE["tokens"] = {"tok-a": "forbidden", "tok-b": "not_found"}
    before = snapshot(store, uid)
    envs, err = await sync(store, uid)
    ck("(j) a credential that sees the repo but not its issues gets a message that says what to give it",
       isinstance(err, gt.GitHubTicketsError) and "cannot read its issues" in str(err)
       and "Issues: read" in str(err), err)
    ck("(j) nothing changed", snapshot(store, uid) == before)
    STATE["tokens"] = {"tok-a": "unauthorized", "tok-b": "unauthorized"}
    envs, err = await sync(store, uid)
    ck("(j) credentials GitHub refuses give the add-one message", isinstance(err, gt.GitHubTicketsError)
       and "Settings > GitHub" in str(err), err)

    print("== (k) the default issue label ==")
    ck("(k) tares:<name>", gt.default_issue_label("my-project") == "tares:my-project")
    ck("(k) cut to 50 characters", len(gt.default_issue_label("x" * 80)) == 50
       and gt.default_issue_label("x" * 80) == ("tares:" + "x" * 80)[:50])


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
