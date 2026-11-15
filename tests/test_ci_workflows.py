"""The heavy CI checks run on a release tag, by hand, or on a PR that asks by label; a push to main runs a light Linux
set only (TR-1233, TR-1235): the shape of ci.yml, parity-macos.yml and windows-crew.yml, and the two shell scripts in
windows-crew.yml run for real (the label check that decides whether a PR gets the Windows shards, and the aggregate check
that must stay green when they are skipped on purpose)."""
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def load(name):
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.pop(True, d.get("on"))  # YAML 1.1 reads a bare `on:` as the boolean True
    return d


ci, mac, win = load("ci.yml"), load("parity-macos.yml"), load("windows-crew.yml")
DRAFT = "github.event.pull_request.draft"

# ---- ci.yml: the light set for a draft, the lighter one for a push to main
ON_MAIN = "github.ref == 'refs/heads/main'"
ck("ci.yml: a push to main and a release tag start it, no other branch (no factory-modular/*)",
   ci["on"]["push"]["branches"] == ["main"] and ci["on"]["push"]["tags"] == ["v*"], str(ci["on"]["push"]))
ck("ci.yml: marking a PR ready runs the workflow (ready_for_review)", "ready_for_review" in ci["on"]["pull_request"].get("types", []))
ck("ci.yml: the default PR events are kept", {"opened", "synchronize", "reopened"} <= set(ci["on"]["pull_request"].get("types", [])))
ck("ci.yml: a label starts nothing (no `labeled` type)", "labeled" not in ci["on"]["pull_request"].get("types", []))
jobs = ci["jobs"]
ck("ci.yml: no macOS job (parity-macos.yml has them)",
   not any("macos" in str(j).lower() for j in jobs.values()), ", ".join(n for n, j in jobs.items() if "macos" in str(j).lower()))
suite = jobs["test"]["strategy"]["matrix"]["suite"]
ck("ci.yml: the test matrix is duckdb alone on a draft and on main, all three suites otherwise",
   isinstance(suite, str) and DRAFT in suite and ON_MAIN in suite and "fromJSON" in suite and '["duckdb"]' in suite
   and '"postgres"' in suite and '"mit"' in suite, str(suite))
for name in ("test-ee", "wheel-clean", "e2e"):
    cond = str(jobs[name].get("if", ""))
    ck(f"ci.yml: {name} is skipped on a draft and on a push to main", DRAFT in cond and "!" in cond and "!= 'refs/heads/main'" in cond, cond)
ck("ci.yml: the Linux parity shards are skipped on a push to main, not on a draft",
   "!= 'refs/heads/main'" in str(jobs["parity"].get("if", "")) and DRAFT not in str(jobs["parity"].get("if", "")), str(jobs["parity"].get("if")))
for name in ("console", "test", "wheel", "secrets"):
    ck(f"ci.yml: {name} runs on a draft and on main (the light set)", "if" not in jobs[name], str(jobs[name].get("if")))
ck("ci.yml: the Linux parity shards stay", jobs["parity"]["runs-on"] == "${{ matrix.os }}" and jobs["parity"]["strategy"]["matrix"]["os"] == ["ubuntu-latest"])

# ---- parity-macos.yml: on a release tag, by hand, or when a PR asks
ck("parity-macos.yml: runs on a release tag, never on a branch push (main, factory-modular/*)",
   mac["on"]["push"] == {"tags": ["v*"]}, str(mac["on"]["push"]))
ck("parity-macos.yml: a PR event is only a label or a push to a PR (nothing else starts it)", mac["on"]["pull_request"]["types"] == ["labeled", "synchronize"])
ck("parity-macos.yml: can be run by hand", "workflow_dispatch" in mac["on"])
job = mac["jobs"]["parity"]
cond = " ".join(str(job["if"]).split())
ck("parity-macos.yml: job name keeps the check name parity (macos-latest N/12)", job["name"] == "parity (macos-latest ${{ matrix.shard }}/12)", job["name"])
ck("parity-macos.yml: on macOS, 12 shards", job["runs-on"] == "macos-latest" and job["strategy"]["matrix"]["shard"] == list(range(1, 13)))
ck("parity-macos.yml: a PR needs the macos-parity label (on labeling it, or on a push while it has it)",
   "github.event_name != 'pull_request'" in cond and cond.count("macos-parity") == 2
   and "github.event.action == 'labeled'" in cond and "github.event.action == 'synchronize'" in cond, cond)
