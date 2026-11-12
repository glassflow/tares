"""merge_ready: one answer to "may this pull request merge?" (TR-855).

One in-process cell, a project with tickets whose pull requests are set directly, GitHub replaced
by a fake read, and the review gate and CI source replaced by fakes the project chose. Covered:
 - the store helper `ticket_for_pr`: finds the ticket by repo (any case) and number, prefers the one
   still open, answers None for another repo, another number, no number, a bad number;
 - a bad pull request reference (None, empty, blank, junk, a number only) is not ready with a plain
   reason and reads nothing; a pull request no ticket has is not ready and names the PR;
 - a failed GitHub read is not ready with the error; a closed, a merged and a draft pull request
   each say so; an open one with a head and everything green is ready with no reasons;
 - the review gate: approved for the head is fine; approved for another commit is stale; changes
   (with its summary), stale, pending and none each hold it back; a gate that raises holds it back
   and the others are still read;
 - CI: success and none do not hold it back; failure names the failing checks; pending says it is
   not finished; a ValueError (a bad required list) gives a plain reason; any other error too;
   the `local` integration gets the cell's source; a project choosing nothing gets `local`;
 - guardrails: none is fine; a blocking decision holds it back; a warn-only one is listed and does
   not; an allowed one is listed and does not; a guardrail that raises blocks;
 - several reasons come back together, and `ready` is true exactly when `reasons` is empty;
 - the route GET /api/factory/merge-ready (200, the answer; an empty pr is 200 not ready; a project
   the caller cannot see is 404) and the merge_ready tool.

Run: .venv/bin/python tests/test_merge_ready.py
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

_TMP = tempfile.mkdtemp(prefix="tares-merge-ready-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-merge-ready"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import factory_github, merge_ready as mr
from tares.integrations import hooks, registry
from tares.integrations.ci import CISource
from tares.integrations.review import ReviewGate
from tares.integrations.types import CIState, Decision, Verdict

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


HEAD = "aaaaaaa1111111111111111111111111111111aa"
OLD = "bbbbbbb2222222222222222222222222222222bb"

# what the fakes answer; a test sets these
GH = {"state": "open", "merged": False, "draft": False, "head": HEAD, "error": "", "calls": []}
REVIEW = {"answer": None}
CI = {"answer": None}
RAILS = {"answer": []}
CI_SEEN = {"source": "unset", "calls": 0, "required": "unset"}


async def fake_read_pr(store, repo, number, prev=None):
    GH["calls"].append((repo, number))
    out = {**(prev or {}), "repo": repo, "number": number,
           "url": f"https://github.com/{repo}/pull/{number}", "base": "factory-modular/m3"}
    if GH["error"]:
        out["error"] = GH["error"]
        if GH["error"].startswith("keep"):
            out["head"] = HEAD
        return out
    out.update({"head": GH["head"], "state": GH["state"], "merged": GH["merged"],
                "draft": GH["draft"]})
    return out


factory_github.read_pr = fake_read_pr


class FakeReview(ReviewGate):
    name = "fakerev"
    title = "Fake review"

    def __init__(self, store=None):
        self.store = store

    async def verdict(self, pr):
        a = REVIEW["answer"]
        if isinstance(a, Exception):
            raise a
        return a


class FakeCI(CISource):
    name = "fakeci"
    title = "Fake CI"

    def __init__(self, store=None):
        self.store = store

    async def state(self, pr, required):
        CI_SEEN["required"] = required
        a = CI["answer"]
        if isinstance(a, Exception):
            raise a
        return a


class SpyLocal(CISource):
    """Stands in for the built-in local integration, to see what it was built with."""
    name = "local"
    title = "Local"

    def __init__(self, store=None, source=None):
        self.store = store
        CI_SEEN["source"] = source

    async def state(self, pr, required):
        CI_SEEN["calls"] += 1
        return CIState(state="success", head=pr.head)


async def fake_run(stage, project, payload):
    a = RAILS["answer"]
    RAILS.setdefault("seen", []).append((stage, project, payload))
    if isinstance(a, Exception):
        raise a
    return a


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}

    def mk(timeout=10):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=dict(root))

    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = mk

    registry.load()
    registry._loaded[("review", "fakerev")] = FakeReview
    registry._loaded[("ci", "fakeci")] = FakeCI
    real_local = registry._loaded.get(("ci", "local"))
    real_run = hooks.run

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        await mf.write_milestone("Store", "", [], project="Shop")
        for t in ("Catalog", "Cart", "Checkout"):
            await m.write_ticket(t, milestone="Store", project="Shop")
        tickets = {t["title"]: t for t in store.list_tickets(shop)}
        cat, cart = tickets["Catalog"], tickets["Cart"]

        def link(ticket, repo="acme/shop", number=7, **more):
            store.update_ticket(ticket["id"], pr={"repo": repo, "number": number,
                                                  "url": f"https://github.com/{repo}/pull/{number}",
                                                  **more})

        def reset():
            GH.update({"state": "open", "merged": False, "draft": False, "head": HEAD,
                       "error": "", "calls": []})
            REVIEW["answer"] = Verdict(state="approved", head=HEAD, by="Fake", summary="Looks good")
            CI["answer"] = CIState(state="success", head=HEAD)
            RAILS["answer"] = []
            RAILS.pop("seen", None)
            CI_SEEN.update({"source": "unset", "calls": 0, "required": "unset"})
            hooks.run = fake_run

        def choose(review="fakerev", ci="fakeci"):
            store.set_project_choice(shop, "review", review, {}, by="test")
            store.set_project_choice(shop, "ci", ci, {}, by="test")

        async def ask(ref="acme/shop#7", source=None):
            return await mr.merge_ready(store, ref, source=source)

        print("== ticket_for_pr ==")
        link(cat, number=7)
        link(cart, repo="acme/other", number=7)
        ck("found by repo and number", (store.ticket_for_pr("acme/shop", 7) or {}).get("id") == cat["id"])
        ck("the repo's case does not matter",
           (store.ticket_for_pr("ACME/Shop", 7) or {}).get("id") == cat["id"])
        ck("the same number in another repo is another ticket",
           (store.ticket_for_pr("acme/other", 7) or {}).get("id") == cart["id"])
        link(cart, repo="Acme/Mixed", number=9)
        ck("a repo stored with capitals is found by its lower-case name (F5)",
           (store.ticket_for_pr("acme/mixed", 9) or {}).get("id") == cart["id"]
           and (store.ticket_for_pr("ACME/MIXED", 9) or {}).get("id") == cart["id"])
        link(cart, repo="acme/other", number=7)
        ck("another number is none", store.ticket_for_pr("acme/shop", 8) is None)
        ck("a number that only contains it is none (70 is not 7)",
           store.ticket_for_pr("acme/shop", 70) is None)
        ck("another repo is none", store.ticket_for_pr("acme/nope", 7) is None)
        for label, repo, num in (("no number", "acme/shop", None), ("a word", "acme/shop", "x"),
                                 ("zero", "acme/shop", 0), ("negative", "acme/shop", -3),
                                 ("no repo", "", 7), ("repo None", None, 7)):
            ck(f"{label} is none, not an error", store.ticket_for_pr(repo, num) is None)
        link(cart, repo="acme/shop", number=7, head="x")
        store.update_ticket(cat["id"], stage="merged")
        ck("two tickets with one PR: the one still open",
           (store.ticket_for_pr("acme/shop", 7) or {}).get("id") == cart["id"])
        store.update_ticket(cart["id"], stage="shipped")
        ck("both done: the first", (store.ticket_for_pr("acme/shop", 7) or {}).get("id") in (cat["id"], cart["id"]))
        store.update_ticket(cat["id"], stage="doing")
        store.update_ticket(cart["id"], stage="doing")
        store.update_ticket(cart["id"], pr=None)
        link(cart, repo="acme/other", number=7)

        print("== a reference that is not a pull request ==")
        reset()
        choose()
        for label, ref in (("None", None), ("empty", ""), ("blank", "   "), ("junk", "hello world"),
                           ("a number only", "7"), ("a repo only", "acme/shop"),
                           ("another site", "https://example.com/acme/shop/pull/7")):
            a = await ask(ref)
            ck(f"{label}: not ready, one plain reason, nothing read",
               a["ready"] is False and len(a["reasons"]) == 1 and a["pr"] is None
               and a["ticket"] is None and GH["calls"] == [], a)
        a = await ask(None)
        ck("None says it is empty", "empty" in a["reasons"][0], a["reasons"])
        a = await ask("hello world")
        ck("junk says what is wrong and what to give",
           "not a pull request reference" in a["reasons"][0] and "owner/repo#12" in a["reasons"][0], a["reasons"])
        a = await ask("x" * 500)
        ck("a long junk reference is cut in the reason", len(a["reasons"][0]) < 200, len(a["reasons"][0]))

        print("== no ticket has the pull request ==")
        a = await ask("acme/shop#99")
        ck("not ready, names the PR, says why",
           a["ready"] is False and a["pr"] == {"repo": "acme/shop", "number": 99}
           and "No ticket" in a["reasons"][0] and a["ticket"] is None and GH["calls"] == [], a)

        print("== GitHub ==")
        GH["error"] = "no GitHub credential on this Tares can read it"
        a = await ask()
        ck("a failed read with no head is not ready, with its error",
           a["ready"] is False and len(a["reasons"]) == 1 and "could not be read" in a["reasons"][0]
           and "no GitHub credential" in a["reasons"][0] and a["head"] == "", a)
        GH["error"] = "keep: rate limited"
        a = await ask()
        ck("a failed read that kept the last snapshot is still not ready, with its error (F1)",
           a["ready"] is False and a["head"] == "" and len(a["reasons"]) == 1
           and "rate limited" in a["reasons"][0] and a["review"] is None and a["ci"] is None, a)
        for label, kept in (("closed", {"state": "closed"}), ("a draft", {"draft": True}),
                            ("merged", {"state": "closed", "merged": True}),
                            ("an older head", {"head": OLD})):
            reset()
            GH.update(kept)
            GH["error"] = "keep: rate limited"
            REVIEW["answer"] = Verdict(state="approved", head=HEAD, by="Fake")
            CI["answer"] = CIState(state="success", head=HEAD)
            a = await ask()
            ck(f"a failed read never answers ready for the snapshot's {label}",
               a["ready"] is False and any("could not be read" in r for r in a["reasons"]), a)
        reset()
        reset()
        for label, change, word in (("closed", {"state": "closed"}, "closed"),
                                    ("merged", {"state": "closed", "merged": True}, "already merged"),
                                    ("a draft", {"draft": True}, "draft"),
                                    ("with no head", {"head": ""}, "no head commit")):
            reset()
            GH.update(change)
            a = await ask()
            ck(f"{label}: not ready, says so",
               a["ready"] is False and any(word in r for r in a["reasons"]), a["reasons"])
        reset()
        GH.update({"state": "closed", "merged": True})
        a = await ask()
        ck("a merged pull request says merged, not closed",
           not any("is closed" in r for r in a["reasons"]), a["reasons"])
        reset()
        a = await ask()
        ck("open, reviewed, green: ready, no reasons",
           a["ready"] is True and a["reasons"] == [] and a["head"] == HEAD, a)
        ck("the answer names the project, the ticket and the PR",
           a["project"] == shop and a["ticket"] and a["pr"]["repo"] == "acme/shop"
           and a["pr"]["number"] == 7 and a["pr"]["state"] == "open", a)
        ck("by the PR's URL too", (await ask("https://github.com/acme/shop/pull/7"))["ready"] is True)
        ck("and with spaces around it", (await ask("  acme/shop#7  "))["ready"] is True)

        print("== the review gate ==")
        cases = (
            ("changes", Verdict(state="changes", head=HEAD, summary="Two problems"), "asked for changes", "Two problems"),
            ("stale", Verdict(state="stale", head=OLD), "older commit", ""),
            ("pending", Verdict(state="pending", head=HEAD), "pending", ""),
            ("none", Verdict(state="none", head=""), "is not reviewed yet", ""),
            ("approved for another commit", Verdict(state="approved", head=OLD, by="Fake"), "older commit", ""),
        )
        for label, v, word, extra in cases:
            reset()
            REVIEW["answer"] = v
            a = await ask()
            ck(f"{label}: not ready, says so",
               a["ready"] is False and len(a["reasons"]) == 1 and word in a["reasons"][0]
               and extra in a["reasons"][0], a["reasons"])
        reset()
        REVIEW["answer"] = Verdict(state="approved", head=OLD, by="Fake")
        a = await ask()
        ck("an approval for another commit is reported as stale", a["review"]["state"] == "stale", a["review"])
        reset()
        REVIEW["answer"] = Verdict(state="approved", head=HEAD[:9], by="Fake")
        a = await ask()
        ck("an approval named by a short hash counts", a["ready"] is True, a)
        reset()
        REVIEW["answer"] = Verdict(state="approved", head="", by="Fake")
        a = await ask()
        ck("an approval that names no head counts", a["ready"] is True, a)
        reset()
        REVIEW["answer"] = Verdict(state="changes", head=HEAD, summary="", findings=["fix it"])
        a = await ask()
        ck("changes without a summary still say something",
           "fix it" in a["reasons"][0] and "aaaaaaa" in a["reasons"][0]
           and a["review"]["findings"] == ["fix it"], a)
        reset()
        REVIEW["answer"] = Verdict(state="changes", head=HEAD, summary="", findings=[], by="CodeRabbit")
        a = await ask()
        ck("a reason names the gate and the commit; with nothing to quote it points at the findings",
           a["reasons"][0].startswith("CodeRabbit asked for changes on aaaaaaa")
           and "see the findings" in a["reasons"][0], a["reasons"])
        reset()
        a = await ask()
        ck("the review part names the gate and who approved",
           a["review"]["integration"] == "fakerev" and a["review"]["state"] == "approved"
           and a["review"]["by"] == "Fake" and a["review"]["head"] == HEAD, a["review"])
        reset()
        REVIEW["answer"] = RuntimeError("the gate is down")
        a = await ask()
        ck("a gate that raises holds it back, and says why",
           a["ready"] is False and a["review"]["state"] == "error"
           and any("gate could not be read" in r and "the gate is down" in r for r in a["reasons"]), a)
        ck("and CI and the guardrails were still read",
           a["ci"]["state"] == "success" and a["guardrails"] == [] and RAILS.get("seen"), a)

        print("== CI ==")
        reset()
        choose()
        await ask()
        ck("the CI source is asked with an empty list of required checks, not None (F5)",
           CI_SEEN["required"] == [], CI_SEEN["required"])
        for label, c, word, ready in (
                ("success", CIState(state="success", head=HEAD), "", True),
                ("none", CIState(state="none", head=HEAD, summary="No CI configured"), "", True),
                ("failure with names", CIState(state="failure", head=HEAD, failing=["pytest -q", "ruff"]), "pytest -q, ruff", False),
                ("failure with no names", CIState(state="failure", head=HEAD, summary="the run was cancelled"), "cancelled", False),
                ("pending", CIState(state="pending", head=HEAD, summary="pytest -q has not run"), "not finished on aaaaaaa: pytest -q has not run", False),
                ("pending with no summary", CIState(state="pending", head=HEAD), "not finished", False)):
            reset()
            CI["answer"] = c
            a = await ask()
            ck(f"{label}: " + ("ready" if ready else "not ready, says why"),
               a["ready"] is ready and (ready or any(word in r for r in a["reasons"]))
               and a["ci"]["state"] == c.state, a)
        reset()
        CI["answer"] = CIState(state="failure", head=HEAD, failing=["ruff"])
        a = await ask()
        ck("a failing CI reason names the commit (F2)",
           a["reasons"] == ["CI is failing on aaaaaaa: ruff"], a["reasons"])
        reset()
        CI["answer"] = ValueError("a required check is not a command")
        a = await ask()
        ck("a bad required list gives a plain reason",
           a["ready"] is False and a["ci"]["state"] == "error"
           and any("required checks are not valid" in r and "not a command" in r for r in a["reasons"]), a)
        reset()
        CI["answer"] = ConnectionError("CI unreachable")
        a = await ask()
        ck("any other CI error gives a plain reason",
           a["ready"] is False and any("CI could not be read" in r and "unreachable" in r for r in a["reasons"]), a)
        ck("and the review was still read", a["review"]["state"] == "approved", a["review"])

        print("== the local CI source ==")
        reset()
        registry._loaded[("ci", "local")] = SpyLocal
        store.set_project_choice(shop, "ci", "local", {}, by="test")
        a = await ask(source="claude_code_x")
        ck("local is built with the cell's source and asked once",
           CI_SEEN["source"] == "claude_code_x" and CI_SEEN["calls"] == 1 and a["ci"]["integration"] == "local"
           and a["ready"] is True, (CI_SEEN, a["reasons"]))
        CI_SEEN.update({"source": "unset", "calls": 0, "required": "unset"})
        a = await ask()
        ck("with no source given, it is built with none", CI_SEEN["source"] is None and a["ready"] is True, CI_SEEN)
        registry._loaded[("ci", "fakeci")] = FakeCI
        store.set_project_choice(shop, "ci", "fakeci", {}, by="test")
        CI_SEEN.update({"source": "unset", "calls": 0, "required": "unset"})
        reset()
        await ask(source="claude_code_x")
        ck("another CI source is not given the source", CI_SEEN["calls"] == 0 and CI_SEEN["source"] == "unset", CI_SEEN)
        if real_local is not None:
            registry._loaded[("ci", "local")] = real_local
        store.set_project_choice(shop, "ci", "local", {}, by="test")
        reset()
        a = await ask(source="claude_code")
        ck("the real local source, with nothing recorded and no required commands, is none: ready",
           a["ci"]["integration"] == "local" and a["ci"]["state"] == "none" and a["ready"] is True, a["ci"])
        choose()

        print("== the guardrails ==")
        reset()
        a = await ask()
        ck("none: ready, nothing listed", a["ready"] is True and a["guardrails"] == [], a["guardrails"])
        ck("asked once, for pre_merge, with the project and the PR",
           len(RAILS["seen"]) == 1 and RAILS["seen"][0][0] == "pre_merge" and RAILS["seen"][0][1] == shop
           and RAILS["seen"][0][2]["repo"] == "acme/shop" and RAILS["seen"][0][2]["number"] == 7
           and RAILS["seen"][0][2]["head"] == HEAD, RAILS.get("seen"))
        reset()
        RAILS["answer"] = [Decision(allow=False, reason="No merges on Friday", integration="calendar")]
        a = await ask()
        ck("a blocking decision holds it back, with who and why",
           a["ready"] is False and len(a["reasons"]) == 1 and "calendar" in a["reasons"][0]
           and "No merges on Friday" in a["reasons"][0] and a["guardrails"][0]["warn_only"] is False, a)
        reset()
        RAILS["answer"] = [Decision(allow=False, reason="Large diff", integration="size", warn_only=True)]
        a = await ask()
        ck("a warn-only decision is listed and does not hold it back",
           a["ready"] is True and a["reasons"] == [] and a["guardrails"][0]["warn_only"] is True
           and a["guardrails"][0]["reason"] == "Large diff", a)
        reset()
        RAILS["answer"] = [Decision(allow=True, reason="fine", integration="size")]
        a = await ask()
        ck("an allowed decision is listed and does not hold it back",
           a["ready"] is True and len(a["guardrails"]) == 1 and a["guardrails"][0]["allow"] is True, a)
        reset()
        RAILS["answer"] = [Decision(allow=False, reason="", integration="")]
        a = await ask()
        ck("a blocking decision with no reason or name still says something",
           a["ready"] is False and "a guardrail" in a["reasons"][0] and "no reason given" in a["reasons"][0], a)
        reset()
        RAILS["answer"] = [Decision(allow=False, reason="Large diff", integration="size", warn_only=True),
                           Decision(allow=False, reason="Freeze", integration="calendar")]
        a = await ask()
        ck("a warning and a block together: blocked, one reason, both listed",
           a["ready"] is False and len(a["reasons"]) == 1 and len(a["guardrails"]) == 2, a)
        reset()
        RAILS["answer"] = None
        a = await ask()
        ck("a guardrail answer of None is nothing", a["ready"] is True and a["guardrails"] == [], a)
        reset()
        RAILS["answer"] = RuntimeError("the policy service is down")
        a = await ask()
        ck("a guardrail that raises blocks, and says why",
           a["ready"] is False and any("guardrails could not be checked" in r and "policy service" in r
                                       for r in a["reasons"])
           and a["guardrails"][0]["allow"] is False, a)
        hooks.run = real_run
        reset()
        hooks.run = real_run
        a = await ask()
        ck("the shipped hooks answer nothing: ready, nothing listed",
           a["ready"] is True and a["guardrails"] == [], a)
        ck("run itself returns an empty list", await real_run("pre_merge", shop, {}) == [])

        print("== several reasons, ready only with none ==")
        reset()
        GH["draft"] = True
        REVIEW["answer"] = Verdict(state="changes", head=HEAD, summary="Fix the cart")
        CI["answer"] = CIState(state="failure", head=HEAD, failing=["pytest -q"])
        RAILS["answer"] = [Decision(allow=False, reason="Freeze", integration="calendar")]
        a = await ask()
        ck("a draft, changes, failing CI and a freeze: four reasons, not ready",
           a["ready"] is False and len(a["reasons"]) == 4, a["reasons"])
        for label in ("draft", "changes", "failing", "Freeze"):
            ck(f"  one of them is the {label}", any(label.lower() in r.lower() for r in a["reasons"]), a["reasons"])
        reset()
        a = await ask()
        ck("ready is exactly 'no reasons'", a["ready"] == (a["reasons"] == []) and a["ready"] is True)

        print("== the route ==")
        reset()
        async with mk() as cx:
            r = await cx.get("/api/factory/merge-ready", params={"pr": "acme/shop#7"})
            body = r.json()
            ck("200 with the answer", r.status_code == 200 and body["ready"] is True and body["head"] == HEAD
               and body["project"] == shop, (r.status_code, body))
            r = await cx.get("/api/factory/merge-ready")
            ck("no pr is 200 and not ready, not an error",
               r.status_code == 200 and r.json()["ready"] is False and "empty" in r.json()["reasons"][0], r.text)
            r = await cx.get("/api/factory/merge-ready", params={"pr": "junk"})
            ck("junk is 200 and not ready", r.status_code == 200 and r.json()["ready"] is False, r.text)
            r = await cx.get("/api/factory/merge-ready", params={"pr": "acme/shop#99"})
            ck("a PR no ticket has is 200 and not ready", r.status_code == 200 and r.json()["ready"] is False
               and r.json()["pr"]["number"] == 99, r.text)
            CI["answer"] = CIState(state="failure", head=HEAD, failing=["pytest -q"])
            r = await cx.get("/api/factory/merge-ready", params={"pr": "acme/shop#7"})
            ck("a failing CI shows through the route", r.status_code == 200 and r.json()["ready"] is False
               and r.json()["ci"]["failing"] == ["pytest -q"], r.text)
            reset()
            r = await cx.get("/api/factory/merge-ready", params={"pr": "acme/shop#7"},
                             headers={"Authorization": "Bearer wrong"})
            ck("a wrong token is refused", r.status_code in (401, 403), r.status_code)
            r = await cx.post("/api/factory/merge-ready", params={"pr": "acme/shop#7"})
            ck("it only reads: POST is not allowed", r.status_code in (404, 405), r.status_code)

        print("== a project the caller cannot see ==")
        from fastapi import HTTPException
        helpers = app.state.host._helpers
        real_404 = helpers["project_or_404"]
        seen_404 = []

        def hidden(uid):
            seen_404.append(uid)
            raise HTTPException(404, "no such project")
        helpers["project_or_404"] = hidden
        try:
            GH["calls"].clear()
            async with mk() as cx:
                r = await cx.get("/api/factory/merge-ready", params={"pr": "acme/shop#7"})
                hid = r.json()
                ck("a hidden project's PR answers 200, and nothing was read",
                   r.status_code == 200 and seen_404 == [shop] and GH["calls"] == [],
                   (r.status_code, seen_404, GH["calls"]))
                r = await cx.get("/api/factory/merge-ready", params={"pr": "acme/shop#99"})
                ck("a PR no ticket has answers the same way as a hidden one (F3)",
                   r.status_code == 200 and r.json()["ready"] is False and hid["ticket"] is None
                   and hid["project"] is None and hid["reasons"][0].startswith("No ticket has acme/shop#")
                   and set(hid) == set(r.json()), (hid, r.json()))
        finally:
            helpers["project_or_404"] = real_404

        print("== the tool ==")
        reset()
        out = json.loads(await mf.merge_ready("acme/shop#7"))
        ck("the tool gives the route's answer", out["ready"] is True and out["head"] == HEAD, out)
        out = json.loads(await mf.merge_ready(""))
        ck("an empty reference through the tool is not ready", out["ready"] is False and out["reasons"], out)
        REVIEW["answer"] = Verdict(state="changes", head=HEAD, summary="Fix it")
        out = json.loads(await mf.merge_ready("https://github.com/acme/shop/pull/7"))
        ck("the tool takes a PR URL and gives the reason", out["ready"] is False
           and any("Fix it" in r for r in out["reasons"]), out)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
