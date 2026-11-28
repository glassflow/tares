"""A ticket list does not wait on GitHub, and a ticket read refreshes only its own pull request
(TR-1474).

The ticket list (also what MCP list_tickets answers from) and the project's GitHub page answer
from the pull request state kept so far: no GitHub call, no wait. A single ticket, its review
brief and its builder brief refresh that ticket's own pull request and no other, wait for it at
most a moment, and share one refresh per pull request between reads that ask together. GitHub is
a fake that records its calls and answers a pull request only after 5 seconds.

Run: .venv/bin/python tests/test_pr_refresh_scope.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-pr-scope-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-pr-scope"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

from tares import factory_api, factory_github
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


REPO = "acme/api"
SLOW = 5           # seconds GitHub takes to answer a pull request
FAST = 0.5         # a read that makes no GitHub call answers well inside this
CAPPED = 3.0       # a ticket read waits REFRESH_WAIT (2.5) for GitHub, with a little room


def old():
    return (now_utc() - timedelta(days=1)).isoformat()


def pr(n, **kw):
    return {"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO, "number": n, "title": f"PR {n}",
            "head": f"{n:04d}abcdef", "branch": f"factory/x{n}", "base": "main", "state": "open",
            "merged": False, "author": "crew-bot", "checked_at": old(), "error": None, **kw}


class GitHub:
    """A GitHub that records every pull request read (a path ending /pulls/N) and answers it
    after SLOW seconds; a 500 for the pull requests in `broken`."""

    def __init__(self):
        self.pulls: list[int] = []
        self.broken: set[int] = set()

    async def handler(self, req: httpx.Request):
        path = req.url.path
        if "/pulls/" in path:
            n = int(path.rsplit("/", 1)[1])
            self.pulls.append(n)
            await asyncio.sleep(SLOW)
            if n in self.broken:
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json={
                "title": f"PR {n}", "state": "open", "merged": False, "head": {"sha": f"{n:04d}abcdef", "ref": f"factory/x{n}"},
                "base": {"ref": "main"}, "user": {"login": "crew-bot"}, "draft": False,
                "html_url": f"https://github.com/{REPO}/pull/{n}"})
        return httpx.Response(200, json={"statuses": [], "check_runs": []})


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    gh = GitHub()

    async def tokens(store, repo):
        return [("tok", "https://api.github.com")]

    factory_github._transport = httpx.MockTransport(gh.handler)
    factory_github._tokens = tokens
    cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                           headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)

    uid = "uc_prscope"
    store.create_project(uid, "custom", "pr-scope", {}, goal="A goal")
    store.set_project_kind(uid, "software_factory")

    def ticket(title, **fields):
        tid = store.create_ticket(uid, "tares", title)
        store.update_ticket(tid, **fields)
        return tid

    # six tickets with a stale pull request each, so any of them could be read again
    tid = {n: ticket(f"Ticket {n}", stage="doing", status="in_progress", pr=pr(n)) for n in range(1, 7)}
    tid[9] = ticket("Not asked", stage="doing", status="in_progress", pr=pr(9))
    nopr = ticket("No pull request yet")
    merged = ticket("Merged", stage="merged", status="done",
                    pr=pr(20, state="closed", merged=True, changed_files=3, additions=9, deletions=1))
    bare = ticket("Only a link", stage="merged", status="done", pr={"url": f"https://github.com/{REPO}/pull/21"})
    empty = ticket("Empty values", stage="doing", status="in_progress", pr={"repo": REPO, "number": None})
    broke = ticket("GitHub fails", stage="doing", status="in_progress", pr=pr(7))
    boom = ticket("Refresh raises", stage="doing", status="in_progress", pr=pr(8))
    gh.broken.add(7)

    async def timed(path):
        t0 = time.monotonic()
        r = await cx.get(path)
        return r, time.monotonic() - t0

    print("== the lists answer from what is stored ==")
    r, took = await timed(f"/api/projects/{uid}/tickets")
    ck("the ticket list answers", r.status_code == 200 and len(r.json()["tickets"]) == len(tid) + 6, r.text[:200])
    ck(f"the ticket list is quick ({took:.2f}s)", took < FAST, took)
    r, took = await timed(f"/api/factory/projects/{uid}/github")
    ck("the GitHub page answers", r.status_code == 200 and r.json()["counts"]["open"] >= 7, r.text[:200])
    ck(f"the GitHub page is quick ({took:.2f}s)", took < FAST, took)
    ck("neither called GitHub", gh.pulls == [], gh.pulls)

    print("== a ticket read refreshes its own pull request ==")
    expect: list[int] = []
    for label, path in (
            ("the ticket", f"/api/projects/{uid}/tickets/{tid[1]}"),
            ("the ticket page", f"/api/factory/projects/{uid}/tickets/{tid[2]}"),
            ("the review brief", f"/api/projects/{uid}/tickets/{tid[3]}/brief"),
            ("the builder brief", f"/api/projects/{uid}/tickets/{tid[4]}/builder-brief")):
        n = len(expect) + 1
        expect.append(n)
        r, took = await timed(path)
        ck(f"{label} answers", r.status_code == 200, r.text[:300])
        ck(f"{label} waits for GitHub at most a moment ({took:.2f}s)", took < CAPPED, took)
        ck(f"{label} read only its own pull request, once", gh.pulls == expect,
           f"read {gh.pulls}, wanted {expect}")
    ck("the pull requests nobody asked about were never read", not {5, 6, 9} & set(gh.pulls), gh.pulls)

    print("== two reads of one ticket make one GitHub call ==")
    before = list(gh.pulls)
    t0 = time.monotonic()
    a, b = await asyncio.gather(cx.get(f"/api/projects/{uid}/tickets/{tid[5]}"),
                                cx.get(f"/api/factory/projects/{uid}/tickets/{tid[5]}"))
    took = time.monotonic() - t0
    ck("both answer", a.status_code == 200 and b.status_code == 200, (a.text[:200], b.text[:200]))
    ck(f"both inside the cap ({took:.2f}s)", took < CAPPED, took)
    ck("one GitHub call for the two", gh.pulls[len(before):] == [5], gh.pulls[len(before):])

    print("== nothing to read, nothing to wait for ==")
    before = list(gh.pulls)
    for label, ref in (("no pull request", nopr), ("a merged pull request already read in full", merged),
                       ("a link with no repo", bare), ("a repo with no number", empty)):
        for path in (f"/api/projects/{uid}/tickets/{ref}", f"/api/factory/projects/{uid}/tickets/{ref}",
                     f"/api/projects/{uid}/tickets/{ref}/brief",
                     f"/api/projects/{uid}/tickets/{ref}/builder-brief"):
            r, took = await timed(path)
            ck(f"{label}: {path.split(uid + '/')[1].split('/')[0]} {path.rsplit('/', 1)[1][:12]} answers quickly",
               r.status_code == 200 and took < FAST, (r.status_code, took, r.text[:200]))
    ck("no GitHub call for any of them", gh.pulls == before, gh.pulls[len(before):])

    print("== a read that has something to write refreshes even a fresh pull request ==")
    fresh = now_utc().isoformat()
    unmarked = ticket("Merged on GitHub, not marked", stage="review", status="in_progress",
                      pr=pr(30, state="closed", merged=True, changed_files=2, checked_at=fresh))
    changes = ticket("Changes asked, head known", stage="changes", status="in_progress",
                     pr=pr(31, checked_at=fresh))
    store.add_review(uid, changes, None, "0000oldhead", "changes", "ran it", "", "reviewer", [], {},
                     lambda *a, **k: None, now_utc())     # a verdict given on an older commit
    idle = ticket("Fresh and idle", stage="doing", status="in_progress", pr=pr(32, checked_at=fresh))
    before = list(gh.pulls)
    r, took = await timed(f"/api/projects/{uid}/tickets/{unmarked}")
    ck("a fresh pull request that is merged but not marked is marked merged on the read",
       r.status_code == 200 and r.json()["stage"] == "merged", (r.status_code, r.text[:200]))
    ck("a merge already read in full makes no GitHub call", gh.pulls == before, gh.pulls[len(before):])
    r, took = await timed(f"/api/projects/{uid}/tickets/{changes}")
    ck("new commits after the verdict move a fresh changes ticket back to review, with no GitHub call",
       r.status_code == 200 and r.json()["stage"] == "review" and 31 not in gh.pulls[len(before):],
       (r.status_code, r.text[:160], gh.pulls[len(before):]))
    r, took = await timed(f"/api/projects/{uid}/tickets/{idle}")
    ck("a fresh pull request with nothing to follow is not read", 32 not in gh.pulls and took < FAST,
       (gh.pulls[len(before):], took))

    print("== a failure never fails the read ==")
    r, took = await timed(f"/api/projects/{uid}/tickets/{broke}")
    ck(f"GitHub answering 500 gives the ticket, as cached ({took:.2f}s)", r.status_code == 200 and took < CAPPED,
       (r.status_code, took))

    async def raises(*a, **k):
        raise RuntimeError("the refresh broke")

    real, factory_github.read_pr = factory_github.read_pr, raises
    try:
        r, took = await timed(f"/api/projects/{uid}/tickets/{boom}")
    finally:
        factory_github.read_pr = real
    ck("a refresh that raises gives the ticket too", r.status_code == 200 and r.json()["id"] == boom,
       (r.status_code, r.text[:200]))
    ck("and does not wait", took < FAST, took)

    ck("no refresh is left registered for a finished one", all(not t.done() for t in factory_api._REFRESHING_PR.values()),
       factory_api._REFRESHING_PR)
    await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
