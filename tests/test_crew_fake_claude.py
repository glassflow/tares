"""The Python fake Claude Code (tests/factory/fake_claude.py) behaves like the bash one it replaces
(tests/factory/fake-claude and fake-ps).

Checks: the session states the fake models (working, waiting, idle, stopped) and what `agents --json`
lists with and without --all; the `--bg` output and the files it leaves; then, where bash and jq are
there (not on Windows), the same arguments and state files go to both fakes and everything they print
and write must be identical (stdout, exit code, calls.jsonl, agents.json, job and ps files).
Sessions with no process (working or blocked still count as active) and the FAKE_CLAUDE_BG_HOLD file that
holds a launch open are checked on both fakes.

Run: .venv/bin/python tests/test_crew_fake_claude.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORY = ROOT / "tests" / "factory"
sys.path.insert(0, str(FACTORY))

import fake_claude  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def clean_env(sb, extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FAKE_CLAUDE_", "FACTORY_", "TARES_")) and k not in ("PWD", "OLDPWD")}
    env.update(HOME=str(sb / "home"), FAKE_CLAUDE_STATE=str(sb / "state"))
    env.update(extra or {})
    return env


def run_python(args, sb, extra=None, ps=False):
    cmd = [sys.executable, str(FACTORY / "fake_claude.py")] + (["--fake-ps"] if ps else []) + list(args)
    return subprocess.run(cmd, cwd=sb / "work", env=clean_env(sb, extra), capture_output=True,
                          text=True, encoding="utf-8")


def run_bash(args, sb, extra=None, ps=False):
    exe = FACTORY / ("fake-ps" if ps else "fake-claude")
    return subprocess.run([str(exe)] + list(args), cwd=sb / "work", env=clean_env(sb, extra),
                          capture_output=True, text=True, encoding="utf-8")


def sandbox(root, name):
    sb = Path(root) / name
    for d in ("home", "state", "work"):
        (sb / d).mkdir(parents=True)
    return sb


def tree(sb):
    """Every file the fakes wrote (last.env is the process environment: not compared), paths and
    contents, with the sandbox's own path made constant."""
    out = {}
    for base in (sb / "state", sb / "home"):
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.name != "last.env":
                out[str(p.relative_to(sb))] = p.read_text(encoding="utf-8").replace(os.path.realpath(sb), "<SB>").replace(str(sb), "<SB>")
    return out


ROWS = [
    fake_claude.agent_row("working", "0000000a", "crew-orchestrator", "/crew", startedAt=1),
    fake_claude.agent_row("waiting", "0000000b", "crew-reviewer", "/crew", startedAt=2),
    fake_claude.agent_row("idle", "0000000c", "crew-releaser", "/crew", startedAt=3),
    fake_claude.agent_row("stopped", "0000000d", "builder", "/repo", startedAt=4),
    {"id": "0000000e", "name": "gone", "cwd": "/x", "state": "done"},
]


def seed_agents(sb):
    fake_claude.write_agents(sb / "state", ROWS)


# Sessions with no process: Claude Code still counts one that is working, or blocked on a person, as active
# (a schedule armed keeps a stopped session "working"); a stopped or finished one drops out.
NOPID_ROWS = [
    {"id": "000000a1", "name": "working-no-pid", "cwd": "/x", "state": "working"},
    {"id": "000000a2", "name": "blocked-no-pid", "cwd": "/x", "state": "blocked"},
    {"id": "000000a3", "name": "stopped-no-pid", "cwd": "/x", "state": "stopped"},
    {"id": "000000a4", "name": "done-no-pid", "cwd": "/x", "state": "done"},
    {"id": "000000a5", "name": "no-state", "cwd": "/x"},
]


def seed_nopid(sb):
    fake_claude.write_agents(sb / "state", NOPID_ROWS)


def seed_version(sb):
    (sb / "state" / "version").write_text("2.1.300\n")
    (sb / "state" / "spawn-version").write_text("2.1.100\n")
    (sb / "state" / "stale-left").write_text("1\n")


