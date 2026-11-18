"""GitHub Actions as the ci integration (TR-853).

Covered: success, failure, pending and none from GitHub fixtures; how each check conclusion counts
(skipped and neutral pass; cancelled, timed_out, action_required, startup_failure, stale and failure
fail; a run still going is pending); a commit with no checks is pending when the repository has
workflow files (or GitHub cannot say) and none when it has none; the "no CI" setting, factory or
project, answers none without asking GitHub; the reviewer's `factory/review` status is never a check;
a job cancelled in a workflow run that a later run of the same workflow on the same commit replaced
(a draft run cancelled by marking the PR ready) is no failure, any other cancel still is, and the
workflow runs are asked for only when a job was cancelled; required names (only those count, a
missing one is pending, a failure beats a missing one, blank names are ignored); `read_pr` gives `ci_checks` and leaves `ci` as it was; `has_workflows` (a folder
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
          head=HEAD, pr_status=200, st_code=200, runs_code=200, st_total=None, runs_total=None,
          wruns=None, wruns_code=200):
    fg._workflows.clear()
    calls.clear()
    G.clear()
    G.update(statuses=statuses or [], runs=runs or [], workflows=workflows, head=head,
             pr_status=pr_status, st_code=st_code, runs_code=runs_code, st_total=st_total,
             runs_total=runs_total, wruns=wruns or [], wruns_code=wruns_code)


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
        if G["st_code"] == "raise":
            raise httpx.ConnectError("down")
        if G["st_code"] != 200:
            return httpx.Response(G["st_code"], json={})
        body = {"statuses": G["statuses"]}
        if G["st_total"] is not None:
            body["total_count"] = G["st_total"]
        return httpx.Response(200, json=body)
    if path.endswith("/check-runs"):
        if G["runs_code"] == "raise":
            raise httpx.ConnectError("down")
        if G["runs_code"] != 200:
            return httpx.Response(G["runs_code"], json={})
        body = {"check_runs": G["runs"]}
        if G["runs_total"] is not None:
            body["total_count"] = G["runs_total"]
        return httpx.Response(200, json=body)
    if path.endswith("/actions/runs"):
        if G["wruns_code"] == "raise":
            raise httpx.ConnectError("down")
        if G["wruns_code"] != 200:
            return httpx.Response(G["wruns_code"], json={})
        return httpx.Response(200, json={"workflow_runs": G["wruns"]})
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

print("== a later run of the same name replaces the earlier one ==")
SUITE = [9000]


def real_run(name, rid, started, status="completed", conclusion="success", suite=None, app=None):
    """A check run as GitHub's check-runs endpoint sends it (the fields the gate reads and those it ignores)."""
    suite = suite or rid + 100
    return {"id": rid, "node_id": f"CR_{rid}", "name": name, "head_sha": HEAD, "status": status,
            "conclusion": conclusion if status == "completed" else None, "started_at": started,
            "completed_at": started if status == "completed" else None,
            "html_url": f"https://github.com/{REPO}/actions/runs/{rid}/job/{rid}",
            "app": app or {"id": 15368, "slug": "github-actions"}, "check_suite": {"id": suite},
            "output": {"title": None, "summary": None}}


def real_status(context, state, sid, updated):
    """A commit status as GitHub's combined-status endpoint sends it."""
    return {"id": sid, "context": context, "state": state, "description": None, "target_url": None,
            "created_at": updated, "updated_at": updated}


DRAFT, READY = "2026-11-17T17:05:04Z", "2026-11-17T17:29:30Z"
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("test", 22, READY)])
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("a cancelled run with a later run of its name that passed: one check, success",
   got["ci"] == "success" and got["ci_checks"] == [{"name": "test", "state": "success"}], got)
out = ci()
ck("...and the gate says success with nothing failing", (out.state, out.failing) == ("success", []), out)
reset(runs=[real_run("test", 22, READY), real_run("test", 11, DRAFT, conclusion="cancelled")])
ck("...whichever order GitHub lists them in", ci().state == "success")
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled")])
out = ci()
ck("a cancelled run with no later run of its name still fails, named",
   (out.state, out.failing) == ("failure", ["test"]), out)
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("lint", 12, DRAFT, conclusion="cancelled"),
            real_run("test", 22, READY)])
out = ci()
ck("a name that never got a later run keeps failing when another name's did",
   (out.state, out.failing) == ("failure", ["lint"]), out)