grp = mac["concurrency"]["group"]
ck("parity-macos.yml: another label gets its own concurrency group, so it cannot cancel a run",
   "github.event.label.name != 'macos-parity'" in grp and "other-label" in grp, grp)
ck("parity-macos.yml: only a PR run is cancelled by a newer one (a tag or manual run always finishes)",
   "github.event_name == 'pull_request'" in str(mac["concurrency"]["cancel-in-progress"]))

# ---- windows-crew.yml
wj = win["jobs"]
ck("windows-crew.yml: runs on a release tag, never on a branch push",
   win["on"]["push"] == {"tags": ["v*"]}, str(win["on"]["push"]))
ck("windows-crew.yml: can be run by hand", "workflow_dispatch" in win["on"])
ck("windows-crew.yml: a PR event is an open, a push, a reopen or a label, and no paths filter hides the check from a docs PR",
   win["on"]["pull_request"] == {"types": ["opened", "synchronize", "reopened", "labeled"]}, str(win["on"]["pull_request"]))
ck("windows-crew.yml: only a PR run is cancelled by a newer one", "github.event_name == 'pull_request'" in str(win["concurrency"]["cancel-in-progress"]))
ck("windows-crew.yml: jobs are changes, shard and windows-crew", list(wj) == ["changes", "shard", "windows-crew"], str(list(wj)))
ck("windows-crew.yml: the shards wait for `changes` and run only when it says so",
   wj["shard"]["needs"] == "changes" and wj["shard"]["if"] == "needs.changes.outputs.run == 'true'", str(wj["shard"].get("if")))
ck("windows-crew.yml: the aggregate keeps its name, always runs and waits for both", wj["windows-crew"]["name"] == "windows-crew"
   and wj["windows-crew"]["needs"] == ["changes", "shard"] and "always()" in wj["windows-crew"]["if"])


def step_script(job, step_id=None, name=None):
    for s in wj[job]["steps"]:
        if (step_id and s.get("id") == step_id) or (name and s.get("name") == name):
            return s["run"]
    raise KeyError(step_id or name)


def sh(script, env, cwd):
    e = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", ""), **env}
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=e, capture_output=True, text=True)


# the aggregate: every combination of what `changes` and the shards can report
agg = step_script("windows-crew", name="Every shard passed, or none was needed")
for changes, run, shards, want in [
    ("success", "true", "success", True),     # a crew PR, every shard green
    ("success", "true", "failure", False),    # a shard failed
    ("success", "true", "cancelled", False),  # a shard was cancelled
    ("success", "true", "skipped", False),    # shards were due and did not run
    ("success", "false", "skipped", True),    # a draft or a non-crew PR: skipped on purpose
    ("success", "false", "success", False),   # not due but ran: something is off
    ("success", "false", "failure", False),
    ("failure", "", "skipped", False),        # `changes` broke: the shards are skipped by accident
    ("cancelled", "", "skipped", False),
]:
    r = sh(agg, {"CHANGES": changes, "RUN": run, "SHARDS": shards}, ROOT)
    ck(f"windows-crew aggregate: changes {changes}, run {run!r}, shards {shards} -> {'green' if want else 'red'}",
       (r.returncode == 0) == want, f"exit {r.returncode}: {r.stdout}{r.stderr}")

# the label check: which events and labels start the shards
decide_script = step_script("changes", step_id="decide")


def decide(event, labels):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "out")
        out.write_text("")
        r = sh(decide_script, {"EVENT": event, "LABELS": labels, "GITHUB_OUTPUT": str(out)}, d)
        return out.read_text().strip(), r


for event, labels, want in [
    ("push", "", "run=true"),                                  # a release tag
    ("workflow_dispatch", "", "run=true"),                     # by hand, on any ref
    ("pull_request", "windows-crew", "run=true"),
    ("pull_request", "macos-parity,windows-crew", "run=true"),
    ("pull_request", "windows-crew,bug", "run=true"),
    ("pull_request", "", "run=false"),                         # no label: the check is green and the shards skipped
    ("pull_request", "macos-parity", "run=false"),
    ("pull_request", "not-windows-crew", "run=false"),         # the whole name, not a part of one
    ("pull_request", "windows-crew-later", "run=false"),
]:
    got, r = decide(event, labels)
    ck(f"windows-crew changes: {event} labels={labels or '-'} -> {want}", got == want and r.returncode == 0, f"{got!r} {r.stderr}")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
