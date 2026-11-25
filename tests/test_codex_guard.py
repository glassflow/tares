"""The factory guard as a Codex station runs it (`tares hook codex-guard`, tares/crew/codex_guard.py, TR-1594).

Codex hands a PreToolUse hook the payload Claude Code does, so the rules are the plugin's guard.py, asked
through the adapter. This runs the adapter the way a Codex station starts it (`tares hook codex-guard
--plugin-root <kit>`, the payload on stdin, the station's variables in the environment), with payloads shaped
as Codex 0.171.0 sends them (TR-1576: `session_id`, `turn_id`, `tool_use_id`, `cwd`).

 a. every case of tests/guard_cases.jsonl (155) and tests/guard_extra_cases.jsonl (27) gets the decision the
    file expects, which is guard.py's; a deny is guard.py's own answer, byte for byte;
 b. a command let through is answered with the same command, its standard input closed; what that does to a
    command: a shell that waits for input ends at once, a pipe, a here-document and an exit code are untouched;
 c. the record: one line per run (the start of a turn, a call let through, a call refused), the owner's alone;
 d. failing shut: whatever stops the guard from running blocks the call (exit 2), where 1 would let it
    through; a session that is not a station's is never touched;
 e. a halted crew, the events log, and the folder the call runs in reach the rules as they do on Claude Code;
 f. `asked` and `input_closed`, for whoever stores or compares a station's commands;
 g. the adapter is light: it loads nothing heavy, and starts within the limit a hook has.

The same guard against the real Codex, by hand: tests/factory/codex_probe/probe_guard.py.

Run: python tests/test_codex_guard.py
"""
import io
import json
import os
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tares.crew import codex_guard as cg  # noqa: E402

KIT = ROOT / "claude-plugin"
GUARD = KIT / "hooks" / "guard.py"
CASES = ROOT / "tests" / "guard_cases.jsonl"
EXTRA = ROOT / "tests" / "guard_extra_cases.jsonl"
SHIM = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
LIMIT_MS = 400   # tests/test_hook_start.py's limit for `tares hook`
P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