reset(runs=[real_run("test", 11, DRAFT, conclusion="success"), real_run("test", 22, READY, conclusion="failure")])
out = ci()
ck("a later run that failed counts, though an earlier one passed", (out.state, out.failing) == ("failure", ["test"]), out)
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("test", 22, READY, conclusion="cancelled")])
ck("two cancelled runs of one name are one failing check", ci().failing == ["test"])
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("test", 22, READY, status="in_progress")])
out = ci()
ck("a later run still going is pending, not failure", (out.state, out.failing) == ("pending", []), out)
reset(runs=[real_run("test", 22, READY, conclusion="failure"), real_run("test", 11, READY, conclusion="success")])
ck("the same start time: the higher id counts", ci().failing == ["test"])
reset(runs=[real_run("test", 11, READY, conclusion="failure"), real_run("test", 22, READY, conclusion="success")])
ck("...in either direction", ci().state == "success")
reset(runs=[{"name": "test", "status": "completed", "conclusion": "failure"},
            {"name": "test", "status": "completed", "conclusion": "success"}])
ck("runs with no id and no time: the first one read counts, as before", ci().state == "failure")
names = [f"job {i}" for i in range(21)]
reset(runs=[real_run(n, 100 + i, DRAFT, conclusion="cancelled") for i, n in enumerate(names)]
      + [real_run(n, 200 + i, READY) for i, n in enumerate(names)])
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("21 names, each a cancelled run then a passing one: 21 checks, all success, ci success",
   got["ci"] == "success" and len(got["ci_checks"]) == 21
   and {c["state"] for c in got["ci_checks"]} == {"success"}, got["ci"])
out = ci(required=["job 3", "job 20"])
ck("...and required names are met by the later run", (out.state, out.failing) == ("success", []), out)
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("test", 22, READY),
            real_run("factory/review", 33, DRAFT, conclusion="failure")])
ck("the reviewer's own run is still not a check", ci().state == "success")

print("== a check name is told apart per GitHub App ==")
ACTIONS, OTHER = {"id": 15368, "slug": "github-actions"}, {"id": 8800, "slug": "other-ci"}
reset(runs=[real_run("test", 11, DRAFT, conclusion="failure"), real_run("test", 22, READY, app=OTHER)])
out = ci()
ck("a failing run of an app, a later passing run of that name from another app: failure, named",
   (out.state, out.failing) == ("failure", ["test"]), out)
reset(runs=[real_run("test", 22, READY, app=OTHER), real_run("test", 11, DRAFT, conclusion="failure")])
ck("...whichever order GitHub lists them in", ci().failing == ["test"])
got = (reset(runs=[real_run("test", 11, DRAFT, conclusion="failure"), real_run("test", 22, READY, app=OTHER)]),
       asyncio.run(fg.read_pr(store, REPO, N)))[1]
ck("...read_pr gives ci failure and both checks", got["ci"] == "failure"
   and got["ci_checks"] == [{"name": "test", "state": "failure"}, {"name": "test", "state": "success"}], got)
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("test", 22, READY),
            real_run("test", 33, READY, app=OTHER)])
ck("a cancelled run is still replaced by a later run of the same app and name", ci().state == "success")
reset(runs=[real_run("test", 11, DRAFT, conclusion="cancelled"), real_run("test", 22, READY),
            real_run("test", 33, DRAFT, conclusion="failure", app=OTHER)])
ck("...while the other app's own failing run keeps failing", ci().failing == ["test"])
a, b = real_run("test", 11, DRAFT, conclusion="failure"), real_run("test", 22, READY)
del a["app"], b["app"]
reset(runs=[a, b])
ck("runs with no app are grouped by name alone, the later one counts", ci().state == "success")
b["app"] = OTHER
reset(runs=[a, b])
ck("a run with no app and a later run with an app are told apart", ci().failing == ["test"])
reset(runs=[real_run("test", 11, DRAFT, conclusion="failure", app={"slug": "x"}),
            real_run("test", 22, READY, app={"slug": "y"})])
ck("an app with a slug and no id is told apart by its slug", ci().failing == ["test"])
reset(runs=[real_run("test", 11, DRAFT, app={"id": None}), real_run("test", 22, READY, conclusion="failure", app="odd")])
ck("an app that is not a mapping counts as no app", ci().failing == ["test"])

