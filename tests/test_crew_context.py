"""`tares factory context <role>` reads what a station's own launch loads (tares/crew/person.py).

The reading goes through a fake claude that records its arguments. This checks that the reading is started with
the flags the station's launch passes (the setting sources, the browser extension, the plugin) and none of its
model or prompt flags, that the summary line counts what loaded (tokens before messages, tools per server,
skills, memory), that no setting, header, path or tool name is printed, and that a launch Tares will not give
still reads with the lean flags.

Run: .venv/bin/python tests/test_crew_context.py
"""
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

_crew_guard.scratch_home()   # the probes below inherit this process's HOME: not the real one
from _crew_fake import fake_crew_env  # noqa: E402
from parity import FakeTares  # noqa: E402
from tares.crew import person  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


T = Path(tempfile.mkdtemp(prefix="tares crew context "))
SECRET = "s3cret-header-value"
READING = {"type": "assistant", "context_usage": {
    "model": "claude-opus-5-5", "total_tokens": 41000, "raw_max_tokens": 200000, "percentage": 20,
    "categories": [{"name": "System prompt", "tokens": 3000, "kind": "used"},
                   {"name": "Messages", "tokens": 9000, "kind": "used"}],
    "mcp_tools": [{"name": "mcp__tares__a", "server_name": "tares", "tokens": 100},
                  {"name": "mcp__tares__b", "server_name": "tares", "tokens": 100},
                  {"name": f"mcp__slack__{SECRET}", "server_name": "slack", "tokens": 100}],
    "memory_files": [{"path": f"/home/u/.claude/projects/{SECRET}/memory/MEMORY.md", "type": "AutoMem", "tokens": 400}],
    "skills": [{"name": "a", "source": "plugin", "tokens": 10}, {"name": "b", "source": "plugin", "tokens": 10}]}}


