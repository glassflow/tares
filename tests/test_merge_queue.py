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
   405, 409 and 422 keep the row queued with GitHub's message; a spent rate limit keeps it queued;
   a second credential is tried when the first is refused;
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
from tares import factory_api, factory_github, merge_queue
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
    if p[3] == "pulls" and method == "GET":
        pr = PRS.get((repo, int(p[4])))
        if pr is None:
            return httpx.Response(404, json={}, headers=hdr)
        return httpx.Response(200, headers=hdr, json={
            "number": int(p[4]), "title": "PR", "state": pr["state"], "merged": pr["merged"],
            "draft": pr["draft"], "html_url": f"https://github.com/{repo}/pull/{p[4]}",
            "head": {"sha": pr["head"], "ref": pr["branch"],
                     "repo": {"full_name": pr.get("head_repo", repo)}},
            "base": {"ref": pr["base"]}})
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
        return httpx.Response(200, json={key: []}, headers=hdr)
    return httpx.Response(404, json={}, headers=hdr)


factory_github._transport = httpx.MockTransport(handler)


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
        return CIState(state="success", head=pr.head)


async def no_rails(stage, project, payload):
    return []


hooks.run = no_rails


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    registry.load()
    registry._loaded[("review", "fakerev")] = FakeReview
    registry._loaded[("ci", "fakeci")] = FakeCI
    store.upsert_github_credential("main", "tok-main")
    root = {"Authorization": f"Bearer {TOKEN}"}
    person = store.create_person("Priya", email="priya@example.com")

    def mk(headers=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=headers or dict(root))

    def project(name, autonomy=None, handed=True):
        uid = "uc_" + name.replace("-", "")
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        if handed:
            store.set_handover(uid, [{"path": "/w/a", "name": name}], autonomy, None, now_utc(),
                               person_id=person["id"])
        store.set_project_choice(uid, "review", "fakerev", {}, by="test")
        store.set_project_choice(uid, "ci", "fakeci", {}, by="test")
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
                      "desk_item", "approved_head", "entered_at", "updated_at"} and a["state"] == "queued", a)
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
        CI_FAIL.discard((repo, 4))
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
        for code in ("405", "409", "422"):
            repo = f"acme/g{code}"
            up = project(f"g{code}", "L4-ship")
            MODE[repo] = code
            t = ticket(up, repo, "Stuck", 1)
            await tick()
            r = row(repo, 1)
            ck(f"{code}: the row stays queued with GitHub's message",
               r["state"] == "queued" and r["reason"] == f"GitHub says {code}" and desk(up) == [], r)
        MODE.clear()
        await tick()
        ck("when GitHub agrees the row merges", row(repo, 1)["state"] == "merged" and stage(up, t) == "merged", row(repo, 1))
        ck("so do the others that were stuck", all(row(f"acme/g{c}", 1)["state"] == "merged" for c in ("405", "409", "422")))

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