print("== the newest status of a context counts ==")
reset(statuses=[real_status("ci/legacy", "failure", 1, DRAFT), real_status("ci/legacy", "success", 2, READY)])
out = ci()
ck("an older failing status, a newer passing one: success", (out.state, out.failing) == ("success", []), out)
reset(statuses=[real_status("ci/legacy", "success", 2, READY), real_status("ci/legacy", "failure", 1, DRAFT)])
ck("...whichever order they come in", ci().state == "success")
reset(statuses=[real_status("ci/legacy", "success", 1, DRAFT), real_status("ci/legacy", "error", 2, READY)])
out = ci()
ck("an older passing status, a newer error: failure, named", (out.state, out.failing) == ("failure", ["ci/legacy"]), out)
reset(statuses=[real_status("ci/legacy", "failure", 1, DRAFT)])
ck("a failing status with no newer one still fails", ci().state == "failure")
reset(statuses=[real_status("ci/legacy", "failure", 1, DRAFT), real_status("ci/other", "success", 2, READY)])
ck("another context's newer status does not hide a failure", ci().failing == ["ci/legacy"])
reset(statuses=[real_status("same", "failure", 1, DRAFT)], runs=[real_run("same", 22, READY)])
ck("a status and a check run of one name are two checks", ci().failing == ["same"])

print("== the overall word and the fallback stamp ==")
reset(statuses=[real_status("ci/legacy", "failure", 1, DRAFT), real_status("ci/legacy", "success", 2, READY)])
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("ci for an older failing status and a newer passing one is success", got["ci"] == "success", got["ci"])
reset(statuses=[real_status("ci/legacy", "success", 1, DRAFT), real_status("ci/legacy", "failure", 2, READY)])
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("...and failure when the newer one failed", got["ci"] == "failure", got["ci"])
old, new = real_run("test", 22, DRAFT, conclusion="cancelled"), real_run("test", 11, READY)
del old["started_at"], new["started_at"]
old["created_at"], new["created_at"] = DRAFT, READY
reset(runs=[old, new])
ck("a run with no started_at is ordered by created_at, not by id", ci().state == "success")
reset(runs=[new, old])
ck("...in either order", ci().state == "success")

print("== a job cancelled because a later run of its workflow replaced it ==")


def wrun(rid, workflow=306034208, head=HEAD, status="completed", event="pull_request", branch="factory/T1", name="CI"):
    """A workflow run as GitHub's actions/runs endpoint sends it (the fields the gate reads)."""
    return {"id": rid, "workflow_id": workflow, "name": name, "head_sha": head, "event": event,
            "head_branch": branch, "status": status}


TEMPLATE = "e2e (${{ matrix.shard }}/2)"   # a matrix job cancelled before it expanded keeps its template name
draft_jobs = [real_run("parity (1/2)", 11, DRAFT, conclusion="cancelled"),
              real_run(TEMPLATE, 12, DRAFT, conclusion="cancelled")]
ready_jobs = [real_run("parity (1/2)", 21, READY), real_run("e2e (1/2)", 22, READY),
              real_run("e2e (2/2)", 23, READY)]
for j, rid in zip(draft_jobs + ready_jobs, (1, 1, 2, 2, 2)):
    j["html_url"] = f"https://github.com/{REPO}/actions/runs/{rid}/job/{j['id']}"
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2)])
got = asyncio.run(fg.read_pr(store, REPO, N))
ck("a cancel in a run a later run of the same workflow replaced is no failure, whatever its name",
   got["ci"] == "success" and TEMPLATE not in [c["name"] for c in got["ci_checks"]], got)
ck("...the later run's own checks are all listed",
   sorted(c["name"] for c in got["ci_checks"]) == ["e2e (1/2)", "e2e (2/2)", "parity (1/2)"], got["ci_checks"])
out = ci()
ck("...and the gate says success with nothing failing", (out.state, out.failing) == ("success", []), out)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(2), wrun(1)])
ck("...whichever order GitHub lists the runs in", ci().state == "success")
reset(runs=draft_jobs, wruns=[wrun(1)])
out = ci()
ck("a cancel with no later run of its workflow still fails, named",
   (out.state, out.failing) == ("failure", ["parity (1/2)", TEMPLATE]), out)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2, workflow=410143077)])