TMP = Path(tempfile.mkdtemp(prefix="codex_guard_"))
BASE = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_", "CODEX_"))}
BASE.update(HOME=str(TMP), USERPROFILE=str(TMP), PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
_n = [0]


def crew_home() -> Path:
    _n[0] += 1
    home = TMP / f"crew{_n[0]}"
    (home / "stations").mkdir(parents=True)
    return home


def payload(tool, command=None, event="PreToolUse", **more):
    """A hook payload as Codex 0.171.0 sends it."""
    _n[0] += 1
    doc = {"session_id": "01a2-thread", "turn_id": "01a2-turn", "cwd": str(TMP), "hook_event_name": event,
           "model": "gpt-5.6-luna", "permission_mode": "default", "transcript_path": str(TMP / "rollout.jsonl")}
    if event == "PreToolUse":
        doc.update(tool_name=tool, tool_use_id=f"exec-{_n[0]:04d}", tool_input={} if command is None else {"command": command})
    else:
        doc["prompt"] = "the next task"
    doc.update(more)
    return doc


def hook(doc, kit=KIT, **env):
    """Run `tares hook codex-guard --plugin-root <kit>` with the payload (a dict, or text) on stdin."""
    data = doc if isinstance(doc, (bytes, str)) else json.dumps(doc)
    if isinstance(data, str):
        data = data.encode("utf-8")
    e = dict(BASE)
    e.update({k: v for k, v in env.items() if v is not None})
    return subprocess.run([sys.executable, "-c", SHIM, "hook", "codex-guard", "--plugin-root", str(kit)],
                          input=data, capture_output=True, env=e, cwd=str(TMP))


def plain(doc, **env):
    """guard.py itself, as Claude Code runs it."""
    e = dict(BASE)
    e.update({k: v for k, v in env.items() if v is not None})
    return subprocess.run([sys.executable, str(GUARD)], input=json.dumps(doc).encode("utf-8"), capture_output=True,
                          env=e, cwd=str(TMP))


def answer(r) -> dict:
    try:
        return json.loads(r.stdout.decode("utf-8")).get("hookSpecificOutput") or {}
    except ValueError:
        return {}


def record(home, station="test") -> list:
    return cg.read_record(cg.record_file(str(home), station))


# ---------------------------------------------------------------------------------------------
print("a. the guard's cases, through the adapter with a Codex payload")
home = crew_home()
for name, file, count in (("guard_cases.jsonl", CASES, 155), ("guard_extra_cases.jsonl", EXTRA, 27)):
    cases = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
    ck(f"{name} has {count} cases", len(cases) == count, len(cases))
    bad = unlike = unclosed = 0
    for c in cases:
        env = {"FACTORY_ROLE": c["role"], "FACTORY_HOME": str(home), "FACTORY_STATION": "test"}
        if c.get("parent"):
            env["FACTORY_PARENT"] = c["parent"]
        doc = payload(c["tool"], c["command"])
        r = hook(doc, **env)
        said = answer(r)
        got = "deny" if said.get("permissionDecision") == "deny" else "allow"
        if got != c["expect"] or r.returncode != 0 or r.stderr:
            bad += 1
            print(f"  FAIL {c['role']} | {c['command']!r} | expected {c['expect']}, got {got} (exit {r.returncode}) {r.stderr[-200:]!r}")
        if got == "deny" and r.stdout != plain(doc, **env).stdout:
            unlike += 1
        want = cg.CLOSE_STDIN + c["command"].rstrip("\n")
        if got == "allow" and c["tool"] == "Bash" and c["command"].strip("\n") and (said.get("updatedInput") or {}).get("command") != want:
            unclosed += 1
    ck(f"all {len(cases)} decide as the file expects, with exit 0 and nothing on stderr", bad == 0, f"{bad} differ")
    ck("each deny is guard.py's own answer, byte for byte", unlike == 0, f"{unlike} differ")
    ck("each command let through is answered with its input closed", unclosed == 0, f"{unclosed} are not")
lines = record(home)
ck("every run left one line in the station's record", len(lines) == 182 and {x.get("said") for x in lines} == {"allow", "deny"},
   (len(lines), {x.get("said") for x in lines}))

# ---------------------------------------------------------------------------------------------
print("b. a command let through runs with its standard input closed")
home = crew_home()
ENV = {"FACTORY_ROLE": "builder", "FACTORY_HOME": str(home), "FACTORY_STATION": "test"}
r = hook(payload("Bash", "git status"), **ENV)
ck("the answer is an allow that carries the command with one line in front",
   answer(r) == {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                 "updatedInput": {"command": "exec </dev/null\ngit status"}} and r.returncode == 0, r.stdout)
r = hook(payload("Bash", "exec </dev/null\ngit status"), **ENV)
ck("a command that already carries the line is let through as it is", r.stdout == b"" and r.returncode == 0, r.stdout)
r = hook(payload("mcp__tares__desk_ask"), **ENV)
ck("an MCP call let through gets no answer: there is nothing to change", r.stdout == b"" and r.returncode == 0, r.stdout)
r = hook(payload("apply_patch"), **ENV)
ck("so does a tool that is neither a command nor an MCP call", r.stdout == b"" and r.returncode == 0, r.stdout)
r = hook(payload("Bash", ""), **ENV)
ck("and a command with no text", r.stdout == b"" and r.returncode == 0, r.stdout)


def as_station(command, **kw):
    """Run a command the way a station does once the guard let it through: the changed text, in a shell whose
    own input is an open pipe nobody writes to (a command could wait on it for ever)."""
    said = answer(hook(payload("Bash", command), **ENV))
    reader, writer = os.pipe()
    try:
        return subprocess.run(["/bin/sh", "-c", said["updatedInput"]["command"]], stdin=reader, capture_output=True,
                              timeout=10, cwd=str(TMP), **kw)
    finally:
        os.close(reader)
        os.close(writer)


t = time.monotonic()
r = as_station("cat")
ck("a command that waits for input ends at once, with nothing read",
   r.returncode == 0 and r.stdout == b"" and time.monotonic() - t < 5, (r.returncode, r.stdout, time.monotonic() - t))
r = as_station("sh")
ck("so does a shell started to be typed into later", r.returncode == 0 and r.stdout == b"", (r.returncode, r.stdout))
r = as_station("printf 'a\\nb\\n' | wc -l")
ck("a pipe inside the command still works", r.stdout.strip() == b"2", r.stdout)
r = as_station("cat <<'EOF'\nfrom a here-document\nEOF")
ck("a here-document still works", r.stdout == b"from a here-document\n", r.stdout)
r = as_station("exit 7")
ck("the exit code is the command's", r.returncode == 7, r.returncode)
r = as_station("echo one\necho two")
ck("a command of several lines runs all of them", r.stdout == b"one\ntwo\n", r.stdout)

# ---------------------------------------------------------------------------------------------
print("c. the record")
home = crew_home()
ENV = {"FACTORY_ROLE": "reviewer", "FACTORY_HOME": str(home), "FACTORY_STATION": "crew-reviewer"}
r = hook(payload("", event="UserPromptSubmit", turn_id="turn-1"), **ENV)
ck("the start of a turn: no answer, exit 0", r.returncode == 0 and r.stdout == b"" and r.stderr == b"", (r.returncode, r.stdout, r.stderr))
hook(payload("Bash", "ls", turn_id="turn-1", tool_use_id="exec-aaa"), **ENV)
hook(payload("Bash", "gh pr merge 3", turn_id="turn-1", tool_use_id="exec-bbb"), **ENV)
hook(payload("mcp__slack__slack_send_message", turn_id="turn-1", tool_use_id="exec-ccc"), **ENV)
file = Path(cg.record_file(str(home), "crew-reviewer"))
lines = record(home, "crew-reviewer")
ck("it is stations/<station>.codex.guard in the crew home, the owner's alone",
   file == home / "stations" / "crew-reviewer.codex.guard" and stat.S_IMODE(file.stat().st_mode) == 0o600, file)
ck("one line each: the turn's start, the call let through, the two refused, with the turn and the call's id",
   [{k: v for k, v in x.items() if k != "at"} for x in lines] == [
       {"turn": "turn-1", "armed": True},
       {"turn": "turn-1", "call": "exec-aaa", "tool": "Bash", "said": "allow"},
       {"turn": "turn-1", "call": "exec-bbb", "tool": "Bash", "said": "deny"},
       {"turn": "turn-1", "call": "exec-ccc", "tool": "mcp__slack__slack_send_message", "said": "deny"}], lines)
ck("each line says when", all(abs(x["at"] - time.time()) < 60 for x in lines), lines)
ck("no line holds the command or the prompt", "gh pr merge" not in file.read_text() and "next task" not in file.read_text())
with open(file, "a", encoding="utf-8") as f:
    f.write("not json\n[1, 2]\n\n")
ck("a line that is not one is passed over when the record is read", len(record(home, "crew-reviewer")) == 4)
ck("a record that is not there reads as empty", cg.read_record(home / "stations" / "nobody.codex.guard") == [])

# ---------------------------------------------------------------------------------------------
print("d. failing shut")
r = hook(payload("Bash", "ls"), FACTORY_ROLE="builder", FACTORY_HOME=str(TMP / "no-such-crew"), FACTORY_STATION="test")
ck("a record that cannot be written blocks the call: exit 2, the reason on stderr, no answer",
   r.returncode == 2 and r.stdout == b"" and b"the guard could not run" in r.stderr and b"the call is blocked" in r.stderr,
   (r.returncode, r.stdout, r.stderr))
r = hook(payload("", event="UserPromptSubmit"), FACTORY_ROLE="builder", FACTORY_HOME=str(TMP / "no-such-crew"), FACTORY_STATION="test")
ck("and blocks the start of a turn", r.returncode == 2, (r.returncode, r.stderr))
home = crew_home()
ENV = {"FACTORY_ROLE": "builder", "FACTORY_HOME": str(home), "FACTORY_STATION": "test"}
r = hook(payload("Bash", "ls"), FACTORY_ROLE="builder", FACTORY_HOME=str(home), FACTORY_STATION="")
ck("a station without a name blocks the call", r.returncode == 2, (r.returncode, r.stderr))
empty_kit = TMP / "kit-without-guard"
(empty_kit / "hooks").mkdir(parents=True)
r = hook(payload("Bash", "ls"), kit=empty_kit, **ENV)
ck("a kit without its guard blocks the call (the Claude Code guard's exit there is 1, which Codex would let through)",
   r.returncode == 2 and b"no script" in r.stderr, (r.returncode, r.stderr))
broken = TMP / "kit-broken"
(broken / "hooks").mkdir(parents=True)
(broken / "hooks" / "guard.py").write_text("raise RuntimeError('rules that do not load')\n")
r = hook(payload("Bash", "ls"), kit=broken, **ENV)
ck("rules that do not load block the call", r.returncode == 2 and b"rules that do not load" in r.stderr, (r.returncode, r.stderr))
r = hook(payload("Bash", "gh pr merge 1"), FACTORY_HOME=str(home), FACTORY_STATION="test")
ck("a session without FACTORY_ROLE is never touched: no answer, exit 0, no line",
   r.returncode == 0 and r.stdout == b"" and record(home) == [], (r.returncode, r.stdout, record(home)))
r = hook("not json at all", **ENV)
ck("a payload that is not JSON is let through, as guard.py lets it", r.returncode == 0 and r.stdout == b"", (r.returncode, r.stdout, r.stderr))

# ---------------------------------------------------------------------------------------------
print("e. what reaches the rules")
home = crew_home()
ENV = {"FACTORY_ROLE": "orchestrator", "FACTORY_HOME": str(home), "FACTORY_STATION": "crew-orchestrator"}
(home / "HALT").write_text('{"reason":"test"}')
r = hook(payload("Bash", "git push origin HEAD"), **ENV)
ck("a halted crew: a push is refused with the halt's reason",
   answer(r).get("permissionDecision") == "deny" and "crew is halted (test)" in answer(r).get("permissionDecisionReason", ""), r.stdout)
r = hook(payload("Bash", "ls"), **ENV)
ck("a halted crew: a harmless command still passes, its input closed", answer(r).get("permissionDecision") == "allow", r.stdout)
events = [json.loads(x) for x in (home / "events.jsonl").read_text().splitlines()]
ck("the refusal is in the crew's events, as on Claude Code",
   len(events) == 1 and events[0]["event"] == "guard-deny" and events[0]["station"] == "crew-orchestrator"
   and events[0]["tool"] == "Bash", events)
stub = TMP / "kit-stub"
(stub / "hooks").mkdir(parents=True)
(stub / "hooks" / "guard.py").write_text(GUARD.read_text(encoding="utf-8") + '''

def decide(env, tool, cmd, cwd=""):   # the stub: write down what it was asked
    with open(env["STUB_SEEN"], "w", encoding="utf-8") as f:
        json.dump({"tool": tool, "cmd": cmd, "cwd": cwd, "role": env.get("FACTORY_ROLE")}, f)
''', encoding="utf-8")
seen = TMP / "seen.json"
out = io.BytesIO()
code = cg.run(stub / "hooks" / "guard.py", {**ENV, "STUB_SEEN": str(seen)},
              json.dumps(payload("Bash", "git tag v1 abc1234\n", cwd="/work/alpha")), out)
ck("the rules are asked with the tool, the command and the folder the call runs in",
   code == 0 and json.loads(seen.read_text()) == {"tool": "Bash", "cmd": "git tag v1 abc1234", "cwd": "/work/alpha",
                                                 "role": "orchestrator"}, seen.read_text())

# ---------------------------------------------------------------------------------------------
print("f. a stored command, without the added line")
ck("`asked` takes the line off the bare text", cg.asked("exec </dev/null\ngit status") == "git status")
ck("and gives the bare text of the form Codex stores", cg.asked("/bin/zsh -lc 'exec </dev/null\ngit status'") == "git status")
ck("a command without the line comes back as it is", cg.asked("/bin/zsh -lc 'git status'") == "/bin/zsh -lc 'git status'"
   and cg.asked("git status") == "git status" and cg.asked(None) == "")
ck("a command that only mentions the line is not changed", cg.asked("echo 'exec </dev/null'") == "echo 'exec </dev/null'")
ck("`input_closed`: the stored command carries the line",
   cg.input_closed({"command": "/bin/zsh -lc 'exec </dev/null\nsleep 5'"})
   and cg.input_closed({"command": "/bin/bash -lc \"exec </dev/null\nsleep 5\""})
   and cg.input_closed({"command": "exec </dev/null\nsleep 5"})
   and cg.input_closed({"command": "/bin/zsh -lc 'unbalanced", "commandActions": [{"type": "unknown", "command": "exec </dev/null\nx"}]}))
ck("`input_closed`: one that does not, or mentions it elsewhere, is not taken for closed",
   not cg.input_closed({"command": "/bin/zsh -lc 'sleep 5'"}) and not cg.input_closed({})
   and not cg.input_closed({"command": "/bin/zsh -lc 'echo hi\nexec </dev/null\n'"}) and not cg.input_closed(None))

# ---------------------------------------------------------------------------------------------
print("g. the adapter is light")
HEAVY = ["duckdb", "fastapi", "starlette", "uvicorn", "yaml", "httpx", "pydantic", "psutil", "keyring",
         "tares.daemon", "tares.config", "tares.store", "tares.crew.codex", "tares.crew.codex_station", "tares.crew.launch"]
code = ("import sys\nimport tares.crew.codex_guard\n"
        f"print(' '.join(m for m in {HEAVY!r} if m in sys.modules))")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=BASE, cwd=str(TMP))
ck("importing it loads nothing heavy, and none of the code that starts or visits a station", r.returncode == 0 and r.stdout.strip() == "",
   (r.returncode, r.stdout.strip(), r.stderr[-200:]))
home = crew_home()
ENV = {"FACTORY_ROLE": "builder", "FACTORY_HOME": str(home), "FACTORY_STATION": "test"}
times = []
for _ in range(7):
    t = time.perf_counter()
    r = hook(payload("Bash", "git status"), **ENV)
    times.append((time.perf_counter() - t) * 1000)
med = statistics.median(times)
print(f"  `tares hook codex-guard`: median {med:.0f} ms over {len(times)} runs (min {min(times):.0f}, max {max(times):.0f}); limit {LIMIT_MS} ms")
ck(f"one run, start to answer, is under {LIMIT_MS} ms (it was {med:.0f} ms)", r.returncode == 0 and med < LIMIT_MS, med)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