def seed_flip(sb):
    (sb / "state" / "version").write_text("2.1.300\n")
    (sb / "state" / "version-flip").write_text("2.1.301\n")


def seed_plugins(sb):
    (sb / "state" / "plugins.json").write_text('[{"id": "a@b", "enabled": true}]\n')


def seed_hook(sb):
    (sb / "state" / "on-bg").write_text('echo "$FAKE_ID" > "$FAKE_CLAUDE_STATE/hook-ran"')
    (sb / "state" / "on-agents").write_text('echo ran > "$FAKE_CLAUDE_STATE/agents-hook-ran"')
    (sb / "state" / "on-agents-at").write_text("2\n")


def seed_ps(sb):
    (sb / "state" / "ps").mkdir()
    (sb / "state" / "ps" / "123.ppid").write_text("456\n")
    (sb / "state" / "ps-list").write_text("1 a\n2 b\n")


def seed_settings(sb):
    (sb / "work" / "s.json").write_text('{"x": 1}')


# name, seed, [(args, env)], ps
CASES = [
    ("version", seed_version, [(["--version"], {})]),
    ("version flip", seed_flip, [(["--version"], {})] * 4),
    ("agents active", seed_agents, [(["agents", "--json"], {})]),
    ("agents with no process", seed_nopid, [(["agents", "--json"], {}), (["agents", "--json", "--all"], {})]),
    ("agents all", seed_agents, [(["agents", "--json", "--all"], {})]),
    ("agents fail", seed_agents, [(["agents", "--json"], {"FAKE_CLAUDE_AGENTS_FAIL": "1"})]),
    ("agents fail after", seed_agents, [(["agents", "--json"], {"FAKE_CLAUDE_AGENTS_FAIL_AFTER": "1"})] * 3),
    ("agents hook", seed_hook, [(["agents", "--json"], {})] * 3),
    ("stop", seed_agents, [(["stop", "0000000a"], {}), (["agents", "--json", "--all"], {})]),
    ("stop keeps state", seed_agents, [(["stop", "0000000a"], {"FAKE_CLAUDE_STOP_KEEPS_STATE": "1"}),
                                       (["agents", "--json", "--all"], {})]),
    ("rm", seed_agents, [(["rm", "0000000b"], {}), (["agents", "--json", "--all"], {})]),
    ("plugin list", seed_plugins, [(["plugin", "list", "--json"], {})]),
    ("plugin list fails", seed_plugins, [(["plugin", "list", "--json"], {"FAKE_CLAUDE_PLUGIN_FAIL": "1"})]),
    ("plugin install", seed_plugins, [(["plugin", "install", "pyright-lsp@x"], {}),
                                      (["plugin", "list", "--json"], {})]),
    ("plugin install on empty", lambda sb: None, [(["plugin", "install", "c@d"], {}),
                                                  (["plugin", "list", "--json"], {})]),
    ("print /context", lambda sb: None, [(["-p", "/context", "--resume", "s1"], {})]),
    ("print /usage", lambda sb: None, [(["-p", "/usage"], {})]),
    ("print /usage without a plan", lambda sb: None, [(["-p", "/usage"], {"FAKE_CLAUDE_NO_PLAN": "1"})]),
    ("print settings inline", lambda sb: None, [(["-p", "--settings", '{"a":"b c"}', "/usage"], {})]),
    ("print settings file", seed_settings, [(["-p", "--settings", "s.json", "/context"], {})]),
    ("bg twice", seed_version, [(["--bg", "-n", "crew-orchestrator", "--plugin-dir", "/p", "go"], {}),
                                (["--bg", "-n", "crew-reviewer", "hello world"], {}),
                                (["--bg", "-n", "third", "x"], {}),
                                (["agents", "--json"], {})]),
    ("bg with all stale spares", seed_version, [(["--bg", "-n", "a", "x"], {})]),
    ("bg with a hold file that is not there", lambda sb: None, [(["--bg", "-n", "h", "x"], {"FAKE_CLAUDE_BG_HOLD": "/nonexistent/hold"})]),
    ("bg hook", seed_hook, [(["--bg", "-n", "h", "x"], {}), (["--bg", "-n", "h2", "x"], {})]),
    ("bg unicode name", lambda sb: None, [(["--bg", "-n", "café", "x"], {})]),
    ("bg resume fails", lambda sb: None, [(["--bg", "--resume", "s", "-n", "r"], {"FAKE_CLAUDE_FAIL_RESUME": "1"}),
                                          (["--bg", "-n", "f"], {"FAKE_CLAUDE_FAIL_RESUME": "1"})]),
    ("bg fresh fails", lambda sb: None, [(["--bg", "-n", "r"], {"FAKE_CLAUDE_FAIL_FRESH": "1"}),
                                         (["--bg", "--resume", "s", "-n", "f"], {"FAKE_CLAUDE_FAIL_FRESH": "1"})]),
    ("an argument with control characters", lambda sb: None, [(["x", "tab\there", 'q"uote', "back\\slash", ""], {})]),
    ("no arguments", lambda sb: None, [([], {})]),
    ("unknown command", lambda sb: None, [(["nonsense"], {})]),
]
PS_CASES = [
    ("ps field of a session", seed_ps, [(["-o", "ppid=", "-p", "123"], {})]),
    ("ps whole list", seed_ps, [(["-axo", "pid=,command="], {})]),
    ("ps whole list with none written", lambda sb: None, [(["-axo", "pid=,command="], {})]),
]

