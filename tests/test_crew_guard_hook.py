"""The guard hook (`tares hook guard`, claude-plugin/hooks/guard.py) refuses what guard.sh refused (TR-971, TR-1103).

Runs every case of tests/factory/guard_test.sh (kept as tests/guard_cases.jsonl: role, tool,
command, expected allow or deny) against claude-plugin/hooks/guard.py in a subprocess, the way Claude
Code starts a hook: the payload on stdin, FACTORY_ROLE in the environment. Then 15 more cases
(tests/guard_extra_cases.jsonl, decisions taken from guard.sh before it was removed) that each pin a branch of guard.py, and the parts
that file does not cover: the halted crew, the deny output and the events log, a payload that is not JSON,
UTF-8 text, commands that make the scan slow, and the start time.

It needs neither bash nor jq, so it runs on Windows too. tests/factory/guard_test.sh runs the same cases
through `tares hook guard`, the way hooks.json starts it.

Run: python tests/test_crew_guard_hook.py
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "claude-plugin" / "hooks" / "guard.py"
CASES = ROOT / "tests" / "guard_cases.jsonl"
EXTRA = ROOT / "tests" / "guard_extra_cases.jsonl"
P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


TMP = Path(tempfile.mkdtemp(prefix="guard_hook_"))
BASE = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
BASE.update(HOME=str(TMP), USERPROFILE=str(TMP), PYTHONIOENCODING="utf-8")


def guard(payload, **env):
    """Run guard.py with the payload (a dict, or text) on stdin; returns the CompletedProcess."""
    data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(data, str):
        data = data.encode("utf-8")
    e = dict(BASE)
    e.update({k: v for k, v in env.items() if v is not None})
    return subprocess.run([sys.executable, str(GUARD)], input=data, capture_output=True, env=e, cwd=str(TMP))


def decision(r):
    return "deny" if b'"deny"' in r.stdout else "allow"


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


print("the cases of guard_test.sh")
cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
ck("the case file has 155 cases", len(cases) == 155, len(cases))
bad = 0
for c in cases:
    env = {"FACTORY_ROLE": c["role"], "FACTORY_HOME": "/nonexistent", "FACTORY_STATION": "test"}
    if c["role"] == "helper":
        env["FACTORY_PARENT"] = c["parent"]
    r = guard({"tool_name": c["tool"], "tool_input": {"command": c["command"]}}, **env)
    got = decision(r)
    if got != c["expect"] or r.returncode != 0 or r.stderr:
        bad += 1
        print(f"  FAIL {c['role']} | {c['command']!r} | expected {c['expect']}, got {got} (exit {r.returncode}) {r.stderr[-200:]!r}")
ck(f"all {len(cases)} cases decide as guard.sh decided", bad == 0, f"{bad} differ")

print("the cases that pin a branch of guard.py (tests/guard_extra_cases.jsonl)")
# Each one fails against a guard.py with the branch broken (the heredoc delimiter backreference, the
# reader lookahead, the `tares factory heavy` anchor, the helper's crew-owner prefix, `-` ending a factory verb,
# `git tag -x`, the per-line grep, the trailing newline of the tool name, the Slack full match, the
# claude anchor, the `.factory` folder) and two pin ASCII-only \w and \s. The expected decision is
# guard.sh's: they were taken from it before it was removed.
extra = [json.loads(line) for line in EXTRA.read_text(encoding="utf-8").splitlines() if line.strip()]
ck("the extra case file has 27 cases", len(extra) == 27, len(extra))
bad = 0
for c in extra:
    env = {"FACTORY_ROLE": c["role"], "FACTORY_HOME": "/nonexistent", "FACTORY_STATION": "test"}
    if c.get("parent"):
        env["FACTORY_PARENT"] = c["parent"]
    r = guard({"tool_name": c["tool"], "tool_input": {"command": c["command"]}}, **env)
    got = decision(r)
    if got != c["expect"] or r.returncode != 0 or r.stderr:
        bad += 1
        print(f"  FAIL {c['role']} | {c['command']!r} | expected {c['expect']}, got {got} (exit {r.returncode}) {r.stderr[-200:]!r}")
ck(f"all {len(extra)} extra cases decide as guard.sh decided", bad == 0, f"{bad} differ")

print("a session without FACTORY_ROLE is never touched")
r = guard(bash("gh pr merge 1"))
ck("no output, exit 0", r.returncode == 0 and r.stdout == b"", (r.returncode, r.stdout))
r = guard(bash("gh pr merge 1"), FACTORY_ROLE="")
ck("an empty FACTORY_ROLE counts as none", r.returncode == 0 and r.stdout == b"", (r.returncode, r.stdout))

print("a halted crew refuses a new session however the CLI is spelled")
halt = TMP / "halted"
halt.mkdir()
(halt / "HALT").write_bytes('{"reason":"test"}'.encode("utf-8"))
for cmd in ("factory spawn x", "/abs/bin/factory spawn x", "/abs/bin/factory revive x", "factory spawn;ls",
            "(factory revive x)", "tares factory spawn builder x", "gh pr merge 3", "git push origin HEAD",
            "kubectl apply -f x.yaml", "claude --bg"):
    r = guard(bash(cmd), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
    ck(f"halted: {cmd!r} is refused with the halt reason", b"crew is halted (test)" in r.stdout, r.stdout[-200:])
r = guard(bash("ls"), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
ck("halted: a harmless command still passes", r.stdout == b"", r.stdout)
r = guard(bash("git commit -m 'git push later'"), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
ck("halted: a commit message that mentions a push still passes", r.stdout == b"", r.stdout)
(halt / "HALT").write_bytes("{}".encode("utf-8"))
r = guard(bash("factory spawn x"), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
ck("halted without a reason: says so", b"crew is halted (no reason given)" in r.stdout, r.stdout)
(halt / "HALT").write_bytes('{"reason":""}'.encode("utf-8"))
r = guard(bash("factory spawn x"), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
ck("halted with an empty reason: prints it empty, as jq does", b"crew is halted (). Finish" in r.stdout, r.stdout)
(halt / "HALT").write_bytes('{"reason":{"a":[1,2]}}'.encode("utf-8"))
r = guard(bash("factory spawn x"), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
ck("halted with a reason that is an object: jq's indented JSON",
   json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecisionReason"].startswith(
       'The crew is halted ({\n  "a": [\n    1,\n    2\n  ]\n}).'), r.stdout)
(halt / "HALT").write_bytes("not json".encode("utf-8"))
r = guard(bash("factory spawn x"), FACTORY_ROLE="orchestrator", FACTORY_HOME=str(halt))
ck("halted with an unreadable file: still refuses", b"crew is halted (" in r.stdout, r.stdout)

print("a console grant for the exact command lifts a one-owner rule (TR-644)")
g_home = TMP / "granted"
(g_home / "stations").mkdir(parents=True)
(g_home / "stations" / "crew-releaser.grants").write_text(
    '- "The releaser may run `gh pr merge 426 -R o/r --squash` in every project" (2026-10-21)\n'
    '@project - "crew-releaser may run `git tag v9` in shop" (2026-10-21)\n'
    '- "The reviewer may run `gh pr merge 7` in every project" (2026-10-21)\n'
    '- "Builders may merge anything once CI is green" (2026-10-21)\n'
    '- "The releaser may run `docker build .` in every project" (2026-10-21)\n', encoding="utf-8")
G = dict(FACTORY_HOME=str(g_home), FACTORY_STATION="crew-releaser", FACTORY_ROLE="releaser")
ck("the exact command, granted to its role: allowed", decision(guard(bash("gh pr merge 426 -R o/r --squash"), **G)) == "allow")
ck("white space does not change the command", decision(guard(bash("gh pr merge  426 -R o/r   --squash"), **G)) == "allow")
ck("granted by station name: allowed", decision(guard(bash("git tag v9"), FACTORY_HOME=str(g_home), FACTORY_STATION="crew-releaser", FACTORY_ROLE="builder")) == "allow")
ck("any other command is still refused", decision(guard(bash("gh pr merge 427 -R o/r --squash"), **G)) == "deny")
ck("the granted command chained to another is refused", decision(guard(bash("gh pr merge 426 -R o/r --squash && git push"), **G)) == "deny")
ck("split over lines it is refused", decision(guard(bash("gh pr merge 426 -R o/r\n--squash"), **G)) == "deny")
ck("a grant to another role does not count", decision(guard(bash("gh pr merge 7"), **G)) == "deny")
ck("words that name no command lift nothing", decision(guard(bash("gh pr merge 9"), **G)) == "deny")
ck("another station has its own grants", decision(guard(bash("gh pr merge 426 -R o/r --squash"), FACTORY_HOME=str(g_home), FACTORY_STATION="crew-other", FACTORY_ROLE="releaser")) == "deny")
ck("a grant does not lift the heavy-command rule", decision(guard(bash("docker build ."), **G)) == "deny")
(g_home / "HALT").write_bytes(b'{"reason":"test"}')
ck("nor a halted crew", decision(guard(bash("gh pr merge 426 -R o/r --squash"), **G)) == "deny")
(g_home / "HALT").unlink()

print("a builder of a crew without a releaser may push the tags a project grant names (TR-1574)")
# Nothing here runs a tag command: the hook only decides, and the folders are local repositories whose `origin` is a
# URL that is never contacted (guard.py reads it from the config with `git remote get-url`).
t_home = TMP / "tagged"
(t_home / "stations").mkdir(parents=True)
ROSTER = {"crew-orchestrator": {"role": "orchestrator"}, "crew-reviewer": {"role": "reviewer"},
          "shop-build-ui": {"role": "builder", "project": "shop"}}
(t_home / "stations.json").write_text(json.dumps(ROSTER), encoding="utf-8")
(t_home / "stations" / "shop-build-ui.grants").write_text(
    '@project - "The crew may push the design tags in arche-design: `tag: glassflow/arche-design paper-tokens-v<N>`'
    ' and `tag: glassflow/arche-design paper-kit-v<N>`, on the merge commit" (2026-11-23)\n'
    '- "The crew may push paper-tokens-v7 in tares" (2026-11-23)\n', encoding="utf-8")


def make_repo(name, url, *more):
    """A repository whose origin is url (none when url is empty) and then the git calls in more, each a list of arguments."""
    d = TMP / name
    d.mkdir()
    for args in ((["init", "-q"], ["remote", "add", "origin", url]) if url else (["init", "-q"],)) + more:
        subprocess.run(["git", "-C", str(d)] + args, check=True, env=BASE, capture_output=True)
    return d


arche = make_repo("arche-design", "https://github.com/glassflow/arche-design.git")
arche_ssh = make_repo("arche-design-ssh", "git@github.com:GlassFlow/Arche-Design")
tares_repo = make_repo("tares-clone", "git@github.com:glassflow/tares.git")
loose = make_repo("no-origin", None)
AR = "https://github.com/glassflow/arche-design.git"
arche_pushes_elsewhere = make_repo("push-elsewhere", AR, ["config", "remote.origin.pushurl", "https://github.com/glassflow/tares.git"])
arche_pushes_same = make_repo("push-same", AR, ["config", "remote.origin.pushurl", "git@github.com:glassflow/arche-design.git"])
arche_two_urls = make_repo("two-urls", AR, ["remote", "set-url", "--add", "origin", "https://github.com/glassflow/tares.git"])
arche_fake_host = make_repo("fake-host", "https://evil.example/github.com/glassflow/arche-design.git")
arche_fake_host2 = make_repo("fake-host2", "https://github.com.evil.example/glassflow/arche-design.git")
arche_local = make_repo("local-path", "/srv/github.com/glassflow/arche-design.git")
SHA = "0123456789abcdef0123456789abcdef01234567"
T = dict(FACTORY_HOME=str(t_home), FACTORY_STATION="shop-build-ui", FACTORY_ROLE="builder")


def tag_call(cmd, folder=arche, **env):
    e = dict(T)
    e.update(env)
    return decision(guard({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(folder)}, **e))


ck("the usual two-step flow in one command: allowed",
   tag_call(f"git tag paper-tokens-v3 {SHA} && git push origin paper-tokens-v3") == "allow")
ck("the other named tag: allowed", tag_call(f"git tag paper-kit-v12 {SHA} && git push origin paper-kit-v12") == "allow")
ck("a short sha: allowed", tag_call("git tag paper-kit-v2 0123abc && git push origin paper-kit-v2") == "allow")
ck("the tag step on its own: allowed", tag_call(f"git tag paper-tokens-v4 {SHA}") == "allow")
ck("the push step on its own: allowed", tag_call("git push origin paper-tokens-v4") == "allow")
ck("after a fetch and a cd to the repo, from another folder: allowed",
   tag_call(f"cd {arche.name} && git fetch origin && git tag paper-kit-v5 {SHA} && git push origin paper-kit-v5", folder=TMP) == "allow")
ck("an ssh origin, with the case of the names changed: allowed",
   tag_call(f"git tag paper-kit-v6 {SHA} && git push origin paper-kit-v6", folder=arche_ssh) == "allow")
ck("checking the tag on the remote with ls-remote: allowed", tag_call("git ls-remote --tags origin paper-tokens-v3") == "allow")

# every case below runs with the grant in place, so each one tests the rule and not a missing grant
ck("another tag name: refused", tag_call(f"git tag v1.2.0 {SHA} && git push origin v1.2.0") == "deny")
ck("a name that only starts like the pattern: refused", tag_call(f"git tag paper-tokens-v3-rc1 {SHA}") == "deny")
ck("a name that only ends like the pattern: refused", tag_call(f"git tag old-paper-tokens-v3 {SHA}") == "deny")
ck("a version with a leading zero: refused", tag_call(f"git tag paper-kit-v03 {SHA}") == "deny")
ck("a name that names no number: refused", tag_call(f"git tag paper-kit-v {SHA}") == "deny")
ck("a tag the prose of a grant mentions: refused", tag_call(f"git tag paper-tokens-v7 {SHA}", folder=tares_repo) == "deny")
ck("the granted tag in another repo: refused", tag_call(f"git tag paper-tokens-v3 {SHA} && git push origin paper-tokens-v3", folder=tares_repo) == "deny")
ck("a folder with no origin: refused", tag_call(f"git tag paper-tokens-v3 {SHA}", folder=loose) == "deny")
ck("the push half of the flow in another repo: refused", tag_call("git push origin paper-tokens-v3", folder=tares_repo) == "deny")
ck("a cd to another repo moves the check there: refused",
   tag_call(f"cd {tares_repo.name} && git tag paper-tokens-v3 {SHA}", folder=TMP) == "deny")
ck("a cd with .. in it: refused", tag_call(f"cd ../{arche.name} && git tag paper-tokens-v3 {SHA}", folder=tares_repo) == "deny")
ck("a cd to a parent folder: refused", tag_call(f"cd .. && git tag paper-tokens-v3 {SHA}", folder=arche) == "deny")
try:
    (arche / "sub").mkdir()
    (tares_repo / "link").symlink_to(arche / "sub", target_is_directory=True)
except OSError:
    print("  (no symlinks here: the cd through a link is not run)")
else:
    # the shell's `cd link/..` ends in tares_repo (the path as typed), `git -C link/..` in arche (the real folder)
    ck("a cd through a link and up again, where git and the shell disagree: refused",
       tag_call("cd link/.. && git push origin paper-kit-v3", folder=tares_repo) == "deny")
    ck("a cd into a link, with no .. : allowed (git and the shell agree on the folder)",
       tag_call("cd link && git push origin paper-kit-v3", folder=tares_repo) == "allow")
# a long chain is shaped before any git call is made: a granted push 200 times and then a refused one must not
# cost 200 `git remote` calls (a hook gets 5 s, and a timeout lets the command through)
LONG = " && ".join(["git push origin paper-kit-v3"] * 200)
t0 = time.perf_counter()
ck("a long chain ending in a refused push: refused", tag_call(LONG + " && git push --tags") == "deny")
ck("...in under a second, not after one git call per segment", time.perf_counter() - t0 < 1.0, f"{time.perf_counter() - t0:.2f} s")
t0 = time.perf_counter()
ck("a long chain of granted pushes: refused (more than 8 segments)", tag_call(LONG) == "deny")
ck("...in under a second", time.perf_counter() - t0 < 1.0, f"{time.perf_counter() - t0:.2f} s")
if os.name != "nt":
    # a `git` on PATH that counts its calls: a chain with a refused segment at its end makes none
    spy = TMP / "spy-bin"
    spy.mkdir()
    counter = TMP / "git-calls"
    real_git = shutil.which("git")
    (spy / "git").write_text(f'#!/bin/sh\necho x >> "{counter}"\nexec "{real_git}" "$@"\n', encoding="utf-8")
    (spy / "git").chmod(0o755)
    spy_path = str(spy) + os.pathsep + BASE.get("PATH", os.environ.get("PATH", ""))
    ck("a chain of granted pushes ending in a refused one: refused",
       tag_call(" && ".join(["git push origin paper-kit-v3"] * 7 + ["git push --tags"]), PATH=spy_path) == "deny")
    ck("...with no git call made for it", not counter.exists(), counter.read_text() if counter.exists() else "")
    ck("a granted push: allowed, with git asked for the remote's urls",
       tag_call("git push origin paper-kit-v3", PATH=spy_path) == "allow" and counter.exists())
ck("eight segments, the most: allowed",
   tag_call(" && ".join(["git fetch origin"] * 4 + ["git push origin paper-kit-v3"] * 4)) == "allow")
ck("nine segments: refused", tag_call(" && ".join(["git fetch origin"] * 5 + ["git push origin paper-kit-v3"] * 4)) == "deny")
ck("a fetch of something else than origin: refused", tag_call(f"git fetch origin --tags && git tag paper-tokens-v3 {SHA}") == "deny")
ck("a fetch of every remote: refused", tag_call(f"git fetch --all && git tag paper-tokens-v3 {SHA}") == "deny")
ck("a fetch of another remote: refused", tag_call(f"git fetch upstream && git tag paper-tokens-v3 {SHA}") == "deny")
ck("git push --mirror: refused", tag_call("git push --mirror origin") == "deny")
ck("a branch that only has -v2 in the middle is not a tag push", tag_call("git push origin docs-v2-notes") == "allow")
ck("...and one that ends in -v2 is", tag_call("git push origin docs-v2") == "deny")
ck("git push --tags: refused", tag_call("git push --tags") == "deny")
ck("git push origin --tags: refused", tag_call("git push origin --tags") == "deny")
ck("--follow-tags: refused", tag_call("git push --follow-tags origin") == "deny")
ck("a tag with the push of every tag: refused", tag_call(f"git tag paper-kit-v8 {SHA} && git push origin paper-kit-v8 --tags") == "deny")
ck("deleting a local tag: refused", tag_call("git tag -d paper-tokens-v3") == "deny")
ck("deleting a local tag, long form: refused", tag_call("git tag --delete paper-tokens-v3") == "deny")
ck("deleting a remote tag with --delete: refused", tag_call("git push origin --delete paper-tokens-v3") == "deny")
ck("deleting a remote tag with a colon: refused", tag_call("git push origin :paper-tokens-v3") == "deny")
ck("moving a tag with -f: refused", tag_call(f"git tag -f paper-tokens-v3 {SHA}") == "deny")
ck("moving a tag with --force: refused", tag_call(f"git tag --force paper-tokens-v3 {SHA}") == "deny")
ck("a force push of a tag: refused", tag_call("git push -f origin paper-tokens-v3") == "deny")
ck("a force push of a tag with a plus: refused", tag_call("git push origin +paper-tokens-v3") == "deny")
ck("an annotated tag: refused", tag_call("git tag -a paper-tokens-v3 -m release") == "deny")
ck("a tag on a branch name instead of a sha: refused", tag_call("git tag paper-tokens-v3 main") == "deny")
ck("a push to a remote that is not origin: refused", tag_call("git push upstream paper-tokens-v3") == "deny")
ck("a refspec that makes the tag from a branch: refused", tag_call("git push origin HEAD:refs/tags/paper-tokens-v3") == "deny")
ck("the tag as a full ref: refused", tag_call("git push origin refs/tags/paper-tokens-v3") == "deny")
ck("gh release create: refused", tag_call("gh release create paper-tokens-v3") == "deny")
ck("the allowed flow chained to another command: refused",
   tag_call(f"git tag paper-tokens-v3 {SHA} && git push origin paper-tokens-v3 && gh pr merge 1") == "deny")
ck("the allowed flow, then a second command after a semicolon: refused",
   tag_call(f"git tag paper-tokens-v3 {SHA}; gh pr merge 1") == "deny")
ck("a command substitution in the flow: refused", tag_call(f"git tag paper-tokens-v3 {SHA} && git push origin $(echo paper-tokens-v3)") == "deny")
ck("a trailing cd with a command substitution: refused", tag_call(f"git tag paper-tokens-v3 {SHA} && cd $(pwd)") == "deny")
ck("a trailing cd with backticks: refused", tag_call(f"git tag paper-tokens-v3 {SHA} && cd `pwd`") == "deny")
FLOW_TAG = f"git tag paper-tokens-v3 {SHA} && git push origin paper-tokens-v3"
ck("a repo that pushes to another repo than it fetches from: refused", tag_call(FLOW_TAG, folder=arche_pushes_elsewhere) == "deny")
ck("a repo whose push url is the same repo in another spelling: allowed", tag_call(FLOW_TAG, folder=arche_pushes_same) == "allow")
ck("a repo with a second url for another repo: refused", tag_call(FLOW_TAG, folder=arche_two_urls) == "deny")
ck("a url that only has github.com in its path: refused", tag_call(FLOW_TAG, folder=arche_fake_host) == "deny")
ck("a url on a host that only starts with github.com: refused", tag_call(FLOW_TAG, folder=arche_fake_host2) == "deny")
ck("a local path with github.com in it: refused", tag_call(FLOW_TAG, folder=arche_local) == "deny")
ck("a quoted name: refused", tag_call(f"git tag 'paper-tokens-v3' {SHA}") == "deny")
ck("a line break in the flow: refused", tag_call(f"git tag paper-tokens-v3 {SHA}\ngit push --tags") == "deny")
ck("a backgrounded half: refused", tag_call(f"git tag paper-tokens-v3 {SHA} & git push --tags") == "deny")
ck("listing tags stays open", tag_call("git tag -l 'paper-*'") == "allow")
ck("listing tags with a sort stays open", tag_call("git tag --sort=-creatordate") == "allow")

print("the same flow stays refused when the rule's other conditions fail (TR-1574)")
(t_home / "stations.json").write_text(json.dumps(dict(ROSTER, **{"crew-releaser": {"role": "releaser"}})), encoding="utf-8")
FLOW = f"git tag paper-tokens-v3 {SHA} && git push origin paper-tokens-v3"
ck("a crew with a releaser: refused", tag_call(FLOW) == "deny")
(t_home / "stations.json").write_text(json.dumps(ROSTER), encoding="utf-8")
ck("with the releaser gone again: allowed", tag_call(FLOW) == "allow")
(t_home / "stations.json").write_text("not json", encoding="utf-8")
ck("a roster that cannot be read: refused", tag_call(FLOW) == "deny")
(t_home / "stations.json").unlink()
ck("no roster: refused", tag_call(FLOW) == "deny")
(t_home / "stations.json").write_text(json.dumps(ROSTER), encoding="utf-8")
ck("the roster restored: allowed", tag_call(FLOW) == "allow")
ck("an orchestrator holding the same grant: refused", tag_call(FLOW, FACTORY_ROLE="orchestrator") == "deny")
ck("a reviewer holding the same grant: refused", tag_call(FLOW, FACTORY_ROLE="reviewer") == "deny")
ck("a helper of a builder, which has no grants of its own: refused",
   tag_call(FLOW, FACTORY_ROLE="helper", FACTORY_PARENT="shop-build-ui", FACTORY_STATION="shop-build-ui-h1") == "deny")
ck("a builder with no grants file: refused", tag_call(FLOW, FACTORY_STATION="shop-build-other") == "deny")
(t_home / "stations" / "shop-build-prose.grants").write_text(
    '- "The crew may push tag: glassflow/arche-design paper-tokens-v3 and paper-kit-v1" (2026-11-23)\n', encoding="utf-8")
ck("a grant that says it in prose only: refused", tag_call(FLOW, FACTORY_STATION="shop-build-prose") == "deny")
(t_home / "HALT").write_bytes(b'{"reason":"test"}')
ck("a halted crew: refused", tag_call(FLOW) == "deny")
(t_home / "HALT").unlink()
r = guard({"tool_name": "Bash", "tool_input": {"command": FLOW}}, **T)
ck("a payload with no cwd is judged from the hook's own folder, which has no origin: refused", decision(r) == "deny")

print("the deny output")
home = TMP / "crewhome"
home.mkdir()
r = guard(bash("gh pr merge 12"), FACTORY_ROLE="reviewer", FACTORY_HOME=str(home), FACTORY_STATION="rev-1")
out = json.loads(r.stdout.decode("utf-8"))
hso = out["hookSpecificOutput"]
ck("it is the PreToolUse deny shape Claude Code reads", r.returncode == 0 and hso["hookEventName"] == "PreToolUse"
   and hso["permissionDecision"] == "deny", out)
ck("the reason names the owner and the way out", hso["permissionDecisionReason"] ==
   "In this factory only the builder station may merge pull requests. Send it a message instead (see the factory-protocol skill), or ask the person to allow this exact command in the console.",
   hso["permissionDecisionReason"])
events = [json.loads(line) for line in (home / "events.jsonl").read_text(encoding="utf-8").splitlines()]
ck("one guard-deny line is appended to events.jsonl", len(events) == 1 and events[0]["event"] == "guard-deny"
   and events[0]["station"] == "rev-1" and events[0]["tool"] == "Bash" and events[0]["reason"] == hso["permissionDecisionReason"], events)
ck("...with a UTC time", len(events[0]["at"]) == 20 and events[0]["at"].endswith("Z") and events[0]["at"][10] == "T", events[0]["at"])
r = guard(bash("gh pr merge 12"), FACTORY_ROLE="reviewer", FACTORY_HOME=str(home))
events = [json.loads(line) for line in (home / "events.jsonl").read_text(encoding="utf-8").splitlines()]
ck("a station without a name logs as ?", events[-1]["station"] == "?" and len(events) == 2, events[-1])
r = guard(bash("gh pr merge 12"), FACTORY_ROLE="reviewer", FACTORY_HOME=str(TMP / "no such dir"))
ck("no crew home folder: it still denies and writes nothing", decision(r) == "deny" and not (TMP / "no such dir").exists(), r.stdout)
r = guard(bash("ls"), FACTORY_ROLE="reviewer", FACTORY_HOME=str(home))
ck("an allowed command prints nothing and logs nothing", r.stdout == b"" and len((home / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2, r.stdout)
r = guard({"tool_name": "mcp__plugin_slack_slack__slack_send_message", "tool_input": {}}, FACTORY_ROLE="builder")
ck("a Slack send is refused for every role with its own reason", b"Crew stations don't message people" in r.stdout, r.stdout)

print("input that is not a command")
for label, payload in (("not JSON", "this is not json"), ("an empty payload", ""), ("a JSON list", "[1, 2]"),
                       ("no tool name", {"tool_input": {"command": "gh pr merge 1"}}),
                       ("a null tool name", {"tool_name": None, "tool_input": {"command": "gh pr merge 1"}}),
                       ("a tool_input that is a string", {"tool_name": "Bash", "tool_input": "gh pr merge 1"}),
                       ("no tool_input", {"tool_name": "Bash"}),
                       ("a command that is a number", {"tool_name": "Bash", "tool_input": {"command": 7}}),
                       ("another tool", {"tool_name": "Read", "tool_input": {"command": "gh pr merge 1"}})):
    r = guard(payload, FACTORY_ROLE="reviewer", FACTORY_HOME="/nonexistent")
    ck(f"{label}: allowed, exit 0, nothing on stderr", r.returncode == 0 and r.stdout == b"" and r.stderr == b"", (r.returncode, r.stdout, r.stderr[-200:]))

print("text that is not ASCII")
text = "Behebe den Fehler über gh pr merge in Zeile 3 – 日本語"
r = guard(bash(f'git commit -m "{text}"'), FACTORY_ROLE="reviewer", FACTORY_HOME="/nonexistent")
ck("a UTF-8 commit message is prose and passes", r.returncode == 0 and r.stdout == b"", (r.returncode, r.stdout, r.stderr[-200:]))
r = guard(bash(f"echo 'éè' && gh pr merge 3"), FACTORY_ROLE="reviewer", FACTORY_HOME="/nonexistent")
ck("a UTF-8 command still has its rules applied, and the reason is valid UTF-8 JSON",
   decision(r) == "deny" and json.loads(r.stdout.decode("utf-8")), r.stdout)
raw = b'{"tool_name":"Bash","tool_input":{"command":"echo \xff\xfe && gh pr merge 3"}}'
r = guard(raw, FACTORY_ROLE="reviewer", FACTORY_HOME="/nonexistent")
ck("bytes that are not UTF-8 do not crash the guard", r.returncode == 0 and decision(r) == "deny", (r.returncode, r.stdout, r.stderr[-200:]))

print("long commands")
long_chain = " && ".join(["git status"] * 400)
long_args = "kubectl " + " ".join(["get"] * 3000) + " pods"
long_quote = 'git commit -m "' + "x " * 20000 + '"'
many_quotes = "echo " + " ".join(["'a b'"] * 3000) + " | cat"
# the quoted-string scans rescan at every quote (6 s for 15000 escaped quotes before the cap)
escaped_quotes = "echo " + '\\"' * 15000 + "; gh pr merge 1"
message_quotes = 'git commit -m "' + 'x\\"' * 15000
for label, cmd in (("a chain of 400 commands", long_chain), ("a kubectl line of 3000 words", long_args),
                   ("a 40 KB commit message", long_quote), ("3000 quoted words", many_quotes),
                   ("15000 escaped quotes", escaped_quotes), ("a message of 15000 escaped quotes", message_quotes)):
    t = time.perf_counter()
    r = guard(bash(cmd), FACTORY_ROLE="builder", FACTORY_HOME="/nonexistent")
    ms = (time.perf_counter() - t) * 1000
    ck(f"{label}: allowed in under 3 s ({ms:.0f} ms)", r.returncode == 0 and r.stdout == b"" and ms < 3000, (r.returncode, r.stdout[-100:], ms))

print("start time")
times = []
for _ in range(7):
    t = time.perf_counter()
    r = guard(bash("gh pr merge 1"), FACTORY_ROLE="builder", FACTORY_HOME="/nonexistent")
    times.append((time.perf_counter() - t) * 1000)
med = statistics.median(times)
print(f"  guard.py: median {med:.0f} ms over {len(times)} runs (min {min(times):.0f}, max {max(times):.0f})")
ck("the median run is under 400 ms", med < 400, med)
src = GUARD.read_text(encoding="utf-8")
ck("guard.py imports nothing from the tares package", "import tares" not in src and "from tares" not in src and "from ." not in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
