"""A factory project's GitHub page: GET /api/factory/projects/{uid}/github, and the newest pull
requests on the project page (`prs_recent`, `prs_total`).

The page lists every pull request the project's tickets opened, by repo: open first, then merged,
then closed without merging, newest first in each. Each says its ticket, CI, the crew's review,
the size of the change and who merged it. A reason GitHub could not be read that all of them
share is said once. A GitHub read keeps the size, author and who merged; a merged pull request
read before that is read once more for them, then never again.

Run: .venv/bin/python tests/test_factory_project_github.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-github-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-github"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

from tares import factory as factory_mod
from tares import factory_github
from tares import words
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


API, WEB = "acme/api", "acme/web"


def ago(**kw):
    return (now_utc() - timedelta(**kw)).isoformat()


def pr(repo, n, title, state="open", **kw):
    return {"url": f"https://github.com/{repo}/pull/{n}", "repo": repo, "number": n, "title": title,
            "head": f"{n:04d}abcdef", "branch": f"factory/x{n}", "base": "main", "state": state,
            "merged": False, "author": "crew-bot", "checked_at": now_utc().isoformat(), "error": None, **kw}


def read_once():
    print("== a GitHub read keeps the size, author and who merged ==")
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/pulls/9"):
            return httpx.Response(200, json={
                "title": "Add it", "state": "closed", "merged": True, "merged_at": "2026-11-01T10:00:00Z",
                "merge_commit_sha": "m1", "head": {"sha": "abc1234567", "ref": "factory/T1"},
                "base": {"ref": "main"}, "user": {"login": "crew-bot"}, "merged_by": {"login": "kim"},
                "created_at": "2026-10-31T10:00:00Z", "closed_at": "2026-11-01T10:00:00Z", "draft": False,
                "commits": 3, "changed_files": 5, "additions": 120, "deletions": 7,
                "html_url": "https://github.com/acme/api/pull/9"})
        return httpx.Response(200, json={"statuses": [], "check_runs": []})

    async def tokens(store, repo):
        return [("tok", "https://api.github.com")]

    old_t, old_tok = factory_github._transport, factory_github._tokens
    factory_github._transport = httpx.MockTransport(handler)
    factory_github._tokens = tokens
    try:
        out = asyncio.run(factory_github.read_pr(None, API, 9, {"repo": API, "number": 9}))
    finally:
        factory_github._transport, factory_github._tokens = old_t, old_tok
    ck("size, commits, author, who merged, base and dates are kept",
       (out["changed_files"], out["additions"], out["deletions"], out["commits"], out["author"],
        out["merged_by"], out["base"], out["created_at"], out["closed_at"], out["draft"])
       == (5, 120, 7, 3, "crew-bot", "kim", "main", "2026-10-31T10:00:00Z", "2026-11-01T10:00:00Z", False), out)
    ck("one call for the pull request, two for CI", len(calls) == 3, calls)
    ck("a merged PR read before the size was kept is read once more",
       factory_github.stale({"repo": API, "number": 9, "merged": True, "checked_at": ago(minutes=2)}))
    ck("a merged PR with its size is never read again", not factory_github.stale(out))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                           headers={"Authorization": f"Bearer {TOKEN}"})

    def project(name):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A goal")
        store.set_project_kind(uid, "software_factory")
        return uid

    def ticket(uid, title, **fields):
        tid = store.create_ticket(uid, "tares", title)
        if fields:
            store.update_ticket(tid, **fields)
        return tid

    g = project("project-g")
    store.set_handover(g, [{"path": "/w/web", "name": "web"}, {"path": "/w/api", "name": "api"}],
                       None, None, now_utc())
    ticket(g, "Merged long ago", stage="merged", status="done",
           pr=pr(API, 41, "First", state="closed", merged=True, merged_at=ago(days=2), merged_by="kim",
                 created_at=ago(days=3), changed_files=7, additions=300, deletions=18, commits=4))
    ticket(g, "Merged lately", stage="merged", status="done",
           pr=pr(API, 44, "Second", state="closed", merged=True, merged_at=ago(hours=5), merged_by="alex",
                 created_at=ago(days=1), changed_files=3, additions=96, deletions=41, commits=2))
    t3 = ticket(g, "In review", stage="review", status="in_progress",
                pr=pr(API, 47, "Third", created_at=ago(hours=3), ci="success",
                      changed_files=2, additions=58, deletions=4, commits=1))
    ticket(g, "Given up", status="canceled",
           pr=pr(API, 43, "Background job", state="closed", created_at=ago(days=2), closed_at=ago(days=1),
                 changed_files=9, additions=420, deletions=30, commits=5))
    ticket(g, "A draft", stage="doing", status="in_progress",
           pr=pr(WEB, 115, "Progress bar", created_at=ago(minutes=40), draft=True, ci="pending"))
    ticket(g, "Merged by the builder, never read", stage="merged", status="done",
           pr={"url": f"https://github.com/{WEB}/pull/110"})
    ticket(g, "No pull request yet")
    store.add_review(g, t3, None, "0047abcdef", "pass", "ran the tests", None, "crew-reviewer", [], {},
                     factory_mod.finding_kind, now_utc())
    n = project("project-n")
    ticket(n, "Nothing opened")

    print("== the GitHub page ==")
    r = await cx.get(f"/api/factory/projects/{g}/github")
    ck("the route answers", r.status_code == 200, r.text[:300])
    a = r.json()
    ck("exactly the page's fields", set(a) == {"id", "name", "icon", "repos", "counts", "checked", "github_error", "now"}, sorted(a))
    ck("repos in the hand-over's order, by their GitHub name", [x["name"] for x in a["repos"]] == [WEB, API],
       [x["name"] for x in a["repos"]])
    api = next(x for x in a["repos"] if x["name"] == API)
    ck("open first, then merged (newest first), then closed",
       [p["number"] for p in api["prs"]] == [47, 44, 41, 43], [p["number"] for p in api["prs"]])
    ck("counts per repo and in all", api["counts"] == {"open": 1, "merged": 2, "closed": 1}
       and a["counts"] == {"open": 2, "merged": 3, "closed": 1}, [api["counts"], a["counts"]])
    ck("lines added and removed count the merged ones only", (api["added"], api["removed"]) == (396, 59),
       (api["added"], api["removed"]))
    p47, p44, p43 = api["prs"][0], api["prs"][1], api["prs"][3]
    ck("an open PR: its ticket, CI, review, size and who opened it",
       p47["ticket"] == "T3" and p47["ticket_href"] == f"/projects/{g}/tickets/T3" and p47["ci"] == "CI green"
       and p47["review"] == "round 1: passed" and p47["size"]["files"] == 2
       and p47["when"] == "opened 3 hours ago by crew-bot" and p47["state_label"] == "Open", p47)
    ck("a merged PR says who merged it and has no CI", p44["when"] == "merged 5 hours ago by alex"
       and p44["ci"] is None and p44["state_label"] == "Merged", p44)
    ck("a closed PR says closed without merging", p43["state"] == "closed"
       and p43["state_label"] == "Closed without merging" and p43["when"] == "closed 1 day ago", p43)
    web = next(x for x in a["repos"] if x["name"] == WEB)
    ck("a draft is a draft", web["prs"][0]["state"] == "draft" and web["prs"][0]["state_label"] == "Draft", web["prs"][0])
    never = next(p for p in web["prs"] if p["number"] == 110)
    ck("a PR the builder marked merged, never read from GitHub, is merged with no size",
       never["state"] == "merged" and never["size"] is None and never["repo"] == WEB, never)
    ck("the last good read", a["checked"] == "just now", a["checked"])
    found = words.offenders_in(a, ("name", "title", "ticket_title", "branch", "base", "when"))
    ck("the words list finds nothing", found == [], found)

    print("== a reason all reads share is said once ==")
    for t in store.list_tickets(g):
        p_ = t["pr"] or {}
        if p_.get("number") in (47, 115):
            store.update_ticket(t["id"], pr={**p_, "error": "no GitHub credential on this Tares can read it (Settings, GitHub)"})
    a = (await cx.get(f"/api/factory/projects/{g}/github")).json()
    rows = [p for x in a["repos"] for p in x["prs"]]
    ck("the banner says it, starting with a capital", a["github_error"]
       == "No GitHub credential on this Tares can read it (Settings, GitHub).", a["github_error"])
    ck("no row repeats it", all(p["error"] is None for p in rows), [p["error"] for p in rows])
    for t in store.list_tickets(g):
        p_ = t["pr"] or {}
        if p_.get("number") == 115:
            store.update_ticket(t["id"], pr={**p_, "error": "GitHub answered 404 for acme/web#115"})
    a = (await cx.get(f"/api/factory/projects/{g}/github")).json()
    rows = {p["number"]: p for x in a["repos"] for p in x["prs"]}
    ck("different reasons: each row says its own", rows[115]["error"] == "GitHub answered 404 for acme/web#115"
       and rows[47]["error"].startswith("no GitHub credential") and a["github_error"] == "2 could not be read, each says why.",
       [rows[115]["error"], rows[47]["error"], a["github_error"]])

    print("== the project page ==")
    d = (await cx.get(f"/api/factory/projects/{g}")).json()
    ck("how many pull requests", d["prs_total"] == 6, d.get("prs_total"))
    ck("the three newest, newest first", [p["number"] for p in d["prs_recent"]] == [115, 47, 44],
       [p["number"] for p in d.get("prs_recent", [])])
    d = (await cx.get(f"/api/factory/projects/{n}")).json()
    ck("a project with none has none", d["prs_total"] == 0 and d["prs_recent"] == [], [d["prs_total"], d["prs_recent"]])
    a = (await cx.get(f"/api/factory/projects/{n}/github")).json()
    ck("and its GitHub page is empty", a["repos"] == [] and a["counts"] == {"open": 0, "merged": 0, "closed": 0}
       and a["checked"] is None and a["github_error"] is None, a)

    print("== who may read it ==")
    ck("not a project: 404", (await cx.get("/api/factory/projects/uc_nothing/github")).status_code == 404)
    store.create_project("uc_plain", "custom", "plain", {}, goal="x")
    ck("not a factory project: 404", (await cx.get("/api/factory/projects/uc_plain/github")).status_code == 404)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as anon:
        ck("no token: refused", (await anon.get(f"/api/factory/projects/{g}/github")).status_code in (401, 403))
    await cx.aclose()


read_once()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
