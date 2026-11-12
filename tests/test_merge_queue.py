"""The merge queue (TR-856): Tares merges a ready pull request itself, one at a time per (repo, base),
in order, with no agent and no tokens.

One in-process cell. GitHub is a stateful fake behind `factory_github._transport` (pull requests,
merge, branch delete, rate-limit header); the review gate and CI are fakes the projects choose; the
guardrail hook answers nothing. Each scenario has its own repo so queues do not meet. Covered:
 - the store: one open row per pull request, the claim (queued only, one `merging` per repo and
   base, approval needed at L3-review), the merge method setting;
 - entry: only a ready pull request of a ticket in review, of a project a crew has, whose head is
   not a milestone branch, gets a row;
 - order: milestone position, ticket position, entry time; a row with an unmerged dependency waits
   and keeps its place while a later row that does not need it passes; merges go one at a time;
 - L4-ship merges without asking: the merge call carries the head and the project's method, the
   branch is deleted (a 422 on that is ignored), the ticket is merged with its history;
 - L3-review: one desk item addressed to the hand-over's person with the question and options in
   the ticket, asked once however many ticks; yes merges that commit; no, a withdrawn question and a
   yes that asks for a change block with the person's words; the console route approves;
 - a new commit withdraws the question, clears the approval and deletes the row, and the pull
   request enters again for the new commit; a closed one is sent back; one merged by anyone else
   becomes merged with no merge call and no error;
 - GitHub's answers: no credential can write (403 from all) blocks with ONE desk item for the repo;
   a 405 blocks the row with a desk item carrying GitHub's message, never the builder; 409 re-reads
   the pull request (moved head: row dropped; conflict: sent back; else queued); 422 keeps the row
   queued; a spent rate limit keeps it queued; a second credential is tried when the first is refused;
 - TR-858: behind a base on a GitHub CI project, the branch is updated and the row waits for a check on
   the new head; the same changes keep the review (a Tares review is written for the new head, an
   outside gate is left alone), different ones go back to the review gate; local CI never updates;
   mergeable null waits ten ticks then blocks with a desk item; a conflict (or a 422 on the update)
   is sent back with the files named; a reviewed pull request whose CI fails or a guardrail blocks
   is sent back once per head; same_changes, compare, pr_files and update_branch on their own
   (null, empty and malformed input included);
 - two ticks at once merge once; a `merging` row left by a dead tick is recovered;
 - the rate limit: remembered per API base, a missing or odd header changes nothing, a low value
   skips the tick with one line;
 - the merge-method route, the approve route, /health, the AUTONOMY text, the sync wiring, the
   line printed when the sync is off, and no em dash in what Tares says.

Run: .venv/bin/python tests/test_merge_queue.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-merge-queue-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-merge-queue"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the daemon's own loop stays out of the way

import httpx

from tares import factory as factory_mod
from tares import factory_api, factory_github, merge_queue, merge_ready
from tares.integrations import hooks, registry
from tares.integrations.ci import CISource
from tares.integrations.review import ReviewGate
from tares.integrations.types import CIState, Verdict
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


API = "https://api.github.com"

# ── the fake GitHub: state a scenario sets, calls it can read back ───────────────────────────────
PRS = {}                 # (repo, number) -> {head, branch, base, state, merged, draft}
MODE = {}                # repo -> how the merge call answers (default "ok")
MERGES = []              # (repo, number, body, token), every merge call
DELETES = []             # (repo, branch)
DELETE_CODE = {"value": 204}
RATE = {"value": 4000}   # the x-ratelimit-remaining header; None sends none
NOT_APPROVED = set()     # (repo, number) the review gate does not approve
CI_FAIL = set()          # (repo, number) whose CI fails
ALL_CALLS = []           # every request path (the network never sees one: this is the transport)
# TR-858: what GitHub says about a branch that is behind, in conflict, or being updated
MERGEABLE = {}           # (repo, number) -> (mergeable, mergeable_state); default (True, "clean")
BEHIND = {}              # (repo, number) -> commits the base has that the head lacks
FILES = {}               # (repo, number) -> the files the compare lists for the head now
AFTER = {}               # (repo, number) -> the files once the branch is updated (default: same)
UPDATE = {}              # repo -> how update-branch answers (default "ok", head changes at once)
UPDATES = []             # (repo, number, body), every update-branch call
PENDING = {}             # (repo, number) -> the head an accepted update will make (see land)
CHECKED = set()          # heads that have a check run
PR_FILES = {}            # (repo, number) -> the pull request's files; missing answers 404
COMPARE_CODE = {}        # repo -> the status the compare answers with (default 200)
RAILS = {}               # repo -> the guardrail decisions before a merge (default none)
CI_NONE = set()          # repos whose CI source says "none"
COMPARE_BODY = {}        # repo -> a body the compare answers 200 with instead of its own


BEFORE = "@@ -1,3 +1,3 @@\n keep\n-old\n+new"
HUNK_MOVED = "@@ -41,3 +41,3 @@\n elsewhere\n-old\n+new"     # the base moved: header and context differ
DIFFERENT = "@@ -1,3 +1,3 @@\n keep\n-old\n+newer"


def fl(name, patch, status="modified"):
    return {"filename": name, "status": status, "patch": patch}


def sha(n, v=0):
    return ("abc1234" + f"{n:03d}{v:03d}").ljust(40, "0")


def handler(req: httpx.Request):
    path, method = req.url.path, req.method
    ALL_CALLS.append((method, path))
    token = req.headers.get("authorization", "").split()[-1]
    hdr = {} if RATE["value"] is None else {"x-ratelimit-remaining": str(RATE["value"])}
    p = path.strip("/").split("/")
    if len(p) < 4 or p[0] != "repos":
        return httpx.Response(404, json={}, headers=hdr)
    repo = f"{p[1]}/{p[2]}"
    if p[3] == "pulls" and method == "GET" and len(p) == 5:
        pr = PRS.get((repo, int(p[4])))
        if pr is None:
            return httpx.Response(404, json={}, headers=hdr)
        return httpx.Response(200, headers=hdr, json={
            "number": int(p[4]), "title": "PR", "state": pr["state"], "merged": pr["merged"],
            "draft": pr["draft"], "html_url": f"https://github.com/{repo}/pull/{p[4]}",
            "head": {"sha": pr["head"], "ref": pr["branch"],
                     "repo": {"full_name": pr.get("head_repo", repo)}},
            "base": {"ref": pr["base"]},
            "mergeable": MERGEABLE.get((repo, int(p[4])), (True, "clean"))[0],
            "mergeable_state": MERGEABLE.get((repo, int(p[4])), (True, "clean"))[1]})
    if p[3] == "pulls" and method == "GET" and p[-1] == "files":
        files = PR_FILES.get((repo, int(p[4])))
        return httpx.Response(404 if files is None else 200, json={} if files is None else files,
                              headers=hdr)
    if p[3] == "pulls" and method == "PUT" and p[-1] == "update-branch":
        n = int(p[4])
        UPDATES.append((repo, n, json.loads(req.content or b"{}")))
        mode = UPDATE.get(repo, "ok")
        if mode == "422_conflict":
            return httpx.Response(422, json={"message": "merge conflict between base and head"},
                                  headers=hdr)
        if mode == "422_plain":
            return httpx.Response(422, json={"message": "expected head sha didn't match"},
                                  headers=hdr)
        if mode == "403":
            return httpx.Response(403, json={"message": "Resource not accessible"}, headers=hdr)
        if mode == "403_rate":
            return httpx.Response(403, json={"message": "rate limit"},
                                  headers={"x-ratelimit-remaining": "0"})
        new = sha(n, 50 + len(UPDATES))
        if mode == "later":
            PENDING[(repo, n)] = new
        else:
            land(repo, n, new)
        return httpx.Response(202, json={"message": "Updating pull request branch."}, headers=hdr)
    if p[3] == "compare" and method == "GET":
        if COMPARE_CODE.get(repo, 200) != 200:
            return httpx.Response(COMPARE_CODE[repo], json={}, headers=hdr)
        if repo in COMPARE_BODY:
            return httpx.Response(200, json=COMPARE_BODY[repo], headers=hdr)
        head = p[4].split("...")[-1]
        n = next((k[1] for k, v in PRS.items() if k[0] == repo and v["head"] == head), None)
        if n is None:
            return httpx.Response(404, json={}, headers=hdr)
        return httpx.Response(200, headers=hdr, json={
            "behind_by": BEHIND.get((repo, n), 0), "ahead_by": 1, "files": FILES.get((repo, n), [])})
    if p[3] == "pulls" and method == "PUT" and p[-1] == "merge":
        n = int(p[4])
        body = json.loads(req.content or b"{}")
        MERGES.append((repo, n, body, token))
        mode = MODE.get(repo, "ok")
        if mode == "403_main" and token == "tok-main":
            return httpx.Response(403, json={"message": "Resource not accessible"}, headers=hdr)
        if mode == "403":
            return httpx.Response(403, json={"message": "Resource not accessible"}, headers=hdr)
        if mode == "403_secondary":      # GitHub's secondary limit: a retry-after, no remaining=0
            return httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"},
                                  headers={"retry-after": "60"})
        if mode == "403_rate":
            return httpx.Response(403, json={"message": "rate limit"},
                                  headers={"x-ratelimit-remaining": "0"})
        if mode == "405_empty":          # GitHub says no and gives no words
            return httpx.Response(405, json={}, headers=hdr)
        if mode == "409_moved":          # a commit landed after the claim
            PRS[(repo, n)]["head"] = sha(n, 9)
            return httpx.Response(409, json={"message": "Head branch was modified"}, headers=hdr)
        if mode == "409_conflict":       # the base moved and the pull request conflicts now
            MERGEABLE[(repo, n)] = (False, "dirty")
            return httpx.Response(409, json={"message": "Base branch was modified"}, headers=hdr)
        if mode in ("405", "409", "422"):
            return httpx.Response(int(mode), json={"message": f"GitHub says {mode}"}, headers=hdr)
        pr = PRS[(repo, n)]
        if body.get("sha") != pr["head"]:
            return httpx.Response(409, json={"message": "Head branch was modified"}, headers=hdr)
        pr.update(merged=True, state="closed")
        return httpx.Response(200, json={"merged": True, "message": "Pull Request successfully merged"},
                              headers=hdr)
    if p[3] == "git" and method == "DELETE":
        DELETES.append((repo, "/".join(p[6:])))
        return httpx.Response(DELETE_CODE["value"], json={}, headers=hdr)
    if p[3] == "commits":
        key = "statuses" if p[-1] == "status" else "check_runs"
        runs = ([{"name": "build", "status": "completed", "conclusion": "success"}]
                if key == "check_runs" and p[4] in CHECKED else [])
        return httpx.Response(200, json={key: runs}, headers=hdr)
    return httpx.Response(404, json={}, headers=hdr)


factory_github._transport = httpx.MockTransport(handler)


def land(repo, n, new=None):
    """The update GitHub accepted makes its commit: the head moves and the branch is not behind."""
    new = new or PENDING.pop((repo, n))
    PRS[(repo, n)]["head"] = new
    BEHIND[(repo, n)] = 0
    if (repo, n) in AFTER:
        FILES[(repo, n)] = AFTER[(repo, n)]
    return new


class FakeReview(ReviewGate):
    name = "fakerev"
    title = "Fake review"

    def __init__(self, store=None):
        self.store = store

    async def verdict(self, pr):
        if (pr.repo, pr.number) in NOT_APPROVED:
            return Verdict(state="none", head=pr.head)
        return Verdict(state="approved", head=pr.head, by="Fake", summary="Looks good")


class FakeCI(CISource):
    name = "fakeci"
    title = "Fake CI"

    def __init__(self, store=None):
        self.store = store

    async def state(self, pr, required):
        if (pr.repo, pr.number) in CI_FAIL:
            return CIState(state="failure", head=pr.head, failing=["test"])
        if pr.repo in CI_NONE:
            return CIState(state="none", head=pr.head)
        return CIState(state="success", head=pr.head)


class FakeGithubCI(FakeCI):
    """The CI integration named `github`: the only one whose branch Tares brings up to date."""
    name = "github"


async def rails(stage, project, payload, ticket=""):
    return RAILS.get(payload.get("repo"), [])


hooks.run = rails


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    registry.load()
    registry._loaded[("review", "fakerev")] = FakeReview
    registry._loaded[("ci", "fakeci")] = FakeCI
    registry._loaded[("ci", "github")] = FakeGithubCI
    store.upsert_github_credential("main", "tok-main")
    root = {"Authorization": f"Bearer {TOKEN}"}
    person = store.create_person("Priya", email="priya@example.com")

    def mk(headers=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=headers or dict(root))

    def project(name, autonomy=None, handed=True, review="fakerev", ci="fakeci"):
        uid = "uc_" + name.replace("-", "")
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        if handed:
            store.set_handover(uid, [{"path": "/w/a", "name": name}], autonomy, None, now_utc(),
                               person_id=person["id"])
        if review:      # None: the project keeps the Tares reviewer, the default gate
            store.set_project_choice(uid, "review", review, {}, by="test")
        store.set_project_choice(uid, "ci", ci, {}, by="test")
        return uid

    def ticket(uid, repo, title, n, milestone=None, position=None, depends=None, stage="review",
               branch=None, base="main", draft=False):
        tid = store.create_ticket(uid, "tares", title, repo=repo, milestone=milestone,
                                  depends_on=depends, position=position)
        store.update_ticket(tid, stage=stage, pr={"url": f"https://github.com/{repo}/pull/{n}",
                                                  "number": n, "repo": repo})
        PRS[(repo, n)] = {"head": sha(n), "branch": branch or f"factory/T{n}", "base": base,
                          "state": "open", "merged": False, "draft": draft}
        return tid

    def reviewed(uid, tid, repo, n):
        """The Tares reviewer's pass on the pull request's head now."""
        store.add_review(uid, tid, f"https://github.com/{repo}/pull/{n}", PRS[(repo, n)]["head"],
                         "pass", "Ran the tests", "", "crew-reviewer", [], {},
                         factory_mod.finding_kind, now_utc())

    def behind(repo, n, before, after, mode="ok"):
        """The pull request is behind its base; what an update will leave of its files, and how
        the update answers."""
        BEHIND[(repo, n)] = 2
        FILES[(repo, n)] = before
        AFTER[(repo, n)] = after
        UPDATE[repo] = mode

    def history(tid, field):
        return [h for h in store.ticket_history(tid) if h["field"] == field]

    def rows(repo, n=None, base="main"):
        out = [r for r in store.merge_queue_list(repo, base) if n is None or r["pr"] == n]
        return out

    def row(repo, n, base="main"):
        got = rows(repo, n, base)
        return got[-1] if got else None

    def stage(uid, tid):
        return store.get_ticket(uid, tid)["stage"]

    def desk(uid):
        return [d for d in store.list_desk("open", uid) if d["asked_by"] == "merge-queue"]

    def answer(d, choice, words=None):
        """What the person's answer on the desk leaves behind: a decision and a closed item."""
        store.add_decision(d["project"], d["id"], d["question"], words or choice, choice, False,
                           "project", True, None, False, d["tickets"], now_utc(), person_id=person["id"])
        store.close_desk_item(d["id"], "answered", None, now_utc())

    async def tick():
        await merge_queue.tick(store)

    def merged_calls(repo):
        return [(n, b) for r, n, b, _ in MERGES if r == repo]

    async with app.router.lifespan_context(app):
        print("== the store ==")
        a = store.merge_queue_add("o/store", "main", 1, "h1", None, None)
        ck("a row starts queued with its columns",
           set(a) == {"id", "repo", "base", "pr", "head", "ticket", "project_uid", "state", "reason",
                      "desk_item", "approved_head", "entered_at", "updated_at", "before_files",
                      "unknown_ticks"} and a["state"] == "queued"
           and a["before_files"] is None and a["unknown_ticks"] == 0, a)
        ck("the same open pull request is added once",
           store.merge_queue_add("o/store", "main", 1, "h1", None, None)["id"] == a["id"]
           and len(rows("o/store")) == 1)
        b = store.merge_queue_add("o/store", "main", 2, "h2", None, None)
        ck("a row cannot be taken while it is unapproved at L3-review", not store.merge_queue_claim(a["id"], True))
        ck("the first claim wins", store.merge_queue_claim(a["id"]))
        ck("the same row cannot be claimed twice", not store.merge_queue_claim(a["id"]))
        ck("no second claim on the same (repo, base) while one is merging", not store.merge_queue_claim(b["id"]))
        c = store.merge_queue_add("o/store", "other", 3, "h3", None, None)
        ck("another base is its own queue", store.merge_queue_claim(c["id"]))
        store.merge_queue_update(a["id"], state="merged")
        ck("once the first is done the next can go", store.merge_queue_claim(b["id"]))
        store.merge_queue_update(b["id"], state="queued", approved_head="h2")
        ck("approved for this head: the L3 claim works", store.merge_queue_claim(b["id"], True))
        store.merge_queue_update(b["id"], state="queued", head="h2-new")
        ck("approved for another head: the L3 claim does not", not store.merge_queue_claim(b["id"], True))
        ck("a merged row no longer counts as open",
           a["id"] not in [r["id"] for r in store.merge_queue_open()])
        try:
            store.merge_queue_update(b["id"], colour="red")
            bad = False
        except ValueError:
            bad = True
        ck("an unknown column is refused", bad)
        ck("the default merge method is squash", store.get_merge_method("uc_none") == "squash")

        print("== the person's words ==")
        j = merge_queue.judge
        ck("yes, merge is a yes", j({"choice": "Yes, merge", "words": "ok"})[0])
        ck("approved is a yes", j({"choice": "", "words": "approved"})[0])
        ck("no, hold is a no", not j({"choice": "No, hold", "words": ""})[0])
        ck("a yes that asks for a change is not a yes",
           not j({"choice": "Yes, but rename the flag first", "words": ""})[0])
        ck("nothing said is not a yes", not j({"choice": "", "words": ""})[0])
        ck("the words come back", j({"choice": "No, hold", "words": "later"})[1] == "No, hold")
        ck("a milestone branch is recognised", merge_queue.is_milestone_branch("factory-modular/m3")
           and not merge_queue.is_milestone_branch("factory/TR-1") and not merge_queue.is_milestone_branch(None))

        print("== entry: who gets a row ==")
        repo = "acme/entry"
        up = project("entry", "L3-review")
        ticket(up, repo, "Ready one", 1)
        ticket(up, repo, "Doing one", 2, stage="doing")
        ticket(up, repo, "Milestone merge", 3, branch="factory-modular/m3")
        ticket(up, repo, "Failing CI", 4)
        CI_FAIL.add((repo, 4))
        ticket(up, repo, "Not reviewed", 5)
        NOT_APPROVED.add((repo, 5))
        ticket(up, repo, "A draft", 6, draft=True)
        uncrewed = project("entry-nocrew", handed=False)
        ticket(uncrewed, "acme/nocrew", "Never handed over", 1)
        await tick()
        ck("a ready pull request in review gets a row", row(repo, 1) is not None, rows(repo))
        ck("a ticket not in review gets none", row(repo, 2) is None)
        ck("a milestone branch gets none", row(repo, 3) is None)
        ck("failing CI gets none", row(repo, 4) is None)
        ck("no approved review gets none", row(repo, 5) is None)
        ck("a draft gets none", row(repo, 6) is None)
        ck("a project the crew does not have gets none", rows("acme/nocrew") == [])
        ck("an L3 project asks first, and nothing merged", row(repo, 1)["state"] == "waiting_approval"
           and merged_calls(repo) == [], row(repo, 1))

        print("== order, dependencies and L4-ship ==")
        repo = "acme/order"
        up = project("order", "L4-ship")
        m1 = store.create_milestone(up, "First", "", [], position=1)
        m2 = store.create_milestone(up, "Second", "", [], position=2)
        t_late = ticket(up, repo, "Second milestone", 1, milestone=m2, position=1)
        t_b = ticket(up, repo, "First, ticket two", 2, milestone=m1, position=2)
        t_a = ticket(up, repo, "First, ticket one", 3, milestone=m1, position=1)
        # a ticket in review whose own pull request is not ready, and one that needs it
        t_slow = ticket(up, repo, "Slow", 4, milestone=m1, position=0.5)
        CI_FAIL.add((repo, 4))
        t_dep = ticket(up, repo, "Needs slow", 5, milestone=m1, position=3, depends=[t_slow])
        t_free = ticket(up, repo, "Free", 6, milestone=m2, position=2)
        del MERGES[:]
        await tick()
        order = [n for n, _ in merged_calls(repo)]
        ck("merged in milestone then ticket order, one after the other", order == [3, 2, 1, 6], order)
        ck("the row that needs an unmerged ticket waited", row(repo, 5)["state"] == "queued"
           and "Waiting for" in (row(repo, 5)["reason"] or ""), row(repo, 5))
        ck("it names what it waits for", "T4" in (row(repo, 5)["reason"] or ""), row(repo, 5))
        ck("a later row that does not need it passed", stage(up, t_free) == "merged")
        ck("the merge carries the head and the default method",
           all(b["sha"] == PRS[(repo, n)]["head"] and b["merge_method"] == "squash"
               for n, b in merged_calls(repo)), merged_calls(repo))
        ck("tickets are merged, rows merged", all(stage(up, t) == "merged" for t in (t_a, t_b, t_late))
           and all(row(repo, n)["state"] == "merged" for n in (1, 2, 3, 6)))
        ck("the history says Tares merged it", any(h.get("value") == "merged" and h.get("by") == "merge-queue"
                                                   for h in store.ticket_history(t_a)), store.ticket_history(t_a))
        ck("the merged pull request is kept on the ticket",
           (store.get_ticket(up, t_a)["pr"] or {}).get("merged") is True)
        ck("each branch was deleted", sorted(b for r, b in DELETES if r == repo) == ["factory/T1", "factory/T2",
                                                                                      "factory/T3", "factory/T6"], DELETES)
        ck("nothing was asked of a person at L4-ship", desk(up) == [])
        ck("the reviewed pull request whose CI fails was sent back to its builder (TR-858)",
           stage(up, t_slow) == "changes" and row(repo, 4) is None
           and [h["value"] for h in history(t_slow, "send_back")] == [f"ci_failure:{sha(4)}"],
           (stage(up, t_slow), row(repo, 4), history(t_slow, "send_back")))
        CI_FAIL.discard((repo, 4))
        # the builder fixes it and pushes: a new head, back in review
        new = land(repo, 4, new=sha(4, 7))
        store.update_ticket(t_slow, stage="review", pr={"url": f"https://github.com/{repo}/pull/4",
                                                        "number": 4, "repo": repo, "head": new})
        del MERGES[:]
        await tick()
        order = [n for n, _ in merged_calls(repo)]
        ck("once the dependency is ready it merges, and the dependent follows in the same pass",
           order == [4, 5], order)
        ck("both tickets are merged", stage(up, t_slow) == "merged" and stage(up, t_dep) == "merged")
        del MERGES[:]
        await tick()
        ck("a later tick merges nothing again", MERGES == [])

        print("== the order of equal rows is by entry ==")
        g = {"by_id": {"a": {"milestone": None, "position": 1}, "b": {"milestone": None, "position": 1}},
             "ms": {}}
        first = {"ticket": "a", "entered_at": 1}
        second = {"ticket": "b", "entered_at": 2}
        ck("same milestone and ticket position: whoever entered first",
           sorted([second, first], key=lambda r: merge_queue._key(g, r)) == [first, second])

        print("== branch delete: 422 is ignored ==")
        repo = "acme/branch"
        up = project("branch", "L4-ship")
        t = ticket(up, repo, "Branch gone", 1)
        DELETE_CODE["value"] = 422
        await tick()
        DELETE_CODE["value"] = 204
        ck("merged even when GitHub would not delete the branch", stage(up, t) == "merged"
           and row(repo, 1)["state"] == "merged", row(repo, 1))

        print("== a project's merge method ==")
        repo = "acme/method"
        up = project("method", "L4-ship")
        async with mk() as cx:
            r0 = await cx.get(f"/api/projects/{up}/merge-method")
            r1 = await cx.put(f"/api/projects/{up}/merge-method", json={"merge_method": "rebase"})
            r2 = await cx.put(f"/api/projects/{up}/merge-method", json={"merge_method": "fast-forward"})
            r3 = await cx.put(f"/api/projects/{up}/merge-method", json={})
            r4 = await cx.get(f"/api/projects/{up}/merge-method")
            r5 = await cx.put("/api/projects/uc_nothing/merge-method", json={"merge_method": "merge"})
        ck("squash until chosen", r0.json() == {"merge_method": "squash"}, r0.text)
        ck("rebase is saved", r1.status_code == 200 and r1.json() == {"merge_method": "rebase"}, r1.text)
        ck("another word is refused", 400 <= r2.status_code < 500 and r3.status_code >= 400, (r2.text, r3.text))
        ck("a refused word changed nothing", r4.json() == {"merge_method": "rebase"}, r4.text)
        ck("an unknown project is a 404", r5.status_code == 404, r5.text)
        t = ticket(up, repo, "Rebased", 1)
        del MERGES[:]
        await tick()
        ck("the merge call uses the project's method", [b["merge_method"] for _, b in merged_calls(repo)] == ["rebase"],
           MERGES)

        print("== L3-review: one question, and a yes merges that commit ==")
        repo = "acme/ask"
        up = project("ask", "L3-review")
        t = ticket(up, repo, "Add the cart", 7)
        del MERGES[:]
        await tick()
        await tick()
        await tick()
        ds = desk(up)
        ck("three ticks leave one question", len(ds) == 1, ds)
        d = ds[0]
        ck("the question names the pull request, the ticket and the commit",
           d["question"] == f"Merge PR #7 (Add the cart) at {sha(7)[:7]}?", d["question"])
        ck("the options are yes or hold", d["options"] == ["Yes, merge", "No, hold"], d["options"])
        ck("it is addressed to the person who handed the project over", d["to_person_id"] == person["id"], d)
        ck("the row waits for it", row(repo, 7)["state"] == "waiting_approval"
           and row(repo, 7)["desk_item"] == d["id"] and merged_calls(repo) == [], row(repo, 7))
        answer(d, "Yes, merge")
        await tick()
        ck("a yes merges the commit that was asked about", [(n, b["sha"]) for n, b in merged_calls(repo)] == [(7, sha(7))],
           MERGES)
        ck("the row and the ticket are merged", row(repo, 7)["state"] == "merged" and stage(up, t) == "merged")
        ck("the approval stays on the row for that commit", row(repo, 7)["approved_head"] == sha(7), row(repo, 7))

        print("== L3-review: no, a withdrawn question and a yes with a change ==")
        repo = "acme/no"
        up = project("no", "L3-review")
        t1 = ticket(up, repo, "Said no", 1)
        t2 = ticket(up, repo, "Withdrawn", 2)
        t3 = ticket(up, repo, "Yes but", 3)
        del MERGES[:]
        await tick()
        by_pr = {int(x["question"].split("#")[1].split(" ")[0]): x for x in desk(up)}
        answer(by_pr[1], "No, hold")
        store.close_desk_item(by_pr[2]["id"], "withdrawn", "test", now_utc())
        answer(by_pr[3], "Yes, merge", words="Yes, but rename the flag first")
        await tick()
        ck("a no blocks with the person's words", row(repo, 1)["state"] == "blocked"
           and row(repo, 1)["reason"] == "The person said: No, hold", row(repo, 1))
        ck("a withdrawn question blocks", row(repo, 2)["state"] == "blocked", row(repo, 2))
        ck("a yes that asks for a change blocks", row(repo, 3)["state"] == "blocked", row(repo, 3))
        ck("none of them merged", merged_calls(repo) == [] and stage(up, t1) == "review")
        await tick()
        ck("a blocked row stays blocked and asks nothing more", desk(up) == [] and
           [row(repo, n)["state"] for n in (1, 2, 3)] == ["blocked"] * 3)
        async with mk() as cx:
            pkey = (await cx.post("/api/keys", json={"name": "user:priya2", "scopes": ["admin"],
                                                     "person_id": person["id"]})).json()["secret"]
        async with mk({"Authorization": f"Bearer {pkey}"}) as cx:
            lift = await cx.post(f"/api/factory/merge-queue/{row(repo, 1)['id']}/approve")
        ck("the person who said no can lift the hold through the route",
           lift.status_code == 200 and row(repo, 1)["state"] == "queued"
           and row(repo, 1)["approved_head"] == sha(1), (lift.text, row(repo, 1)))
        del MERGES[:]
        await tick()
        ck("and it merges that commit", [n for n, _ in merged_calls(repo)] == [1] and stage(up, t1) == "merged")
        PRS[(repo, 2)]["head"] = sha(2, 1)
        await tick()
        ck("a blocked row whose pull request got a new commit is dropped and asks afresh",
           row(repo, 2)["head"] == sha(2, 1) and row(repo, 2)["state"] == "waiting_approval", row(repo, 2))

        print("== L3-review: the console route approves ==")
        repo = "acme/route"
        up = project("route", "L3-review")
        t = ticket(up, repo, "By route", 1)
        await tick()
        mid = row(repo, 1)["id"]
        omar = store.create_person("Omar", email="omar@example.com")
        async with mk() as cx:
            bad = await cx.post("/api/factory/merge-queue/mq_nothing/approve")
            sysk = await cx.post(f"/api/factory/merge-queue/{mid}/approve")
            key = (await cx.post("/api/keys", json={"name": "user:priya", "scopes": ["admin"],
                                                    "person_id": person["id"]})).json()["secret"]
            okey = (await cx.post("/api/keys", json={"name": "user:omar", "scopes": ["admin"],
                                                     "person_id": omar["id"]})).json()["secret"]
            rkey = (await cx.post("/api/keys", json={"name": "read:priya", "scopes": ["read"],
                                                     "person_id": person["id"]})).json()["secret"]
        pk = {"Authorization": f"Bearer {key}"}
        async with mk({"Authorization": f"Bearer {rkey}"}) as cx:
            nope = await cx.post(f"/api/factory/merge-queue/{mid}/approve")
        async with mk({"Authorization": f"Bearer {okey}"}) as cx:
            other = await cx.post(f"/api/factory/merge-queue/{mid}/approve")
        async with mk({**pk, "x-tares-station": "crew-builder", "x-tares-role": "builder"}) as cx:
            station = await cx.post(f"/api/factory/merge-queue/{mid}/approve")
        ck("an unknown entry is a 404", bad.status_code == 404, bad.text)
        ck("a crew station is refused even with the addressee's own key", station.status_code == 403
           and row(repo, 1)["state"] == "waiting_approval", (station.text, row(repo, 1)))
        ck("an admin who is not the person it is addressed to is refused", other.status_code == 403
           and row(repo, 1)["state"] == "waiting_approval", (other.text, row(repo, 1)))
        ck("the cell's own credential is no person, so it is refused too", sysk.status_code == 403, sysk.text)
        ck("a read-scope key is refused", nope.status_code == 403, nope.text)
        async with mk(pk) as cx:
            ok = await cx.post(f"/api/factory/merge-queue/{mid}/approve")
            again = await cx.post(f"/api/factory/merge-queue/{mid}/approve")
        ck("the person it is addressed to approves the commit", ok.status_code == 200
           and ok.json()["approved_head"] == sha(1) and ok.json()["state"] == "queued", ok.text)
        ck("its question is closed as answered", store.get_desk_item(row(repo, 1)["desk_item"])["state"] == "answered")
        ck("a second approval is a 409, it is not waiting", again.status_code == 409, again.text)
        del MERGES[:]
        await tick()
        ck("the next tick merges it", [n for n, _ in merged_calls(repo)] == [1] and stage(up, t) == "merged")

        print("== the route, for a row with no question ==")
        repo = "acme/route2"
        up = project("route2", "L3-review")
        t = ticket(up, repo, "No question", 1)
        bare = store.merge_queue_add(repo, "main", 1, sha(1), t, up, state="waiting_approval")
        async with mk() as cx:
            ikey = (await cx.post("/api/keys", json={"name": "ingest:omar", "scopes": ["ingest"],
                                                     "person_id": omar["id"]})).json()["secret"]
        async with mk({"Authorization": f"Bearer {ikey}"}) as cx:
            ing = await cx.post(f"/api/factory/merge-queue/{bare['id']}/approve")
        kept = row(repo, 1)["state"]
        async with mk(pk) as cx:      # an admin key
            adm = await cx.post(f"/api/factory/merge-queue/{bare['id']}/approve")
        ck("with no question, a key without the admin scope is refused",
           ing.status_code == 403 and kept == "waiting_approval", (ing.text, kept))
        ck("with no question, an admin approves", adm.status_code == 200 and row(repo, 1)["state"] == "queued", adm.text)

        print("== a new commit voids everything ==")
        repo = "acme/push"
        up = project("push", "L3-review")
        t = ticket(up, repo, "Pushed again", 1)
        await tick()
        old = row(repo, 1)
        old_q = old["desk_item"]
        PRS[(repo, 1)]["head"] = sha(1, 1)
        del MERGES[:]
        await tick()
        new = row(repo, 1)
        ck("the old question is withdrawn", store.get_desk_item(old_q)["state"] == "withdrawn"
           and "new commits" in (store.get_desk_item(old_q)["reason"] or ""), store.get_desk_item(old_q))
        ck("the row of the old commit is gone", old["id"] not in [r["id"] for r in rows(repo)], rows(repo))
        ck("the pull request entered again for the new commit, asking again",
           new["head"] == sha(1, 1) and new["state"] == "waiting_approval" and new["approved_head"] is None
           and new["desk_item"] != old_q, new)
        ck("the new question names the new commit", sha(1, 1)[:7] in store.get_desk_item(new["desk_item"])["question"])
        # approved, then pushed: the approval does not carry over
        answer(store.get_desk_item(new["desk_item"]), "Yes, merge")
        PRS[(repo, 1)]["head"] = sha(1, 2)
        await tick()
        after = row(repo, 1)
        ck("an approved commit that was pushed over is not merged", merged_calls(repo) == [] and
           after["head"] == sha(1, 2) and after["approved_head"] is None and after["state"] == "waiting_approval", after)

        print("== closed, and merged by someone else ==")
        repo = "acme/other"
        up = project("other", "L3-review")
        t_closed = ticket(up, repo, "Closed", 1)
        t_theirs = ticket(up, repo, "Merged by hand", 2)
        await tick()
        q_closed = row(repo, 1)["desk_item"]
        PRS[(repo, 1)].update(state="closed")
        PRS[(repo, 2)].update(state="closed", merged=True)
        del MERGES[:]
        await tick()
        ck("a closed pull request ends its row, and its question is withdrawn",
           row(repo, 1)["state"] == "sent_back" and store.get_desk_item(q_closed)["state"] == "withdrawn", row(repo, 1))
        ck("one merged by someone else is merged with no merge call", row(repo, 2)["state"] == "merged"
           and stage(up, t_theirs) == "merged" and MERGES == [], (row(repo, 2), MERGES))
        ck("the ticket of the closed one stays where it was", stage(up, t_closed) == "review")

        print("== no credential can write ==")
        repo = "acme/readonly"
        up = project("readonly", "L4-ship")
        MODE[repo] = "403"
        t1 = ticket(up, repo, "First", 1)
        t2 = ticket(up, repo, "Second", 2)
        await tick()
        await tick()
        ds = desk(up)
        ck("both rows are blocked", row(repo, 1)["state"] == "blocked" and row(repo, 2)["state"] == "blocked",
           (row(repo, 1), row(repo, 2)))
        ck("ONE question for the repo, however many pull requests and ticks", len(ds) == 1, ds)
        ck("the question says what to add", ds[0]["question"] == "No GitHub credential on this Tares can write to "
           "acme/readonly. Add one with write access in Settings, GitHub.", ds[0]["question"])
        ck("nothing merged", stage(up, t1) == "review")
        item = ds[0]["id"]
        PRS[(repo, 2)].update(state="closed")
        await tick()
        ck("one blocked row leaving does not close the question the other still needs",
           row(repo, 2)["state"] == "sent_back" and row(repo, 1)["state"] == "blocked"
           and store.get_desk_item(item)["state"] == "open", (row(repo, 2), store.get_desk_item(item)))
        MODE[repo] = "ok"
        await tick()
        ck("a credential that can write now: the blocked row merges by itself",
           row(repo, 1)["state"] == "merged" and stage(up, t1) == "merged", row(repo, 1))
        ck("and the question for the credential is closed", store.get_desk_item(item)["state"] == "withdrawn"
           and desk(up) == [], store.get_desk_item(item))
        del MODE[repo]

        print("== GitHub's secondary rate limit is not a missing credential ==")
        repo = "acme/secondary"
        up = project("secondary", "L4-ship")
        MODE[repo] = "403_secondary"
        t = ticket(up, repo, "Slow down", 1)
        await tick()
        ck("a 403 with retry-after keeps the row queued and asks no one",
           row(repo, 1)["state"] == "queued" and desk(up) == [] and "rate limit" in (row(repo, 1)["reason"] or ""),
           (row(repo, 1), desk(up)))
        del MODE[repo]
        await tick()
        ck("and it merges once GitHub lets go", row(repo, 1)["state"] == "merged")

        print("== a pull request from a fork never deletes the base repo's branch ==")
        repo = "acme/fork"
        up = project("fork", "L4-ship")
        t = ticket(up, repo, "From a fork", 1)
        PRS[(repo, 1)]["head_repo"] = "someone/fork"
        del DELETES[:]
        await tick()
        ck("it merges, and no branch of the base repo is deleted", row(repo, 1)["state"] == "merged"
           and [d for d in DELETES if d[0] == repo] == [], (row(repo, 1), DELETES))

        print("== a team at L3-review holds a hand-over's L4-ship ==")
        repo = "acme/ceiling"
        up = project("ceiling", "L4-ship")
        store.set_setting(factory_api.CREW_SETTING, json.dumps({"autonomy": "L3-review"}))
        ck("the team's L3-review wins over the hand-over's L4-ship",
           merge_queue.autonomy_of(store, up) == "L3-review", merge_queue.autonomy_of(store, up))
        t = ticket(up, repo, "Held by the team", 1)
        del MERGES[:]
        await tick()
        ck("so it asks the person and merges nothing", row(repo, 1)["state"] == "waiting_approval"
           and merged_calls(repo) == [], row(repo, 1))
        store.set_setting(factory_api.CREW_SETTING, json.dumps({"autonomy": "L4-ship"}))
        ck("a team at L4-ship lets the hand-over's L4-ship stand",
           merge_queue.autonomy_of(store, up) == "L4-ship")
        store.set_setting(factory_api.CREW_SETTING, json.dumps({}))
        ck("a team with no setting changes nothing", merge_queue.autonomy_of(store, up) == "L4-ship")
        ck("and a project handed over with no autonomy falls to L3-review",
           merge_queue.autonomy_of(store, project("ceiling2", None)) == "L3-review")

        print("== GitHub says no to this one pull request ==")
        for code in ("409", "422"):
            repo = f"acme/g{code}"
            up = project(f"g{code}", "L4-ship")
            MODE[repo] = code
            t = ticket(up, repo, "Stuck", 1)
            await tick()
            r = row(repo, 1)
            ck(f"{code}: the row stays queued with GitHub's message",
               r["state"] == "queued" and r["reason"] == f"GitHub says {code}" and desk(up) == []
               and stage(up, t) == "review", r)
        MODE.clear()
        await tick()
        ck("when GitHub agrees the row merges", row(repo, 1)["state"] == "merged" and stage(up, t) == "merged", row(repo, 1))
        ck("so does the other that was stuck", row("acme/g409", 1)["state"] == "merged")

        print("== 405: a rule of the repo, to the desk and never to the builder ==")
        for mode, said in (("405", "GitHub says 405"), ("405_empty", "GitHub answered 405")):
            repo = f"acme/g{mode}"
            up = project(f"g{mode.replace('_', '')}", "L4-ship")
            MODE[repo] = mode
            t = ticket(up, repo, "Held by a rule", 1)
            await tick()
            await tick()
            r, d = row(repo, 1), desk(up)
            ck(f"{mode}: the row is blocked with GitHub's words", r["state"] == "blocked"
               and said in r["reason"], r)
            ck(f"{mode}: one desk item for the person carries them", len(d) == 1
               and said in d[0]["context"] and "PR #1" in d[0]["question"] and r["desk_item"] == d[0]["id"], d)
            ck(f"{mode}: the builder hears nothing: still in review, nothing sent back",
               stage(up, t) == "review" and history(t, "send_back") == [], (stage(up, t), history(t, "send_back")))
        MODE.clear()
        await tick()
        ck("a blocked row is not lined up again by itself", row("acme/g405", 1)["state"] == "blocked")
        merge_queue.approve(store, row("acme/g405", 1))
        await tick()
        ck("a person's approval in the console releases it", row("acme/g405", 1)["state"] == "merged")
        ck("the 405 row with an empty message was not touched", row("acme/g405_empty", 1)["state"] == "blocked")
        fake = dict(row("acme/g405_empty", 1))
        merge_queue._blocked_by_rule(store, fake, store.get_ticket(fake["project_uid"], fake["ticket"]), "")
        ck("with no words at all it says so", "GitHub gave no reason" in row("acme/g405_empty", 1)["reason"],
           row("acme/g405_empty", 1))

        print("== 409: the head moved, the base moved, or nothing changed ==")
        repo = "acme/g409m"
        up = project("g409m", "L4-ship")
        MODE[repo] = "409_moved"
        t = ticket(up, repo, "A commit landed", 1)
        h0 = PRS[(repo, 1)]["head"]
        await tick()
        ck("a changed head deletes the row, with no desk item and no send back",
           row(repo, 1) is None and desk(up) == [] and stage(up, t) == "review", rows(repo))
        MODE.clear()
        await tick()
        ck("it enters again for the new commit and merges it", row(repo, 1)["state"] == "merged"
           and row(repo, 1)["head"] == sha(1, 9) != h0, row(repo, 1))
        repo = "acme/g409c"
        up = project("g409c", "L4-ship")
        MODE[repo] = "409_conflict"
        t = ticket(up, repo, "The base moved", 1)
        PR_FILES[(repo, 1)] = [{"filename": "api/routes.py", "status": "modified"}]
        await tick()
        ck("a pull request GitHub now says conflicts is sent back as a conflict",
           stage(up, t) == "changes" and row(repo, 1)["state"] == "sent_back"
           and "Conflicts with main" in row(repo, 1)["reason"], (stage(up, t), row(repo, 1)))
        ck("with the history row the bounce count reads",
           [(h["value"], h["by"]) for h in history(t, "send_back")] == [(f"conflict:{sha(1)}", "merge-queue")],
           history(t, "send_back"))

        print("== TR-858: GitHub's own answer on whether it merges, in merge_ready ==")
        repo = "acme/brief"
        up = project("brief", "L4-ship")
        t = ticket(up, repo, "Reads mergeable", 1)
        MERGEABLE[(repo, 1)] = (None, "unknown")
        ans = await merge_ready.merge_ready(store, f"{repo}#1")
        ck("a null mergeable is reported as null, not as false",
           ans["pr"]["mergeable"] is None and ans["pr"]["mergeable_state"] == "unknown", ans["pr"])
        MERGEABLE[(repo, 1)] = (False, "dirty")
        ans = await merge_ready.merge_ready(store, f"{repo}#1")
        ck("a conflict is reported", ans["pr"]["mergeable"] is False and ans["pr"]["mergeable_state"] == "dirty", ans["pr"])
        del MERGEABLE[(repo, 1)]
        ans = await merge_ready.merge_ready(store, f"{repo}#1")
        ck("a clean one is reported", ans["pr"]["mergeable"] is True and ans["pr"]["mergeable_state"] == "clean", ans["pr"])
        got = await factory_github.read_pr(store, repo, 1)
        ck("read_pr carries both", got["mergeable"] is True and got["mergeable_state"] == "clean", got)

        print("== TR-858: behind, the same changes: the review is kept ==")
        repo = "acme/same"
        up = project("same", "L4-ship", review=None, ci="github")
        t = ticket(up, repo, "Behind", 1)
        reviewed(up, t, repo, 1)
        h0 = PRS[(repo, 1)]["head"]
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", HUNK_MOVED)], "later")
        del MERGES[:], UPDATES[:]
        await tick()
        r = row(repo, 1)
        ck("the branch is updated with the head it was reviewed at, and nothing merges",
           len(UPDATES) == 1 and UPDATES[0][2] == {"expected_head_sha": h0} and merged_calls(repo) == []
           and r["state"] == "waiting_ci" and r["reason"] == merge_queue.UPDATING, (UPDATES, r))
        saved = json.loads(r["before_files"])
        ck("the files before are kept as JSON with the head, the patch reduced to its changed lines",
           saved == {"head": h0, "files": [{"filename": "a.py", "status": "modified", "patch": "-old\n+new"}]}, saved)
        await tick()
        await tick()
        ck("with the head unchanged the row waits and the branch is not updated again",
           len(UPDATES) == 1 and row(repo, 1)["state"] == "waiting_ci" and merged_calls(repo) == [], row(repo, 1))
        new = land(repo, 1)
        await tick()
        r = row(repo, 1)
        ck("a new head with no check on it yet holds, no review written, no merge",
           r["state"] == "waiting_ci" and r["head"] == h0 and "Waiting for checks" in r["reason"]
           and [x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"] == []
           and merged_calls(repo) == [], r)
        CHECKED.add(new)
        await tick()
        r = row(repo, 1)
        mq = [x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"]
        ck("with a check on the new head the row is queued on it, and nothing merges in that tick",
           r["state"] == "queued" and r["head"] == new and r["before_files"] is None and merged_calls(repo) == [], r)
        ck("the review is written again for the new head, with the agreed words and no findings",
           len(mq) == 1 and mq[0]["head"] == new and mq[0]["verdict"] == "pass"
           and mq[0]["verified"] == f"Same changes against main as the reviewed commit {h0[:7]}; only the base moved",
           mq)
        await tick()
        ck("the next tick merges the new head", [b["sha"] for _, b in merged_calls(repo)] == [new]
           and stage(up, t) == "merged", merged_calls(repo))

        print("== TR-858: behind, a project whose CI says none: no check to wait for ==")
        repo = "acme/cinone"
        up = project("cinone", "L4-ship", review=None, ci="github")
        t = ticket(up, repo, "No CI", 1)
        reviewed(up, t, repo, 1)
        CI_NONE.add(repo)
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", HUNK_MOVED)], "later")
        await tick()
        h0 = PRS[(repo, 1)]["head"]
        new = land(repo, 1)
        await tick()
        r = row(repo, 1)
        ck("with CI state none and no check on the new head the row is queued on it at once",
           r["state"] == "queued" and r["head"] == new and "Waiting" not in (r["reason"] or "")
           and len([x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"]) == 1, r)
        await tick()
        ck("and merges", row(repo, 1)["state"] == "merged" and stage(up, t) == "merged", row(repo, 1))

        print("== TR-858: behind, an outside review gate: the head is updated and nothing else ==")
        repo = "acme/outside"
        up = project("outside", "L4-ship", review="fakerev", ci="github")
        t = ticket(up, repo, "Outside gate", 1)
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", HUNK_MOVED)])
        await tick()
        new = PRS[(repo, 1)]["head"]
        CHECKED.add(new)
        await tick()
        ck("no review is written for it, the row is queued on the new head",
           [x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"] == []
           and row(repo, 1)["state"] == "queued" and row(repo, 1)["head"] == new, row(repo, 1))
        await tick()
        ck("and it merges", row(repo, 1)["state"] == "merged" and stage(up, t) == "merged", row(repo, 1))

        print("== TR-858: behind, different changes: back to the review gate ==")
        variants = [("changed lines", [fl("a.py", BEFORE)], [fl("a.py", DIFFERENT)]),
                    ("a compare with no files", [fl("a.py", BEFORE)], []),
                    ("a file with no patch", [fl("a.py", BEFORE)], [fl("a.py", None)]),
                    ("no patch on either side", [fl("a.py", None)], [fl("a.py", None)]),
                    ("another file", [fl("a.py", BEFORE)], [fl("a.py", BEFORE), fl("b.py", BEFORE)]),
                    ("another status", [fl("a.py", BEFORE)], [fl("a.py", BEFORE, "renamed")])]
        for i, (label, before, after) in enumerate(variants):
            repo = f"acme/diff{i}"
            up = project(f"diff{i}", "L4-ship", review=None, ci="github")
            t = ticket(up, repo, "Changes", 1)
            reviewed(up, t, repo, 1)
            behind(repo, 1, before, after)
            del MERGES[:]
            await tick()
            CHECKED.add(PRS[(repo, 1)]["head"])
            await tick()
            h = history(t, "merge_queue")
            ck(f"{label}: the row is gone and the ticket stays in review", row(repo, 1) is None
               and stage(up, t) == "review", (rows(repo), stage(up, t)))
            ck(f"{label}: the history says why", len(h) == 1 and h[0]["value"] == "needs_review"
               and h[0]["reason"] == "the branch update changed what this pull request does; it needs a new review"
               and h[0]["by"] == "merge-queue", h)
            await tick()
            ck(f"{label}: nothing merged, no review was written, and it is not lined up again",
               merged_calls(repo) == [] and row(repo, 1) is None
               and [x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"] == [], rows(repo))

        print("== TR-858: before_files empty or missing ==")
        repo = "acme/nobefore"
        up = project("nobefore", "L4-ship", review=None, ci="github")
        t = ticket(up, repo, "Empty list", 1)
        reviewed(up, t, repo, 1)
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", BEFORE)], "later")
        await tick()
        h0 = PRS[(repo, 1)]["head"]
        store.merge_queue_update(row(repo, 1)["id"], before_files=json.dumps({"head": h0, "files": []}))
        CHECKED.add(land(repo, 1))
        await tick()
        ck("an empty saved list is never the same changes: back to the review gate",
           row(repo, 1) is None and len(history(t, "merge_queue")) == 1
           and [x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"] == [], rows(repo))
        repo = "acme/nosaved"
        up = project("nosaved", "L4-ship", review=None, ci="github")
        t = ticket(up, repo, "Nothing saved", 1)
        reviewed(up, t, repo, 1)
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", BEFORE)], "later")
        await tick()
        store.merge_queue_update(row(repo, 1)["id"], before_files=None)
        CHECKED.add(land(repo, 1))
        await tick()
        ck("a waiting row with nothing saved is an ordinary new commit: the row goes, no review is kept",
           row(repo, 1) is None and history(t, "merge_queue") == []
           and [x for x in store.ticket_reviews(t) if x["by"] == "merge-queue"] == [], rows(repo))

        print("== TR-858: local CI does not update a branch that is only behind ==")
        repo = "acme/local"
        up = project("local", "L4-ship", ci="fakeci")
        t = ticket(up, repo, "Local CI", 1)
        BEHIND[(repo, 1)] = 3
        before_calls = len([c for c in ALL_CALLS if "compare" in c[1]])
        del UPDATES[:]
        await tick()
        ck("it merges as it is: no update, no compare", UPDATES == [] and row(repo, 1)["state"] == "merged"
           and len([c for c in ALL_CALLS if "compare" in c[1]]) == before_calls, (UPDATES, row(repo, 1)))
        repo = "acme/stateonly"
        up = project("stateonly", "L4-ship", review="fakerev", ci="github")
        t = ticket(up, repo, "State says behind", 1)
        MERGEABLE[(repo, 1)] = (True, "behind")
        del UPDATES[:]
        await tick()
        ck("mergeable_state behind alone is not read as behind: the compare says it is not",
           UPDATES == [] and row(repo, 1)["state"] == "merged", (UPDATES, row(repo, 1)))
        repo = "acme/falseother"
        up = project("falseother", "L4-ship")
        t = ticket(up, repo, "Not clean, not dirty", 1)
        MERGEABLE[(repo, 1)] = (False, "blocked")
        await tick()
        ck("mergeable false with a state other than dirty is not a conflict",
           history(t, "send_back") == [] and row(repo, 1)["state"] == "merged", row(repo, 1))

        print("== TR-858: GitHub cannot tell yet: mergeable null ==")
        repo = "acme/null"
        up = project("null", "L4-ship")
        t = ticket(up, repo, "Unknown", 1)
        MERGEABLE[(repo, 1)] = (None, "unknown")
        del MERGES[:]
        for i in range(1, merge_queue.UNKNOWN_LIMIT):
            await tick()
            r = row(repo, 1)
            if r["state"] != "queued" or r["unknown_ticks"] != i or desk(up):
                break
        ck("nine ticks in a row wait, queued, counted, no desk item, no merge",
           r["state"] == "queued" and r["unknown_ticks"] == merge_queue.UNKNOWN_LIMIT - 1 and desk(up) == []
           and merged_calls(repo) == [] and "still working out" in r["reason"], r)
        await tick()
        r, d = row(repo, 1), desk(up)
        ck("the tenth blocks the row with a desk item that says GitHub cannot tell",
           r["state"] == "blocked" and len(d) == 1 and "cannot tell" in d[0]["question"]
           and r["desk_item"] == d[0]["id"] and stage(up, t) == "review" and history(t, "send_back") == [], (r, d))
        await tick()
        ck("later ticks leave it blocked with the one item", row(repo, 1)["state"] == "blocked" and len(desk(up)) == 1)
        del MERGEABLE[(repo, 1)]
        merge_queue.approve(store, row(repo, 1))
        await tick()
        ck("once GitHub can tell, a person's approval releases it and it merges", row(repo, 1)["state"] == "merged"
           and merged_calls(repo) != [], row(repo, 1))
        repo = "acme/null2"
        up = project("null2", "L4-ship")
        t = ticket(up, repo, "Briefly unknown", 1)
        MERGEABLE[(repo, 1)] = (None, "unknown")
        for _ in range(3):
            await tick()
        ck("three ticks of null count three", row(repo, 1)["unknown_ticks"] == 3, row(repo, 1))
        MERGEABLE[(repo, 1)] = (True, "clean")
        await tick()
        ck("an answer ends the count and the row merges", row(repo, 1)["state"] == "merged"
           and row(repo, 1)["unknown_ticks"] == 0, row(repo, 1))

        print("== TR-858: a conflict is sent back with the files named ==")
        repo = "acme/conf"
        up = project("conf", "L4-ship")
        t1 = ticket(up, repo, "Lands first", 1, position=1)
        t2 = ticket(up, repo, "Conflicts", 2, position=2)
        t3 = ticket(up, repo, "Clean", 3, position=3)
        PR_FILES[(repo, 1)] = [{"filename": "api/routes.py"}, {"filename": "x.py"}, {"filename": None}]
        PR_FILES[(repo, 2)] = [{"filename": "z.py"}, {"filename": "api/routes.py"}, {"filename": "y.py"}]
        MERGEABLE[(repo, 2)] = (False, "dirty")
        del MERGES[:]
        await tick()
        r = row(repo, 2)
        ck("the conflicting one is sent back with the reason naming the file both changed",
           stage(up, t2) == "changes" and r["state"] == "sent_back"
           and r["reason"] == "Conflicts with #1 in api/routes.py; rebase on main and push", r)
        ck("the stage history carries the reason, and send_back carries kind:head by merge-queue",
           [(h["value"], h["by"], h["reason"]) for h in history(t2, "send_back")]
           == [(f"conflict:{sha(2)}", "merge-queue", r["reason"])]
           and history(t2, "stage")[-1]["value"] == "changes", history(t2, "send_back"))
        ck("it never reached a merge call, and the queue moved on to the next", [n for n, _ in merged_calls(repo)] == [1, 3]
           and stage(up, t3) == "merged", merged_calls(repo))
        new = land(repo, 2, new=sha(2, 7))
        del MERGEABLE[(repo, 2)]
        store.update_ticket(t2, stage="review", pr={"url": f"https://github.com/{repo}/pull/2", "number": 2,
                                                    "repo": repo, "head": new})
        await tick()
        ck("the builder's new head deletes the sent back row, and the pull request enters again and merges",
           len(rows(repo, 2)) == 1 and row(repo, 2)["state"] == "merged" and row(repo, 2)["head"] == new, rows(repo, 2))
        repo = "acme/conf0"
        up = project("conf0", "L4-ship")
        t = ticket(up, repo, "First of the repo", 1)
        MERGEABLE[(repo, 1)] = (False, "dirty")
        await tick()
        ck("with nothing merged yet for that repo and base, the reason names the base only",
           row(repo, 1)["reason"] == "Conflicts with main; rebase on main and push" and stage(up, t) == "changes",
           row(repo, 1))
        repo = "acme/conf1"
        up = project("conf1", "L4-ship")
        t1 = ticket(up, repo, "Lands first", 1, position=1)
        t2 = ticket(up, repo, "Conflicts", 2, position=2)
        MERGEABLE[(repo, 2)] = (False, "dirty")
        await tick()
        ck("when GitHub gives no file list the reason names the base only",
           row(repo, 2)["reason"] == "Conflicts with main; rebase on main and push", row(repo, 2))
        repo = "acme/conf2"
        up = project("conf2", "L4-ship")
        t1 = ticket(up, repo, "Lands first", 1, position=1)
        t2 = ticket(up, repo, "Conflicts", 2, position=2)
        names = [f"f{i}.py" for i in range(7)]
        PR_FILES[(repo, 1)] = [{"filename": n} for n in names]
        PR_FILES[(repo, 2)] = [{"filename": n} for n in reversed(names[:6])]
        MERGEABLE[(repo, 2)] = (False, "dirty")
        await tick()
        ck("many shared files: the first five in order, then how many more",
           row(repo, 2)["reason"] == "Conflicts with #1 in f0.py, f1.py, f2.py, f3.py, f4.py and 1 more; "
                                     "rebase on main and push", row(repo, 2))
        repo = "acme/conf3"
        up = project("conf3", "L4-ship")
        t1 = ticket(up, repo, "Lands first", 1, position=1)
        t2 = ticket(up, repo, "Conflicts", 2, position=2)
        PR_FILES[(repo, 1)] = [{"filename": "a.py"}]
        PR_FILES[(repo, 2)] = []
        MERGEABLE[(repo, 2)] = (False, "dirty")
        await tick()
        ck("a pull request with an empty file list shares nothing: the base only",
           row(repo, 2)["reason"] == "Conflicts with main; rebase on main and push", row(repo, 2))

        print("== TR-858: the update itself is refused ==")
        for mode in ("422_conflict", "422_plain", "403"):
            repo = f"acme/up{mode}"
            up = project("up" + mode.replace("_", ""), "L4-ship", review="fakerev", ci="github")
            t = ticket(up, repo, "Update refused", 1)
            behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", BEFORE)], mode)
            del UPDATES[:], MERGES[:]
            await tick()
            r = row(repo, 1)
            ck(f"{mode}: one update call, nothing merged, nothing saved on the row",
               len(UPDATES) == 1 and merged_calls(repo) == [] and r["before_files"] is None, (UPDATES, r))
            if mode == "422_conflict":
                ck("a 422 that mentions conflict is a conflict: sent back, once",
                   r["state"] == "sent_back" and stage(up, t) == "changes"
                   and r["reason"] == "Conflicts with main; rebase on main and push"
                   and [h["value"] for h in history(t, "send_back")] == [f"conflict:{sha(1)}"], (r, history(t, "send_back")))
            elif mode == "422_plain":
                ck("a 422 that does not mention conflict stays queued with GitHub's words, no send back",
                   r["state"] == "queued" and r["reason"] == "expected head sha didn't match"
                   and history(t, "send_back") == [] and stage(up, t) == "review", r)
                UPDATE[repo] = "ok"
                await tick()
                ck("and it is updated on the next tick", row(repo, 1)["state"] == "waiting_ci", row(repo, 1))
            else:
                ck("no credential can write: blocked with the one desk item, no send back",
                   r["state"] == "blocked" and r["reason"] == merge_queue.no_write_reason(repo)
                   and len(desk(up)) == 1 and history(t, "send_back") == [], (r, desk(up)))
        repo = "acme/cmp500"
        up = project("cmp500", "L4-ship", review="fakerev", ci="github")
        t = ticket(up, repo, "Compare fails", 1)
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", BEFORE)])
        COMPARE_CODE[repo] = 500
        del UPDATES[:]
        await tick()
        ck("a compare GitHub cannot answer holds the row queued with the reason, no update, no merge",
           row(repo, 1)["state"] == "queued" and row(repo, 1)["reason"].startswith("Could not tell whether the branch is behind")
           and [u for u in UPDATES if u[0] == repo] == [] and merged_calls(repo) == [], (UPDATES, merged_calls(repo)))
        del COMPARE_CODE[repo]
        await tick()
        ck("and goes on when it can", row(repo, 1)["state"] == "waiting_ci" and len([u for u in UPDATES if u[0] == repo]) == 1, (UPDATES, row(repo, 1)["state"]))

        print("== TR-858: a reviewed pull request that fails goes back, once per head ==")
        repo = "acme/cifail"
        up = project("cifail", "L4-ship")
        t = ticket(up, repo, "CI fails", 1)
        CI_FAIL.add((repo, 1))
        await tick()
        ck("stage changes, reason is merge_ready's, one send_back ci_failure:head by merge-queue, no row",
           stage(up, t) == "changes" and row(repo, 1) is None
           and [(h["value"], h["by"]) for h in history(t, "send_back")] == [(f"ci_failure:{sha(1)}", "merge-queue")]
           and "CI is failing on" in history(t, "stage")[-1]["reason"], history(t, "send_back"))
        store.update_ticket(t, stage="review")
        await tick()
        await tick()
        ck("the same head is not sent back again, however often it is looked at",
           stage(up, t) == "review" and len(history(t, "send_back")) == 1 and desk(up) == [], history(t, "send_back"))
        new = land(repo, 1, new=sha(1, 7))
        await tick()
        ck("a new head that fails too is sent back again, for that head",
           stage(up, t) == "changes" and [h["value"] for h in history(t, "send_back")]
           == [f"ci_failure:{sha(1)}", f"ci_failure:{new}"], history(t, "send_back"))
        repo = "acme/rail"
        up = project("rail", "L4-ship")
        t = ticket(up, repo, "Guardrail blocks", 1)
        RAILS[repo] = [SimpleNamespace(allow=False, reason="touches the payments folder", integration="policy",
                                       warn_only=False)]
        await tick()
        ck("a blocking guardrail is a send back of kind guardrail",
           stage(up, t) == "changes" and [h["value"] for h in history(t, "send_back")] == [f"guardrail:{sha(1)}"]
           and history(t, "stage")[-1]["reason"] == "Blocked by policy: touches the payments folder",
           history(t, "send_back"))
        repo = "acme/railwarn"
        up = project("railwarn", "L4-ship")
        t = ticket(up, repo, "Guardrail warns", 1)
        RAILS[repo] = [SimpleNamespace(allow=False, reason="look at this", integration="policy", warn_only=True)]
        await tick()
        ck("a guardrail that only warns sends nothing back and the pull request merges",
           history(t, "send_back") == [] and stage(up, t) == "merged", history(t, "send_back"))
        repo = "acme/unrev"
        up = project("unrev", "L4-ship")
        t = ticket(up, repo, "Not reviewed, CI fails", 1)
        NOT_APPROVED.add((repo, 1))
        CI_FAIL.add((repo, 1))
        await tick()
        ck("a pull request nobody approved is not sent back for its CI",
           stage(up, t) == "review" and history(t, "send_back") == [], history(t, "send_back"))
        repo = "acme/late"
        up = project("late", "L4-ship")
        t = ticket(up, repo, "Fails after it queued", 1)
        MODE[repo] = "403_rate"
        await tick()
        ck("it queues (the merge is held by the rate limit)", row(repo, 1)["state"] == "queued", row(repo, 1))
        factory_github.rate_remaining.clear()
        MODE[repo] = "ok"
        CI_FAIL.add((repo, 1))
        await tick()
        ck("a queued row whose CI then fails is sent back and marked sent_back",
           row(repo, 1)["state"] == "sent_back" and stage(up, t) == "changes"
           and [h["value"] for h in history(t, "send_back")] == [f"ci_failure:{sha(1)}"], (row(repo, 1), history(t, "send_back")))
        CI_FAIL.discard((repo, 1))
        new = land(repo, 1, new=sha(1, 7))
        store.update_ticket(t, stage="review", pr={"url": f"https://github.com/{repo}/pull/1", "number": 1,
                                                   "repo": repo, "head": new})
        await tick()
        ck("the builder's push clears it and the pull request merges", len(rows(repo, 1)) == 1
           and row(repo, 1)["state"] == "merged", rows(repo, 1))

        print("== TR-858: the pieces on their own ==")
        a = [fl("a.py", BEFORE)]
        ck("same files, same lines, a moved hunk header: the same", merge_queue.same_changes(a, [fl("a.py", HUNK_MOVED)]))
        ck("different lines: not the same", not merge_queue.same_changes(a, [fl("a.py", DIFFERENT)]))
        ck("a missing patch is changed, even against itself",
           not merge_queue.same_changes([fl("a.py", None)], [fl("a.py", None)]))
        ck("an empty patch is changed", not merge_queue.same_changes([fl("a.py", "")], [fl("a.py", "")]))
        ck("empty, missing and malformed lists are never the same",
           not merge_queue.same_changes([], []) and not merge_queue.same_changes(None, None)
           and not merge_queue.same_changes(a, None) and not merge_queue.same_changes(None, a)
           and not merge_queue.same_changes(a, []) and not merge_queue.same_changes("x", "x")
           and not merge_queue.same_changes([None], [None]) and not merge_queue.same_changes([{}], [{}]))
        ck("a different set of files is not the same", not merge_queue.same_changes(a, a + [fl("b.py", BEFORE)]))
        ck("a different status is not the same", not merge_queue.same_changes(a, [fl("a.py", BEFORE, "added")]))
        ck("the order of the files does not matter",
           merge_queue.same_changes([fl("a.py", BEFORE), fl("b.py", DIFFERENT)], [fl("b.py", DIFFERENT), fl("a.py", HUNK_MOVED)]))
        ck("a file listed twice is not comparable", not merge_queue.same_changes(a + a, a + a))
        slim = merge_queue._slim([fl("a.py", BEFORE), fl("b.py", None), "junk", None])
        ck("_slim keeps the name, the status and the reduced patch, and None for no patch",
           slim == [{"filename": "a.py", "status": "modified", "patch": "-old\n+new"},
                    {"filename": "b.py", "status": "modified", "patch": None}], slim)
        ck("_slim of nothing is nothing", merge_queue._slim(None) == [] and merge_queue._slim([]) == [])
        ck("a saved list compares to the same list again (the reduced patch is stable)",
           merge_queue.same_changes(slim[:1], [fl("a.py", BEFORE)]))
        ck("_load_before survives nothing, junk and the wrong shape",
           all(merge_queue._load_before({"before_files": v}) == ("", [])
               for v in (None, "", "not json", "[]", '{"files": 3}', "null")) and merge_queue._load_before({}) == ("", []))
        ck("_load_before reads what _branch_ok wrote",
           merge_queue._load_before({"before_files": json.dumps({"head": "h", "files": slim})}) == ("h", slim))
        none_failed = [{}, {"pr": None}, {"pr": {"merged": True}}, {"pr": {"draft": True, "state": "open"}},
                       {"pr": {"state": "closed"}}, {"pr": {"state": "open"}, "head": "h"},
                       {"pr": {"state": "open"}, "head": "h", "review": {"state": "none"}, "ci": {"state": "failure"}},
                       {"pr": {"state": "open"}, "head": "", "review": {"state": "approved"}, "ci": {"state": "failure"}},
                       {"pr": {"state": "open"}, "head": "h", "review": {"state": "approved"}, "ci": None,
                        "guardrails": None}]
        ck("_reviewed_failure answers None for everything that is not a reviewed failure",
           all(merge_queue._reviewed_failure(a_) is None for a_ in none_failed))
        ck("_reviewed_failure names CI before a guardrail, with the reason merge_ready gave",
           merge_queue._reviewed_failure({"pr": {"state": "open"}, "head": "h", "review": {"state": "approved"},
                                          "ci": {"state": "failure"}, "reasons": ["x", "CI is failing on abc: test"]})
           == ("ci_failure", "CI is failing on abc: test"))
        repo = "acme/ghx"
        PRS[(repo, 1)] = {"head": sha(1), "branch": "b", "base": "main", "state": "open", "merged": False,
                          "draft": False}
        BEHIND[(repo, 1)] = 4
        FILES[(repo, 1)] = [fl("a.py", BEFORE)]
        got = await factory_github.compare(store, repo, "main", sha(1))
        ck("compare: behind_by, ahead_by and the files", got["ok"] and got["behind_by"] == 4 and got["ahead_by"] == 1
           and got["files"] == [fl("a.py", BEFORE)], got)
        BEHIND[(repo, 1)] = 0
        ck("compare: zero behind is still ok", (await factory_github.compare(store, repo, "main", sha(1)))["behind_by"] == 0)
        got = await factory_github.compare(store, repo, "main", "nosuchhead")
        ck("compare: a 404 is not ok and says why", not got["ok"] and got["error"], got)
        COMPARE_CODE[repo] = 500
        got = await factory_github.compare(store, repo, "main", sha(1))
        ck("compare: a 500 is not ok", not got["ok"] and got["error"], got)
        del COMPARE_CODE[repo]
        for label, body in (("a list", []), ("an empty object", {}), ("a string", "x"),
                            ("behind_by null", {"behind_by": None, "files": []})):
            COMPARE_BODY[repo] = body
            got = await factory_github.compare(store, repo, "main", sha(1))
            ck(f"compare: {label} is not ok, and has no behind_by to trust", not got["ok"] and got["error"], got)
        del COMPARE_BODY[repo]
        COMPARE_BODY[repo] = {"behind_by": 2}
        got = await factory_github.compare(store, repo, "main", sha(1))
        ck("compare: a body with no files is ok with an empty list", got["ok"] and got["files"] == [] and got["behind_by"] == 2, got)
        del COMPARE_BODY[repo]
        PR_FILES[(repo, 1)] = [fl("a.py", BEFORE)]
        ck("pr_files: the files", await factory_github.pr_files(store, repo, 1) == [fl("a.py", BEFORE)])
        PR_FILES[(repo, 1)] = []
        ck("pr_files: none is an empty list", await factory_github.pr_files(store, repo, 1) == [])
        del PR_FILES[(repo, 1)]
        ck("pr_files: unreadable is None", await factory_github.pr_files(store, repo, 1) is None)
        UPDATE[repo] = "ok"
        got = await factory_github.update_branch(store, repo, 1, sha(1))
        ck("update_branch: accepted", got["ok"] and got["status"] == 202 and not got["conflict"], got)
        UPDATE[repo] = "422_conflict"
        got = await factory_github.update_branch(store, repo, 1, sha(1))
        ck("update_branch: a 422 that mentions conflict", not got["ok"] and got["conflict"], got)
        UPDATE[repo] = "422_plain"
        got = await factory_github.update_branch(store, repo, 1, sha(1))
        ck("update_branch: a 422 that does not", not got["ok"] and not got["conflict"]
           and got["message"] == "expected head sha didn't match", got)
        UPDATE[repo] = "403"
        got = await factory_github.update_branch(store, repo, 1, sha(1))
        ck("update_branch: no credential can write", not got["ok"] and got["no_write"], got)

        print("== a spent rate limit on the merge ==")
        repo = "acme/spent"
        up = project("spent", "L4-ship")
        MODE[repo] = "403_rate"
        t = ticket(up, repo, "Waits", 1)
        RATE["value"] = 4000
        await tick()
        r = row(repo, 1)
        ck("it is queued, not blocked, and says why", r["state"] == "queued" and "rate limit" in r["reason"]
           and desk(up) == [], r)
        factory_github.rate_remaining.clear()
        MODE[repo] = "ok"
        await tick()
        ck("and merges later", row(repo, 1)["state"] == "merged")

        print("== two ticks at once, a dead tick ==")
        repo = "acme/double"
        up = project("double", "L4-ship")
        t = ticket(up, repo, "Once", 1)
        del MERGES[:]
        await asyncio.gather(tick(), tick())
        ck("merged once", len(merged_calls(repo)) == 1 and stage(up, t) == "merged", MERGES)
        ck("one open or merged row for it", len(rows(repo, 1)) == 1, rows(repo))
        repo = "acme/dead"
        up = project("dead", "L4-ship")
        t_m = ticket(up, repo, "Merged while dead", 1)
        PRS[(repo, 1)].update(merged=True, state="closed")
        store.merge_queue_add(repo, "main", 1, PRS[(repo, 1)]["head"], t_m, up, state="merging")
        t_o = ticket(up, repo, "Open when dead", 2)
        r2 = store.merge_queue_add(repo, "main", 2, PRS[(repo, 2)]["head"], t_o, up, state="merging")
        del MERGES[:]
        await tick()
        ck("a merging row GitHub shows merged becomes merged", row(repo, 1)["state"] == "merged"
           and stage(up, t_m) == "merged", row(repo, 1))
        ck("a merging row GitHub does not show merged goes back in line, then merges (L4)",
           row(repo, 2)["state"] == "merged" and [n for n, _ in merged_calls(repo)] == [2], (row(repo, 2), MERGES))
        repo = "acme/dead3"
        up = project("dead3", "L3-review")
        t_o = ticket(up, repo, "Open at L3", 1)
        store.merge_queue_add(repo, "main", 1, PRS[(repo, 1)]["head"], t_o, up, state="merging")
        del MERGES[:]
        await tick()
        ck("at L3 a recovered row asks again and merges nothing", row(repo, 1)["state"] == "waiting_approval"
           and merged_calls(repo) == [], row(repo, 1))

        print("== the claim's own checks, read once more before the PUT ==")
        repo = "acme/guard"
        up = project("guard", "L4-ship")
        t_h = ticket(up, repo, "Pushed after the claim", 1)
        t_r = ticket(up, repo, "Broken after the claim", 2)
        r_h = store.merge_queue_add(repo, "main", 1, sha(1), t_h, up, state="merging")
        r_r = store.merge_queue_add(repo, "main", 2, sha(2), t_r, up, state="merging")
        PRS[(repo, 1)]["head"] = sha(1, 1)
        CI_FAIL.add((repo, 2))
        del MERGES[:]
        await merge_queue._merge(store, dict(r_h), store.get_ticket(up, t_h))
        await merge_queue._merge(store, dict(r_r), store.get_ticket(up, t_r))
        ck("a head that moved after the claim is not merged, and its row is dropped",
           merged_calls(repo) == [] and row(repo, 1) is None, (MERGES, rows(repo)))
        ck("gates that failed after the claim stop the merge, and the row goes back in line with the reason",
           row(repo, 2)["state"] == "queued" and bool(row(repo, 2)["reason"]), row(repo, 2))
        CI_FAIL.discard((repo, 2))

        print("== recovery marks a merged row by itself ==")
        repo = "acme/recover"
        up = project("recover", "L4-ship")
        t = ticket(up, repo, "Merged while dead", 1)
        PRS[(repo, 1)].update(merged=True, state="closed")
        rr = store.merge_queue_add(repo, "main", 1, PRS[(repo, 1)]["head"], t, up, state="merging")
        await merge_queue._recover(store)
        ck("the recovery alone ends the row and the ticket, with no later walker",
           row(repo, 1)["state"] == "merged" and stage(up, t) == "merged", row(repo, 1))

        print("== the team goes to L4-ship while a question is open ==")
        repo = "acme/switch"
        up = project("switch", "L3-review")
        t = ticket(up, repo, "Asked, then released", 1)
        await tick()
        q = row(repo, 1)["desk_item"]
        ck("it asks at L3-review", row(repo, 1)["state"] == "waiting_approval"
           and store.get_desk_item(q)["state"] == "open")
        store.set_handover(up, [{"path": "/w/a", "name": "switch"}], "L4-ship", None, now_utc(),
                           person_id=person["id"])
        del MERGES[:]
        await tick()
        ck("at L4-ship the question is withdrawn and the pull request merges, nobody asked",
           store.get_desk_item(q)["state"] == "withdrawn" and row(repo, 1)["state"] == "merged"
           and [n for n, _ in merged_calls(repo)] == [1], (store.get_desk_item(q), row(repo, 1)))

        print("== the rate limit ==")
        resp = lambda v: httpx.Response(200, headers={} if v is None else {"x-ratelimit-remaining": v})
        factory_github.rate_remaining.clear()
        factory_github.note_rate("https://a", resp("50"))
        factory_github.note_rate("https://b", resp("4000"))
        ck("kept per API base, and only the low one is listed", factory_github.rate_low() == {"https://a": 50},
           factory_github.rate_remaining)
        factory_github.note_rate("https://a", resp(None))
        factory_github.note_rate("https://a", resp("many"))
        ck("a missing or odd header changes nothing", factory_github.rate_remaining["https://a"] == 50)
        factory_github.note_rate("https://a", resp("4000"))
        ck("a later answer replaces it", factory_github.rate_low() == {})
        factory_github.rate_remaining.clear()
        repo = "acme/limit"
        up = project("limit", "L4-ship")
        t = ticket(up, repo, "Held", 1)
        factory_github.rate_remaining[API] = 99
        out = io.StringIO()
        before = len(ALL_CALLS)
        with contextlib.redirect_stdout(out):
            await tick()
        lines = [x for x in out.getvalue().splitlines() if x.strip()]
        ck("a low value skips the tick, with one line, and calls GitHub not at all",
           len(lines) == 1 and "merge queue" in lines[0] and len(ALL_CALLS) == before and rows(repo) == [], (lines, rows(repo)))
        factory_github.rate_remaining[API] = 100
        await tick()
        ck("100 left is enough", row(repo, 1)["state"] == "merged", row(repo, 1))
        RATE["value"] = None
        factory_github.rate_remaining.clear()
        repo = "acme/nohdr"
        up = project("nohdr", "L4-ship")
        t = ticket(up, repo, "No header", 1)
        await tick()
        ck("GitHub sending no header is no problem", row(repo, 1)["state"] == "merged")
        RATE["value"] = 4000

        print("== a second credential ==")
        store.upsert_github_credential("second", "tok-second")
        repo = "acme/two"
        up = project("two", "L4-ship")
        MODE[repo] = "403_main"
        t = ticket(up, repo, "Second credential", 1)
        await tick()
        ck("a credential that is refused does not stop another that may write", row(repo, 1)["state"] == "merged"
           and any(tok == "tok-second" for _, _, _, tok in MERGES), row(repo, 1))

        print("== the sync runs the queue, and says when it cannot ==")
        repo = "acme/sync"
        up = project("sync", "L4-ship")
        t = ticket(up, repo, "By the sync", 1)
        os.environ["TARES_PR_SYNC_SECONDS"] = "0.05"
        stop = asyncio.Event()
        task = asyncio.create_task(factory_api.sync_prs(store, stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if stage(up, t) == "merged":
                break
        stop.set()
        await asyncio.wait_for(task, 3)
        ck("a pull request is merged by the sync loop alone", stage(up, t) == "merged", stage(up, t))
        os.environ["TARES_PR_SYNC_SECONDS"] = "off"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await asyncio.wait_for(factory_api.sync_prs(store, asyncio.Event()), 2)
        ck("with the sync off one line says the queue does not run",
           len([x for x in out.getvalue().splitlines() if x.strip()]) == 1
           and "merge queue does not run" in out.getvalue(), out.getvalue())

        print("== TR-1092: the bounce ladder ==")
        MQ = "merge-queue"

        def bounce_review(uid, tid, repo, n, verdict="changes", text="Fix the parser", v=0):
            """A review round that sends the ticket back (or, with `pass`, one that does not)."""
            fs = [{"severity": "high", "file": "a.py", "line": 1, "blocking": True, "text": text}] \
                if text else []
            store.add_review(uid, tid, f"https://github.com/{repo}/pull/{n}", sha(n, 50 + v), verdict,
                             "Ran the tests", "", "crew-reviewer", fs, {}, factory_mod.finding_kind,
                             now_utc())

        def back(uid, tid, value="conflict:abc", reason="Conflicts with main"):
            store.add_ticket_history(tid, uid, "send_back", value, reason, MQ, MQ, now_utc())

        def ladder_rows(tid):
            return history(tid, "ladder")

        def mine(uid):
            return [d for d in store.list_desk("open", uid) if d["asked_by"] == MQ and "come back" in d["question"]]

        # a ticket that no PR enters: in review and not approved, so the queue never touches it
        def stuck(name, n=1, stage_="review", autonomy="L4-ship"):
            repo = f"acme/{name}"
            up = project(name, autonomy)
            tid = ticket(up, repo, "Keeps coming back", n, stage=stage_)
            NOT_APPROVED.add((repo, n))
            return up, tid, repo

        up, t, repo = stuck("lad-units")
        ck("a ticket with no reviews and no history has 0 bounces",
           merge_queue.bounces(store, t) == 0 and merge_queue.last_bounce_reason(store, t) == ""
           and merge_queue.ladder_step(store, t) == 0)
        ck("no ticket, an empty id and an unknown id are 0",
           merge_queue.bounces(store, None) == 0 and merge_queue.bounces(store, "") == 0
           and merge_queue.bounces(store, "no-such") == 0 and merge_queue.last_bounce_reason(store, None) == ""
           and merge_queue.ladder_step(store, "") == 0)
        bounce_review(up, t, repo, 1, "pass", "")
        ck("a pass is not a bounce", merge_queue.bounces(store, t) == 0)
        bounce_review(up, t, repo, 1, "changes", "Fix the parser", 1)
        bounce_review(up, t, repo, 1, "block", "Do not touch payments", 2)
        ck("a changes review and a block each count once", merge_queue.bounces(store, t) == 2)
        back(up, t, "conflict:", "Conflicts with main")
        ck("a send_back whose value has no head counts, the first line of the last reason is kept",
           merge_queue.bounces(store, t) == 3 and merge_queue.last_bounce_reason(store, t) == "Conflicts with main")
        store.add_ticket_history(t, up, "send_back", None, None, MQ, MQ, now_utc())
        ck("a send_back with no value and no reason counts too, and the reason is then empty",
           merge_queue.bounces(store, t) == 4 and merge_queue.last_bounce_reason(store, t) == "")
        store.add_ticket_history(t, up, "merge_queue", "needs_review", "the update changed it", MQ, MQ, now_utc())
        store.add_ticket_history(t, up, "stage", "changes", "x", MQ, MQ, now_utc())
        ck("the queue's needs_review row and stage rows are not bounces", merge_queue.bounces(store, t) == 4)
        bounce_review(up, t, repo, 1, "changes", "  Fix the\n   parser \u2014 now  ", 3)
        ck("the latest bounce's reason is one plain line with no em dash",
           merge_queue.last_bounce_reason(store, t) == "Fix the parser, now", merge_queue.last_bounce_reason(store, t))
        bounce_review(up, t, repo, 1, "changes", "x" * 400, 4)
        ck("a long reason is cut", len(merge_queue.last_bounce_reason(store, t)) == merge_queue.REASON_SHOWN
           and merge_queue.last_bounce_reason(store, t).endswith("..."))
        bounce_review(up, t, repo, 1, "block", "", 5)
        ck("a bounce with no findings has no reason", merge_queue.last_bounce_reason(store, t) == "")
        store.add_ticket_history(t, up, "ladder", "3", None, MQ, MQ, now_utc())
        store.add_ticket_history(t, up, "ladder", "x", None, MQ, MQ, now_utc())
        ck("ladder_step is the highest numeric ladder row", merge_queue.ladder_step(store, t) == 3)
        store.add_ticket_history(t, up, "ladder", "5", None, MQ, MQ, now_utc())
        ck("and 5 after the second", merge_queue.ladder_step(store, t) == 5)

        print("== TR-1092: three send backs of any kind reach the orchestrator ==")
        up, t, repo = stuck("lad3")
        bounce_review(up, t, repo, 1, "changes", "Rename the flag")
        back(up, t, f"conflict:{sha(1)}", "Conflicts with main in a.py")
        await tick()
        ck("two bounces write nothing", ladder_rows(t) == [] and mine(up) == [], ladder_rows(t))
        back(up, t, f"ci_failure:{sha(1)}", "CI is failing on abc1234: test")
        await tick()
        r3 = ladder_rows(t)
        ck("the third writes one ladder row, value 3, by merge-queue, with the last reason",
           len(r3) == 1 and r3[0]["value"] == "3" and r3[0]["by"] == MQ and r3[0]["role"] == MQ
           and r3[0]["reason"] == "CI is failing on abc1234: test", r3)
        await tick()
        await tick()
        ck("ticks later add no second row, and no desk item yet", len(ladder_rows(t)) == 1 and mine(up) == [])
        back(up, t, f"guardrail:{sha(1)}", "Blocked by policy: payments")
        await tick()
        ck("a fourth bounce adds nothing", len(ladder_rows(t)) == 1 and mine(up) == [], ladder_rows(t))

        print("== TR-1092: at five, one desk item for the hand-over's person ==")
        back(up, t, f"ci_failure:{sha(1, 9)}", "CI is failing on abc1239: lint")
        await tick()
        rows5 = [x["value"] for x in ladder_rows(t)]
        d5 = mine(up)
        label = factory_mod.label(store.get_ticket(up, t))
        ck("the fifth writes the 5 row", rows5 == ["3", "5"], rows5)
        ck("one desk item, the agreed words, not blocking, the ticket on it, to the hand-over's person",
           len(d5) == 1 and d5[0]["question"] == f"{label} has come back 5 times: CI is failing on abc1239: lint. What should happen?"
           and d5[0]["blocking"] is False and d5[0]["tickets"] == [label]
           and d5[0]["to_person_id"] == person["id"] and d5[0]["asked_by"] == MQ, d5)
        await tick()
        await tick()
        ck("more ticks add no row and no second item", len(ladder_rows(t)) == 2 and len(mine(up)) == 1)
        back(up, t, f"conflict:{sha(1, 8)}", "Conflicts again")
        await tick()
        ck("a sixth bounce adds nothing", len(ladder_rows(t)) == 2 and len(mine(up)) == 1)
        store.close_desk_item(mine(up)[0]["id"], "answered", None, now_utc())
        await tick()
        ck("an answered item is not asked again", len(ladder_rows(t)) == 2 and mine(up) == []
           and len(store.list_desk("answered", up)) == 1)

        print("== TR-1092: five at once writes both rows and one item ==")
        up, t, repo = stuck("lad05", stage_="changes")
        for i in range(5):
            back(up, t, f"conflict:{sha(1, i)}", f"Conflict {i}")
        await tick()
        ck("both rows, one item, in a ticket in changes",
           [x["value"] for x in ladder_rows(t)] == ["3", "5"] and len(mine(up)) == 1)

        print("== TR-1092: an item written before its row is not written twice ==")
        up, t, repo = stuck("ladretry")
        for i in range(5):
            back(up, t, f"conflict:{sha(1, i)}", f"Conflict {i}")
        merge_queue._desk_five(store, up, store.get_ticket(up, t), 5, "Conflict 4")
        ck("the item is there and the 5 row is not", len(mine(up)) == 1 and ladder_rows(t) == [])
        await tick()
        ck("the next tick writes the rows and no second item",
           [x["value"] for x in ladder_rows(t)] == ["3", "5"] and len(mine(up)) == 1, mine(up))

        print("== TR-1092: a hand-over with no person, and bounces with no reason ==")
        up, t, repo = stuck("ladnoone")
        store.set_handover(up, [{"path": "/w/a", "name": "ladnoone"}], "L4-ship", None, now_utc())
        for i in range(5):
            bounce_review(up, t, repo, 1, "changes", "", i)
        await tick()
        d = mine(up)
        label = factory_mod.label(store.get_ticket(up, t))
        ck("no person: the item has no addressee and the ladder still climbs",
           len(d) == 1 and d[0]["to_person_id"] is None and [x["value"] for x in ladder_rows(t)] == ["3", "5"], d)
        ck("no reason: the question has no colon part and ends with the question",
           d and d[0]["question"] == f"{label} has come back 5 times. What should happen?"
           and ladder_rows(t)[0]["reason"] is None, (d, ladder_rows(t)))

        print("== TR-1092: who is looked at ==")
        up, t, repo = stuck("ladstage", stage_="doing")
        for i in range(5):
            back(up, t, f"conflict:{sha(1, i)}", "x")
        await tick()
        ck("a ticket in doing is left alone", ladder_rows(t) == [] and mine(up) == [])
        up2 = project("ladnocrew", handed=False)
        t2 = ticket(up2, "acme/ladnocrew", "No crew", 1)
        for i in range(5):
            back(up2, t2, f"conflict:{sha(1, i)}", "x")
        await tick()
        ck("a project no crew has is left alone", ladder_rows(t2) == [] and mine(up2) == [])

        repo = "acme/ladmerge"
        up = project("ladmerge", "L4-ship")
        t = ticket(up, repo, "Came back three times, then merged", 1, stage="review")
        for i in range(3):
            back(up, t, f"conflict:{sha(1, i)}", f"Conflict {i}")
        reviewed(up, t, repo, 1)
        await tick()
        ck("a ticket the queue merges in the tick gets no ladder row, though it had three bounces",
           stage(up, t) == "merged" and ladder_rows(t) == [] and mine(up) == [], (stage(up, t), ladder_rows(t)))

        print("== TR-1092: a return that is the queue's own does not count ==")
        repo = "acme/ladsame"
        up = project("ladsame", "L4-ship", review=None, ci="github")
        t = ticket(up, repo, "Behind, twice sent back before", 1, stage="review")
        bounce_review(up, t, repo, 1, "changes", "First round")
        bounce_review(up, t, repo, 1, "changes", "Second round", 1)
        reviewed(up, t, repo, 1)
        h0 = PRS[(repo, 1)]["head"]
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", HUNK_MOVED)], "later")
        await tick()
        ck("a row waiting on CI after the branch update adds nothing",
           row(repo, 1)["state"] == "waiting_ci" and merge_queue.bounces(store, t) == 2, merge_queue.bounces(store, t))
        new = land(repo, 1)
        CHECKED.add(new)
        await tick()
        mq = [x for x in store.ticket_reviews(t) if x["by"] == MQ]
        ck("Tares's same-changes review of the new head is a pass and is not counted",
           len(mq) == 1 and mq[0]["verdict"] == "pass" and merge_queue.bounces(store, t) == 2, (mq, merge_queue.bounces(store, t)))
        await tick()
        ck("the pull request merges, and no ladder row was written", stage(up, t) == "merged"
           and ladder_rows(t) == [] and mine(up) == [])
        repo = "acme/ladneeds"
        up = project("ladneeds", "L4-ship", review=None, ci="github")
        t = ticket(up, repo, "Behind, different changes", 1, stage="review")
        bounce_review(up, t, repo, 1, "changes", "First round")
        bounce_review(up, t, repo, 1, "changes", "Second round", 1)
        reviewed(up, t, repo, 1)
        behind(repo, 1, [fl("a.py", BEFORE)], [fl("a.py", DIFFERENT)], "later")
        await tick()
        CHECKED.add(land(repo, 1))
        await tick()
        ck("a branch update that needs a new review is not a bounce either",
           history(t, "merge_queue")[-1]["value"] == "needs_review" and merge_queue.bounces(store, t) == 2
           and ladder_rows(t) == [], (history(t, "merge_queue"), merge_queue.bounces(store, t)))
        repo = "acme/ladwait"
        up = project("ladwait", "L3-review")
        t = ticket(up, repo, "Waits for a person", 1)
        bounce_review(up, t, repo, 1, "changes", "First round")
        bounce_review(up, t, repo, 1, "changes", "Second round", 1)
        reviewed(up, t, repo, 1)
        await tick()
        ck("a row waiting for approval is not a bounce", row(repo, 1)["state"] == "waiting_approval"
           and merge_queue.bounces(store, t) == 2 and ladder_rows(t) == [])

        print("== TR-1092: my_work lists the ones that climbed ==")
        up3, t3, repo3 = stuck("lad3only")
        for i in range(3):
            back(up3, t3, f"conflict:{sha(1, i)}", f"Conflict {i}")
        await tick()
        up0, t0, repo0 = stuck("ladnone")
        async with mk() as cx:
            wk = (await cx.get("/api/crew/my-work")).json()
        by_proj = {b["project"]: b for b in wk["bouncing"]}
        ck("bouncing is a list on the route, with each ticket in the shape",
           isinstance(wk["bouncing"], list) and wk["bouncing"]
           and all(set(b) == {"project", "ticket", "title", "holder", "stage", "count", "ladder", "reason"}
                   for b in wk["bouncing"]), wk["bouncing"][:1])
        ck("the ticket at six bounces is there at step 5 with its count and last reason",
           by_proj["lad3"]["count"] == 6 and by_proj["lad3"]["ladder"] == 5
           and by_proj["lad3"]["reason"] == "Conflicts again" and by_proj["lad3"]["stage"] == "review",
           by_proj.get("lad3"))
        ck("the ticket at three bounces is there at step 3",
           by_proj["lad3only"]["count"] == 3 and by_proj["lad3only"]["ladder"] == 3
           and by_proj["lad3only"]["reason"] == "Conflict 2", by_proj.get("lad3only"))
        ck("a ticket in changes with five bounces is there too", by_proj["lad05"]["count"] == 5
           and by_proj["lad05"]["ladder"] == 5 and by_proj["lad05"]["stage"] == "changes", by_proj.get("lad05"))
        ck("a ticket with no bounces, and tickets with bounces but no ladder row, are not listed",
           not ({"ladnone", "ladsame", "ladneeds", "ladwait", "ladstage"} & set(by_proj)), sorted(by_proj))
        nobody = store.create_person("Nobody", email="nobody@example.com")
        async with mk() as cx:
            nkey = (await cx.post("/api/keys", json={"name": "user:nobody", "scopes": ["admin"],
                                                     "person_id": nobody["id"]})).json()["secret"]
        async with mk({"Authorization": f"Bearer {nkey}"}) as cx:
            theirs = (await cx.get("/api/crew/my-work")).json()
        ck("a person with no ticket that bounced gets an empty list, not a missing one",
           theirs.get("bouncing") == [], theirs)
        store.update_ticket(t3, stage="merged")
        async with mk() as cx:
            wk = (await cx.get("/api/crew/my-work")).json()
        ck("a ticket that has merged leaves the list", "lad3only" not in {b["project"] for b in wk["bouncing"]},
           [b["project"] for b in wk["bouncing"]])

        print("== health, and what Tares says ==")
        async with mk() as cx:
            h = (await cx.get("/health")).json()
        ck("/health lists merge_queue", "merge_queue" in h.get("capabilities", []), h.get("capabilities"))
        ck("L4-ship says Tares merges after the gate and CI",
           factory_mod.AUTONOMY["L4-ship"] == "Tares merges after the review gate passes and CI is green")
        said = [factory_mod.AUTONOMY[k] for k in factory_mod.AUTONOMY]
        said += [d["question"] + d["context"] + d["why"] for u in
                 ("uc_ask", "uc_readonly") for d in store.list_desk(None, u)]
        said += [r["reason"] or "" for r in store.merge_queue_open()]
        ck("no em dash in any of it", not any("—" in s for s in said), said)
        ck("the module and the table keep their row states",
           all(s in merge_queue.__doc__ for s in ("queued", "waiting_ci", "waiting_approval", "merging",
                                                  "merged", "sent_back", "blocked")))


asyncio.run(main())

# nothing here reached the network: every GitHub call went through the fake transport
ck("every GitHub call went through the in-process transport",
   isinstance(factory_github._transport, httpx.MockTransport) and len(ALL_CALLS) > 50, len(ALL_CALLS))
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
