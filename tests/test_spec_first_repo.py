"""`tares factory spec` from an empty folder or a repo with no commit (tares/crew/person.py): the commands it
names, and making the repo when the person says yes at a terminal.

A fake `gh` on PATH stands in for GitHub: it records its arguments and adds a local bare repo as origin, so
the push is real and nothing leaves the machine. git runs for real with a fixed name and email.

Run: python tests/test_spec_first_repo.py
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tares.crew import person as p  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


TMP = tempfile.mkdtemp(prefix="tares-spec-first-repo-")
for k, v in {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
             "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
    os.environ[k] = v

BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
LOG = os.path.join(TMP, "gh.log")
BARE = os.path.join(TMP, "remote.git")
subprocess.run(["git", "init", "-q", "--bare", BARE], check=True)
# the fake gh: `gh repo create <name> --private --source . --remote origin --push` adds the bare repo as
# origin and pushes to it, as GitHub would after creating the repo
with open(os.path.join(BIN, "gh"), "w") as f:
    f.write(f"""#!/bin/sh
echo "$@" >> "{LOG}"
[ -n "$GH_FAIL" ] && {{ echo "GraphQL: Name already exists on this account" >&2; exit 1; }}
git remote add origin "{BARE}" && git push -q -u origin HEAD
""")
os.chmod(os.path.join(BIN, "gh"), os.stat(os.path.join(BIN, "gh")).st_mode | stat.S_IEXEC)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

print("== the name and the commands ==")
ck("a folder name becomes a GitHub repo name", p._repo_name("/x/AI events: 2026!") == "AI-events-2026")
ck("a name with nothing usable falls back", p._repo_name("/x/!!!") == "project")
empty = os.path.join(TMP, "ai_events_calendar")
os.makedirs(empty)
ck("an empty folder: init, a first commit, and gh makes the private repo and pushes",
   p.first_repo_commands(empty, False) == ["git init", 'git commit --allow-empty -m "Start"',
                                           "gh repo create ai_events_calendar --private --source . --remote origin --push"],
   str(p.first_repo_commands(empty, False)))
withorigin = os.path.join(TMP, "has-origin")
os.makedirs(withorigin)
git("init", "-q", cwd=withorigin)
git("remote", "add", "origin", BARE, cwd=withorigin)
ck("a repo that has an origin: a first commit, then a push to it",
   p.first_repo_commands(withorigin, True) == ['git commit --allow-empty -m "Start"', "git push -u origin HEAD"],
   str(p.first_repo_commands(withorigin, True)))

print("== asking ==")
ck("with no terminal to ask at, the answer is no", p._ask_yes("Make it?") is False)

print("== making the repo ==")
p.make_first_repo(empty, False)
ck("the folder is a repo with a commit", p.has_commit(empty))
ck("gh was asked for a private repo named after the folder, from here, pushed",
   open(LOG).read().strip() == "repo create ai_events_calendar --private --source . --remote origin --push", open(LOG).read())
ck("and the commit reached the remote", git("rev-parse", "HEAD", cwd=empty).stdout == git("rev-parse", "HEAD", cwd=BARE).stdout
   or git("log", "-1", "--format=%s", cwd=BARE).stdout.strip() == "Start")

bare2 = os.path.join(TMP, "remote2.git")
subprocess.run(["git", "init", "-q", "--bare", bare2], check=True)
pushed = os.path.join(TMP, "pushed")
os.makedirs(pushed)
git("init", "-q", cwd=pushed)
git("remote", "add", "origin", bare2, cwd=pushed)
open(LOG, "w").close()
p.make_first_repo(pushed, True)
ck("a repo with an origin is pushed there, and gh is not asked", p.has_commit(pushed) and open(LOG).read() == ""
   and git("log", "-1", "--format=%s", cwd=bare2).stdout.strip() == "Start")

failing = os.path.join(TMP, "failing")
os.makedirs(failing)
os.environ["GH_FAIL"] = "1"
try:
    p.make_first_repo(failing, False)
    msg = ""
except CrewError as e:
    msg = str(e)
os.environ.pop("GH_FAIL")
ck("a gh that fails stops with its own last line", msg == "gh repo create failed: GraphQL: Name already exists on this account", msg)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