# ====================================================================================
# 1. The Python fake alone (every platform)
print("== the Python fake alone")
tmp = tempfile.mkdtemp(prefix="fakeclaude-")
sb = sandbox(tmp, "solo")
seed_agents(sb)
r = run_python(["agents", "--json"], sb)
active = {row["name"]: row for row in json.loads(r.stdout)}
ck("agents lists the working, waiting and idle sessions and not the stopped or done ones",
   r.returncode == 0 and sorted(active) == ["crew-orchestrator", "crew-releaser", "crew-reviewer"], r.stdout)
r = run_python(["agents", "--json", "--all"], sb)
ck("agents --all lists every session", len(json.loads(r.stdout)) == 5, r.stdout)
ck("the states carry the fields Claude Code gives them",
   (ROWS[0]["state"], ROWS[0]["status"]) == ("working", "busy") and (ROWS[1]["state"], ROWS[1]["status"]) == ("blocked", "waiting")
   and ROWS[2]["status"] == "idle" and "pid" in ROWS[2] and ROWS[3]["state"] == "stopped" and "pid" not in ROWS[3])
sb2 = sandbox(tmp, "nopid")
seed_nopid(sb2)
r = run_python(["agents", "--json"], sb2)
ck("a session with no process counts as active when it is working or blocked on a person, and not when stopped, done or without a state",
   sorted(x["name"] for x in json.loads(r.stdout)) == ["blocked-no-pid", "working-no-pid"], r.stdout)
ck("...the blocked one is listed because of its state alone", fake_claude._is_active({"state": "blocked"}) and not fake_claude._is_active({"state": "stopped"}))
r = run_python(["stop", "0000000a"], sb)
rows = {x["id"]: x for x in json.loads((sb / "state" / "agents.json").read_text())}
ck("stop leaves the session stopped, with no process", rows["0000000a"]["state"] == "stopped" and "pid" not in rows["0000000a"])
r = run_python(["--bg", "-n", "crew-x", "--plugin-dir", "/p", "boot"], sb)
ck("--bg prints 'backgrounded', the new id and the name", r.stdout == "backgrounded · 00000001 · crew-x\n" and r.returncode == 0, repr(r.stdout))
rows = json.loads((sb / "state" / "agents.json").read_text())
ck("--bg adds a working session with a process", rows[-1]["id"] == "00000001" and rows[-1]["state"] == "working" and rows[-1]["pid"] == 40001
   and rows[-1]["cwd"] == str(sb / "work") or rows[-1]["cwd"] == os.path.realpath(sb / "work"), rows[-1])
