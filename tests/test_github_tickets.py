"""GitHub Issues as a project's tickets (TR-568, TR-569): the link on a project, the sync that keeps
its tickets current, and the routes and connector that drive it.

Runs against a fake GitHub (httpx.MockTransport answering like its GraphQL endpoint), never the
live API. Covers the first sync (tickets GH-n in issue order), the status rules (closed reasons,
the "in progress" issue label, reopened), GitHub milestones used by an issue (created, adopted,
ordered, unused ones left out), blocked-by as dependencies, an issue losing the issue label, the
sync changing nothing when the fetch is incomplete (repository gone, no issues while tickets are
open, a failed second page), the rate limit backoff, trying the stored credentials in turn and
remembering the one that works, the default issue label and the events on `github_issues`. Also
the limits (1000 issues, 100 milestones), answers GitHub sends half empty, an unlink while a fetch
is in flight, the connector's registration, and (TR-569) linking, syncing and unlinking a project
through the API and the connector's poll. And (TR-570) every rule that keeps a Linear ticket or
milestone in Linear holds for GitHub ones, a stage or a merge never sets a GitHub ticket's status,
the link checks its refusals again after GitHub answers, and a rate limit answers 429.

Run: .venv/bin/python tests/test_github_tickets.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-gh-api-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-github"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

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
        "http_raw": None,                   # (status, headers) answered instead of any data
        "transport_error": False,           # the connection fails
        "fail_label": None,                 # HTTP 502 for this issue label only
        "during_fetch": None,               # called once, inside the first request
        "mangle": None,                     # fn(data, after) that damages a good answer
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
    if STATE["transport_error"]:
        raise httpx.ConnectError("connection refused", request=request)
    hook, STATE["during_fetch"] = STATE["during_fetch"], None
    if hook:
        hook()
    mode = STATE["tokens"].get(token)
    if mode is None or mode == "unauthorized":
        return httpx.Response(401, json={"message": "Bad credentials"})
    if STATE["http_raw"]:
        status, headers = STATE["http_raw"]
        return httpx.Response(status, headers=headers, text="no")
    if v["label"] == STATE["fail_label"]:
        return httpx.Response(502, text="bad gateway")
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
    ms = STATE["milestones"]
    data = {
        "rateLimit": {"remaining": 4999, "resetAt": "2026-10-19T16:00:00Z"},
        "repository": {
            "nameWithOwner": "acme/app", "url": "https://github.com/acme/app",
            "milestones": {"nodes": ms[:100], "pageInfo": {"hasNextPage": len(ms) > 100}},
            "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": more,
                                                    "endCursor": str(start + size) if more else None}}}}
    if STATE["mangle"]:
        STATE["mangle"](data, v.get("after"))
    return httpx.Response(200, json={"data": data})


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


def fresh(name="Edge", label=LABEL):
    """A new store with one linked project, for the checks that need a known starting point."""
    store = Store(os.path.join(tempfile.mkdtemp(prefix="tares-gh-edge-"), "t.duckdb"))
    store.upsert_github_credential("cred-a", "tok-a")
    uid = "uc_edge"
    store.create_project(uid, "custom", name, {"objects": []})
    store.set_project_github(uid, {"repo": "acme/app", "issue_label": label,
                                   "url": "https://github.com/acme/app"})
    return store, uid


async def fresh_synced(**kw):
    reset()
    store, uid = fresh(**kw)
    envs, err = await sync(store, uid)
    assert err is None and len(store.list_tickets(uid)) == 4, err
    return store, uid


async def edges():
    print("== (m) the limits ==")
    reset()
    store, uid = fresh()
    STATE["issues"] += [issue(n, f"Bulk {n}") for n in range(100, 996 + 100)]
    envs, err = await sync(store, uid)
    ck("(m) exactly 1000 issues are fine", err is None and len(store.list_tickets(uid)) == 1000
       and store.get_project_github(uid)["issues"] == 1000, err)
    STATE["issues"].append(issue(5000, "One too many"))
    before = snapshot(store, uid)
    envs, err = await sync(store, uid)
    ck("(m) 1001 issues raise", isinstance(err, gt.GitHubTicketsError) and "more than 1000" in str(err), err)
    ck("(m) and change nothing", snapshot(store, uid) == before)
    ck("(m) the error is on the link", "more than 1000" in (store.get_project_github(uid)["error"] or ""))
    reset()
    store, uid = await fresh_synced()
    before = snapshot(store, uid)
    STATE["milestones"] = [{"id": f"M_{n}", "number": n, "title": f"Mile {n}", "description": ""}
                           for n in range(1, 102)]
    envs, err = await sync(store, uid)
    ck("(m) more than 100 milestones raise", isinstance(err, gt.GitHubTicketsError)
       and "more than 100 milestones" in str(err), err)
    ck("(m) and change nothing", snapshot(store, uid) == before)
    STATE["milestones"] = STATE["milestones"][:100]
    envs, err = await sync(store, uid)
    ck("(m) exactly 100 milestones are fine", err is None, err)

    print("== (n) the zero-issue guard ==")
    store, uid = await fresh_synced()
    ck("(n) only todo tickets are open", {t["status"] for t in store.list_tickets(uid)} == {"todo"})
    for i in STATE["issues"]:
        i["labels"] = ["bug"]
    before = snapshot(store, uid)
    envs, err = await sync(store, uid)
    ck("(n) todo tickets and no issues raise", isinstance(err, gt.GitHubTicketsError)
       and "renamed" in str(err), err)
    ck("(n) and change nothing", snapshot(store, uid) == before)
    store, uid = await fresh_synced()
    for n in (1, 2, 3):
        gh(n).update(state="CLOSED", stateReason="COMPLETED")
    gh(4)["labels"] = [LABEL, "in progress"]
    await sync(store, uid)
    for i in STATE["issues"]:
        i["labels"] = ["bug"]
    envs, err = await sync(store, uid)
    ck("(n) an in_progress ticket counts as open too", isinstance(err, gt.GitHubTicketsError)
       and "renamed" in str(err), err)
    reset()
    gh(1).update(state="CLOSED", stateReason="COMPLETED")
    store, uid = fresh()
    STATE["issues"] = [gh(1)]
    await sync(store, uid)
    for i in STATE["issues"]:
        i["labels"] = ["bug"]
    envs, err = await sync(store, uid)
    ck("(n) with nothing open, no issues is no reason to refuse", err is None, err)

    print("== (o) milestone mirrors ==")
    store, uid = await fresh_synced()
    ms = {m["name"]: m for m in store.list_milestones(uid)}
    gh(1)["milestone"] = None
    gh(2)["milestone"] = None
    envs, err = await sync(store, uid)
    ms2 = {m["name"]: m for m in store.list_milestones(uid)}
    ck("(o) a mirror stays while its GitHub milestone exists, used or not",
       err is None and "Alpha" in ms2 and ms2["Alpha"]["id"] == ms["Alpha"]["id"], ms2)
    ck("(o) and the tickets that left it have none", byid(store, uid)["GH-1"]["milestone"] is None)
    STATE["milestones"][1]["title"] = "Beta, renamed"
    envs, err = await sync(store, uid)
    ms3 = {m["id"]: m for m in store.list_milestones(uid)}
    ck("(o) a rename in GitHub is followed", ms3[ms["Beta"]["id"]]["name"] == "Beta, renamed", ms3)
    STATE["milestones"] = [m for m in STATE["milestones"] if m["id"] != "M_alpha"]
    envs, err = await sync(store, uid)
    ms4 = {m["id"] for m in store.list_milestones(uid)}
    ck("(o) a mirror whose GitHub milestone is gone from the repo is deleted",
       err is None and ms["Alpha"]["id"] not in ms4 and ms["Beta"]["id"] in ms4, ms4)

    print("== (p) a blocker that lost the issue label ==")
    store, uid = await fresh_synced()
    t = byid(store, uid)
    ck("(p) GH-2 starts blocked by GH-1", t["GH-2"]["depends_on"] == [t["GH-1"]["id"]])
    gh(1)["labels"] = ["bug"]
    envs, err = await sync(store, uid)
    t = byid(store, uid)
    ck("(p) GH-1 is canceled and still there", err is None and t["GH-1"]["status"] == "canceled")
    ck("(p) and no longer a dependency, though GitHub still lists it as a blocker",
       t["GH-2"]["depends_on"] == [] and t["GH-3"]["depends_on"] == [], (t["GH-2"], t["GH-3"]))

    print("== (q) GitHub's refusals and a dead connection ==")
    store, uid = await fresh_synced()
    before = snapshot(store, uid)
    for label, raw, limited, text in [
            ("HTTP 429", (429, {}), True, "rate limit"),
            ("403 with only retry-after", (403, {"retry-after": "60"}), True, "rate limit"),
            ("403 with remaining 0", (403, {"x-ratelimit-remaining": "0"}), True, "rate limit"),
            ("a plain 403", (403, {}), False, "answered 403")]:
        STATE["http_raw"] = raw
        envs, err = await sync(store, uid)
        lk = store.get_project_github(uid)
        ck(f"(q) {label}: raises" + (", rate limited, backs off" if limited else ", not a rate limit"),
           isinstance(err, gt.GitHubTicketsError) and err.rate_limited is limited and text in str(err)
           and bool(lk["backoff_until"]) is limited and text in lk["error"], (err, lk))
        store.set_project_github(uid, {**lk, "backoff_until": None})
    ck("(q) and nothing changed", snapshot(store, uid) == before)
    STATE["http_raw"] = None
    STATE["transport_error"] = True
    envs, err = await sync(store, uid)
    ck("(q) a dead connection is GitHubTicketsError: cannot reach GitHub",
       isinstance(err, gt.GitHubTicketsError) and str(err).startswith("cannot reach GitHub")
       and not err.rate_limited, err)
    ck("(q) recorded on the link, nothing changed", "cannot reach GitHub" in
       (store.get_project_github(uid)["error"] or "") and snapshot(store, uid) == before)
    STATE["transport_error"] = False

    print("== (s) an answer with parts missing ==")
    store, uid = await fresh_synced()
    STATE["page_size"] = 2
    gh(1)["title"] = "Parse invoices, renamed"
    before = snapshot(store, uid)

    def repo_of(d):
        return d["repository"]

    def drop(path_fn, key, value="gone"):
        def go(d, after):
            node = path_fn(d)
            if value == "gone":
                node.pop(key, None)
            else:
                node[key] = value
        return go

    def on(page, fn):
        """Damage page 1 (page == 1) or any later page (page == 2) only."""
        return lambda d, after: fn(d, after) if (after is None) == (page == 1) else None

    variants = [
        ("page 1: issues null", on(1, drop(repo_of, "issues", None)), "issues"),
        ("page 1: issues missing", on(1, drop(repo_of, "issues")), "issues"),
        ("page 1: issues.pageInfo missing", on(1, drop(lambda d: d["repository"]["issues"], "pageInfo")), "pageInfo"),
        ("page 1: issues.nodes null", on(1, drop(lambda d: d["repository"]["issues"], "nodes", None)), "nodes"),
        ("page 1: milestones null", on(1, drop(repo_of, "milestones", None)), "milestones"),
        ("page 1: milestones missing", on(1, drop(repo_of, "milestones")), "milestones"),
        ("page 1: milestones.pageInfo missing", on(1, drop(lambda d: d["repository"]["milestones"], "pageInfo")), "pageInfo"),
        ("page 1: milestones.nodes null", on(1, drop(lambda d: d["repository"]["milestones"], "nodes", None)), "nodes"),
        ("page 1: endCursor missing", on(1, drop(lambda d: d["repository"]["issues"]["pageInfo"], "endCursor")), "endCursor"),
        ("page 2: issues null", on(2, drop(repo_of, "issues", None)), "issues"),
        ("page 2: issues missing", on(2, drop(repo_of, "issues")), "issues"),
        ("page 2: issues.pageInfo missing", on(2, drop(lambda d: d["repository"]["issues"], "pageInfo")), "pageInfo"),
        ("page 2: issues.nodes null", on(2, drop(lambda d: d["repository"]["issues"], "nodes", None)), "nodes"),
    ]
    for label, fn, field in variants:
        STATE["mangle"] = fn
        envs, err = await sync(store, uid)
        ck(f"(s) {label}: raises and says what was missing", isinstance(err, gt.GitHubTicketsError)
           and f"GitHub sent an answer without {field}" in str(err), err)
        ck(f"(s) {label}: nothing changed, error on the link", snapshot(store, uid) == before
           and "without" in (store.get_project_github(uid)["error"] or ""))
    STATE["mangle"] = None
    envs, err = await sync(store, uid)
    ck("(s) the same sync with a whole answer works", err is None
       and byid(store, uid)["GH-1"]["title"] == "Parse invoices, renamed", err)
    STATE["page_size"] = 100

    print("== (u) unlinked while GitHub was answering ==")
    reset()
    store, uid = fresh()

    def unlink():
        store.set_project_github(uid, None)
    STATE["during_fetch"] = unlink
    envs, err = await sync(store, uid)
    ck("(u) a first sync writes nothing", err is None and envs == [] and store.list_tickets(uid) == []
       and store.list_milestones(uid) == [] and store.get_project_github(uid) is None, (err, envs))
    store.set_project_github(uid, {"repo": "acme/app", "issue_label": LABEL})
    envs, err = await sync(store, uid)
    assert len(store.list_tickets(uid)) == 4

    def unlink_like_the_route():
        store.set_project_github(uid, None)
        for t in store.list_tickets(uid):
            store.update_ticket_owner(t["id"], "tares")
        for m in store.list_milestones(uid):
            store.update_milestone(m["id"], owner="tares", external_id=None)
    gh(1)["title"] = "Renamed while unlinking"
    STATE["during_fetch"] = unlink_like_the_route
    envs, err = await sync(store, uid)
    after = store.list_tickets(uid)
    ck("(u) an unlink in the middle: no events, no duplicates, the link stays None",
       err is None and envs == [] and len(after) == 4 and {t["owner"] for t in after} == {"tares"}
       and store.get_project_github(uid) is None, (err, envs, after))
    ck("(u) and the ticket it would have renamed is as it was", byid(store, uid)["GH-1"]["title"] == "Parse invoices")
    store.set_project_github(uid, {"repo": "acme/app", "issue_label": LABEL})
    STATE["http_raw"] = (502, {})
    STATE["during_fetch"] = unlink
    envs, err = await sync(store, uid)
    ck("(u) a failed fetch does not bring the link back either",
       isinstance(err, gt.GitHubTicketsError) and store.get_project_github(uid) is None, err)
    STATE["http_raw"] = None
    store.set_project_github(uid, {"repo": "acme/app", "issue_label": LABEL})

    def relink_elsewhere():
        store.set_project_github(uid, {"repo": "acme/other", "issue_label": LABEL})
    STATE["during_fetch"] = relink_elsewhere
    envs, err = await sync(store, uid)
    ck("(u) a link changed to another repo in the middle is not written to either",
       envs == [] and store.get_project_github(uid) == {"repo": "acme/other", "issue_label": LABEL})


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

    print("== (r) the connector is registered ==")
    from tares.config import CatalogError, catalog_from_db, validate_source_dict
    from tares.connectors import REGISTRY, SPECS
    from tares.connectors.github_issues import GitHubIssuesConnector
    row = next(x for x in store.list_catalog_sources() if x["name"] == "github_issues")
    ck("(r) the stored source names the connector github_issues, polling every 60s",
       row["connector"] == "github_issues" and row["poll"] == "60s" and row["type"] == "event_stream", row)
    ck("(r) the registry key equals that name", REGISTRY.get("github_issues") is GitHubIssuesConnector)
    try:
        validate_source_dict(row)
        valid = True
    except CatalogError as e:
        valid = str(e)
    ck("(r) the stored row passes the validation the catalog uses", valid is True, valid)
    try:
        validate_source_dict({**row, "connector": "github_issue"})
        ck("(r) and an unknown connector name still fails it", False)
    except CatalogError:
        ck("(r) and an unknown connector name still fails it", True)
    ck("(r) internal, polled, labeled for the picker", SPECS["github_issues"]["internal"] is True
       and SPECS["github_issues"]["mode"] == "poll" and SPECS["github_issues"]["poll"] == "60s"
       and SPECS["github_issues"]["label"] == "GitHub issues (tickets)", SPECS.get("github_issues"))
    labels = {x["name"]: x for x in row["config"]["labels"]}
    ck("(r) labels ticket (the key), project and repo, read from the event payload",
       set(labels) == {"ticket", "project", "repo"} and labels["ticket"].get("primary")
       and labels["repo"]["field"] == "repo", labels)
    cat = catalog_from_db(store)
    ck("(r) the catalog builds it as a github_issues source", cat.sources["github_issues"].connector
       == "github_issues" and isinstance(REGISTRY[cat.sources["github_issues"].connector](
           cat.sources["github_issues"], store), GitHubIssuesConnector))
    cur = store.get_project_github(uid)
    store.create_project("uc_plain", "custom", "Plain", {"objects": []})
    ck("(r) github_projects lists the linked project, not one without a link",
       store.github_projects() == [(uid, cur)])
    store.set_project_github(uid, None)
    ck("(r) and an unlinked project drops out", store.github_projects() == [])
    store.set_project_github(uid, cur)
    ck("(r) linking again lists it", [u for u, _l in store.github_projects()] == [uid])

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
    # an Enterprise credential sorts first by name and is the one the link remembers, and GitHub
    # would answer it well: only the api_url filter keeps it from being called
    store.upsert_github_credential("aaa-ghe", "tok-aaa", api_url="https://ghe.example.com/api/v3")
    store.set_project_github(uid, {**store.get_project_github(uid), "credential": "cred-ghe"})
    STATE["tokens"] = {"tok-a": "not_found", "tok-b": "ok", "tok-ghe": "ok", "tok-aaa": "ok"}
    STATE["calls"].clear()
    envs, err = await sync(store, uid)
    order = [tok for tok, _after in STATE["calls"]]
    ck("(j) the second credential is used when the first gets NOT_FOUND", err is None
       and order[:2] == ["tok-a", "tok-b"], (err, order))
    ck("(j) and is remembered on the link", store.get_project_github(uid)["credential"] == "cred-b")
    ck("(j) an Enterprise credential is never tried, remembered or not",
       "tok-ghe" not in order and "tok-aaa" not in order and order, order)
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

    await edges()


async def api():
    from tares.config import SourceCfg
    from tares.connectors.github_issues import GitHubIssuesConnector
    from tares.daemon import make_app
    reset()
    app = make_app()
    store = app.state.store
    store.upsert_github_credential("cred-a", "tok-a")
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})

        async def new_project(name):
            r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": []})
            return r.json()["id"]

        print("== (A) linking a project ==")
        uid = await new_project("p")
        r = await cx.post("/api/projects/nope/github", json={"repo": "acme/app"})
        ck("(A) an unknown project -> 404", r.status_code == 404, r.text)
        for body, text in [({}, "owner/name"), ({"repo": "acme"}, "owner/name"),
                           ({"repo": "acme/app", "issue_label": "   "}, "1 to 50"),
                           ({"repo": "acme/app", "issue_label": "x" * 51}, "1 to 50")]:
            r = await cx.post(f"/api/projects/{uid}/github", json=body)
            ck(f"(A) {body} -> 400", r.status_code == 400 and text in r.json()["detail"], r.text)
        ck("(A) none of them linked it", store.get_project_github(uid) is None)
        r = await cx.post(f"/api/projects/{uid}/github", json={"repo": "https://github.com/acme/app.git"})
        tickets = r.json()["tickets"]
        ck("(A) link by pasted URL, issue label defaulting to tares:<name>: GH-1..GH-4 in order",
           r.status_code == 200 and [t["identifier"] for t in tickets] == ["GH-1", "GH-2", "GH-3", "GH-4"]
           and all(t["owner"] == "github" for t in tickets), r.text)
        lk = r.json()["github"]
        ck("(A) the link records repo, issue label, url, credential and the sync",
           lk["repo"] == "acme/app" and lk["issue_label"] == "tares:p" and lk["url"] == "https://github.com/acme/app"
           and lk["credential"] == "cred-a" and lk["synced_at"] and lk["error"] is None and lk["issues"] == 4, lk)
        ck("(A) the github_issues source exists and is in the project",
           "github_issues" in app.state.runtime.catalog.sources and "github_issues" in store.project_sources(uid))
        n = store.con.execute("SELECT count(*) FROM events WHERE source = 'github_issues' AND "
                              "event_type = 'ticket_created'").fetchone()[0]
        ck("(A) one event per ticket found", n == 4, n)
        await asyncio.sleep(0.3)             # the source's own first poll, then the checks drive the polls
        app.state.runtime._stop("github_issues")
        r = await cx.get(f"/api/projects/{uid}/tickets")
        ck("(A) GET tickets shows the github link, and no linear one", r.json()["github"]["repo"] == "acme/app"
           and r.json()["github"]["issue_label"] == "tares:p" and r.json()["linear"] is None
           and len(r.json()["tickets"]) == 4, r.text)
        r = await cx.post(f"/api/projects/{uid}/github", json={"repo": "acme/app", "issue_label": "tares:p"})
        ck("(A) linking the same repo and issue label again is fine", r.status_code == 200
           and len(r.json()["tickets"]) == 4, r.text)
        r = await cx.post(f"/api/projects/{uid}/github", json={"repo": "acme/app", "issue_label": "other"})
        ck("(A) another issue label on a linked project -> 409, unlink first", r.status_code == 409
           and "unlink it first" in r.json()["detail"], r.text)
        r = await cx.post(f"/api/projects/{uid}/github", json={"repo": "acme/lib"})
        ck("(A) another repo on a linked project -> 409", r.status_code == 409, r.text)

        print("== (B) refusals ==")
        other = await new_project("other")
        r = await cx.post(f"/api/projects/{other}/github", json={"repo": "acme/app", "issue_label": "TARES:P"})
        ck("(B) the same repo + issue label in another project (any case) -> 409 naming the owner",
           r.status_code == 409 and "acme/app with issue label TARES:P is already the ticket home of project p"
           in r.json()["detail"], r.text)
        ck("(B) and nothing was linked", store.get_project_github(other) is None)
        lin = await new_project("lin")
        store.set_project_linear(lin, {"id": "lp1", "name": "Invoicing"})
        r = await cx.post(f"/api/projects/{lin}/github", json={"repo": "acme/app", "issue_label": "tares:lin"})
        ck("(B) a project using Linear cannot link GitHub -> 409", r.status_code == 409
           and "live in Linear; stop using Linear first" in r.json()["detail"], r.text)
        r = await cx.post(f"/api/projects/{uid}/linear", json={"project": "anything"})
        ck("(B) a project using GitHub cannot link Linear -> 409", r.status_code == 409
           and "live in GitHub; stop using GitHub first" in r.json()["detail"], r.text)
        ck("(B) and the Linear link was not made", store.get_project_linear(uid) is None)
        mine = await new_project("mine")
        await cx.post(f"/api/projects/{mine}/tickets", json={"title": "mine"})
        r = await cx.post(f"/api/projects/{mine}/github", json={"repo": "acme/app", "issue_label": "tares:mine"})
        ck("(B) a project with its own tickets cannot link -> 409", r.status_code == 409
           and "already has its own tickets" in r.json()["detail"], r.text)
        bad = await new_project("bad")
        STATE["repo_null"] = True
        r = await cx.post(f"/api/projects/{bad}/github", json={"repo": "acme/app", "issue_label": "tares:bad"})
        ck("(B) a repo no credential can read -> 400 with the reason", r.status_code == 400
           and "no stored GitHub credential can read acme/app" in r.json()["detail"], r.text)
        ck("(B) no link saved, no tickets", store.get_project_github(bad) is None and store.list_tickets(bad) == [])
        STATE["repo_null"] = False
        STATE["tokens"] = {"tok-a": "forbidden"}
        r = await cx.post(f"/api/projects/{bad}/github", json={"repo": "acme/app", "issue_label": "tares:bad"})
        ck("(B) a credential without Issues: read -> 400 that says so", r.status_code == 400
           and "Issues: read" in r.json()["detail"] and store.get_project_github(bad) is None, r.text)
        STATE["tokens"] = {"tok-a": "ok"}
        STATE["transport_error"] = True
        r = await cx.post(f"/api/projects/{bad}/github", json={"repo": "acme/app", "issue_label": "tares:bad"})
        ck("(B) GitHub unreachable while linking -> 400, nothing saved", r.status_code == 400
           and store.get_project_github(bad) is None, r.text)
        STATE["transport_error"] = False
        flaky = await new_project("flaky")
        STATE["during_fetch"] = lambda: STATE.update(transport_error=True)   # GitHub drops after the check
        r = await cx.post(f"/api/projects/{flaky}/github", json={"repo": "acme/app", "issue_label": "tares:flaky"})
        STATE["transport_error"] = False
        lk = store.get_project_github(flaky) or {}
        ck("(B) the first sync failing does not undo the link: it keeps the credential that passed the check",
           r.status_code == 200 and lk.get("credential") == "cred-a" and "reach" in (lk.get("error") or ""), r.text)
        await cx.delete(f"/api/projects/{flaky}/github")

        print("== (C) sync now ==")
        r = await cx.post(f"/api/projects/{bad}/github/sync")
        ck("(C) a project that is not linked -> 409", r.status_code == 409, r.text)
        r = await cx.post("/api/projects/nope/github/sync")
        ck("(C) an unknown project -> 404", r.status_code == 404, r.text)
        gh(1).update(state="CLOSED", stateReason="COMPLETED")
        gh(2)["title"] = "Store invoices, renamed"
        r = await cx.post(f"/api/projects/{uid}/github/sync")
        t = {x["identifier"]: x for x in r.json()["tickets"]}
        ck("(C) sync now brings the changes, with the link", r.status_code == 200 and t["GH-1"]["status"] == "done"
           and t["GH-2"]["title"] == "Store invoices, renamed" and r.json()["github"]["error"] is None, r.text)
        STATE["http_raw"] = (502, {})
        r = await cx.post(f"/api/projects/{uid}/github/sync")
        ck("(C) GitHub answering 502 -> 502", r.status_code == 502 and "answered 502" in r.json()["detail"], r.text)
        STATE["http_raw"] = None
        STATE["transport_error"] = True
        r = await cx.post(f"/api/projects/{uid}/github/sync")
        ck("(C) GitHub unreachable -> 502", r.status_code == 502 and "cannot reach" in r.json()["detail"], r.text)
        STATE["transport_error"] = False
        STATE["tokens"] = {"tok-a": "unauthorized"}
        before = store.list_tickets(uid)
        r = await cx.post(f"/api/projects/{uid}/github/sync")
        ck("(C) a refused credential -> 400, tickets as they were, error on the link", r.status_code == 400
           and "Settings > GitHub" in r.json()["detail"] and store.list_tickets(uid) == before
           and "Settings > GitHub" in store.get_project_github(uid)["error"], r.text)
        STATE["tokens"] = {"tok-a": "ok"}
        r = await cx.post(f"/api/projects/{uid}/github/sync")
        ck("(C) and the next sync clears it", r.status_code == 200 and r.json()["github"]["error"] is None, r.text)

        print("== (D) the connector's poll ==")
        STATE["issues"] += [issue(21, "Second project one", labels=("tares:q",)),
                            issue(22, "Second project two", labels=("tares:q",))]
        second = await new_project("second")
        r = await cx.post(f"/api/projects/{second}/github", json={"repo": "acme/app", "issue_label": "tares:q"})
        ck("(D) a second project links with its own issue label", r.status_code == 200
           and [t["identifier"] for t in r.json()["tickets"]] == ["GH-21", "GH-22"], r.text)
        conn = GitHubIssuesConnector(SourceCfg(name="github_issues", type="event_stream",
                                               connector="github_issues", poll_seconds=60, config={}), store)
        ck("(D) a poll with nothing new emits nothing", await conn.poll() == [])
        gh(3).update(state="CLOSED", stateReason="NOT_PLANNED")
        gh(22)["labels"] = ["in progress", "tares:q"]
        gh(4)["title"] = "Retry failed emails, renamed"
        envs = await conn.poll()
        ck("(D) it syncs every linked project and returns the events",
           sorted((e.labels["project"], e.event_type) for e in envs) ==
           [("p", "ticket_renamed"), ("p", "ticket_status"), ("second", "ticket_status")], 
           [(e.labels["project"], e.event_type) for e in envs])
        ck("(D) keyed by identifier, on github_issues", all(e.source == "github_issues"
           and e.key_value.startswith("GH-") for e in envs))
        store.append(envs)
        t = {x["identifier"]: x for x in store.list_tickets(second)}
        ck("(D) the tickets moved", t["GH-22"]["status"] == "in_progress")
        gh(21)["title"] = "Second project one, renamed"
        STATE["fail_label"] = LABEL
        envs = await conn.poll()
        ck("(D) one project failing does not stop the other", [e.event_type for e in envs] == ["ticket_renamed"]
           and "answered 502" in (store.get_project_github(uid)["error"] or ""), envs)
        STATE["fail_label"] = None
        STATE["http_raw"] = (502, {})
        try:
            await conn.poll()
            ck("(D) when nothing synced the poll raises, so the source's health shows it", False)
        except gt.GitHubTicketsError:
            ck("(D) when nothing synced the poll raises, so the source's health shows it", True)
        STATE["http_raw"] = None
        await conn.poll()

        print("== (E) unlinking ==")
        r = await cx.post(f"/api/projects/{uid}/docs", json={"kind": "working", "title": "W", "body": "steps"})
        r = await cx.put(f"/api/projects/{uid}/tickets/GH-4", json={"working_doc": r.json()["id"]})
        ck("(E) a GitHub ticket can get a working doc", r.json()["working_doc_body"] == "steps", r.text)
        ms_before = store.list_milestones(uid)
        ck("(E) the project has GitHub-owned milestones to start with", ms_before and
           all(m["owner"] == "github" and m["external_id"] for m in ms_before), ms_before)
        r = await cx.delete(f"/api/projects/{uid}/github")
        ck("(E) unlink: the link is gone, tickets stay and are Tares's", r.status_code == 200
           and r.json()["github"] is None and len(r.json()["tickets"]) == 4
           and all(x["owner"] == "tares" for x in r.json()["tickets"]), r.text)
        ck("(E) the working doc is still there", next(x for x in r.json()["tickets"]
           if x["identifier"] == "GH-4")["working_doc"], r.text)
        ms_after = store.list_milestones(uid)
        ck("(E) milestones are Tares's, without the GitHub id", len(ms_after) == len(ms_before)
           and all(m["owner"] == "tares" and m["external_id"] is None for m in ms_after), ms_after)
        ck("(E) the store has no link", store.get_project_github(uid) is None
           and [u for u, _l in store.github_projects()] == [second])
        r = await cx.get(f"/api/projects/{uid}/tickets")
        ck("(E) GET tickets shows github: null", r.json()["github"] is None, r.text)
        r = await cx.put(f"/api/projects/{uid}/tickets/GH-1", json={"status": "todo"})
        ck("(E) and the tickets can be edited in Tares", r.status_code == 200 and r.json()["status"] == "todo", r.text)
        r = await cx.post(f"/api/projects/{uid}/github/sync")
        ck("(E) sync now is refused after the unlink", r.status_code == 409, r.text)
        n = len(await conn.poll())
        ck("(E) and the poll no longer touches the project", store.list_tickets(uid)[0]["owner"] == "tares" and n == 0)

        print("== (F) a GitHub ticket and milestone are GitHub's (TR-570) ==")
        STATE["issues"] += [issue(31, "Own one", labels=("tares:own",), milestone="M_alpha"),
                            issue(32, "Own two", labels=("tares:own",), milestone="M_alpha", blocked_by=["I_31"])]
        own = await new_project("own")
        r = await cx.post(f"/api/projects/{own}/github", json={"repo": "acme/app", "issue_label": "tares:own"})
        ck("(F) a project to test on, linked to GitHub", r.status_code == 200
           and [t["identifier"] for t in r.json()["tickets"]] == ["GH-31", "GH-32"], r.text)
        before = {t["identifier"]: t for t in store.list_tickets(own)}
        for field, value in [("title", "Renamed"), ("status", "done"), ("position", 5),
                             ("milestone", None), ("depends_on", [])]:
            r = await cx.put(f"/api/projects/{own}/tickets/GH-31", json={field: value})
            ck(f"(F) PUT {field} on a GitHub ticket -> 409 'lives in GitHub'", r.status_code == 409
               and r.json()["detail"] == f"GH-31 lives in GitHub: change its {field} there", r.text)
        r = await cx.put(f"/api/projects/{own}/tickets/GH-31", json={"title": "x", "status": "done", "position": 1})
        ck("(F) several fields at once are named together", r.status_code == 409
           and r.json()["detail"] == "GH-31 lives in GitHub: change its position, status, title there", r.text)
        ck("(F) none of them changed the ticket",
           {t["identifier"]: t for t in store.list_tickets(own)} == before)
        r = await cx.post(f"/api/projects/{own}/docs", json={"kind": "working", "title": "W31", "body": "plan"})
        doc = r.json()["id"]
        r = await cx.put(f"/api/projects/{own}/tickets/GH-31", json={"working_doc": doc, "repo": "acme/app"})
        ck("(F) a GitHub ticket still takes a working doc and a repo", r.status_code == 200
           and r.json()["working_doc_body"] == "plan" and r.json()["repo"] == "acme/app"
           and r.json()["title"] == "Own one" and r.json()["owner"] == "github", r.text)
        r = await cx.delete(f"/api/projects/{own}/tickets/GH-31")
        ck("(F) DELETE a GitHub ticket -> 409", r.status_code == 409
           and r.json()["detail"] == "GH-31 lives in GitHub: remove it there"
           and store.get_ticket(own, "GH-31") is not None, r.text)
        r = await cx.post(f"/api/projects/{own}/tickets", json={"title": "Mine"})
        ck("(F) POST a ticket in a GitHub project -> 409", r.status_code == 409
           and r.json()["detail"] == "this project's tickets live in GitHub; add the ticket there and it "
           "shows up here on the next sync" and len(store.list_tickets(own)) == 2, r.text)

        ms = {m["name"]: m for m in store.list_milestones(own)}
        ck("(F) the GitHub milestone is there to test on", ms.get("Alpha", {}).get("owner") == "github", ms)
        alpha = ms["Alpha"]
        r = await cx.post(f"/api/projects/{own}/milestones", json={"name": "Brand new"})
        ck("(F) a new milestone name in a GitHub project -> 409", r.status_code == 409
           and r.json()["detail"] == "this project's milestones live in GitHub: add it there, then write "
           "its checks here" and "Brand new" not in {m["name"] for m in store.list_milestones(own)}, r.text)
        r = await cx.put(f"/api/projects/{own}/milestones/{alpha['id']}", json={"name": "Alpha 2"})
        ck("(F) renaming a GitHub milestone -> 409", r.status_code == 409
           and r.json()["detail"] == "Alpha lives in GitHub: rename or reorder it there", r.text)
        r = await cx.put(f"/api/projects/{own}/milestones/{alpha['id']}", json={"position": 9})
        ck("(F) reordering a GitHub milestone -> 409", r.status_code == 409
           and r.json()["detail"] == "Alpha lives in GitHub: rename or reorder it there", r.text)
        r = await cx.put(f"/api/projects/{own}/milestones/{alpha['id']}",
                         json={"goal": "Ship it", "checks": [{"check": "run it", "expect": "it runs"}]})
        ck("(F) a GitHub milestone still takes a goal and checks", r.status_code == 200
           and r.json()["goal"] == "Ship it" and r.json()["checks"]
           and r.json()["name"] == "Alpha" and r.json()["position"] == alpha["position"], r.text)
        r = await cx.post(f"/api/projects/{own}/milestones",
                          json={"name": "Alpha", "goal": "Ship it again", "position": 9})
        ck("(F) writing checks under the existing name works, its position stays GitHub's", r.status_code == 201
           and r.json()["goal"] == "Ship it again" and r.json()["position"] == alpha["position"], r.text)
        r = await cx.delete(f"/api/projects/{own}/milestones/{alpha['id']}")
        ck("(F) DELETE a GitHub milestone -> 409", r.status_code == 409
           and r.json()["detail"] == "Alpha lives in GitHub: remove it there"
           and store.get_milestone(own, alpha["id"]) is not None, r.text)

        print("== (F) the stage never sets a GitHub ticket's status ==")
        from tares import factory_api
        status31 = store.get_ticket(own, "GH-31")["status"]
        r = await cx.post(f"/api/projects/{own}/tickets/GH-31/stage", json={"stage": "doing"})
        ck("(F) set_stage doing changes the stage, not the status", r.status_code == 200
           and r.json()["stage"] == "doing" and r.json()["status"] == status31, r.text)
        r = await cx.post(f"/api/projects/{own}/tickets/GH-31/stage", json={"stage": "merged"})
        ck("(F) set_stage merged changes the stage, not the status", r.status_code == 200
           and r.json()["stage"] == "merged" and r.json()["status"] == status31 != "done", r.text)
        plain = await new_project("plain")
        await cx.post(f"/api/projects/{plain}/tickets", json={"title": "Plain"})
        r = await cx.post(f"/api/projects/{plain}/tickets/T1/stage", json={"stage": "merged"})
        ck("(F) a Tares ticket's status still follows its stage", r.status_code == 200
           and r.json()["stage"] == "merged" and r.json()["status"] == "done", r.text)
        merged_pr = {"merged": True, "repo": "acme/app", "number": 5}
        ck("(F) merged_fields: a GitHub ticket is marked merged, its status left alone",
           factory_api.merged_fields({"owner": "github", "stage": "doing"}, merged_pr) == {"stage": "merged"})
        ck("(F) merged_fields: a Linear ticket the same",
           factory_api.merged_fields({"owner": "linear", "stage": "doing"}, merged_pr) == {"stage": "merged"})
        ck("(F) merged_fields: a Tares ticket is set done",
           factory_api.merged_fields({"owner": "tares", "stage": "doing"}, merged_pr)
           == {"stage": "merged", "status": "done"})

        async def fake_read_pr(_store, repo, number, prev=None):
            return {**(prev or {}), "repo": repo, "number": number, "merged": True, "state": "closed",
                    "url": f"https://github.com/{repo}/pull/{number}", "checked_at": now_utc().isoformat()}

        real_read_pr, factory_api.factory_github.read_pr = factory_api.factory_github.read_pr, fake_read_pr
        try:
            status32 = store.get_ticket(own, "GH-32")["status"]
            r = await cx.post(f"/api/projects/{own}/tickets/GH-32/stage", json={"stage": "review", "pr": "acme/app#5"})
        finally:
            factory_api.factory_github.read_pr = real_read_pr
        ck("(F) a PR GitHub shows merged makes a GitHub ticket merged, not done", r.status_code == 200
           and r.json()["stage"] == "merged" and r.json()["status"] == status32 != "done", r.text)

        print("== (G) the link checks again after GitHub answers (TR-570) ==")
        holder = await new_project("holder")
        ran_all: list = []
        for what, hook, text in [
                ("another project took the same repo and issue label",
                 lambda: store.set_project_github(holder, {"repo": "acme/app", "issue_label": lab}),
                 "already the ticket home of project holder"),
                ("Linear was linked to the project",
                 lambda: store.set_project_linear(racer, {"id": "lp9", "name": "Invoicing"}),
                 "live in Linear; stop using Linear first"),
                ("the project got a ticket of its own",
                 lambda: store.create_ticket(racer, "tares", "Mine"),
                 "already has its own tickets in Tares"),
                ("the project was linked to another repo",
                 lambda: store.set_project_github(racer, {"repo": "acme/lib", "issue_label": lab}),
                 "is linked to acme/lib with issue label")]:
            lab = f"tares:race{len(ran_all)}"
            racer = await new_project(f"racer {len(ran_all)}")
            ran_all.append(1)
            ran = []
            STATE["during_fetch"] = lambda hook=hook: (ran.append(1), hook())
            r = await cx.post(f"/api/projects/{racer}/github", json={"repo": "acme/app", "issue_label": lab})
            STATE["during_fetch"] = None
            link = store.get_project_github(racer)
            ck(f"(G) {what} while GitHub answered -> 409, nothing saved", ran and r.status_code == 409
               and text in r.json()["detail"] and (link is None or link["repo"] == "acme/lib")
               and not [t for t in store.list_tickets(racer) if t["owner"] == "github"], (ran, r.text))
            if what.startswith("another"):
                ck("(G) and the project that held it still does", store.get_project_github(holder) is not None)

        print("== (G) a rate limit is not GitHub being down ==")
        before = store.list_tickets(own)
        for how in ("gql_rate_limit", "http_rate_limit"):
            STATE[how] = True
            r = await cx.post(f"/api/projects/{own}/github/sync")
            STATE[how] = False
            ck(f"(G) sync now with {how} -> 429, not 502", r.status_code == 429
               and r.json()["detail"] == "GitHub rate limit reached", r.text)
            ck("(G) the tickets stay as they were, the link says why and holds off",
               store.list_tickets(own) == before
               and store.get_project_github(own)["error"] == "GitHub rate limit reached"
               and store.get_project_github(own)["backoff_until"])
            link = store.get_project_github(own)
            link.pop("backoff_until")
            store.set_project_github(own, link)
        for how in ("gql_rate_limit", "http_rate_limit"):
            STATE[how] = True
            r = await cx.post(f"/api/projects/{own}/github", json={"repo": "acme/app", "issue_label": "tares:own"})
            STATE[how] = False
            ck(f"(G) linking with {how} -> 429, not 400", r.status_code == 429
               and r.json()["detail"] == "GitHub rate limit reached", r.text)
        link = store.get_project_github(own)
        link.pop("backoff_until", None)
        store.set_project_github(own, link)
        STATE["http_raw"] = (502, {})
        r = await cx.post(f"/api/projects/{own}/github/sync")
        STATE["http_raw"] = None
        ck("(G) GitHub answering 502 is still 502", r.status_code == 502, r.text)
        r = await cx.post(f"/api/projects/{own}/github/sync")
        ck("(G) and the next sync clears it", r.status_code == 200 and r.json()["github"]["error"] is None, r.text)

        await cx.aclose()


async def run():
    await main()
    await api()


if __name__ == "__main__":
    asyncio.run(run())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
