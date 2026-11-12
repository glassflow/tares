"""`tares factory spec` and `tares factory session` in Python (tares/crew/person.py, TR-966).

The commands' printed text is covered by the parity scenarios (spec_*, session_*). This checks what they
cannot see from outside: the small helpers (the numbered question, git, project matching, the two lists
Tares gives, the plugin clash text) and the usage errors, plus that both commands are routed to person.py.

Run: .venv/bin/python tests/test_crew_session.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "factory"))

os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

_crew_guard.scratch_home()   # the probes below inherit this process's HOME: not the real one (TR-1164)
from _crew_fake import fake_crew_env  # noqa: E402
from parity import FakeTares  # noqa: E402
from tares.crew import dispatch, person  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


T = Path(tempfile.mkdtemp(prefix="tares crew session "))
GIT = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def mkrepo(path, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=GIT)
    if commit:
        (path / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=GIT)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=GIT)


def asking(answers):
    """choose() with `answers` typed on stdin; (value, what it printed)."""
    real_in, real_err = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = io.StringIO(answers), io.StringIO()
    try:
        value = person.choose("Which?", 2, ["a|first", "b|second", "c|third"])
        return value, sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stderr = real_in, real_err


def run(env, *argv, fake=None, stdin="", **extra):
    e = {**env, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    if fake is not None:
        e["TARESD_URL"] = fake.url
    e.update(extra)
    return subprocess.run([sys.executable, "-m", "tares.crew", *argv], cwd=str(ROOT), env=e, input=stdin,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


try:
    # --- choose: the numbered question
    v, out = asking("\n")
    ck("choose: Enter takes the recommended one", v == "b" and "2) second   (recommended)" in out, out)
    v, _ = asking("3\n")
    ck("choose: a number", v == "c")
    v, _ = asking("a\n")
    ck("choose: the value itself", v == "a")
    v, out = asking("9\nzz\n1\n")
    ck("choose: a wrong answer asks again", v == "a" and out.count("Pick a number from 1 to 3.") == 2, out)
    v, out = asking("")
    ck("choose: the end of input takes the recommended one", v == "b", out)
    v, _ = asking("1")
    ck("choose: a last line with no newline is no answer", v == "b")
    v, _ = asking("٣\n\n")
    ck("choose: a non-ASCII digit is not a number", v == "b")

    # --- the small text helpers
    ck("words: the first n, one space between", person._words("  a  b\tc\nd e ", 4) == "a b c d")
    ck("words: fewer than n", person._words("only", 4) == "only")
    ck("cut_slug: 40 characters and no trailing hyphen",
       person._cut_slug("a" * 39 + " bbb") == "a" * 39 and person._cut_slug("My Links App!") == "my-links-app")
    ck("tsv: null is empty, tab, newline and backslash escaped",
       person._tsv(None) == "" and person._tsv("a\tb\nc\\d\re") == "a\\tb\\nc\\\\d\\re")

    # --- projects match by name, id or slug
    rows = [{"id": "p1", "name": "Links App"}, {"id": "p2", "name": "Shop"}, "junk", {"id": "p3", "name": None}]
    ck("match_project: the exact name", person.match_project("Shop", rows)["id"] == "p2")
    ck("match_project: the id", person.match_project("p1", rows)["name"] == "Links App")
    ck("match_project: the same slug", person.match_project("links-app", rows)["id"] == "p1"
       and person.match_project("LINKS  app", rows)["id"] == "p1")
    ck("match_project: none, and a row that is not an object is skipped",
       person.match_project("nope", rows) is None and person.match_project("junk", rows) is None)

    # --- git
    repo = T / "shop"
    mkrepo(repo)
    ck("has_commit: a repo with a commit", person.has_commit(str(repo)))
    empty = T / "empty"
    mkrepo(empty, commit=False)
    ck("has_commit: a repo with none, and a folder that is no repo",
       not person.has_commit(str(empty)) and not person.has_commit(str(T / "nothing")))
    hold = T / "hold"
    mkrepo(hold / "b-api")
    mkrepo(hold / "a-web")
    mkrepo(hold / "no-commit", commit=False)
    mkrepo(hold / ".hidden")
    (hold / "plain").mkdir()
    (hold / "file.txt").write_text("x", encoding="utf-8")
    (hold / "a-web" / "inner").mkdir()
    found = [os.path.basename(p) for p in person.repos_in(str(hold))]
    ck("repos_in: repos with a commit, in name order, not hidden ones or plain folders", found == ["a-web", "b-api"], str(found))
    ck("repos_in: a folder that is not there is none", person.repos_in(str(T / "gone")) == [])
    cwd = os.getcwd()
    try:
        os.chdir(repo / ".git")
        inside_git_dir = person.git_root()
        os.chdir(hold / "a-web" / "inner")
        ck("git_root: the top of the repo you are in, in the system's own form",
           os.path.normcase(person.git_root()) == os.path.normcase(os.path.realpath(hold / "a-web")))
        os.chdir(T)
        ck("git_root: outside a repo it is empty", person.git_root() == "" and inside_git_dir == "")
    finally:
        os.chdir(cwd)

    # --- git spells a path its own way (C:/dir on Windows): both are read as the same place
    real_git = person._git

    def odd_git(args, cwd=None):
        rc, out = real_git(args, cwd)
        return (rc, out.replace(os.sep, os.sep + "." + os.sep) if "--show-toplevel" in args and rc == 0 else out)

    person._git = odd_git
    try:
        ck("repos_in: a top-level path spelled another way is still the folder",
           [os.path.basename(p) for p in person.repos_in(str(hold))] == ["a-web", "b-api"])
        try:
            os.chdir(hold / "a-web")
            ck("git_root: the path is given in the system's own form", person.git_root() == os.path.normpath(person.git_root())
               and "." + os.sep not in person.git_root() and person.git_root() != "")
        finally:
            os.chdir(cwd)
    finally:
        person._git = real_git

    # --- the lists Tares gives
    class Fake:
        def __init__(self, answers):
            self.answers = answers

        def tares_http(self, method, path):
            a = self.answers[path]
            if isinstance(a, Exception):
                raise a
            return a if isinstance(a, str) else json.dumps(a)

    projects = {"projects": [
        {"id": "p1", "name": "Links App", "kind": "software_factory", "status": "done"},
        {"id": "p2", "name": "Shop", "kind": "software_factory"},
        {"id": "p3", "name": "Other", "kind": "custom", "status": "active"},
        "junk"]}
    fp = person.factory_projects(Fake({"/api/projects": projects}))
    ck("factory_projects: software factory only, a missing status is active",
       fp == [{"id": "p1", "name": "Links App", "status": "done"}, {"id": "p2", "name": "Shop", "status": "active"}], str(fp))
    ck("factory_projects: nothing when Tares cannot say",
       person.factory_projects(Fake({"/api/projects": "not json"})) == []
       and person.factory_projects(Fake({"/api/projects": {"projects": "x"}})) == []
       and person.factory_projects(Fake({"/api/projects": person.CrewError("down")})) == [])
    ck("releasing_projects: the list, else none",
       person.releasing_projects(Fake({"/api/crew/projects": {"releasing": [{"name": "Shop"}]}})) == [{"name": "Shop"}]
       and person.releasing_projects(Fake({"/api/crew/projects": {"projects": []}})) == []
       and person.releasing_projects(Fake({"/api/crew/projects": "x"})) == [])

    # --- a contributor: someone who is not the project's lead
    def contrib(me, lead="u-lead", key="Links App", whoami=None):
        row = {"id": "p1", "name": "Links App", "kind": "software_factory",
               "lead": {"id": lead, "name": "Lena"} if lead else None}
        return person.spec_contributor(Fake({"/api/projects": {"projects": [row]},
                                             "/api/whoami": whoami if whoami is not None else {"person": {"id": me}}}), key)

    ck("spec_contributor: not the lead is a contributor", contrib("u-me") == ("Links App", "Lena"), str(contrib("u-me")))
    ck("spec_contributor: the project's slug finds it", contrib("u-me", key="links-app") == ("Links App", "Lena"))
    ck("spec_contributor: the lead is not", contrib("u-lead") is None)
    ck("spec_contributor: no lead on the project is not", contrib("u-me", lead=None) is None)
    ck("spec_contributor: nobody signed in is not", contrib("", whoami={"person": None}) is None)
    ck("spec_contributor: a project the person did not name is not", contrib("u-me", key="Shop") is None
       and contrib("u-me", key="") is None)
    ck("spec_contributor: a lookup that fails is not",
       person.spec_contributor(Fake({"/api/projects": person.CrewError("down")}), "Links App") is None)

    # --- another installed version of the plugin
    home = T / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    rec = {"plugins": {
        "tares@marketplace": [{"version": "1.0.0", "scope": "user"},
                              {"version": "2.0.0", "scope": "project", "projectPath": "/w/shop"},
                              {"version": "9.9.9", "scope": "user"}],
        "other@marketplace": [{"version": "1.0.0", "scope": "user"}]}}
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(rec), encoding="utf-8")
    lines = person.installed_tares(str(home), "9.9.9")
    ck("installed_tares: other versions of tares only, with where", lines == [
        "tares@marketplace 1.0.0 user", "tares@marketplace 2.0.0 project (/w/shop)"], str(lines))
    ck("installed_tares: no record is none", person.installed_tares(str(T / "nohome"), "1") == [])
    text = person.clash_text(lines[1:], "/kit/plugin", "9.9.9")
    ck("clash_text: says how to remove it, in the project it was installed for",
       "claude plugin uninstall tares@marketplace --scope project (run it in /w/shop)" in text
       and "(/kit/plugin, version 9.9.9)" in text and "—" not in text, text)
    ck("clash_text: a user install needs no scope", text.split("uninstall ")[1] != "" and
       person.clash_text(lines[:1], "/kit", "9").endswith("claude plugin uninstall tares@marketplace"))

    # --- the usage errors, before any claude is opened
    env = fake_crew_env(T / "crew")
    fake = FakeTares({})
    claude = T / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    claude.chmod(0o755)
    try:
        if sys.platform != "win32":  # the fake claude is a shell script, which Windows cannot run
            r = run(env, "spec", "--project", fake=fake, CLAUDE_BIN=str(claude))
            ck("spec: --project with no name is a usage error", r.returncode == 1 and "usage: " in r.stderr
               and "spec [--project <name>]" in r.stderr, r.stderr)
            r = run(env, "spec", "--nope", fake=fake, CLAUDE_BIN=str(claude))
            ck("spec: an unknown option", r.returncode == 1 and "unknown option --nope" in r.stderr, r.stderr)
            r = run(env, "session", "--nope", fake=fake, CLAUDE_BIN=str(claude))
            ck("session: an unknown option", r.returncode == 1 and "unknown option --nope" in r.stderr, r.stderr)
            r = run(env, "session", "Shop", fake=fake, CLAUDE_BIN=str(claude), FACTORY_STATION="crew-orchestrator")
            ck("session: a station may not open one", r.returncode == 1
               and "only the person runs this" in r.stderr, r.stderr)
        bare = {k: v for k, v in env.items() if k != "CLAUDE_BIN"}
        (T / "emptybin").mkdir()
        for cmd in ("spec", "session"):
            r = run(bare, cmd, fake=fake, PATH=str(T / "emptybin"), HOME=str(T / "nohome"), USERPROFILE=str(T / "nohome"))
            ck(f"{cmd}: no Claude Code is said before Tares is asked anything", r.returncode == 1
               and "claude not found on PATH" in r.stderr
               and not [q for q in fake.requests if q["route"] not in ("GET /health", "GET /api/crew/settings")], r.stderr)
    finally:
        fake.stop()

    # --- routed to person.py, which the dispatcher loads only when one of them runs
    ck("the dispatcher routes spec and session to person.py",
       all(k in dispatch.PORTED for k in (("spec",), ("session",))))
    probe = ("import sys; from tares.crew import dispatch; print('person' in ' '.join(m for m in sys.modules "
             "if m.startswith('tares.crew')))")
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    ck("importing the dispatcher does not import person.py", r.stdout.strip() == "False", r.stdout + r.stderr)
finally:
    shutil.rmtree(T, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
