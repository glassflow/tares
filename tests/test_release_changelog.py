"""release.sh collects changelog.d/ into CHANGELOG.md (TR-1249): run for real in a temp git repo, and the shape of the workflow that asks for a fragment."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env

ROOT = Path(__file__).resolve().parent.parent

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


rel = (ROOT / "scripts" / "release.sh").read_text()
collect_at, commit_at = rel.find("changelog_collect.py"), rel.find("git -C \"$ROOT\" commit")
ck("release.sh calls changelog_collect.py before it commits", 0 <= collect_at < commit_at)
ck("release.sh stages CHANGELOG.md and changelog.d with pyproject.toml", "changelog.d" in rel[collect_at:commit_at] and "CHANGELOG.md" in rel[collect_at:commit_at])
ck("release.sh takes the summary from CHANGELOG_SUMMARY", "CHANGELOG_SUMMARY" in rel)
ck("a bad fragment stops the release before the version bump", collect_at < rel.find("sed -i"))
ck("the usage line is kept", "usage: scripts/release.sh <version>" in rel and "set -euo pipefail" in rel)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, env=clean_env())


def repo():
    tmp = Path(tempfile.mkdtemp(prefix="tares-release-changelog-"))
    (tmp / "scripts").mkdir()
    for f in ("release.sh", "check_release_block.sh", "changelog_collect.py"):
        shutil.copy(ROOT / "scripts" / f, tmp / "scripts" / f)
    (tmp / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n')
    (tmp / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Old.\n")
    (tmp / "changelog.d").mkdir()
    (tmp / "changelog.d" / "README.md").write_text("About.\n")
    (tmp / "changelog.d" / "TR-1.md").write_text("Added\nA new thing.\n")
    (tmp / "changelog.d" / "TR-2.md").write_text("Fixed\nA fixed thing.\n")
    git(tmp, "init", "-q")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-qm", "start")
    return tmp


def release(tmp, version, **env):
    return subprocess.run(["bash", str(tmp / "scripts" / "release.sh"), version], cwd=tmp, capture_output=True, text=True,
                          env=clean_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",   # a CI runner has no git identity
                                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", **env))


tmp = repo()
try:
    r = release(tmp, "1.1.0-rc.1", CHANGELOG_SUMMARY="Two things.")
    ck("release.sh runs in a temp repo", r.returncode == 0, r.stdout + r.stderr)
    files = set(git(tmp, "show", "--name-status", "--format=", "HEAD").stdout.split("\n"))
    names = {ln.split("\t")[-1]: ln.split("\t")[0] for ln in files if ln}
    ck("one commit holds pyproject.toml, CHANGELOG.md and the deleted fragments",
       names == {"pyproject.toml": "M", "CHANGELOG.md": "M", "changelog.d/TR-1.md": "D", "changelog.d/TR-2.md": "D"}, str(names))
    ck("the commit is the release and the tag points at it", git(tmp, "rev-list", "--count", "HEAD").stdout.strip() == "2"
       and git(tmp, "rev-parse", "v1.1.0-rc.1").stdout == git(tmp, "rev-parse", "HEAD").stdout)
    text = (tmp / "CHANGELOG.md").read_text()
    ck("CHANGELOG.md has the section with the summary and both entries, Unreleased empty",
       re.search(r"## \[Unreleased\]\n\n## \[1\.1\.0-rc\.1\] - \d{4}-\d\d-\d\d\n\nTwo things\.\n\n### Added\n- A new thing\.\n\n### Fixed\n- A fixed thing\.\n\n## \[1\.0\.0\]", text), text)
    ck("README.md stays and the tree is clean", (tmp / "changelog.d" / "README.md").exists() and git(tmp, "status", "--short").stdout == "", git(tmp, "status", "--short").stdout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

tmp = repo()
try:
    (tmp / "changelog.d" / "TR-3.md").write_text("Removed\nNot a category.\n")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-qm", "bad")
    before = git(tmp, "rev-parse", "HEAD").stdout
    r = release(tmp, "1.1.0-rc.1")
    ck("a bad fragment aborts the release naming the file", r.returncode != 0 and "TR-3.md" in r.stderr, r.stdout + r.stderr)
    ck("a bad fragment: no commit, no tag, nothing changed",
       git(tmp, "rev-parse", "HEAD").stdout == before and git(tmp, "tag").stdout == "" and git(tmp, "status", "--short").stdout == ""
       and 'version = "1.0.0"' in (tmp / "pyproject.toml").read_text(), git(tmp, "status", "--short").stdout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

tmp = repo()
try:
    shutil.rmtree(tmp / "changelog.d")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-qm", "no dir")
    r = release(tmp, "1.1.0-rc.1")
    ck("a repo with no changelog.d still releases", r.returncode == 0 and git(tmp, "tag").stdout.strip() == "v1.1.0-rc.1", r.stdout + r.stderr)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---- the workflow that asks for a fragment
wf = (ROOT / ".github" / "workflows" / "changelog.yml").read_text()
d = yaml.safe_load(wf)
on = d.get("on", d.get(True))
types = on["pull_request"]["types"]
ck("changelog.yml runs on a pull request, on labeled and unlabeled too, and on a draft",
   {"opened", "synchronize", "reopened", "labeled", "unlabeled"} <= set(types) and "if" not in d["jobs"]["changelog"], str(types))
ck("changelog.yml has the job `changelog`, alone, needing no other job", list(d["jobs"]) == ["changelog"] and "needs" not in d["jobs"]["changelog"])
steps = d["jobs"]["changelog"]["steps"]
ck("changelog.yml checks out the full history", any(s.get("with", {}).get("fetch-depth") == 0 for s in steps))
run = next(s for s in steps if "run" in s)
ck("changelog.yml passes the base branch and labels by env, never inside the script text",
   "${{" not in run["run"] and {"BASE_REF", "LABELS"} <= set(run["env"]) and "check_changelog.py" in run["run"] and "--name-status" in run["run"], str(run))
ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
ck("ci.yml does not run on a label (that would start the heavy jobs): `labeled` is only in changelog.yml", "labeled" not in ci)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
