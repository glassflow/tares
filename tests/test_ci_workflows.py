"""The heavy CI checks run once per PR, not on every push (TR-1233): the shape of ci.yml, parity-macos.yml and
windows-crew.yml, and the two shell scripts in windows-crew.yml run for real (the diff that decides whether a PR needs
the Windows shards, and the aggregate check that must stay green when they are skipped on purpose)."""
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

# ---- ci.yml: the light set for a draft
ck("ci.yml: marking a PR ready runs the workflow (ready_for_review)", "ready_for_review" in ci["on"]["pull_request"].get("types", []))
ck("ci.yml: the default PR events are kept", {"opened", "synchronize", "reopened"} <= set(ci["on"]["pull_request"].get("types", [])))
ck("ci.yml: a label starts nothing (no `labeled` type)", "labeled" not in ci["on"]["pull_request"].get("types", []))
jobs = ci["jobs"]
ck("ci.yml: no macOS job (parity-macos.yml has them)",
   not any("macos" in str(j).lower() for j in jobs.values()), ", ".join(n for n, j in jobs.items() if "macos" in str(j).lower()))
suite = jobs["test"]["strategy"]["matrix"]["suite"]
ck("ci.yml: the test matrix is duckdb alone on a draft, all three suites otherwise",
   isinstance(suite, str) and DRAFT in suite and "fromJSON" in suite and '["duckdb"]' in suite and '"postgres"' in suite and '"mit"' in suite, str(suite))
for name in ("test-ee", "wheel-clean", "e2e"):
    ck(f"ci.yml: {name} is skipped on a draft", DRAFT in str(jobs[name].get("if", "")) and "!" in str(jobs[name].get("if", "")), str(jobs[name].get("if")))
for name in ("console", "test", "parity", "wheel", "secrets"):
    ck(f"ci.yml: {name} runs on a draft (the light set)", DRAFT not in str(jobs[name].get("if", "")), str(jobs[name].get("if")))

