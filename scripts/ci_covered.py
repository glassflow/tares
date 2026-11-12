#!/usr/bin/env python3
"""Did this push to main already pass CI as a pull request? (TR-1234)

A squash merge of a PR that was up to date with main makes a commit whose tree is the PR head's tree, and
that tree already ran the whole CI on the PR. Running it again on main is a duplicate. This script says
whether a push to main is covered by a PR run of one workflow, and writes two lines to $GITHUB_OUTPUT:

    covered=true|false
    note=<one line: which PR run covered it, or why nothing did>

It answers "covered" only when every one of these holds, and "not covered" (so the caller runs everything)
on anything else, an API error included:

  1. the event is a push to main (a manual run and a PR event are never covered);
  2. the pushed commit belongs to exactly one merged PR whose base is main;
  3. the tree of the pushed commit equals the tree of that PR's head;
  4. the PR head already had every commit main had before the push (compare behind_by is 0), so the run on the
     PR, which GitHub makes on the head merged with main, tested exactly the head's tree;
  5. the newest pull_request run of the workflow on that head finished with success;
  6. that run had the full set: a job whose name starts with --marker succeeded (a draft PR runs a light set
     without it).

Usage: ci_covered.py --workflow ci.yml --marker e2e
Environment: GITHUB_REPOSITORY, GITHUB_SHA, GITHUB_EVENT_NAME, GITHUB_REF, BEFORE (the main tip before the
push), GH_TOKEN (read by `gh api`), GITHUB_OUTPUT (optional).
"""
import argparse
import json
import os
import subprocess
import sys


def gh_api(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def decide(api, repo, sha, before, workflow, marker, event="push", ref="refs/heads/main"):
    """(covered, note). api(path) returns the parsed JSON of a GitHub API path under /."""
    if event != "push" or ref != "refs/heads/main":
        return False, f"not a push to main ({event} on {ref}): everything runs"
    if not before or set(before) <= {"0"}:
        return False, "no previous tip of main: everything runs"
    prs = [p for p in api(f"repos/{repo}/commits/{sha}/pulls") if p.get("merged_at") and (p.get("base") or {}).get("ref") == "main"]
    if len(prs) != 1:
        return False, f"{len(prs)} merged PRs for this commit, expected 1: everything runs"
    pr = prs[0]
    head = pr["head"]["sha"]
    t_main = api(f"repos/{repo}/git/commits/{sha}")["tree"]["sha"]
    t_head = api(f"repos/{repo}/git/commits/{head}")["tree"]["sha"]
    if t_main != t_head:
        return False, f"the merge changed the tree PR {pr['number']} tested (main moved after its last run): everything runs"
    behind = api(f"repos/{repo}/compare/{before}...{head}").get("behind_by")
    if behind != 0:
        return False, f"PR {pr['number']} head was {behind} commits behind main: everything runs"
    runs = api(f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={head}&event=pull_request&per_page=50").get("workflow_runs", [])
    if not runs:
        return False, f"no {workflow} run on PR {pr['number']} head {head[:8]}: everything runs"
    run = max(runs, key=lambda r: (r.get("created_at") or "", r.get("run_number") or 0))
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return False, f"the newest {workflow} run on PR {pr['number']} is {run.get('status')}/{run.get('conclusion')}: everything runs"
    jobs = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
    if not any(j.get("name", "").startswith(marker) and j.get("conclusion") == "success" for j in jobs):
        return False, f"that run of {workflow} did not run the full set (no passed job named '{marker}...'): everything runs"
    return True, f"same tree as PR {pr['number']} head {head[:8]}, whose {workflow} run passed: {run.get('html_url')}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--marker", required=True)
    a = ap.parse_args()
    e = os.environ
    try:
        covered, note = decide(gh_api, e["GITHUB_REPOSITORY"], e["GITHUB_SHA"], e.get("BEFORE", ""), a.workflow, a.marker,
                               e.get("GITHUB_EVENT_NAME", ""), e.get("GITHUB_REF", ""))
    except Exception as ex:  # an API fault must never skip a run
        covered, note = False, f"could not tell ({type(ex).__name__}: {ex}): everything runs"
    print(("covered: " if covered else "not covered: ") + note)
    out = e.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"covered={'true' if covered else 'false'}\nnote={note}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
