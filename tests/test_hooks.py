"""Hook points: where Tares asks the project's guardrails before it acts (TR-861).

One in-process cell and a guardrail the factory chose, which answers what a test tells it to and
records what it was asked. Covered:
 - the table: pre_build, pre_merge and pre_release are the hook points; `payload_for` gives every
   promised key its empty value for a missing, empty or None payload, keeps what the caller sent,
   never shares a default list between calls, and raises for a stage that is not a hook point;
 - `run`: nothing while no store is bound or the guardrail allows with nothing to say; the decision
   when it blocks or warns; the guardrail's own error comes through; an unknown stage raises;
 - `gate`: ok with no decisions; a block gives "Blocked by <name>: <reason>"; a warn-only decision
   is a warning and does not block; a guardrail that raises, or a stage that does not exist,
   blocks with a plain reason;
 - a hook point added to `HOOK_POINTS` reaches the guardrail with no change anywhere else;
 - pre_build: the hand-over route asks before it records; a block answers 409 with the reason and
   records nothing, a warning lets it through, an unreachable guardrail blocks, and the guardrail
   sees the goal, the spec text, the repos and the autonomy; the hand_over tool shows the reason;
 - pre_merge: merge_ready passes the files, the review and CI; a block is not ready, a warning is
   ready and listed;
 - pre_release: GET /api/factory/release-ready and the release_ready tool: ready with nothing to
   say, blocked with the reason, a warning, an unreachable guardrail, empty and missing inputs,
   a comma separated `merged_prs`, a project the caller cannot see (404), a wrong token;
 - /health lists the hook_points capability, and the releaser's manual asks release_ready.

Run: .venv/bin/python tests/test_hooks.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-hooks-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-hooks"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import factory_github, merge_ready as mr
from tares.integrations import hooks, registry
from tares.integrations.ci import CISource
from tares.integrations.guardrails import Guardrail
from tares.integrations.review import ReviewGate
from tares.integrations.types import CIState, Decision, Verdict

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


HEAD = "aaaaaaa1111111111111111111111111111111aa"

# what the fake guardrail answers, and what it was asked
RAIL = {"answer": None, "seen": []}
GH = {"calls": []}


async def fake_read_pr(store, repo, number, prev=None):
    GH["calls"].append((repo, number))
    return {**(prev or {}), "repo": repo, "number": number, "head": HEAD, "state": "open",
            "merged": False, "draft": False, "base": "factory-modular/m4",
            "url": f"https://github.com/{repo}/pull/{number}"}


async def fake_pr_files(store, repo, number):
    return [{"filename": "tares/a.py", "additions": 3, "deletions": 1},
            {"filename": "docs/b.md", "additions": 10, "deletions": 0}]


factory_github.read_pr = fake_read_pr
factory_github.pr_files = fake_pr_files


class FakeRail(Guardrail):
    name = "fakerail"
    title = "Fake guardrail"

    def __init__(self, store=None):
        self.store = store

    async def check(self, stage, payload):
        RAIL["seen"].append((stage, payload))
        a = RAIL["answer"]
        if isinstance(a, Exception):
            raise a
        return a


class FakeReview(ReviewGate):
    name = "fakerev"
    title = "Fake review"

    def __init__(self, store=None):
        self.store = store

    async def verdict(self, pr):
        return Verdict(state="approved", head=HEAD, by="Fake")


class FakeCI(CISource):
    name = "fakeci"
    title = "Fake CI"

    def __init__(self, store=None):
        self.store = store

    async def state(self, pr, required):
        return CIState(state="success", head=HEAD)


def block(reason="No builds on Friday", who="calendar"):
    return Decision(allow=False, reason=reason, integration=who)


def warn(reason="Large diff", who="size"):
    return Decision(allow=False, reason=reason, integration=who, warn_only=True)


def say(answer):
    RAIL["answer"] = answer
    RAIL["seen"] = []


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
    registry._loaded[("guardrails", "fakerail")] = FakeRail
    registry._loaded[("review", "fakerev")] = FakeReview
    registry._loaded[("ci", "fakeci")] = FakeCI

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        await mf.write_milestone("Store", "", [], project="Shop")
        await m.write_ticket("Catalog", milestone="Store", project="Shop")
        cat = store.list_tickets(shop)[0]
        store.update_ticket(cat["id"], pr={"repo": "acme/shop", "number": 7,
                                           "url": "https://github.com/acme/shop/pull/7"})
        store.set_project_choice(shop, "review", "fakerev", {}, by="test")
        store.set_project_choice(shop, "ci", "fakeci", {}, by="test")

        print("== the table ==")
        ck("the three hook points", list(hooks.HOOK_POINTS) == ["pre_build", "pre_merge", "pre_release"],
           list(hooks.HOOK_POINTS))
        for stage, keys in (("pre_build", {"project", "goal", "spec_text", "repos", "autonomy"}),
                            ("pre_merge", {"repo", "pr", "head", "base", "files", "ticket", "review", "ci"}),
                            ("pre_release", {"repo", "commit", "milestone", "merged_prs"})):
            for label, given in (("None", None), ("empty", {})):
                got = hooks.payload_for(stage, given)
                ck(f"{stage} with {label}: every promised key is there", keys <= set(got), got)
        got = hooks.payload_for("pre_merge", None)
        ck("an empty pre_merge payload has empty values, not missing ones",
           got["files"] == [] and got["review"] == {} and got["ci"] == {} and got["pr"] == 0
           and got["repo"] == "", got)
        got = hooks.payload_for("pre_release", {"commit": "abc", "extra": 1, "milestone": None})
        ck("what the caller sent is kept, extras too, and a None gives the empty value",
           got["commit"] == "abc" and got["extra"] == 1 and got["milestone"] == "", got)
        a = hooks.payload_for("pre_release", {})
        a["merged_prs"].append("x")
        ck("a default list is never shared between calls", hooks.payload_for("pre_release", {})["merged_prs"] == [])
        try:
            hooks.payload_for("pre_deploy", {})
            ck("a stage that is not a hook point raises", False, "no error")
        except ValueError as e:
            ck("a stage that is not a hook point raises, naming the real ones",
               "pre_deploy" in str(e) and "pre_merge" in str(e), str(e))
        for bad in ("", None, "PRE_MERGE"):
            try:
                hooks.payload_for(bad, {})
                ck(f"{bad!r} raises", False, "no error")
            except ValueError:
                ck(f"{bad!r} is not a hook point", True)

        print("== run and gate with the shipped guardrail ==")
        ck("the built-in guardrail has nothing to say at any hook point",
           [await hooks.run(s, shop, {}) for s in hooks.HOOK_POINTS] == [[], [], []])
        g = await hooks.gate("pre_build", shop, {})
        ck("so the gate is open", g == {"ok": True, "reasons": [], "warnings": [], "decisions": []}, g)

        print("== run and gate with a guardrail the factory chose ==")
        store.set_choice("guardrails", "fakerail", {}, by="test")
        say(None)
        ck("a guardrail answering None says nothing", await hooks.run("pre_build", shop, {}) == [])
        say(Decision(allow=True))
        ck("an allow with no reason says nothing", await hooks.run("pre_build", shop, {}) == [])
        say(Decision(allow=True, reason="fine", integration="fakerail"))
        out = await hooks.run("pre_build", shop, {})
        ck("an allow with a reason is passed on", len(out) == 1 and out[0].reason == "fine", out)
        say(block())
        out = await hooks.run("pre_merge", shop, {"repo": "acme/shop", "pr": 4})
        ck("a block comes back as it was", len(out) == 1 and out[0].allow is False
           and out[0].reason == "No builds on Friday", out)
        ck("the guardrail was asked once, with the stage and the shaped payload",
           len(RAIL["seen"]) == 1 and RAIL["seen"][0][0] == "pre_merge"
           and RAIL["seen"][0][1]["pr"] == 4 and RAIL["seen"][0][1]["files"] == [], RAIL["seen"])
        say(RuntimeError("the policy service is down"))
        try:
            await hooks.run("pre_build", shop, {})
            ck("run lets the guardrail's error through", False, "no error")
        except RuntimeError as e:
            ck("run lets the guardrail's error through", "policy service" in str(e), str(e))
        say(block())
        try:
            await hooks.run("pre_deploy", shop, {})
            ck("run raises for an unknown stage", False, "no error")
        except ValueError:
            ck("run raises for an unknown stage", True)
            ck("and never asked the guardrail", RAIL["seen"] == [], RAIL["seen"])
        ck("with no project it still asks (the factory's guardrail)",
           len(await hooks.run("pre_build", None, {})) == 1)
        ck("with an empty project too", len(await hooks.run("pre_build", "", {})) == 1)

        say(None)
        g = await hooks.gate("pre_build", shop, {})
        ck("gate: nothing to say is ok", g["ok"] is True and g["reasons"] == [] and g["warnings"] == [], g)
        say(block())
        g = await hooks.gate("pre_build", shop, {})
        ck("gate: a block is not ok and names who and why",
           g["ok"] is False and g["reasons"] == ["Blocked by calendar: No builds on Friday"]
           and g["warnings"] == [] and g["decisions"][0]["allow"] is False, g)
        say(warn())
        g = await hooks.gate("pre_build", shop, {})
        ck("gate: a warning is ok and listed as a warning",
           g["ok"] is True and g["reasons"] == [] and g["warnings"] == ["size: Large diff"]
           and g["decisions"][0]["warn_only"] is True, g)
        say(Decision(allow=False, reason="", integration="", warn_only=True))
        g = await hooks.gate("pre_build", shop, {})
        ck("gate: a warning with no reason is ok and says nothing",
           g["ok"] is True and g["warnings"] == [], g)
        say(Decision(allow=False, reason="", integration=""))
        g = await hooks.gate("pre_build", shop, {})
        ck("gate: a block with no name or reason still says something",
           g["ok"] is False and g["reasons"] == ["Blocked by a guardrail: no reason given"], g)
        say(RuntimeError("the policy service is down"))
        g = await hooks.gate("pre_build", shop, {})
        ck("gate: a guardrail that cannot be reached blocks, and says why",
           g["ok"] is False and len(g["reasons"]) == 1 and "could not be checked" in g["reasons"][0]
           and "policy service" in g["reasons"][0], g)
        g = await hooks.gate("pre_deploy", shop, {})
        ck("gate: a stage that does not exist blocks", g["ok"] is False and "could not be checked" in g["reasons"][0], g)

        print("== a hook point added later ==")
        hooks.HOOK_POINTS["pre_deploy"] = hooks._fill({"env": "", "targets": []})
        try:
            say(block("No deploys to prod"))
            g = await hooks.gate("pre_deploy", shop, {"env": "prod"})
            ck("it reaches the guardrail and blocks, with no other change",
               g["ok"] is False and "No deploys to prod" in g["reasons"][0], g)
            ck("with its own payload keys", RAIL["seen"][0][0] == "pre_deploy"
               and RAIL["seen"][0][1] == {"env": "prod", "targets": []}, RAIL["seen"])
        finally:
            del hooks.HOOK_POINTS["pre_deploy"]
        g = await hooks.gate("pre_deploy", shop, {})
        ck("and once removed it is not a hook point again", g["ok"] is False and "not a hook point" in g["reasons"][0], g)

        print("== no store bound ==")
        bound = hooks._store
        hooks._store = None
        try:
            say(block())
            ck("nothing to ask: no decisions, and the guardrail was not called",
               await hooks.run("pre_build", shop, {}) == [] and RAIL["seen"] == [])
            ck("an unknown stage still raises", await _raises(hooks.run("nope", shop, {})))
        finally:
            hooks._store = bound

        print("== pre_build: the hand-over ==")
        spec = store.create_doc(shop, "spec", "Spec", "Build a shop with a cart.", by="test")
        body = {"repos": [{"path": "/w/shop", "name": "acme/shop"}], "autonomy": "L3-review"}
        async with mk() as cx:
            say(block("No new builds this week"))
            r = await cx.post(f"/api/projects/{shop}/handover", json=body)
            ck("a block refuses the hand-over with 409 and the reason",
               r.status_code == 409 and "No new builds this week" in r.text
               and "not handed to the crew" in r.text, (r.status_code, r.text))
            ck("and nothing was recorded", not (store.get_handover(shop) or {}).get("repos"),
               store.get_handover(shop))
            seen = RAIL["seen"][0]
            ck("the guardrail saw the project, its goal, the spec, the repos and the autonomy",
               seen[0] == "pre_build" and seen[1]["project"] == shop and seen[1]["goal"] == "Sell things"
               and "cart" in seen[1]["spec_text"] and seen[1]["repos"] == [{"path": "/w/shop", "name": "acme/shop"}]
               and seen[1]["autonomy"] == "L3-review", seen)

            say(RuntimeError("the policy service is down"))
            r = await cx.post(f"/api/projects/{shop}/handover", json=body)
            ck("a guardrail that cannot be reached refuses it too",
               r.status_code == 409 and "could not be checked" in r.text
               and not (store.get_handover(shop) or {}).get("repos"), (r.status_code, r.text))

            say(warn())
            r = await cx.post(f"/api/projects/{shop}/handover", json=body)
            ck("a warning lets it through", r.status_code == 200
               and (store.get_handover(shop) or {}).get("repos"), (r.status_code, r.text))

            say(None)
            r = await cx.post(f"/api/projects/{shop}/handover",
                              json={"repos": [{"path": "/w/shop2", "name": "acme/shop2"}]})
            ck("nothing to say: it is recorded, a later hand-over adds the repo",
               r.status_code == 200 and len((store.get_handover(shop) or {}).get("repos") or []) == 2,
               (r.status_code, r.text))
            ck("the second hand-over's guardrail saw the repos it will hold, and the old autonomy",
               len(RAIL["seen"][0][1]["repos"]) == 2 and RAIL["seen"][0][1]["autonomy"] == "L3-review",
               RAIL["seen"])

            blog = json.loads(await m.create_project("Blog", ""))["id"]
            say(None)
            r = await cx.post(f"/api/projects/{blog}/handover", json={"repos": [{"path": "/w/blog"}]})
            ck("a project with no spec and no goal is asked with empty values, and goes through",
               r.status_code == 200 and RAIL["seen"][0][1]["spec_text"] == ""
               and RAIL["seen"][0][1]["goal"] == "", (r.status_code, RAIL["seen"]))
            say(block("No new builds this week"))
            r = await cx.post(f"/api/projects/{blog}/handover", json={"repos": [{"path": "/w/other"}]})
            ck("a refused later hand-over leaves the repos it had",
               r.status_code == 409 and len((store.get_handover(blog) or {}).get("repos") or []) == 1,
               (r.status_code, store.get_handover(blog)))

        say(block("No new builds this week"))
        out = await mf.hand_over(project="Shop", repo="/w/another")
        ck("the hand_over tool shows the reason", "No new builds this week" in out, out)

        print("== pre_merge: merge_ready ==")
        say(None)
        a = await mr.merge_ready(store, "acme/shop#7")
        ck("nothing to say: ready", a["ready"] is True and a["guardrails"] == [], a)
        seen = RAIL["seen"][0]
        ck("the guardrail is asked at pre_merge with the PR, its files, its review and its CI",
           seen[0] == "pre_merge" and seen[1]["repo"] == "acme/shop" and seen[1]["pr"] == 7
           and seen[1]["head"] == HEAD and seen[1]["base"] == "factory-modular/m4"
           and [f["path"] for f in seen[1]["files"]] == ["tares/a.py", "docs/b.md"]
           and seen[1]["files"][0]["additions"] == 3 and seen[1]["review"]["state"] == "approved"
           and seen[1]["ci"]["state"] == "success" and seen[1]["ticket"], seen)
        say(block("No merges on Friday"))
        a = await mr.merge_ready(store, "acme/shop#7")
        ck("a block: not ready, with the reason",
           a["ready"] is False and any("No merges on Friday" in r for r in a["reasons"]), a)
        say(warn())
        a = await mr.merge_ready(store, "acme/shop#7")
        ck("a warning: ready, and listed", a["ready"] is True and a["guardrails"][0]["warn_only"] is True, a)
        say(RuntimeError("the policy service is down"))
        a = await mr.merge_ready(store, "acme/shop#7")
        ck("an unreachable guardrail: not ready", a["ready"] is False
           and any("could not be checked" in r for r in a["reasons"]), a)

        async def no_files(store, repo, number):
            return None
        factory_github.pr_files = no_files
        say(None)
        a = await mr.merge_ready(store, "acme/shop#7")
        ck("a PR whose files could not be read still asks, with no files",
           a["ready"] is True and RAIL["seen"][0][1]["files"] == [], a)
        factory_github.pr_files = fake_pr_files

        print("== pre_release: the route ==")
        async with mk() as cx:
            async def rel(**params):
                r = await cx.get("/api/factory/release-ready", params=params)
                return r

            say(None)
            r = await rel(project=shop, commit="abc123", repo="acme/shop", milestone="M4",
                          merged_prs="acme/shop#7, acme/shop#8,,")
            ck("nothing to say: ready", r.status_code == 200 and r.json() == {"ready": True, "reasons": [], "warnings": [], "guardrails": []},
               (r.status_code, r.text))
            seen = RAIL["seen"][0]
            ck("asked at pre_release with the commit, the milestone and the PRs split and trimmed",
               seen[0] == "pre_release" and seen[1] == {"repo": "acme/shop", "commit": "abc123",
                                                       "milestone": "M4",
                                                       "merged_prs": ["acme/shop#7", "acme/shop#8"]}, seen)
            say(block("No releases on Friday"))
            r = await rel(project=shop, commit="abc123")
            ck("a block: not ready, with the reason",
               r.json()["ready"] is False and r.json()["reasons"] == ["Blocked by calendar: No releases on Friday"], r.text)
            ck("with only a project and a commit the other keys are empty, not missing",
               RAIL["seen"][0][1] == {"repo": "", "commit": "abc123", "milestone": "", "merged_prs": []}, RAIL["seen"])
            say(warn())
            r = await rel(project=shop, commit="abc123")
            ck("a warning: ready, and shown", r.json() == {"ready": True, "reasons": [],
                                                              "warnings": ["size: Large diff"],
                                                              "guardrails": [{"allow": False, "reason": "Large diff",
                                                                              "integration": "size", "warn_only": True}]}, r.text)
            say(RuntimeError("the policy service is down"))
            r = await rel(project=shop, commit="abc123")
            ck("an unreachable guardrail: not ready, says so",
               r.status_code == 200 and r.json()["ready"] is False and "could not be checked" in r.json()["reasons"][0], r.text)
            say(block())
            r = await rel(commit="abc123")
            ck("no project: not ready, asks for it, never asks the guardrail",
               r.status_code == 200 and r.json()["ready"] is False and "project" in r.json()["reasons"][0]
               and RAIL["seen"] == [], r.text)
            r = await rel(project="   ", commit="abc123")
            ck("a blank project is the same", r.json()["ready"] is False and RAIL["seen"] == [], r.text)
            r = await rel(project=shop)
            ck("no commit: not ready, asks for it, never asks the guardrail",
               r.status_code == 200 and r.json()["ready"] is False and "commit" in r.json()["reasons"][0]
               and RAIL["seen"] == [], r.text)
            r = await rel(project=shop, commit="  ")
            ck("a blank commit is the same", r.json()["ready"] is False and RAIL["seen"] == [], r.text)
            r = await rel(project="no-such-project", commit="abc123")
            ck("a project that does not exist is 404", r.status_code == 404, (r.status_code, r.text))
            r = await cx.get("/api/factory/release-ready", params={"project": shop, "commit": "abc"},
                             headers={"Authorization": "Bearer wrong"})
            ck("a wrong token is refused", r.status_code in (401, 403), r.status_code)
            r = await cx.post("/api/factory/release-ready", params={"project": shop, "commit": "abc"})
            ck("it only reads: POST is not allowed", r.status_code in (404, 405), r.status_code)

        print("== pre_release: the tool ==")
        say(None)
        out = json.loads(await mf.release_ready(project="Shop", commit="abc123", repo="acme/shop",
                                                merged_prs=["acme/shop#7", "acme/shop#8"]))
        ck("the tool gives the route's answer", out["ready"] is True, out)
        ck("and passes the PR list on", RAIL["seen"][0][1]["merged_prs"] == ["acme/shop#7", "acme/shop#8"], RAIL["seen"])
        say(block("No releases on Friday"))
        out = json.loads(await mf.release_ready(project="Shop", commit="abc123"))
        ck("a block through the tool", out["ready"] is False and "No releases on Friday" in out["reasons"][0], out)
        out = json.loads(await mf.release_ready(project="Shop", commit="abc123", merged_prs=None))
        ck("no PR list is fine", out["ready"] is False and RAIL["seen"][0][1]["merged_prs"] == [], out)
        out = json.loads(await mf.release_ready(project="Shop", commit=""))
        ck("an empty commit through the tool is not ready", out["ready"] is False and out["reasons"], out)

        print("== /health and the releaser's manual ==")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            r = await cx.get("/health")
            ck("/health lists hook_points", "hook_points" in (r.json().get("capabilities") or []), r.text)
        skill = open(os.path.join(ROOT, "claude-plugin", "skills", "station-releaser", "SKILL.md")).read()
        ck("the releaser asks release_ready before it tags", "release_ready" in skill)
        ck("and stops when it is not ready, with result blocked",
           re.search(r"release_ready.*ready: false.*stop.*result: blocked", skill, re.S) is not None)

        print("== no em dash in what a person reads ==")
        say(block())
        g = await hooks.gate("pre_build", shop, {})
        r = json.loads(await mf.release_ready(project="Shop", commit=""))
        text = json.dumps([g["reasons"], r["reasons"]], ensure_ascii=False)
        ck("the reasons carry no em dash", chr(0x2014) not in text, text)
        doc = open(os.path.join(ROOT, "tares", "integrations", "hooks.py")).read()
        ck("nor does hooks.py", chr(0x2014) not in doc)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


async def _raises(coro):
    try:
        await coro
    except ValueError:
        return True
    return False


asyncio.run(main())
