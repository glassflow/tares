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
ck("the case file has the 154 cases of guard_test.sh", len(cases) == 154, len(cases))
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
# reader lookahead, the factory-heavy anchor, the helper's crew-owner prefix, `-` ending a factory verb,
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