out = ci()
ck("a later run of another workflow does not replace it", out.state == "failure" and TEMPLATE in out.failing, out)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2, head="other")])
ck("a later run on another commit does not replace it", TEMPLATE in ci().failing)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1)])
ck("a replacing run GitHub does not list does not replace it", TEMPLATE in ci().failing)
reset(runs=draft_jobs + ready_jobs, wruns=[], wruns_code=500)
ck("the workflow runs cannot be read (500): cancels count as before", TEMPLATE in ci().failing)
ck("...and read_pr marks the read partial", asyncio.run(fg.read_pr(store, REPO, N))["ci_partial"] is True)
reset(runs=draft_jobs + ready_jobs, wruns=[], wruns_code=403)
ck("...a 403 (no actions:read) is the same: counted as before, partial",
   TEMPLATE in ci().failing and asyncio.run(fg.read_pr(store, REPO, N))["ci_partial"] is True)
reset(runs=draft_jobs + ready_jobs, wruns=[], wruns_code="raise")
ck("...or the call raises", TEMPLATE in ci().failing)
ck("...and that too is partial", asyncio.run(fg.read_pr(store, REPO, N))["ci_partial"] is True)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2)])
ck("a workflow-run read that worked is not partial", asyncio.run(fg.read_pr(store, REPO, N))["ci_partial"] is False)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2, event="workflow_dispatch")])
ck("a later run of another event (a manual dispatch) does not replace it", TEMPLATE in ci().failing)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2, branch="other")])
ck("a later run of another branch does not replace it", TEMPLATE in ci().failing)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2)])
failed = real_run("lint", 13, DRAFT, conclusion="failure")
failed["html_url"] = f"https://github.com/{REPO}/actions/runs/1/job/13"
reset(runs=draft_jobs + ready_jobs + [failed], wruns=[wrun(1), wrun(2)])
ck("a job that failed in a replaced run still fails", ci().failing == ["lint"])
timed = real_run("lint", 13, DRAFT, conclusion="timed_out")
timed["html_url"] = failed["html_url"]
reset(runs=draft_jobs + ready_jobs + [timed], wruns=[wrun(1), wrun(2)])
ck("...a timed out one too", ci().failing == ["lint"])
pending = [dict(j) for j in ready_jobs]
pending[1] = real_run("e2e (1/2)", 22, READY, status="in_progress")
pending[1]["html_url"] = ready_jobs[1]["html_url"]
reset(runs=draft_jobs + pending, wruns=[wrun(1), wrun(2)])
out = ci()
ck("the replacing run still going: pending, the replaced cancel not counted", (out.state, out.failing) == ("pending", []), out)
cancelled_later = [dict(j) for j in ready_jobs]
cancelled_later[1] = real_run("e2e (1/2)", 22, READY, conclusion="cancelled")
cancelled_later[1]["html_url"] = ready_jobs[1]["html_url"]
reset(runs=draft_jobs + cancelled_later, wruns=[wrun(1), wrun(2)])
ck("a cancel in the newest run still fails", ci().failing == ["e2e (1/2)"])
bare = real_run("lone", 14, DRAFT, conclusion="cancelled")
bare["html_url"] = ""
reset(runs=[bare, real_run("other", 21, READY)], wruns=[wrun(1), wrun(2)])
ck("a cancelled check with no run in its URL still fails", ci().failing == ["lone"])
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2)])
asyncio.run(fg.read_pr(store, REPO, N))
ck("a commit with a cancelled job costs one more call, for the workflow runs",
   [c for c in calls if c.endswith("/actions/runs")] == [f"/repos/{REPO}/actions/runs"] and len(calls) == 4, calls)
reset(runs=[real_run("test", 22, READY)], wruns=[wrun(2)])
asyncio.run(fg.read_pr(store, REPO, N))
ck("a commit with no cancelled job costs no extra call", len(calls) == 3, calls)
reset(runs=draft_jobs + ready_jobs, wruns=[wrun(1), wrun(2)], head="")
asyncio.run(fg.read_pr(store, REPO, N))
ck("a PR with no head asks nothing about workflow runs", not [c for c in calls if c.endswith("/actions/runs")], calls)

print("== the run that replaced it has not finished ==")
partial_jobs = [real_run("test (duckdb)", 11, DRAFT), real_run(TEMPLATE, 12, DRAFT, conclusion="skipped"),
                real_run("test (postgres)", 13, DRAFT, conclusion="cancelled")]
