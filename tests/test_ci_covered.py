"""scripts/ci_covered.py decides whether a push to main was already tested as a pull request (TR-1234).
It runs here against a fake GitHub API: the merge that should skip the run, and every way it must not."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci_covered  # noqa: E402

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


REPO, SHA, HEAD, BEFORE = "o/r", "m" * 40, "h" * 40, "b" * 40


def world(**kw):
    """The API answers for a clean squash merge of PR 7; keyword arguments change one fact."""
    w = {
        "prs": [{"number": 7, "merged_at": "2026-11-12T10:00:00Z", "base": {"ref": "main"}, "head": {"sha": HEAD}}],
        "tree_main": "T1", "tree_head": "T1", "behind_by": 0,
        "runs": [{"id": 11, "run_number": 5, "created_at": "2026-11-12T09:00:00Z", "status": "completed", "conclusion": "success", "html_url": "https://x/runs/11", "pull_requests": [{"number": 7}]}],
        "jobs": [{"name": "test (duckdb 1/5)", "conclusion": "success"}, {"name": "e2e (1/2)", "conclusion": "success"}],
        "boom": None,
    }
    w.update(kw)
    return w


def api_for(w):
    def api(path):
        if w["boom"] and w["boom"] in path:
            raise RuntimeError("HTTP 502")
        if path == f"repos/{REPO}/commits/{SHA}/pulls":
            return w["prs"]
        if path == f"repos/{REPO}/git/commits/{SHA}":
            return {"tree": {"sha": w["tree_main"]}}
        if path == f"repos/{REPO}/git/commits/{HEAD}":
            return {"tree": {"sha": w["tree_head"]}}
        if path == f"repos/{REPO}/compare/{BEFORE}...{HEAD}":
            return {"behind_by": w["behind_by"]}
        if path.startswith(f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={HEAD}&event=pull_request"):
            return {"workflow_runs": w["runs"]}
        if path.startswith(f"repos/{REPO}/actions/runs/") and "/jobs" in path:
            run_id = int(path.split("/")[5])
            return {"jobs": w["jobs"] if run_id == 11 else w.get("jobs_other", [])}
        raise AssertionError("unexpected API path " + path)
    return api


def ask(w, before=BEFORE, **kw):
    return ci_covered.decide(api_for(w), REPO, SHA, before, "ci.yml", "e2e", **kw)


c, note = ask(world())
ck("same tree, head up to date with main, PR run passed with the full set -> covered", c, note)
ck("the note names the PR, the head and the run", "PR 7" in note and "hhhhhhhh" in note and "https://x/runs/11" in note, note)

for label, w, kw in [
    ("a different tree (main moved after the last run, or the merge was not a squash of the head)", world(tree_main="T2"), {}),
    ("the PR head was behind main (the PR run tested another tree)", world(behind_by=2), {}),
    ("the PR run failed", world(runs=[dict(world()["runs"][0], conclusion="failure")]), {}),
    ("the PR run was cancelled", world(runs=[dict(world()["runs"][0], conclusion="cancelled")]), {}),
    ("the PR run is still going", world(runs=[dict(world()["runs"][0], status="in_progress", conclusion=None)]), {}),
    ("there was no PR run", world(runs=[]), {}),
    ("the commit belongs to no PR (a direct push)", world(prs=[]), {}),
    ("the PR was not merged", world(prs=[dict(world()["prs"][0], merged_at=None)]), {}),
    ("the PR went to another branch", world(prs=[dict(world()["prs"][0], base={"ref": "factory/X"})]), {}),
    ("two PRs claim the commit", world(prs=world()["prs"] * 2), {}),
    ("the run was the draft's light set (no e2e job)", world(jobs=[{"name": "test (duckdb 1/5)", "conclusion": "success"}]), {}),
    ("the e2e job was skipped", world(jobs=[{"name": "e2e (1/2)", "conclusion": "skipped"}]), {}),
    ("a run of the workflow is the newest one and it failed (an older green run does not count)",
     world(runs=[dict(world()["runs"][0]), {"id": 12, "run_number": 6, "created_at": "2026-11-12T09:30:00Z", "status": "completed", "conclusion": "failure", "html_url": "u", "pull_requests": [{"number": 7}]}]), {}),
    ("the only run belongs to another PR that shares the head sha", world(runs=[dict(world()["runs"][0], pull_requests=[{"number": 9}])]), {}),
    ("the only run lists no PR (a fork run)", world(runs=[dict(world()["runs"][0], pull_requests=[])]), {}),
    ("the event is a manual run", world(), {"event": "workflow_dispatch", "ref": "refs/heads/main"}),
    ("the push is to another branch", world(), {"ref": "refs/heads/factory-modular/x"}),
    ("the event is a pull request", world(), {"event": "pull_request"}),
]:
    c, note = ask(w, **kw)
    ck(f"not covered: {label}", not c and "everything runs" in note, note)

c, note = ask(world(), before="")
ck("not covered: no previous tip of main", not c, note)
c, note = ask(world(), before="0" * 40)
ck("not covered: the previous tip is the null sha (a new branch)", not c, note)

newest = world(runs=[dict(world()["runs"][0], conclusion="failure"), {"id": 12, "run_number": 6, "created_at": "2026-11-12T09:30:00Z", "status": "completed", "conclusion": "success", "html_url": "https://x/runs/12", "pull_requests": [{"number": 7}]}],
               jobs_other=[{"name": "e2e (2/2)", "conclusion": "success"}])
c, note = ask(newest)
ck("covered when the newest run passed, although an older one failed (a fix was pushed)", c and "https://x/runs/12" in note, note)

# main() never lets an API fault skip a run: any error is "not covered"
fake = Path(tempfile.mkdtemp()) / "bin"
fake.mkdir()
(fake / "gh").write_text("#!/bin/sh\necho 'HTTP 502' >&2\necho 'try again later' >&2\nexit 1\n")
(fake / "gh").chmod(0o755)
out = Path(tempfile.mkdtemp()) / "out"
env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA, "BEFORE": BEFORE,
       "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(out)}
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci_covered.py"), "--workflow", "ci.yml", "--marker", "e2e"], env=env, capture_output=True, text=True)
lines = out.read_text().splitlines()
ck("an API error exits 0 and writes covered=false with the reason", r.returncode == 0 and lines[0] == "covered=false" and lines[1].startswith("note=could not tell"), f"{r.returncode} {lines} {r.stderr}")
ck("the output file holds exactly the two lines the workflows read, although gh said its error on two lines", len(lines) == 2 and "try again later" in lines[1], str(lines))

# a gh that answers with the fake data end to end
w = world()
answers = {
    f"repos/{REPO}/commits/{SHA}/pulls": w["prs"],
    f"repos/{REPO}/git/commits/{SHA}": {"tree": {"sha": "T1"}},
    f"repos/{REPO}/git/commits/{HEAD}": {"tree": {"sha": "T1"}},
    f"repos/{REPO}/compare/{BEFORE}...{HEAD}": {"behind_by": 0},
    f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={HEAD}&event=pull_request&per_page=50": {"workflow_runs": w["runs"]},
    f"repos/{REPO}/actions/runs/11/jobs?per_page=100": {"jobs": w["jobs"]},
}
(fake / "answers.json").write_text(json.dumps(answers))
(fake / "gh").write_text(f"#!{sys.executable}\nimport json, sys\na = json.load(open({str(fake / 'answers.json')!r}))\nprint(json.dumps(a[sys.argv[2]]))\n")
out.write_text("")
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci_covered.py"), "--workflow", "ci.yml", "--marker", "e2e"], env=env, capture_output=True, text=True)
lines = out.read_text().splitlines()
ck("through `gh api` end to end -> covered=true and a note naming the run", r.returncode == 0 and lines[0] == "covered=true" and "https://x/runs/11" in lines[1], f"{r.stdout} {r.stderr} {lines}")

# break test: without the newline cleaning a two-line gh error would put a third line in the output file
src = (ROOT / "scripts" / "ci_covered.py").read_text()
bare = src.replace('note = " ".join(note.split())', 'pass')
assert bare != src
(fake / "ci_covered_bare.py").write_text(bare)
(fake / "gh").write_text("#!/bin/sh\necho 'HTTP 502' >&2\necho 'try again later' >&2\nexit 1\n")
out.write_text("")
subprocess.run([sys.executable, str(fake / "ci_covered_bare.py"), "--workflow", "ci.yml", "--marker", "e2e"], env=env, capture_output=True, text=True)
ck("break test: without the cleaning the two-line gh error would break the output file", len(out.read_text().splitlines()) != 2)

# break test: a decide() that skips the tree comparison is caught by the "different tree" case
src = (ROOT / "scripts" / "ci_covered.py").read_text()
broken = src.replace("if t_main != t_head:", "if False:")
assert broken != src
ns = {"__name__": "broken"}
exec(compile(broken, "broken", "exec"), ns)
c, _ = ns["decide"](api_for(world(tree_main="T2")), REPO, SHA, BEFORE, "ci.yml", "e2e")
ck("break test: without the tree comparison the different-tree merge would be called covered", c)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
