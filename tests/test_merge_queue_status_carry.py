"""A review that came only as the `factory/review` status, and a branch update by the merge queue.

The queue brings a branch up to date and keeps the review when the pull request's changes are the
same. With no review round to go by, the status on the new commit decides: a status that does not
pass (failure, or still pending) is never overridden by the queue's own pass.

One in-process cell with the real store, merge queue, review gate and GitHub CI source. GitHub is a
stateful fake behind `factory_github._transport`.

Run: .venv/bin/python tests/test_merge_queue_status_carry.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-status-carry-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = "root-token-status-carry"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the daemon's own loop stays out of the way

import httpx

from tares import factory_api, factory_github, merge_queue
from tares.integrations import hooks
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake GitHub ──────────────────────────────────────────────────────────
PRS = {}            # repo -> {head, state, merged}; one pull request, number 1, per repo
MERGES = []         # (repo, sha), every merge call
BEHIND = {}         # repo -> commits the base has that the head lacks
STATUSES = {}       # head -> the factory/review commit status on it (a state word)
REVIEW_RUNS = {}    # head -> the conclusion of a factory/review check run on it
FAIL_READ = {}      # head -> which read of its commit status answers 500, counting from 1
STATUS_READS = {}   # head -> how many times its commit status was read
FILES = [{"filename": "a.py", "status": "modified", "patch": "@@ -1,3 +1,3 @@\n keep\n-old\n+new"}]


def handler(req: httpx.Request):
    p = req.url.path.strip("/").split("/")
    method = req.method
    if len(p) < 4 or p[0] != "repos":
        return httpx.Response(404, json={})
    repo = f"{p[1]}/{p[2]}"
    if p[3] == "pulls" and method == "GET" and len(p) == 5 and repo in PRS:
        pr = PRS[repo]
        return httpx.Response(200, json={
            "number": 1, "title": "PR", "state": pr["state"], "merged": pr["merged"],
            "draft": False, "html_url": f"https://github.com/{repo}/pull/1",
            "head": {"sha": pr["head"], "ref": "factory/T1", "repo": {"full_name": repo}},
            "base": {"ref": "main"}, "mergeable": True, "mergeable_state": "clean"})
    if p[3] == "pulls" and method == "PUT" and p[-1] == "update-branch":
        return httpx.Response(202, json={"message": "Updating pull request branch."})
    if p[3] == "pulls" and method == "PUT" and p[-1] == "merge":
        MERGES.append((repo, json.loads(req.content or b"{}").get("sha")))
        PRS[repo].update(merged=True, state="closed")
        return httpx.Response(200, json={"merged": True, "message": "merged"})
    if p[3] == "compare" and method == "GET":
        return httpx.Response(200, json={"behind_by": BEHIND.get(repo, 0), "ahead_by": 1,
                                         "files": FILES})
    if p[3] == "git" and method == "DELETE":
        return httpx.Response(204, json={})
    if p[3] == "commits" and p[-1] == "status":
        head = p[4]
        STATUS_READS[head] = STATUS_READS.get(head, 0) + 1
        if FAIL_READ.get(head) == STATUS_READS[head]:
            return httpx.Response(500, json={})
        got = ([{"context": "factory/review", "state": STATUSES[head], "description": "Looked",
                 "updated_at": "2026-11-25T10:00:00Z"}] if head in STATUSES else [])
        return httpx.Response(200, json={"statuses": got})
    if p[3] == "commits" and p[-1] == "check-runs":
        head = p[4]
        runs = [{"name": "build", "status": "completed", "conclusion": "success"}]
        if head in REVIEW_RUNS:
            runs.append({"name": "factory/review", "status": "completed",
                         "conclusion": REVIEW_RUNS[head], "completed_at": "2026-11-25T10:00:00Z",
                         "output": {"title": "Looked"}})
        return httpx.Response(200, json={"check_runs": runs})
    return httpx.Response(404, json={})


factory_github._transport = httpx.MockTransport(handler)


async def rails(stage, project, payload, ticket=""):
    return []


hooks.run = rails


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    store.upsert_github_credential("main", "tok-main")
    store.set_choice("ci", "github", {}, "test", default=False)    # a project may choose it
    person = store.create_person("Priya", email="priya@example.com")
    heads = iter(f"{n:x}".ljust(40, "0") for n in range(0xa00, 0xb00))

    def queue_heads(tid):
        return [r["head"] for r in store.ticket_reviews(tid) if r["by"] == "merge-queue"]

    def merged(repo):
        return [s for r, s in MERGES if r == repo]

    def queue_row(repo):
        got = store.merge_queue_list(repo, "main")
        return got[-1] if got else None

    def said(tid):
        return [h for h in store.ticket_history(tid) if h["field"] == "merge_queue"]

    async def tick():
        await merge_queue.tick(store)

    async def approved(name):
        """A pull request approved by the `factory/review` status alone (no review round), at L4
        with the Tares reviewer and GitHub CI. Returns (project, ticket, repo, head)."""
        uid, repo, a = f"uc_{name}", f"acme/{name}", next(heads)
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        store.set_handover(uid, [{"path": "/w/a", "name": name}], "L4-ship", None, now_utc(),
                           person_id=person["id"])
        store.set_project_choice(uid, "ci", "github", {}, by="test")
        tid = store.create_ticket(uid, "tares", "Work", repo=repo)
        PRS[repo] = {"head": a, "state": "open", "merged": False}
        STATUSES[a] = "success"
        store.update_ticket(tid, stage="review", pr={
            "url": f"https://github.com/{repo}/pull/1", "number": 1, "repo": repo, "head": a,
            "verdict": "pass", "verdict_head": a})
        return uid, tid, repo, a

    async def update(repo, say=None, run=None, fail_read=None):
        """The base moves: a tick asks GitHub to update the branch, GitHub makes the commit (with
        `say` as its factory/review status, or `run` as that check run), and the next tick decides
        whether the review is carried onto it. Returns the new commit."""
        BEHIND[repo] = 2
        await tick()
        assert queue_row(repo)["state"] == "waiting_ci", queue_row(repo)
        b = PRS[repo]["head"] = next(heads)
        BEHIND[repo] = 0
        if say:
            STATUSES[b] = say
        if run:
            REVIEW_RUNS[b] = run
        if fail_read:
            FAIL_READ[b] = fail_read
        await tick()
        return b

    def sent_back(label, tid, repo, word):
        ck(f"{label}: no merge over a factory/review status that does not pass",
           merged(repo) == [], f"MERGE called with {merged(repo)}")
        got = said(tid)
        ck(f"{label}: the row goes and the ticket says it needs a new review",
           queue_row(repo) is None and len(got) == 1 and got[0]["value"] == "needs_review"
           and word in got[0]["reason"] and "—" not in got[0]["reason"], (queue_row(repo), got))

    async with app.router.lifespan_context(app):
        print("== a status on the new commit that does not pass ==")
        for name, kw, word in (("failing", {"say": "failure"}, "changes"),
                               ("failingrun", {"run": "failure"}, "changes"),
                               ("waiting", {"say": "pending"}, "pending")):
            uid, tid, repo, _ = await approved(name)
            b = await update(repo, **kw)
            await tick()
            await tick()
            ck(f"{name}: the queue writes no pass on the new commit", queue_heads(tid) == [],
               queue_heads(tid))
            sent_back(name, tid, repo, word)

        # `waiting` above: the review that was still running passes, the pull request sync reads it
        STATUSES[b] = "success"
        await factory_api.refresh_pr(store, uid, store.get_ticket(uid, tid))
        await tick()
        await tick()
        ck("waiting: once the status passes, the pull request enters again and merges",
           merged(repo) == [b], (merged(repo), queue_row(repo), store.get_ticket(uid, tid)["pr"]))

        print("== the review is carried as before ==")
        for name, kw in (("passing", {"say": "success"}), ("silent", {}),
                         ("crashed", {"say": "error"})):    # `error` is no verdict
            uid, tid, repo, _ = await approved(name)
            b = await update(repo, **kw)
            await tick()
            ck(f"{name}: carried and merged", queue_heads(tid) == [b] and merged(repo) == [b],
               (queue_heads(tid), merged(repo), queue_row(repo)))

        print("== a second update, after the queue carried it once ==")
        for name, say, word in (("twice", "failure", "changes"), ("twicewait", "pending", "pending")):
            uid, tid, repo, _ = await approved(name)
            # a commit made while the base has moved again: the queue carries and does not merge yet
            BEHIND[repo] = 2
            await tick()
            b = PRS[repo]["head"] = next(heads)
            await tick()
            await tick()            # still behind: it asks for the second update
            ck(f"{name}: the first commit is carried and waits for the second",
               queue_heads(tid) == [b] and merged(repo) == []
               and (queue_row(repo) or {}).get("state") == "waiting_ci",
               (queue_heads(tid), merged(repo), queue_row(repo)))
            c = PRS[repo]["head"] = next(heads)
            STATUSES[c] = say
            BEHIND[repo] = 0
            await tick()
            await tick()
            await tick()
            ck(f"{name}: only the first commit got the queue's pass", queue_heads(tid) == [b],
               (queue_heads(tid), b, c))
            sent_back(name, tid, repo, word)

            # the outside review passes the second commit after all, the pull request sync reads it
            STATUSES[c] = "success"
            await factory_api.refresh_pr(store, uid, store.get_ticket(uid, tid))
            for _ in range(4):
                await tick()
            ck(f"{name}: once the status passes, the pull request enters again and merges",
               merged(repo) == [c], (merged(repo), queue_row(repo), store.get_ticket(uid, tid)["pr"]))

        print("== the new commit's status cannot be read ==")
        # the queue reads the new commit three times in the tick that decides: twice for its gates,
        # then once before it carries. Only the third one fails, so the gates are all fine
        for name, say in (("unreadfail", "failure"), ("unreadpass", "success")):
            uid, tid, repo, _ = await approved(name)
            b = await update(repo, say=say, fail_read=3)
            ck(f"{name}: not carried unread, the row waits",
               queue_heads(tid) == [] and merged(repo) == []
               and (queue_row(repo) or {}).get("state") == "waiting_ci",
               (STATUS_READS.get(b), queue_heads(tid), merged(repo), queue_row(repo)))
            await tick()
            await tick()
            if say == "success":
                ck(f"{name}: read the next time, it is carried and merged",
                   queue_heads(tid) == [b] and merged(repo) == [b],
                   (queue_heads(tid), merged(repo), queue_row(repo)))
            else:
                ck(f"{name}: read the next time, no pass is written", queue_heads(tid) == [],
                   queue_heads(tid))
                sent_back(name, tid, repo, "changes")

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