# ---- ci.yml: a push to main whose tree already passed on its PR (TR-1234)
cov = jobs["covered"]
ck("ci.yml: `covered` runs on a push to main only", "push" in cov["if"] and "refs/heads/main" in cov["if"], str(cov.get("if")))
ck("ci.yml: `covered` can read runs and PRs, and nothing more", cov["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}, str(cov.get("permissions")))
ck("ci.yml: `covered` hands over the previous tip of main", any("github.event.before" in str(s.get("env", {})) for s in cov["steps"]))
ck("ci.yml: `covered` looks for the full set (the e2e job) of ci.yml", any("--workflow ci.yml" in s.get("run", "") and "--marker e2e" in s.get("run", "") for s in cov["steps"]))
for name in ("console", "test", "parity", "test-ee", "wheel", "wheel-clean", "e2e"):
    c = " ".join(str(jobs[name].get("if", "")).split())
    ck(f"ci.yml: {name} waits for `covered` and skips only when it said true (a skipped or failed `covered` runs it)",
       jobs[name].get("needs") == "covered" and "!cancelled()" in c and "needs.covered.outputs.covered != 'true'" in c, f"{jobs[name].get('needs')} {c}")
ck("ci.yml: `secrets` is never skipped for a covered tree", "needs" not in jobs["secrets"] and "if" not in jobs["secrets"])
ck("ci.yml: the Linux parity shards stay", jobs["parity"]["runs-on"] == "${{ matrix.os }}" and jobs["parity"]["strategy"]["matrix"]["os"] == ["ubuntu-latest"])

# ---- parity-macos.yml: after a merge, or when asked
ck("parity-macos.yml: runs on a push to main", "main" in mac["on"]["push"]["branches"])
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
ck("parity-macos.yml: a main push is never cancelled", "refs/heads/main" in str(mac["concurrency"]["cancel-in-progress"]))

# ---- windows-crew.yml
wj = win["jobs"]
ck("windows-crew.yml: marking a PR ready runs the workflow", "ready_for_review" in win["on"]["pull_request"].get("types", []))
ck("windows-crew.yml: jobs are changes, shard and windows-crew", list(wj) == ["changes", "shard", "windows-crew"], str(list(wj)))
ck("windows-crew.yml: the shards wait for `changes` and run only when it says so",
   wj["shard"]["needs"] == "changes" and wj["shard"]["if"] == "needs.changes.outputs.run == 'true'", str(wj["shard"].get("if")))
ck("windows-crew.yml: `changes` asks ci_covered.py about the windows-crew run, on a push only",
   any(s.get("id") == "covered" and "--workflow windows-crew.yml" in s["run"] and "push" in s["if"] for s in wj["changes"]["steps"]))
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

r = sh(agg, {"CHANGES": "success", "RUN": "false", "SHARDS": "skipped", "NOTE": "same tree as PR 7 head abcd1234, whose windows-crew.yml run passed: https://x/runs/9"}, ROOT)
ck("windows-crew aggregate: skipped because the PR run covered it -> green, and it names that run",
   r.returncode == 0 and "https://x/runs/9" in r.stdout, f"exit {r.returncode}: {r.stdout}{r.stderr}")

# the diff: a throwaway repo with a base commit, then a PR commit that touches one path
diff = step_script("changes", step_id="diff")


def decide(event, draft, path, covered=""):
    with tempfile.TemporaryDirectory() as d:
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, check=True, capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        Path(d, "README.md").write_text("x\n")
        g("add", "-A"); g("commit", "-q", "-m", "base")
        base = g("rev-parse", "HEAD").stdout.strip()
        p = Path(d, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("y\n")
        g("add", "-A"); g("commit", "-q", "-m", "pr")
        out = Path(d, "..", "out-%d" % os.getpid()).resolve()
        out.write_text("")
        r = sh(diff, {"EVENT": event, "DRAFT": draft, "BASE": base, "COVERED": covered, "GITHUB_OUTPUT": str(out)}, d)
        got = out.read_text().strip()
        out.unlink()
        return got, r


for event, draft, path, want, *cov_ in [
    ("push", "", "ui/src/x.ts", "run=true"),                      # a push to main always runs
    ("workflow_dispatch", "", "ui/src/x.ts", "run=true"),
    ("push", "", "ui/src/x.ts", "run=true", "false"),             # asked, not covered: runs
    ("push", "", "tares/crew/state.py", "run=false", "true"),     # covered by the PR run: skipped
    ("workflow_dispatch", "", "ui/src/x.ts", "run=true", "true"), # a manual run is never covered
    ("pull_request", "false", "tares/crew/state.py", "run=true", "true"),  # nor is a PR event
    ("pull_request", "false", "tares/crew/state.py", "run=true"),
    ("pull_request", "false", "claude-plugin/skills/x.md", "run=true"),
    ("pull_request", "false", "tests/factory/parity.py", "run=true"),
    ("pull_request", "false", "tests/test_crew_state.py", "run=true"),
    ("pull_request", "false", "tests/ci_windows.py", "run=true"),
    ("pull_request", "false", "tests/test_ci_windows.py", "run=true"),
    ("pull_request", "false", "tares/cli.py", "run=true"),
    ("pull_request", "false", "tests/_crew_fake.py", "run=true"),    # helpers the crew tests import
    ("pull_request", "false", "tests/_crew_guard.py", "run=true"),
    ("pull_request", "false", "tests/_env.py", "run=true"),
    ("pull_request", "false", "tares/login.py", "run=true"),
    ("pull_request", "false", "tares/__init__.py", "run=true"),
    ("pull_request", "false", "pyproject.toml", "run=true"),
    ("pull_request", "false", ".github/workflows/windows-crew.yml", "run=true"),
    ("pull_request", "false", "ui/src/x.ts", "run=false"),         # nothing the shards test
    ("pull_request", "false", "tares/store.py", "run=false"),
    ("pull_request", "false", ".github/workflows/ci.yml", "run=false"),
    ("pull_request", "true", "tares/crew/state.py", "run=false"),  # a draft waits for ready_for_review
]:
    got, r = decide(event, draft, path, *cov_)
    ck(f"windows-crew changes: {event} draft={draft or '-'} touching {path}{' covered=' + cov_[0] if cov_ else ''} -> {want}", got == want and r.returncode == 0, f"{got!r} {r.stderr}")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
