"""The crew reviewer's verdict posted as the cell's GitHub App (TR-1244).

`record_review` records the round on Tares and, when the cell holds a GitHub App credential that can
write to the pull request's repo, also posts it there: one comment and the `factory/review` check
run on the reviewed head, both naming the crew in text with no @-mention. With no App credential, or
none that can write, nothing is posted and the answer says why, so the reviewer posts with `gh`. A
personal token is the person's own login and is never used to post. The review gate and the pull
request read take the verdict from a commit status or a check run, whoever posted it, the later of
the two, and a new head makes it stale.

Run: .venv/bin/python tests/test_review_post.py
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

_TMP = tempfile.mkdtemp(prefix="tares-review-post-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-review-post"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory_github, github_credentials

P = F = 0
EMDASH = chr(0x2014)


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


HEAD = "aaaaaaa1111"
SEEN: list[tuple[str, str, str, dict]] = []     # (method, path, bearer token, json body)
RUNS: list[dict] = []                            # check runs GitHub holds for the head
STATUSES: list[dict] = []
DENY = {"check": set(), "comment": set()}        # tokens GitHub refuses for that call (403)


def fake_github(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    tok = request.headers.get("authorization", "").removeprefix("Bearer ")
    body = json.loads(request.content) if request.content else {}
    SEEN.append((request.method, request.url.path, tok, body))
    if request.method == "POST" and parts[3] == "check-runs":
        if tok in DENY["check"]:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        RUNS.append({"name": body["name"], "head_sha": body["head_sha"], "status": body["status"],
                     "conclusion": body["conclusion"], "output": body["output"],
                     "completed_at": "2026-11-12T10:00:00Z"})
        return httpx.Response(201, json={"id": len(RUNS)})
    if request.method == "POST" and parts[3] == "issues":
        if tok in DENY["comment"]:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return httpx.Response(201, json={"id": 1})
    if parts[3] == "pulls":
        return httpx.Response(200, json={
            "number": int(parts[4]), "title": "PR", "state": "open", "merged": False,
            "html_url": f"https://github.com/acme/shop/pull/{parts[4]}",
            "head": {"sha": HEAD, "ref": "factory/T1"}})
    if parts[5] == "status":
        return httpx.Response(200, json={"statuses": STATUSES, "total_count": len(STATUSES)})
    return httpx.Response(200, json={"check_runs": [r for r in RUNS if r["head_sha"] == HEAD],
                                     "total_count": len(RUNS)})


factory_github._transport = httpx.MockTransport(fake_github)


def posts(kind):
    return [s for s in SEEN if s[0] == "POST" and s[1].split("/")[4] == kind]


def unit():
    print("== the comment names the crew and leaves nothing out ==")
    text = factory_github.review_comment(
        "changes", "ashish-crew-reviewer", "Ashish Bagri", "pytest -q: 14 passed", "no load test",
        [{"severity": "P3", "text": "naming", "blocking": False},
         {"severity": "P2", "file": "a.py", "line": 3, "text": "missing null check", "blocking": True}],
        "aaaaaaa1111")
    ck("it names the reviewer and the crew's person in text",
       "Reviewed by ashish-crew-reviewer (Ashish Bagri's crew)" in text, text)
    ck("it has no @-mention and no em dash", "@" not in text and EMDASH not in text, text)
    ck("verified and not verified are in it", "pytest -q: 14 passed" in text and "no load test" in text, text)
    ck("the blocking finding comes first, with where it is",
       text.index("missing null check") < text.index("naming") and "a.py:3" in text
       and "blocking" in text, text)
    for empty in (None, []):
        t = factory_github.review_comment("pass", "r", "", "ran it", "", empty, "aaaaaaa1111")
        ck(f"findings {empty!r} say there are none", "No findings." in t and "Findings" not in t, t)
    ck("with no person the reviewer stands alone", "Reviewed by r." in t, t)
    ck("no not-verified section when there is none", "Not verified" not in t, t)

    print("== a verdict from a status or a check run, the later one ==")
    s_ok = {"context": "factory/review", "state": "success", "description": "Pass",
            "updated_at": "2026-11-12T09:00:00Z"}
    r_bad = {"name": "factory/review", "status": "completed", "conclusion": "failure",
             "completed_at": "2026-11-12T10:00:00Z", "output": {"title": "Changes requested"}}
    ck("no verdict is None", factory_github._review_of([], []) is None)
    ck("a status alone", factory_github._review_of([s_ok], []) == {"state": "success", "text": "Pass"})
    ck("a check run alone", factory_github._review_of([], [r_bad]) == {
        "state": "failure", "text": "Changes requested"})
    ck("the later of the two wins",
       factory_github._review_of([s_ok], [r_bad])["state"] == "failure"
       and factory_github._review_of([{**s_ok, "updated_at": "2026-11-12T11:00:00Z"}], [r_bad])["state"] == "success")
    ck("a check run still going is pending",
       factory_github._review_of([], [{"name": "factory/review", "status": "in_progress"}])["state"] == "pending")
    ck("another check run is not a verdict",
       factory_github._review_of([], [{**r_bad, "name": "build"}]) is None)
    ck("a failing review check run is not a failing CI",
       factory_github._ci([], [r_bad]) == "none"
       and factory_github._ci([], [r_bad, {"name": "build", "status": "completed", "conclusion": "success"}]) == "success"
       and factory_github._check_list([], [r_bad]) == [])


async def post_cases(store):
    print("== posting: the App, and only the App ==")
    real_get = github_credentials.get_token

    async def fake_get(st, ref, repo=None, min_life=None):
        c = st.get_github_credential(ref)
        if github_credentials.is_app(c):
            return "app-" + c["name"]
        return await real_get(st, ref, repo, min_life)
    github_credentials.get_token = fake_get
    post = factory_github.post_review

    r = await post(store, "acme/shop", 7, HEAD, "pass", "body", "Pass")
    ck("no credential at all: not posted, and why", not r["posted"] and "no GitHub App" in r["why"], r)
    store.upsert_github_credential("gh", "ghp_x")
    SEEN.clear()
    r = await post(store, "acme/shop", 7, HEAD, "pass", "body", "Pass")
    ck("a personal token never posts", not r["posted"] and "no GitHub App" in r["why"] and not SEEN, (r, SEEN))

    store.upsert_github_credential("app1", "", kind="app")
    SEEN.clear()
    RUNS.clear()
    r = await post(store, "acme/shop", 7, HEAD, "pass", "the comment", "Pass")
    ck("an App posts the check run and the comment", r == {
        "posted_check": True, "posted_comment": True, "posted": True, "why": ""}, r)
    run = RUNS[-1]
    ck("the run is factory/review on the head, success for a pass",
       run["name"] == "factory/review" and run["head_sha"] == HEAD and run["conclusion"] == "success"
       and run["status"] == "completed" and run["output"]["title"] == "Pass", run)
    ck("both calls used the App's token, on the PR's repo",
       [(s[1], s[2]) for s in SEEN] == [("/repos/acme/shop/check-runs", "app-app1"),
                                        ("/repos/acme/shop/issues/7/comments", "app-app1")], SEEN)
    ck("the comment is the body given", SEEN[1][3] == {"body": "the comment"}, SEEN[1])
    for verdict in ("changes", "block"):
        await post(store, "acme/shop", 7, HEAD, verdict, "b", "t")
        ck(f"{verdict} is a failure on the run", RUNS[-1]["conclusion"] == "failure", RUNS[-1])
    await post(store, "acme/shop", 7, HEAD, "pass", "b", "x" * 300)
    ck("a long title is cut to what GitHub shows", len(RUNS[-1]["output"]["title"]) == 100, RUNS[-1])

    print("== an App that cannot write says so and posts nothing ==")
    SEEN.clear()
    RUNS.clear()
    DENY["check"] = {"app-app1"}
    r = await post(store, "acme/shop", 7, HEAD, "pass", "b", "Pass")
    ck("refused: not posted, 403 named, no comment either",
       not r["posted"] and not r["posted_check"] and "403" in r["why"] and not posts("issues") and not RUNS, (r, SEEN))
    store.upsert_github_credential("app2", "", kind="app")
    SEEN.clear()
    r = await post(store, "acme/shop", 7, HEAD, "pass", "b", "Pass")
    ck("the next App that can write posts", r["posted"] and SEEN[-1][2] == "app-app2", (r, SEEN))
    DENY["check"] = set()

    print("== only half went up ==")
    store.delete_github_credential("app2")
    DENY["comment"] = {"app-app1"}
    r = await post(store, "acme/shop", 7, HEAD, "pass", "b", "Pass")
    ck("the check run without the comment: posted is false and the comment is named",
       r["posted_check"] and not r["posted_comment"] and not r["posted"] and "comment" in r["why"], r)
    DENY["comment"] = set()


async def api_cases(app, store):
    global HEAD
    print("== record_review through the API ==")
    root = {"Authorization": f"Bearer {TOKEN}"}
    headers = dict(root)
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station="", role="", parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
    await mf.write_milestone("Store", "", [], project="Shop")
    for t in ("Catalog", "Cart"):
        await m.write_ticket(t, milestone="Store", project="Shop")
    as_station("crew-orchestrator", "orchestrator")
    await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
    await mf.assign_ticket("T2", "shop-build-m1", project="Shop")
    as_station("shop-build-m1", "builder", "crew-orchestrator")
    await mf.set_stage("T1", "doing", project="Shop")
    await mf.set_stage("T1", "review", pr="https://github.com/acme/shop/pull/7", project="Shop")
    as_station("crew-reviewer", "reviewer")

    # the App from the cases above can write: the review is posted, with findings null or empty too
    SEEN.clear()
    RUNS.clear()
    out = json.loads(await mf.record_review("T1", HEAD, "pass", "pytest -q: 14 passed", project="Shop"))
    ck("a review with no findings is recorded and posted",
       out["round"] == 1 and out["github"]["posted"] is True, out)
    comment = [s for s in SEEN if s[1].endswith("/issues/7/comments")][0][3]["body"]
    ck("the comment says no findings and names the station", "No findings." in comment
       and "Reviewed by crew-reviewer" in comment and "@" not in comment, comment)
    ck("the run is on the reviewed head", RUNS and RUNS[-1]["head_sha"] == HEAD and RUNS[-1]["conclusion"] == "success", RUNS)

    # the pull request read takes the check run as the verdict, and a new head makes it stale
    t1 = store.get_ticket(shop, "T1")
    pr = await factory_github.read_pr(store, "acme/shop", 7, t1["pr"])
    ck("read_pr: the check run is the verdict, CI is not touched by it",
       pr["verdict"] == "pass" and pr["verdict_head"] == HEAD and pr["verdict_text"] == "Pass"
       and pr["ci"] == "none", {k: pr.get(k) for k in ("verdict", "verdict_head", "verdict_text", "ci")})
    old = HEAD
    HEAD = "bbbbbbb2222"
    pr = await factory_github.read_pr(store, "acme/shop", 7, t1["pr"])
    ck("a new head has no verdict of its own", pr["verdict"] is None and pr["head"] == "bbbbbbb2222", pr)
    HEAD = old

    # changes with findings: blocking first in the comment
    SEEN.clear()
    out = json.loads(await mf.record_review(
        "T1", HEAD, "changes", "ran it", "no load test",
        [{"severity": "P3", "text": "naming", "blocking": False},
         {"severity": "P2", "file": "a.py", "line": 3, "text": "missing null check"}], project="Shop"))
    comment = [s for s in SEEN if s[1].endswith("/issues/7/comments")][0][3]["body"]
    ck("changes posts a failure with the blocking finding first",
       out["github"]["posted"] and RUNS[-1]["conclusion"] == "failure"
       and comment.index("missing null check") < comment.index("naming"), (out, comment))
    ck("the run's title says how many block", RUNS[-1]["output"]["title"] == "Changes requested, 1 blocking", RUNS[-1])

    # no App can write: the round is still recorded, nothing is posted, and the answer says why
    DENY["check"] = {"app-app1"}
    SEEN.clear()
    out = json.loads(await mf.record_review(
        "T1", HEAD, "pass", "again", "", [], {"F1": "fixed", "F2": "fixed"}, project="Shop"))
    ck("refused by GitHub: recorded, not posted, with the reason",
       out["round"] == 3 and out["github"]["posted"] is False and "403" in out["github"]["why"], out)
    DENY["check"] = set()
    store.delete_github_credential("app1")
    out = json.loads(await mf.record_review("T1", HEAD, "pass", "once more", project="Shop"))
    ck("no App: recorded, not posted, the reviewer posts with gh",
       out["round"] == 4 and out["github"]["posted"] is False and "no GitHub App" in out["github"]["why"], out)

    # a ticket with no pull request has nowhere to post
    as_station("shop-build-m1", "builder", "crew-orchestrator")
    await mf.set_stage("T2", "doing", project="Shop")
    await mf.set_stage("T2", "review", project="Shop")
    as_station("crew-reviewer", "reviewer")
    out = json.loads(await mf.record_review("T2", HEAD, "pass", "read it", project="Shop"))
    ck("no linked pull request: recorded, not posted",
       out["round"] == 1 and out["github"]["posted"] is False and "no linked pull request" in out["github"]["why"], out)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        await post_cases(store)
        store.upsert_github_credential("app1", "", kind="app")
        await api_cases(app, store)


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