for j in partial_jobs:
    j["html_url"] = f"https://github.com/{REPO}/actions/runs/1/job/{j['id']}"
for st in ("queued", "in_progress", "waiting", "requested", "pending"):
    reset(runs=partial_jobs, wruns=[wrun(1), wrun(2, status=st)])
    out = ci()
    got = asyncio.run(fg.read_pr(store, REPO, N))
    ck(f"the cancel is dropped but the replacing run is {st} with no jobs yet: pending, nothing failing",
       (out.state, out.failing) == ("pending", []) and got["ci"] == "pending", out)
ck("...and ci_checks lists it as a pending check, so the queue waits",
   {"name": "CI", "state": "pending", "waiting": True,
    "jobs": sorted(["test (duckdb)", "test (postgres)", TEMPLATE])} in got["ci_checks"]
   and [c["name"] for c in got["ci_checks"] if c["state"] == "failure"] == [], got["ci_checks"])
reset(runs=partial_jobs, wruns=[wrun(1), wrun(2)])
ck("the replacing run finished: its checks are all there is, no pending entry",
   ci().state == "success" and not [c for c in asyncio.run(fg.read_pr(store, REPO, N))["ci_checks"] if c.get("waiting")])
reset(runs=partial_jobs, wruns=[wrun(1), wrun(2, status="queued")])
out = ci(required=["test (duckdb)"])
ck("required names: a required job that passed elsewhere does not hide the run still to come (T50 F4)",
   (out.state, out.failing) == ("pending", []), out)
out = ci(required=["test (duckdb)", "test (postgres)"])
ck("...a required name whose cancel was dropped is pending too", (out.state, out.failing) == ("pending", []), out)
reset(runs=partial_jobs, wruns=[wrun(1), wrun(2)])
ck("...and once the replacing run has finished, the required name that passed is success",
   ci(required=["test (duckdb)"]).state == "success")
reset(runs=partial_jobs + [real_run("lint", 14, DRAFT, conclusion="failure")], wruns=[wrun(1), wrun(2, status="queued")])
ck("...a required failure is still named while the run waits",
   (ci(required=["lint"]).state, ci(required=["lint"]).failing) == ("failure", ["lint"]))
reset(runs=partial_jobs, wruns=[wrun(1), wrun(2, event="workflow_dispatch", status="queued")])
out = ci()
ck("an unfinished run of another event is no replacement and no wait: the cancel still fails",
   (out.state, out.failing) == ("failure", ["test (postgres)"]), out)
reset(runs=partial_jobs, wruns=[wrun(1), wrun(2, workflow=410143077, status="queued")])
ck("...nor an unfinished run of another workflow", ci().failing == ["test (postgres)"])
reset(runs=partial_jobs + [real_run("lint", 14, DRAFT, conclusion="failure")], wruns=[wrun(1), wrun(2, status="queued")])
ck("a failure that was read still beats the wait", ci().failing == ["lint"])
reset(runs=partial_jobs, wruns=[wrun(2, status="queued"), wrun(1), wrun(3, workflow=7, status="queued")])
ck("two workflows: only the one that replaced a run is waited for",
   [w["name"] for w in fg._waiting_runs([wrun(2, status="queued"), wrun(1), wrun(3, workflow=7, status="queued")], HEAD)] == ["CI"])

preview_jobs = [real_run("test (duckdb)", 11, DRAFT), real_run("preview build", 31, DRAFT, conclusion="cancelled")]
preview_jobs[0]["html_url"] = f"https://github.com/{REPO}/actions/runs/1/job/11"
preview_jobs[1]["html_url"] = f"https://github.com/{REPO}/actions/runs/7/job/31"
preview_runs = [wrun(1), wrun(7, workflow=999, name="preview"), wrun(8, workflow=999, name="preview", status="queued")]
reset(runs=preview_jobs, wruns=preview_runs)
out = ci(required=["test (duckdb)"])
ck("required names: a rerun of an unrelated workflow still to start does not hold the gate",
   (out.state, out.failing) == ("success", []), out)
ck("...and read_pr still lists it as waiting, with the jobs its runs held",
   {"name": "preview", "state": "pending", "waiting": True, "jobs": ["preview build"]}
   in asyncio.run(fg.read_pr(store, REPO, N))["ci_checks"])
