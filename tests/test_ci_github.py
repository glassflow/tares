"""GitHub Actions as the ci integration (TR-853).

Covered: success, failure, pending and none from GitHub fixtures; how each check conclusion counts
(skipped and neutral pass; cancelled, timed_out, action_required, startup_failure, stale and failure
fail; a run still going is pending); a commit with no checks is pending when the repository has
workflow files (or GitHub cannot say) and none when it has none; the "no CI" setting, factory or
project, answers none without asking GitHub; the reviewer's `factory/review` status is never a check;
required names (only those count, a missing one is pending, a failure beats a missing one, blank
names are ignored); `read_pr` gives `ci_checks` and leaves `ci` as it was; `has_workflows` (a folder
with files, an empty or missing folder, no answer, a second credential, a path that is a file,
caching of True and False but not of no answer); empty and null inputs (no store, no repository,
no required list, no head, a PR that cannot be read, no credential).

Run: python tests/test_ci_github.py
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

import httpx

from tares import factory_github as fg
from tares.integrations import registry
from tares.integrations.builtin.ci_github import GithubCI
from tares.integrations.types import CIState, PRRef
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


REPO, N, HEAD = "acme/api", 7, "abc1234def5678"
tmp = tempfile.mkdtemp(prefix="tares-ci-github-")
store = Store(os.path.join(tmp, "t.duckdb"))
store.upsert_github_credential("main", "tok-main")

# what GitHub says right now; each scenario replaces it
G: dict = {}
calls: list[str] = []


def reset(statuses=None, runs=None, workflows=(200, [{"type": "file", "name": "ci.yml"}]),
          head=HEAD, pr_status=200):
    fg._workflows.clear()
    calls.clear()
    G.clear()
    G.update(statuses=statuses or [], runs=runs or [], workflows=workflows, head=head,
             pr_status=pr_status)


def handler(req: httpx.Request):
    path = req.url.path
    calls.append(path)
    if path.endswith(f"/pulls/{N}"):
        if G["pr_status"] != 200:
            return httpx.Response(G["pr_status"], json={})
        return httpx.Response(200, json={
            "title": "Add it", "state": "open", "merged": False,
            "head": {"sha": G["head"], "ref": "factory/T1"}, "base": {"ref": "main"},
            "html_url": f"https://github.com/{REPO}/pull/{N}"})
    if path.endswith("/status"):
        return httpx.Response(200, json={"statuses": G["statuses"]})
    if path.endswith("/check-runs"):
        return httpx.Response(200, json={"check_runs": G["runs"]})
    if path.endswith("/contents/.github/workflows"):
        code, body = G["workflows"]
        return httpx.Response(code, json=body)
    return httpx.Response(404, json={})


fg._transport = httpx.MockTransport(handler)


def run(name, state="completed", conclusion="success"):
    return {"name": name, "status": state, "conclusion": conclusion if state == "completed" else None}


def status(context, state):
    return {"context": context, "state": state}


def pr(**kw):
    base = dict(repo=REPO, number=N, head=HEAD, base="main", url="", ticket="T1", project_uid="p1")
    base.update(kw)
    return PRRef(**base)


def ci(required=None, integration=None, **prkw):
    return asyncio.run((integration or GithubCI(store)).state(pr(**prkw), required if required is not None else []))


def workflow_calls():
    return [c for c in calls if c.endswith("/contents/.github/workflows")]


print("== the class ==")
g = GithubCI(store)
ck("slot, name, title, ee", (g.slot, g.name, g.title, g.ee) == ("ci", "github", "GitHub Actions", False))
ck("one setting: no_ci, a bool, not required",
   [(f.name, f.label, f.kind, f.required) for f in g.settings_schema]
   == [("no_ci", "This repository has no CI", "bool", False)], g.settings_schema)
ck("check_connection", asyncio.run(g.check_connection({})) == (True, "Uses this Tares's GitHub credentials"))
ck("registered as a built-in of the ci slot", registry.get("ci", "github") is GithubCI)

print("== success, failure, pending, none ==")
reset(runs=[run("test"), run("lint")], statuses=[status("legacy/ci", "success")])
out = ci()
ck("all green is success", out == CIState("success", HEAD, [], f"https://github.com/{REPO}/pull/{N}/checks"), out)
ck("no workflow lookup when checks exist", workflow_calls() == [], calls)
reset(runs=[run("test", conclusion="failure"), run("lint")])
out = ci()
ck("a failing run is failure and is named", (out.state, out.failing) == ("failure", ["test"]), out)
reset(runs=[run("test"), run("lint", state="in_progress")])
out = ci()
ck("a run still going is pending", (out.state, out.failing) == ("pending", []), out)
reset(runs=[run("lint", state="queued"), run("test", conclusion="failure")])
ck("a failure beats a pending run", ci().state == "failure")
reset(statuses=[status("ci/legacy", "failure")])
out = ci()
ck("a failing commit status is failure and is named", (out.state, out.failing) == ("failure", ["ci/legacy"]), out)
reset(statuses=[status("ci/legacy", "error")])
ck("an error status is failure", ci().state == "failure")
reset(statuses=[status("ci/legacy", "pending")])
ck("a pending status is pending", ci().state == "pending")
ck("head is the pull request's head", ci().head == HEAD)
reset(runs=[run("test")], head="fff0000")
ck("the head GitHub reports wins over the one asked about", ci().head == "fff0000", ci())

print("== how each conclusion counts ==")
for concl, want in (("success", "success"), ("skipped", "success"), ("neutral", "success"),
                    ("cancelled", "failure"), ("timed_out", "failure"),
                    ("action_required", "failure"), ("startup_failure", "failure"),
                    ("stale", "failure"), ("failure", "failure")):
    reset(runs=[run("job", conclusion=concl)])
    out = ci()
    ck(f"{concl} counts as {want}", out.state == want and out.failing == (["job"] if want == "failure" else []), out)

print("== a commit with no checks ==")
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
out = ci()
ck("workflow files: CI has not started, pending, no failing names", (out.state, out.failing) == ("pending", []), out)
ck("it asked about the head commit", workflow_calls() == ["/repos/acme/api/contents/.github/workflows"], calls)
reset(workflows=(404, {"message": "Not Found"}))
ck("no workflows folder: none", ci().state == "none")
reset(workflows=(200, []))
ck("an empty workflows folder: none", ci().state == "none")
reset(workflows=(500, {}))
out = ci()
ck("GitHub cannot say: pending, not none", (out.state, out.failing) == ("pending", []), out)
reset(workflows=(403, {"message": "rate limited"}))
ck("a refused answer: pending", ci().state == "pending")
reset(workflows=(200, {"type": "file", "name": "workflows"}))
ck("the path is a file: no answer, pending", ci().state == "pending")

print("== the no CI setting ==")
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
store.set_choice("ci", "github", {"no_ci": True}, "t")
out = ci()
ck("factory setting no_ci: none", out.state == "none", out)
ck("and GitHub was not asked at all", calls == [], calls)
ck("project without its own row follows the factory", ci(project_uid="p9").state == "none")
ck("no project at all follows the factory", ci(project_uid="").state == "none")
store.set_project_choice("p2", "ci", "github", {"no_ci": False}, "t")
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
ck("a project's own row wins over the factory's", ci(project_uid="p2").state == "pending")
store.set_choice("ci", "github", {"no_ci": False}, "t")
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
ck("no_ci false: the workflow rule applies", ci().state == "pending")
store.set_project_choice("p3", "ci", "github", {"no_ci": True}, "t")
reset(runs=[run("test", conclusion="failure")])
ck("project no_ci: none even when checks exist", ci(project_uid="p3").state == "none")
ck("another project is not affected", ci(project_uid="p4").state == "failure")
store.clear_project_choice("p3", "ci")
store.set_choice("ci", "github", {"no_ci": False}, "t")
ck("setting off again: checks count", ci().state == "failure")
fresh = Store(os.path.join(tmp, "fresh.duckdb"))
fresh.upsert_github_credential("main", "tok-main")
ck("nothing chosen: the setting is off", asyncio.run(GithubCI(fresh).state(pr(), [])).state == "failure")

print("== the reviewer's status is never a check ==")
reset(statuses=[status("factory/review", "success")], workflows=(404, {}))
ck("only factory/review on the commit: none, as if nothing reported", ci().state == "none")
reset(statuses=[status("factory/review", "failure")], runs=[run("test")])
out = ci()
ck("a changes verdict does not fail CI", out.state == "success" and out.failing == [], out)
reset(statuses=[status("factory/review", "pending")], runs=[run("test")])
ck("a pending verdict does not make CI pending", ci().state == "success")
reset(statuses=[status("factory/review", "success")], runs=[run("test", conclusion="failure")])
out = ci()
ck("a failing run beside it is named alone", (out.state, out.failing) == ("failure", ["test"]), out)
reset(statuses=[status("factory/review", "success")], runs=[run("test")])
ck("required names ask for it: it is not found", ci(["factory/review"]).state == "pending")

print("== required names ==")
reset(runs=[run("test"), run("lint", conclusion="failure"), run("e2e", state="in_progress")])
ck("a failure outside the required names does not count", ci(["test"]).state == "success")
out = ci(["test", "lint"])
ck("a failing required name is named", (out.state, out.failing) == ("failure", ["lint"]), out)
out = ci(["test", "e2e"])
ck("a required check still going is pending", (out.state, out.failing) == ("pending", []), out)
out = ci(["test", "docs"])
ck("a required name with no check is pending", (out.state, out.failing) == ("pending", []), out)
out = ci(["lint", "docs"])
ck("a failure beats a missing required name", (out.state, out.failing) == ("failure", ["lint"]), out)
ck("blank and null names are ignored", ci(["test", "", "  ", None]).state == "success")
ck("only blank names means every check", ci(["", None]).state == "failure")
ck("an empty list means every check", ci([]).state == "failure")
ck("names match exactly, not by case", ci(["Test"]).state == "pending")
reset(statuses=[status("ci/legacy", "success")], runs=[run("test")])
ck("a required name can be a commit status", ci(["ci/legacy", "test"]).state == "success")
reset(workflows=(404, {}))
ck("required names and no checks on a repo without workflows: none", ci(["test"]).state == "none")
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
ck("required names and no checks yet: pending", ci(["test"]).state == "pending")

print("== read_pr gives ci_checks ==")
reset(statuses=[status("a", "success"), status("b", "failure"), status("c", "error"), status("d", "pending"),
                status("factory/review", "success")],
      runs=[run("e"), run("f", conclusion="neutral"), run("g", conclusion="skipped"),
            run("h", conclusion="cancelled"), run("i", state="in_progress"),
            run("j", conclusion="timed_out"), run("k", conclusion="action_required"),
            run("l", conclusion="startup_failure"), run("m", conclusion="stale")])
got = asyncio.run(fg.read_pr(store, REPO, N))
want = [("a", "success"), ("b", "failure"), ("c", "failure"), ("d", "pending"), ("e", "success"),
        ("f", "success"), ("g", "success"), ("h", "failure"), ("i", "pending"), ("j", "failure"),
        ("k", "failure"), ("l", "failure"), ("m", "failure")]
ck("one entry per status and run, reviewer left out, same mapping",
   [(c["name"], c["state"]) for c in got["ci_checks"]] == want, got.get("ci_checks"))
ck("ci is still the one word", got["ci"] == "failure", got["ci"])
ck("verdict still read from the review status", got["verdict"] == "pass", got.get("verdict"))
reset()
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("no checks: an empty list and ci none", got["ci_checks"] == [] and got["ci"] == "none", got)
reset(statuses=[status("factory/review", "success")])
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("only the reviewer's status: an empty list", got["ci_checks"] == [] and got["ci"] == "none", got)
ck("an unnamed run or status is kept with an empty name",
   fg._check_list([{"state": "success"}], [{"status": "completed", "conclusion": "failure"}])
   == [{"name": "", "state": "success"}, {"name": "", "state": "failure"}])
ck("an unknown status word waits as pending", fg._status_state("expected") == "pending")
ck("null lists are empty", fg._check_list(None, None) == [])
ck("a read adds no call to GitHub: pull, status, check runs", len(calls) == 3, calls)

print("== has_workflows ==")
fg._workflows.clear()
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
ck("a folder with a file: True", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is True)
n = len(workflow_calls())
ck("asked again: remembered, no call", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is True and len(workflow_calls()) == n)
ck("another commit is a new question", asyncio.run(fg.has_workflows(store, REPO, "other")) is True and len(workflow_calls()) == n + 1)
reset(workflows=(404, {}))
ck("404: False", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is False)
n = len(workflow_calls())
ck("False is remembered too", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is False and len(workflow_calls()) == n)
reset(workflows=(200, []))
ck("empty folder: False", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is False)
reset(workflows=(500, {}))
ck("no answer: None", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is None)
G["workflows"] = (200, [{"type": "file", "name": "ci.yml"}])
ck("no answer is not remembered: asked again", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is True)
reset(workflows=(200, [{"type": "dir", "name": "sub"}]))
ck("a folder holding only a folder: False", asyncio.run(fg.has_workflows(store, REPO, HEAD)) is False)
reset()
ck("an empty repo: None, no call", asyncio.run(fg.has_workflows(store, "", HEAD)) is None and calls == [])
ck("an empty ref: None, no call", asyncio.run(fg.has_workflows(store, REPO, "")) is None and calls == [])
ck("null repo and ref: None", asyncio.run(fg.has_workflows(store, None, None)) is None)
seen = []
fg._transport = httpx.MockTransport(lambda r: (seen.append(str(r.url)), httpx.Response(404, json={}))[1])
reset()
asyncio.run(fg.has_workflows(store, REPO, "abc123"))
ck("the request names the ref", seen and seen[0].endswith("contents/.github/workflows?ref=abc123"), seen)
fg._transport = httpx.MockTransport(handler)
for i in range(fg._WORKFLOWS_KEPT + 5):
    fg._workflows[(REPO, f"r{i}")] = True
reset(workflows=(200, [{"type": "file", "name": "ci.yml"}]))
fg._workflows.update({(REPO, f"r{i}"): True for i in range(fg._WORKFLOWS_KEPT)})
asyncio.run(fg.has_workflows(store, REPO, "fresh"))
ck("the remembered answers stay few", len(fg._workflows) <= fg._WORKFLOWS_KEPT, len(fg._workflows))

print("== two credentials ==")
store.upsert_github_credential("second", "tok-second")
order = []


def two(req: httpx.Request):
    tok = req.headers.get("authorization", "")
    if req.url.path.endswith("/contents/.github/workflows"):
        order.append(tok)
        if tok.endswith("tok-main"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json=[{"type": "file", "name": "ci.yml"}])
    return httpx.Response(404, json={})


fg._transport = httpx.MockTransport(two)
fg._workflows.clear()
ck("a credential that cannot see the repo does not decide: True from the one that can",
   asyncio.run(fg.has_workflows(store, REPO, HEAD)) is True and len(order) == 2, order)
fg._transport = httpx.MockTransport(handler)

print("== empty and null inputs ==")
reset()
try:
    asyncio.run(GithubCI(None).state(pr(), []))
    ck("no store raises", False)
except RuntimeError as e:
    ck("no store raises, in words", "store" in str(e), e)
ck("check_connection works without a store", asyncio.run(GithubCI().check_connection({}))[0] is True)
for label, kw in (("no repository", {"repo": ""}), ("no number", {"number": 0}), ("number None", {"number": None})):
    try:
        ci(**kw)
        ck(f"{label} raises", False)
    except ValueError as e:
        ck(f"{label} raises ValueError", "repository and number" in str(e), e)
reset(runs=[run("test")])
ck("no head given: the read head is used", ci(head="").head == HEAD)
ck("project_uid None", ci(project_uid=None).state == "success")
ck("ticket empty", ci(ticket="").state == "success")
reset(pr_status=404)
try:
    ci()
    ck("a PR GitHub cannot show raises", False)
except RuntimeError as e:
    ck("a PR GitHub cannot show raises, naming the answer", "404" in str(e), e)
reset(pr_status=500)
try:
    ci()
    ck("a server error raises", False)
except RuntimeError as e:
    ck("a server error raises", True)
empty = Store(os.path.join(tmp, "empty.duckdb"))
try:
    asyncio.run(GithubCI(empty).state(pr(), []))
    ck("no credential raises", False)
except RuntimeError as e:
    ck("no credential raises, saying where to add one", "Settings, GitHub" in str(e), e)
broken = GithubCI(store)
store_real = broken.store
class Bad:
    def __getattr__(self, name):
        return getattr(store_real, name)
    def get_choice(self, slot):
        raise RuntimeError("down")
    def get_project_choice(self, uid, slot):
        raise RuntimeError("down")
reset(runs=[run("test")])
ck("a store that cannot read the settings: the setting is off", asyncio.run(GithubCI(Bad()).state(pr(), [])).state == "success")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
