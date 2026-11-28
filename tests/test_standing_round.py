"""The standing review round (TR-1663): the merge queue carries a reviewer's pass, it never gives one.

A merge queue pass written after a branch update does not override the reviewer's later `changes`
or `block`, and nothing that shows or acts on "the review verdict" may read it as a pass.

One in-process cell with the real store, routes, screens and review gate. GitHub is a fake behind
`factory_github._transport`.

Run: .venv/bin/python tests/test_standing_round.py
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

_TMP = tempfile.mkdtemp(prefix="tares-standing-round-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-standing-round"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the daemon's own loop stays out of the way

import httpx

from tares import factory as factory_mod
from tares import factory_github, mcp_factory, status_words
from tares.integrations import hooks
from tares.integrations.builtin.review_tares import TaresReview
from tares.integrations.types import PRRef
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake GitHub: open pull requests, number 1 in each repo, all green ─────
PRS = {}            # repo -> the head of its pull request


def handler(req: httpx.Request):
    p = req.url.path.strip("/").split("/")
    if req.method != "GET" or len(p) < 4 or p[0] != "repos":
        return httpx.Response(404, json={})
    repo = f"{p[1]}/{p[2]}"
    if p[3] == "pulls" and len(p) == 5 and repo in PRS:
        return httpx.Response(200, json={
            "number": 1, "title": "PR", "state": "open", "merged": False, "draft": False,
            "html_url": f"https://github.com/{repo}/pull/1",
            "head": {"sha": PRS[repo], "ref": "factory/T1", "repo": {"full_name": repo}},
            "base": {"ref": "main"}, "mergeable": True, "mergeable_state": "clean"})
    if p[3] == "commits" and p[-1] == "status":
        return httpx.Response(200, json={"statuses": []})
    if p[3] == "commits" and p[-1] == "check-runs":
        return httpx.Response(200, json={"check_runs": [
            {"name": "build", "status": "completed", "conclusion": "success"}]})
    return httpx.Response(404, json={})


factory_github._transport = httpx.MockTransport(handler)


async def rails(stage, project, payload, ticket=""):
    return []


hooks.run = rails

A, B, C, D, E = (x * 40 for x in "abcde")     # the commits of a reviewed pull request
R, Q = "crew-reviewer", factory_mod.MERGE_QUEUE

# name -> (the rounds in order as (verdict, head, by), the pull request's head is E,
#          the round that stands as (number, verdict, on the head) or None)
SCENARIOS = {
    # the timeline of the pull request that showed it: the queue's pass written last, over the
    # reviewer's `changes` on the same commit
    "timeline": ([("changes", A, R), ("pass", B, R), ("pass", C, Q), ("pass", D, Q),
                  ("changes", D, R), ("changes", E, R), ("pass", E, Q)], (6, "changes", True)),
    "overblock": ([("pass", B, R), ("block", E, R), ("pass", E, Q)], (2, "block", True)),
    "older": ([("pass", B, R), ("changes", D, R), ("pass", E, Q)], (2, "changes", False)),
    "carried": ([("pass", A, R), ("pass", E, Q)], (2, "pass", True)),
    "queueonly": ([("pass", D, Q), ("pass", E, Q)], (2, "pass", True)),
    "norounds": ([], None),
}
# what turn_of says for a pull request in review with that standing round, at L3-review
TURN = {"timeline": "changes_asked", "overblock": "stuck", "older": "reviewing",
        "carried": "ready_for_you", "queueonly": "ready_for_you", "norounds": "reviewing"}


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    store.upsert_github_credential("main", "tok-main")
    person = store.create_person("Priya", email="priya@example.com")

    def project(name, autonomy="L4-ship"):
        """A factory project a crew has, with the Tares reviewer."""
        uid = "uc_" + name
        store.create_project(uid, name, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        store.set_handover(uid, [{"path": "/w/a", "name": name}], autonomy, None, now_utc(),
                           person_id=person["id"])
        return uid

    def ticket(uid, repo, head, title="Work", stage="review"):
        tid = store.create_ticket(uid, "tares", title, repo=repo)
        PRS[repo] = head
        store.update_ticket(tid, stage=stage, pr={"url": f"https://github.com/{repo}/pull/1",
                                                  "number": 1, "repo": repo})
        return tid

    def round_on(uid, tid, repo, verdict, head, by="crew-reviewer"):
        finds = [] if verdict == "pass" else [{"severity": "P2", "file": "a.py", "line": 1,
                                               "blocking": True, "text": "A batch is lost"}]
        return store.add_review(uid, tid, f"https://github.com/{repo}/pull/1", head, verdict,
                                "Read the delta", "", by, finds, {}, factory_mod.finding_kind,
                                now_utc())

    async with app.router.lifespan_context(app):
        await readers(app, store, project, ticket, round_on)

    print(f"\n{P} passed, {F} failed")
    return F


def short(v):
    """(round, verdict, current) of a verdict the server answers, or None."""
    return (v["round"], v["verdict"], v["current"]) if v else None


async def readers(app, store, project, ticket, round_on):
    auth = {"Authorization": f"Bearer {TOKEN}"}

    def client(**kw):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cell",
                                 headers=auth, **kw)

    mcp_factory._cx = lambda timeout=10: client()
    mcp_factory.TARESD = "http://cell"

    def reviewed(name, rounds, stage="review"):
        """A project with one ticket whose pull request is at E and has these rounds."""
        uid, repo = project(name, "L3-review"), f"acme/{name}"
        tid = ticket(uid, repo, E, stage=stage)
        store.update_ticket(tid, pr={**store.get_ticket(uid, tid)["pr"], "head": E,
                                     "state": "open"})
        for verdict, head, by in rounds:
            round_on(uid, tid, repo, verdict, head, by=by)
        store.merge_queue_add(repo, "main", 1, E, tid, uid, "queued")
        return uid, repo, tid

    async with client() as http:
        async def get(path, **params):
            r = await http.get(path, params=params or None)
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
            return r.json()

        for name, (rounds, want) in SCENARIOS.items():
            print(f"== readers: {name} ==")
            uid, repo, tid = reviewed(name, rounds)
            label = factory_mod.label(store.get_ticket(uid, tid))
            kind = want[1] if want else None

            def same(who, got, expect=want):
                ck(f"{name}: {who}", got == expect, f"got {got!r}, want {expect!r}")

            got = store.last_reviews([tid]).get(tid)
            same("store.last_reviews", (got["round"], got["verdict"]) if got else None,
                 want[:2] if want else None)

            gate = await TaresReview(store).verdict(PRRef(repo, 1, E, "main", "", tid, uid))
            state = (("none" if not want else "stale" if not want[2]
                      else {"pass": "approved", "changes": "changes", "block": "changes"}[kind]))
            same("the review gate", gate.state, state)

            full = await get(f"/api/projects/{uid}/tickets/{label}")
            same("get one ticket: verdict", short(full["verdict"]))
            same("get one ticket: every round still listed", len(full["reviews"]), len(rounds))

            listed = (await get(f"/api/projects/{uid}/tickets"))["tickets"][0]
            same("list tickets: verdict", short(listed["verdict"]))

            one = app.state.deps.ticket_out(uid, store.get_ticket(uid, tid))
            same("a ticket's view outside a list: verdict", short(one["verdict"]))

            row = (await get("/api/review-queue", project=uid))["queue"]
            row = next(q for q in row if q["project"] == uid)
            same("review queue: verdict", short(row["verdict"]))

            tool = json.loads(await mcp_factory.review_queue())
            note = next(q for q in tool["queue"] if q["title"] == "Work"
                        and q["project_name"] == name)["verdict_note"]
            same("MCP review_queue: says the verdict is for an older commit",
                 bool(note), bool(want) and not want[2])

            repos = (await get(f"/api/projects/{uid}/repos"))["repos"]
            pr = next(p for r in repos for p in r["open_prs"])
            same("project repos: the pull request's review", short(pr["review"]))

            brief = (await get(f"/api/projects/{uid}/tickets/{label}/builder-brief"))["brief"]
            line = next((x for x in brief.splitlines()
                         if x.startswith("- Reviewer's verdict:")), None)
            same("builder brief: reviewer's verdict",
                 (line.split(": ", 1)[1].split(".")[0], "older commit" in line) if line else None,
                 (kind, not want[2]) if want else None)

            queue = await get(f"/api/projects/{uid}/merge-queue")
            mq = queue["groups"][0]["rows"][0]
            words = {None: "Not reviewed yet",
                     "pass": "Approved by the crew reviewer",
                     "changes": ("The crew reviewer asked for changes" if want and want[2]
                                 else "Not reviewed yet"),
                     "block": status_words.reviewer_stopped("A batch is lost",
                                                            "the crew reviewer")}[kind]
            same("merge queue page: review line", mq["review"], words)
            same("merge queue page: whose turn", mq["turn"], TURN[name])

            word = status_words.VERDICT.get(kind, kind)
            line = (None if not want else f"round {want[0]}: {word}"
                    + ("" if want[2] else ", then the pull request changed"))
            page = await get(f"/api/factory/projects/{uid}")
            same("project page: moving ticket review and turn",
                 (page["moving"][0]["review"], page["moving"][0]["turn"]), (line, TURN[name]))
            same("project page: recent pull request review", page["prs_recent"][0]["review"], line)
            gh = await get(f"/api/factory/projects/{uid}/github")
            same("project GitHub page: review", gh["repos"][0]["prs"][0]["review"], line)
            all_ = await get("/api/factory/tickets", project=uid)
            same("tickets page: review and turn",
                 (all_["tickets"][0]["review"], all_["tickets"][0]["turn"]), (line, TURN[name]))

            reviews = await get("/api/factory/reviews", project=uid)
            lists = ("waiting_for_you", "waiting_for_others", "with_the_crew", "nobody_on_it")
            found = [(k, x) for k in lists for x in reviews[k]]
            words = {None: "Not reviewed yet",
                     "pass": f"Passed, round {want[0]}" if want else "",
                     "changes": f"Changes asked, round {want[0]}" if want else "",
                     "block": status_words.reviewer_stopped() + (f", round {want[0]}" if want
                                                                 else "")}[kind]
            same("reviews page: review line and turn",
                 [(x["review"], x["turn"]) for _, x in found], [(words, TURN[name])])
            same("reviews page: waits for a person only on a standing pass",
                 [k.startswith("waiting_for") for k, _ in found], [TURN[name] == "ready_for_you"])

            page = await get(f"/api/factory/projects/{uid}/tickets/{label}")
            same("ticket page: every round still listed",
                 [r["round"] for r in page["reviews"]], list(range(1, len(rounds) + 1)))

        print("== readers: a ticket sent back for changes ==")
        # the reviewer's `changes` is on an older commit than the head, a queue pass is on the head:
        # new commits after `changes` put the ticket back in review
        for name, key, stage in (("movedback", "older", "review"),
                                 ("stays", "timeline", "changes")):
            uid, repo, tid = reviewed(name, SCENARIOS[key][0], stage="changes")
            label = factory_mod.label(store.get_ticket(uid, tid))
            await get(f"/api/projects/{uid}/tickets/{label}")
            got = store.get_ticket(uid, tid)["stage"]
            ck(f"{name}: a read of the pull request leaves the ticket in {stage}", got == stage, got)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