def run(env, *argv, fake, **extra):
    e = {**env, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", "TARESD_URL": fake.url, **extra}
    return subprocess.run([sys.executable, "-m", "tares.crew", *argv], cwd=str(ROOT), env=e, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def station(root, name):
    """A crew home with one orchestrator station, its settings file holding a secret, and the folder it works in."""
    env = fake_crew_env(root)
    crew = Path(env["FACTORY_CREW_HOME"])
    work = root / "work"
    work.mkdir(exist_ok=True)
    settings = crew / "stations" / f"{name}.settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"env": {"TARES_AUTH_TOKEN": SECRET}, "statusLine": {"type": "command"}}),
                        encoding="utf-8")
    (crew / "stations.json").write_text(json.dumps({name: {
        "role": "orchestrator", "id": "deadbeef", "sessionId": "sess-1", "cwd": str(work),
        "settings": str(settings)}}), encoding="utf-8")
    return env, crew


def context_call(env):
    calls = (Path(env["FAKE_CLAUDE_STATE"]) / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    mine = [json.loads(c) for c in calls if "/context" in json.loads(c)["args"]]
    return mine[-1]["args"] if mine else []


def after(args, flag):
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


try:
    # --- the small functions
    ck("load flags: only the ones that decide what loads, with their values",
       person._load_flags(["--bg", "-n", "x", "--model", "opus", "--settings", "f.json", "--setting-sources",
                           "project,local", "--no-chrome", "--append-system-prompt", "words", "--mcp-config", "m.json",
                           "--strict-mcp-config", "--plugin-dir", "/kit"])
       == ["--setting-sources", "project,local", "--no-chrome", "--mcp-config", "m.json", "--strict-mcp-config",
           "--plugin-dir", "/kit"])
    ck("load flags: a flag with no value is dropped", person._load_flags(["--plugin-dir"]) == [])
    ck("summary: an empty reading counts nothing",
       person.launch_summary({}) == "launch: 0 tokens before messages; tools by server: none; skills: 0; memory: off")
    ck("summary: junk entries are ignored",
       person.launch_summary({"total_tokens": "x", "mcp_tools": [1, None], "skills": ["a"], "memory_files": [3],
                              "categories": [None]}).endswith("tools by server: none; skills: 0; memory: off"))
    ck("summary: memory counts only the auto-memory source",
       "memory: off" in person.launch_summary({"memory_files": [{"type": "Project", "path": "CLAUDE.md"}]})
       and "memory: on" in person.launch_summary({"memory_files": [{"type": "AutoMem"}]}))

    # --- the stock reading: flags, first line, summary line
    env, crew = station(T / "a", "crew-orchestrator")
    fake = FakeTares({"POST /ingest/claude_code": (200, {"ok": True})})
    try:
        r = run(env, "context", "orchestrator", fake=fake)
        out = r.stdout.splitlines()
        ck("context: the first line is as before", r.returncode == 0 and out[:1] == [
            "crew-orchestrator: 95300 of 200000 tokens (48%), sent to Tares"], r.stdout + r.stderr)
        ck("context: a summary line follows", out[1:] == [
            "launch: 37700 tokens before messages; tools by server: tares 1; skills: 1; memory: off"], r.stdout)
        a = context_call(env)
        ck("context: the setting sources of the launch", after(a, "--setting-sources") == "project,local", str(a))
        ck("context: the browser extension off, as in the launch", "--no-chrome" in a, str(a))
        ck("context: the plugin of the launch", bool(after(a, "--plugin-dir")), str(a))
        ck("context: none of the launch's model, prompt or session flags",
           not any(x in a for x in ("--bg", "--model", "--effort", "--append-system-prompt", "--permission-mode",
                                    "--autocompact", "-n")), str(a))
        ck("context: still read through an unsaved copy of the session",
           after(a, "--resume") == "sess-1" and "--fork-session" in a and "--no-session-persistence" in a, str(a))
        ck("context: the flags come before --resume",
           a.index("--setting-sources") < a.index("--resume") and a.index("--plugin-dir") < a.index("--resume"), str(a))
        ck("context: the reading's settings keep hooks off", json.loads(
            (Path(env["FAKE_CLAUDE_STATE"]) / "p-settings.json").read_text(encoding="utf-8")).get("disableAllHooks") is True)
        ck("context: no setting value is printed", SECRET not in r.stdout + r.stderr)
    finally:
        fake.stop()

    # --- a reading with several servers, skills and memory on: counts and server names only
    env, crew = station(T / "b", "crew-orchestrator")
    reading = T / "b" / "reading.json"
    reading.write_text(json.dumps(READING), encoding="utf-8")
    fake = FakeTares({"POST /ingest/claude_code": (200, {"ok": True})})
    try:
        r = run(env, "context", "orchestrator", fake=fake, FAKE_CLAUDE_CONTEXT_FILE=str(reading))
        out = r.stdout.splitlines()
        ck("context: tokens before messages, tools per server, skills and memory",
           out[1:] == ["launch: 32000 tokens before messages; tools by server: slack 1, tares 2; skills: 2; memory: on"],
           r.stdout + r.stderr)
        ck("context: no tool name, path or setting value is printed", SECRET not in r.stdout + r.stderr
           and "mcp__" not in r.stdout and "MEMORY.md" not in r.stdout, r.stdout)
        sent = json.loads(fake.asked("POST /ingest/claude_code")[0]["body"]) if fake.asked("POST /ingest/claude_code") else {}
        ck("context: the whole reading still goes to Tares", sent.get("context_usage", {}).get("total_tokens") == 41000, str(sent)[:200])
    finally:
        fake.stop()

    # --- a launch Tares will not give, with no saved copy: the lean flags and the plugin still apply
    env, crew = station(T / "c", "crew-orchestrator")
    fake = FakeTares({"POST /ingest/claude_code": (200, {"ok": True}),
                      "GET /api/factory/stations/crew-orchestrator/launch": (500, {"detail": "down"})})
    try:
        r = run(env, "context", "orchestrator", fake=fake)
        a = context_call(env)
        ck("no launch: the reading still happens", r.returncode == 0 and "launch: " in r.stdout, r.stdout + r.stderr)
        ck("no launch: the lean flags and the plugin",
           after(a, "--setting-sources") == "project,local" and "--no-chrome" in a and bool(after(a, "--plugin-dir")), str(a))
    finally:
        fake.stop()
finally:
    shutil.rmtree(T, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