ck("...with no required names it still holds the gate", ci().state == "pending")
ck("...a required job of that workflow holds it", ci(required=["test (duckdb)", "preview build"]).state == "pending")
ck("...so does the workflow's own name when it is required", ci(required=["test (duckdb)", "preview"]).state == "pending")
ck("...and its cancel is dropped, not failing", ci(required=["preview build"]).failing == [])
reset(runs=preview_jobs, wruns=[wrun(1), wrun(7, workflow=999, name="preview"), wrun(8, workflow=999, name="preview")])
ck("...once it finished, the gate passes either way", ci(required=["test (duckdb)"]).state == "success")

print("== _replaced_runs ==")
ck("no runs, None, junk: nothing replaced",
   fg._replaced_runs([], HEAD) == set() and fg._replaced_runs(None, HEAD) == set()
   and fg._replaced_runs([None, "x", {}, {"id": "no", "workflow_id": 1}, {"id": 5}], HEAD) == set())
ck("two runs of one workflow: the lower one is replaced", fg._replaced_runs([wrun(1), wrun(2)], HEAD) == {1})
ck("three runs: only the newest is left", fg._replaced_runs([wrun(3), wrun(1), wrun(2)], HEAD) == {1, 2})
ck("each workflow keeps its own newest",
   fg._replaced_runs([wrun(1), wrun(4), wrun(2, workflow=7), wrun(3, workflow=7)], HEAD) == {1, 2})
ck("string ids are read as numbers", fg._replaced_runs([wrun("1"), wrun("2")], HEAD) == {1})
ck("a run of another commit is ignored", fg._replaced_runs([wrun(1), wrun(2, head="x")], HEAD) == set())
ck("a run with no head_sha counts for the commit asked about",
   fg._replaced_runs([wrun(1), {**wrun(2), "head_sha": None}], HEAD) == {1})
ck("a run of another event or branch is its own group",
   fg._replaced_runs([wrun(1), wrun(2, event="workflow_dispatch"), wrun(3, branch="x")], HEAD) == set())
ck("_waiting_runs: a lone unfinished run is nothing to wait for", fg._waiting_runs([wrun(1, status="queued")], HEAD) == [])
ck("_waiting_runs: none, junk, a finished newest run", fg._waiting_runs(None, HEAD) == []
   and fg._waiting_runs([None, {"id": 1}], HEAD) == [] and fg._waiting_runs([wrun(1), wrun(2)], HEAD) == [])
ck("_waiting_runs: names the jobs each workflow's runs held",
   fg._waiting_runs([wrun(1), wrun(2, status="queued")], HEAD, [
       {"name": "a", "html_url": "x/actions/runs/1/job/1"}, {"name": "b", "html_url": "x/actions/runs/9/job/2"},
       {"name": "a", "html_url": "x/actions/runs/2/job/3"}, {"name": "", "html_url": "x/actions/runs/1/job/4"}])
   == [{"name": "CI", "jobs": ["a"]}])
ck("_waiting_runs: a run with no name is named for what it is",
   [w["name"] for w in fg._waiting_runs([wrun(1), {**wrun(2, status="queued"), "name": None}], HEAD)] == ["workflow run"])
ck("_run_id reads the run, not the job",
   fg._run_id({"html_url": "https://github.com/a/b/actions/runs/44240624428/job/134587351900"}) == 44240624428
   and fg._run_id({"html_url": None}) is None and fg._run_id({}) is None and fg._run_id(None) is None
   and fg._run_id({"html_url": "https://github.com/a/b/pull/1"}) is None)

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

print("== a read that gave only part of the checks ==")
for label, kw in (("check runs answered 502", dict(runs_code=502)),
                  ("check runs answered 403", dict(runs_code=403)),
                  ("check runs raised", dict(runs_code="raise")),
                  ("statuses answered 502", dict(st_code=502)),
                  ("statuses raised", dict(st_code="raise"))):
    reset(statuses=[status("outside/ci", "success")] if "runs" in label else [],
          runs=[] if "runs" in label else [run("test")], **kw)
    out = ci()
    ck(f"{label}: green on the other side is pending, never success", (out.state, out.failing) == ("pending", []), out)
    got = asyncio.run(fg.read_pr(store, REPO, N))
    ck(f"{label}: read_pr marks the read partial", got["ci_partial"] is True, got.get("ci_partial"))