ck("--bg writes the job file", json.loads((sb / "home" / ".claude" / "jobs" / "00000001" / "state.json").read_text()) == {"sessionId": "session-1"})
r = run_python(["-o", "ppid=", "-p", "40001"], sb, ps=True)
ck("the fake ps answers for the new session", r.stdout == "41001\n", repr(r.stdout))
calls = [json.loads(line) for line in (sb / "state" / "calls.jsonl").read_text().splitlines()]
ck("every call is recorded with its arguments", calls[-1]["args"] == ["--bg", "-n", "crew-x", "--plugin-dir", "/p", "boot"]
   and list(calls[-1]) == ["cwd", "args"], calls[-1])


def hold_test(label, cmd, sbx):
    """A launch with FAKE_CLAUDE_BG_HOLD waits while the file exists and finishes once it is gone."""
    hold = sbx / "hold"
    hold.write_text("")
    proc = subprocess.Popen(cmd, cwd=sbx / "work", env=clean_env(sbx, {"FAKE_CLAUDE_BG_HOLD": str(hold)}),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    calls_file = sbx / "state" / "calls.jsonl"
    for _ in range(100):  # the call is recorded before the wait
        if calls_file.exists() and calls_file.read_text().strip():
            break
        time.sleep(0.05)
    time.sleep(0.5)
    waiting = proc.poll() is None and not (sbx / "state" / "n").exists()
    hold.unlink()
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        out = "(timed out) " + out
    ck(f"{label}: a launch waits while the hold file exists, then registers the session",
       waiting and out == "backgrounded · 00000001 · held\n" and (sbx / "state" / "n").exists(), f"{waiting} {out!r} {err!r}")


hold_test("the Python fake", [sys.executable, str(FACTORY / "fake_claude.py"), "--bg", "-n", "held", "x"], sandbox(tmp, "hold-py"))
if sys.platform != "win32" and shutil.which("bash") and shutil.which("jq"):
    hold_test("the bash fake", [str(FACTORY / "fake-claude"), "--bg", "-n", "held", "x"], sandbox(tmp, "hold-bash"))
r = subprocess.run([sys.executable, str(FACTORY / "fake_claude.py"), "--version"], env={k: v for k, v in os.environ.items() if k != "FAKE_CLAUDE_STATE"},
                   capture_output=True, text=True)
ck("without FAKE_CLAUDE_STATE it refuses", r.returncode == 1)

# ====================================================================================
# 2. The two fakes side by side (needs bash and jq)
print("== bash fake and Python fake side by side")
if sys.platform == "win32" or not (shutil.which("bash") and shutil.which("jq")):
    print("  skip  no bash or jq (or Windows): the comparison with the bash fake")
else:
    for name, seed, calls_ in CASES + PS_CASES:
        ps = (name, seed, calls_) in PS_CASES
        a, b = sandbox(tmp, "bash-" + name.replace(" ", "_")), sandbox(tmp, "py-" + name.replace(" ", "_"))
        seed(a)
        seed(b)
        same = True
        detail = ""
        for args, extra in calls_:
            ra, rb = run_bash(args, a, extra, ps), run_python(args, b, extra, ps)
            oa, ob = [r.stdout.replace(str(s), "<SB>").replace(os.path.realpath(s), "<SB>")
                      for r, s in ((ra, a), (rb, b))]
            if (ra.returncode, oa) != (rb.returncode, ob):
                same, detail = False, f"{args}: bash {(ra.returncode, oa)} python {(rb.returncode, ob)}"
                break
        if same and tree(a) != tree(b):
            ta, tb = tree(a), tree(b)
            diff = [k for k in sorted(set(ta) | set(tb)) if ta.get(k) != tb.get(k)]
            same, detail = False, f"files differ: {diff}: bash {ta.get(diff[0])!r} python {tb.get(diff[0])!r}"
        ck(f"same output and files: {name}", same, detail)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
