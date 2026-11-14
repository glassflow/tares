"""test_schema_compat must fail, not pass, when it compared nothing (TR-1334).

Each case builds a scratch git repo with a copy of the check and a release tag, then runs the copy:
a control that passes, a baseline tag with no schema folder (ls-tree lists nothing), and a baseline
whose tree cannot be listed (ls-tree fails).
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env

HERE = Path(__file__).resolve().parent
SCHEMAS = "tares/crew/schemas"
fails = 0
SCRATCH = Path(tempfile.mkdtemp(prefix="schema-guards-env-"))
GITCFG = SCRATCH / "gitconfig"
GITCFG.write_text("", encoding="utf-8")


def git_env() -> dict:
    """clean_env() for a scratch repo: none of the caller's git settings (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE,
    the user's config: gpgsign, hooksPath), the pattern of test_crew_state.py."""
    env = clean_env(GIT_CONFIG_GLOBAL=str(GITCFG), GIT_CONFIG_NOSYSTEM="1")
    for k in [k for k in env if k.startswith("GIT_") and k not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")]:
        del env[k]
    return env


def check(label, cond, detail=""):
    global fails
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


def sh(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env())
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def make_repo(with_schemas_at_tag):
    repo = Path(tempfile.mkdtemp(prefix="schema-guards-"))
    sh(repo, "init", "-q")
    sh(repo, "config", "user.email", "t@example.com")
    sh(repo, "config", "user.name", "t")
    (repo / "tests").mkdir()
    shutil.copy(HERE / "test_schema_compat.py", repo / "tests" / "test_schema_compat.py")
    sdir = repo / SCHEMAS
    sdir.mkdir(parents=True)
    (sdir / "a.json").write_text('{"type": "object", "properties": {"x": {"type": "string"}}}\n', newline="\n")
    if with_schemas_at_tag:
        sh(repo, "add", "-A")
    else:
        sh(repo, "add", "tests")
    sh(repo, "commit", "-q", "-m", "baseline")
    sh(repo, "tag", "v1.0.0")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "later", "--allow-empty")
    return repo


def run(repo):
    env = git_env()
    env.pop("CI", None)
    r = subprocess.run([sys.executable, str(repo / "tests" / "test_schema_compat.py")], capture_output=True,
                       text=True, env=env, cwd=str(repo))
    return r.returncode, r.stdout + r.stderr


def tidy(repo):
    def force(fn, path, _):
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    shutil.rmtree(repo, onerror=force)


print("a baseline with its schema files")
repo = make_repo(True)
try:
    code, out = run(repo)
    check("control: the check passes", code == 0 and "1 schema files compared" in out, (code, out))
finally:
    tidy(repo)

print("a baseline tag with no schema folder")
repo = make_repo(False)
try:
    code, out = run(repo)
    check("ls-tree lists nothing: FAIL no schema files compared, exit 1",
          code == 1 and "FAIL no schema files compared" in out and "lists none" in out
          and "no breaking change" not in out, (code, out))
finally:
    tidy(repo)

print("a baseline with schema files whose folder moved in the working tree")
repo = make_repo(True)
try:
    shutil.move(str(repo / SCHEMAS), str(repo / "moved"))
    code, out = run(repo)
    check("the removal is printed and the message does not say the tag lists none",
          code == 1 and "a.json $: schema file removed" in out and "FAIL no schema files compared" in out
          and "lists none" not in out and "no breaking change" not in out, (code, out))
finally:
    tidy(repo)

print("a baseline whose tree cannot be listed")
repo = make_repo(True)
try:
    tree = sh(repo, "rev-parse", "v1.0.0^{tree}")
    obj = repo / ".git" / "objects" / tree[:2] / tree[2:]
    os.chmod(obj, stat.S_IWRITE)
    obj.unlink()
    code, out = run(repo)
    check("ls-tree fails: FAIL could not list the schema files, exit 1",
          code == 1 and "could not list the schema files" in out and "no breaking change" not in out, (code, out))
finally:
    tidy(repo)
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'FAILED' if fails else 'passed'}: {fails} failed")
sys.exit(1 if fails else 0)