reset(runs_code=502, workflows=(404, {}))
ck("no checks and a failed read: pending, not none", asyncio.run(GithubCI(store).state(pr(), [])).state == "pending")
ck("no checks and a failed read: workflows not asked", not any("/contents/" in c for c in calls), calls)
reset(st_code=403, runs_code="raise", workflows=(404, {}))
ck("no checks and both reads failed: pending", asyncio.run(GithubCI(store).state(pr(), [])).state == "pending")
reset(runs=[run("test", conclusion="failure")], st_code=502)
out = ci()
ck("a failure that was read is still a failure when the other read failed",
   (out.state, out.failing) == ("failure", ["test"]), out)
reset(runs=[run("test")], st_code=502)
ck("required names do not make a failed read count as green", ci(["test"]).state == "pending")
reset(runs=[run("test")], st_code=502, workflows=(404, {}))
ck("no workflow lookup when the read was partial and a check came back", ci().state == "pending")
reset(runs=[run("test")], statuses=[status("s", "success")])
ck("complete reads are not partial", asyncio.run(fg.read_pr(store, REPO, N))["ci_partial"] is False)
reset(runs=[run("test")], runs_total=250)
out = ci()
ck("GitHub holds more check runs than were sent: pending", out.state == "pending", out)
reset(runs=[run("test")], statuses=[status("s", "success")], st_total=101)
ck("GitHub holds more statuses than were sent: pending", ci().state == "pending")
reset(runs=[run("test", conclusion="failure")], runs_total=250)
ck("more than were sent, and one failing among them: still failure", ci().state == "failure")
reset(runs=[run("test")], runs_total=1, statuses=[status("s", "success")], st_total=1)
ck("a total that matches is complete", ci().state == "success")
reset(runs=[run("test")], runs_total="x")
ck("a total that is no number is ignored", ci().state == "success")
ck("_more_than_read", fg._more_than_read(3, [1, 2]) and not fg._more_than_read(2, [1, 2])
   and not fg._more_than_read(None, [1]) and not fg._more_than_read("", [1]))

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

def mixed(first, second):
    """Two credentials in name order (main, second): what each answers for the workflows folder."""
    def h(req: httpx.Request):
        tok = req.headers.get("authorization", "")
        if req.url.path.endswith("/contents/.github/workflows"):
            code, body = first if tok.endswith("tok-main") else second
            if code == "raise":
                raise httpx.ConnectError("down")
            return httpx.Response(code, json=body)
        return httpx.Response(404, json={})
    return httpx.MockTransport(h)


for label, first, second, want in (
        ("404 then 403: no answer, not none", (404, {}), (403, {}), None),
        ("404 then 502: no answer", (404, {}), (502, {}), None),
        ("404 then an exception: no answer", (404, {}), ("raise", {}), None),
        ("403 then 404: no answer", (403, {}), (404, {}), None),
        ("an empty folder then 403: no answer", (200, []), (403, {}), None),
        ("a path that is a file then 404: no answer", (200, {"type": "file"}), (404, {}), None),
        ("404 then 404: none", (404, {}), (404, {}), False),
        ("404 then an empty folder: none", (404, {}), (200, []), False),
        ("403 then files: found", (403, {}), (200, [{"type": "file", "name": "ci.yml"}]), True),
        ("404 then files: found", (404, {}), (200, [{"type": "file", "name": "ci.yml"}]), True)):
    fg._transport = mixed(first, second)
    fg._workflows.clear()
    got = asyncio.run(fg.has_workflows(store, REPO, HEAD))
    ck(label, got is want, got)
    if want is None:
        ck(label + ": and it is not remembered", (REPO, HEAD) not in fg._workflows)


def with_pr(first, second):
    """The PR served normally with no checks at all; the workflows folder answers as `mixed`."""
    inner = mixed(first, second)

    def h(req: httpx.Request):
        if req.url.path.endswith("/contents/.github/workflows"):
            return inner.handler(req)
        return handler(req)
    return httpx.MockTransport(h)


reset()
fg._transport = with_pr((404, {}), (403, {}))
ck("state: no checks, workflows 404 then 403: pending, not none",
   asyncio.run(GithubCI(store).state(pr(), [])).state == "pending")
reset()
fg._transport = with_pr((404, {}), (404, {}))
ck("state: no checks, workflows 404 from both: none",
   asyncio.run(GithubCI(store).state(pr(), [])).state == "none")
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
