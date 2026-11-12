#!/usr/bin/env python3
"""A stand-in for `claude` (and for `ps`) in the factory tests, in Python so it runs on Windows too.

It reproduces what tests/factory/fake-claude and tests/factory/fake-ps emit and write, argument for
argument, so the parity harness (tests/factory/parity.py) can drive the crew with it on any platform.
tests/test_crew_fake_claude.py holds the two fakes to each other. The bash fakes stay in place: the
bash suites still use them.

    fake_claude.py <claude arguments>      the fake `claude`
    fake_claude.py --fake-ps <ps arguments> the fake `ps`

State lives in $FAKE_CLAUDE_STATE: calls.jsonl (every call: cwd and arguments), agents.json (what
`claude agents --json` lists), n (sessions started), ps/ (the process listing of each session),
plugins.json, and the one-shot hooks and switches the bash fake documents (version, version-flip,
stale-left, spawn-version, on-agents, on-bg, ...). A session started with --bg also gets
$HOME/.claude/jobs/<id>/state.json. The fake only: a file `stamp-version` makes each new session's row
in the list carry `version` (what the installed or spare version is), for a run with no fake `ps` on the PATH.

Session states, as `claude agents --json` shows them (see AGENT_STATES): working (busy in a turn),
waiting (blocked on a person), idle (between turns) and stopped (no process). `agent_row` builds a row
in one of them and `write_agents` puts rows where the fake reads them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The fields `claude agents --json` carries for a session in each state (pid only for a live process).
AGENT_STATES = {
    "working": {"state": "working", "status": "busy", "pid": True},
    "waiting": {"state": "blocked", "status": "waiting", "pid": True},
    "idle": {"state": "working", "status": "idle", "pid": True},
    "stopped": {"state": "stopped", "pid": False},
}

CONTEXT_REPLY = (
    '{"type":"assistant","context_usage":{"model":"claude-opus-5-5","total_tokens":95300,'
    '"raw_max_tokens":200000,"percentage":48,"categories":[{"name":"System prompt","tokens":3000,'
    '"kind":"used"},{"name":"Messages","tokens":57600,"kind":"used"},{"name":"Free space",'
    '"tokens":106400,"kind":"free"},{"name":"Autocompact buffer","tokens":33000,"kind":"buffer"}],'
    '"mcp_tools":[{"name":"mcp__tares__a","server_name":"tares","tokens":120}],'
    '"skills":[{"name":"station-builder","source":"plugin","tokens":800}]}}'
)
USAGE_REPLY = (
    '{"type":"assistant","usage_report":{"rate_limits":{"limits":[{"kind":"session","percent":27,'
    '"resets_at":"2099-01-01T17:00:00+00:00","scope":null,"is_active":true},{"kind":"weekly_all",'
    '"percent":5,"resets_at":"2099-01-05T00:00:00+00:00","scope":null,"is_active":false}],'
    '"extra_usage":{"is_enabled":false}}}}'
)


def agent_row(state: str, id: str, name: str = "", cwd: str = "", **extra) -> dict:
    """One row of `claude agents --json` for a session in `state` (working, waiting, idle or stopped)."""
    spec = AGENT_STATES[state]
    row = {"id": id, "name": name, "cwd": cwd, "state": spec["state"]}
    if "status" in spec:
        row["status"] = spec["status"]
    if spec["pid"]:
        row["pid"] = extra.get("pid", 40000 + int(id[-4:], 16))
    row.update({k: v for k, v in extra.items() if k != "pid"})
    return row


def write_agents(state_dir, rows: list) -> None:
    """Put rows where the fake serves `claude agents --json` from."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "agents.json", rows)


def _dumps(value) -> str:
    """JSON the way jq prints it: two-space indent, UTF-8 left as it is."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def _compact(value) -> str:
    """JSON the way `jq -c` prints it: no spaces; control characters and DEL escaped."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).replace("\x7f", "\\u007f")


def _write_json(path: Path, value) -> None:
    path.write_text(_dumps(value) + "\n", encoding="utf-8")


def _read(path: Path, default: str = "") -> str:
    """A file's first line without its newline (what `cat` and `$(...)` give), or default."""
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return default


def _bash():
    return shutil.which("bash")


def _run_hook(hook: str, env: dict | None = None) -> None:
    """Run a one-shot hook command, as the bash fake does with `bash -c`; its output and failure are ignored.

    On Windows the command goes through cmd.exe (subprocess's shell=True), not bash: the hooks the scenarios
    write there are `"python.exe" "hook.py"`, whose backslashed paths bash would read as escapes, and the
    machine may have no bash, or the WSL one. A hook written in shell syntax stays a POSIX scenario."""
    try:
        if sys.platform == "win32":
            subprocess.run(hook, shell=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            return
        bash = _bash()
        if not bash:
            return
        subprocess.run([bash, "-c", hook], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass


def _bump(path: Path) -> int:
    """Add one to the counter kept in path and return the new value."""
    try:
        n = int(_read(path, "0") or 0) + 1
    except ValueError:
        n = 1
    path.write_text(f"{n}\n", encoding="utf-8")
    return n


def _agents_of(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_active(row: dict) -> bool:
    return "pid" in row or (row.get("state") or "") in ("working", "blocked")


def _cwd() -> str:
    """The logical working directory, as bash's $PWD."""
    pwd = os.environ.get("PWD")
    try:
        if pwd and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def claude(args: list[str]) -> int:
    state = os.environ.get("FAKE_CLAUDE_STATE")
    if not state:
        print("fake-claude: FAKE_CLAUDE_STATE: set FAKE_CLAUDE_STATE", file=sys.stderr)
        return 1
    S = Path(state)
    S.mkdir(parents=True, exist_ok=True)
    agents_file = S / "agents.json"
    if not agents_file.exists():
        agents_file.write_text("[]\n", encoding="utf-8")
    with open(S / "calls.jsonl", "a", encoding="utf-8", newline="\n") as f:
        f.write(_compact({"cwd": _cwd(), "args": list(args)}) + "\n")
    if not args:  # the bash fake's pipeline fails on no arguments: the call is recorded, then it exits 1
        return 1
    with open(S / "last.env", "w", encoding="utf-8", newline="\n") as f:
        for k, v in os.environ.items():
            f.write(f"{k}={v}\n")
    env = os.environ
    cmd = args[0] if args else ""

    def set_agents(fn) -> None:
        _write_json(agents_file, fn(_agents_of(agents_file)))

    if cmd == "--version":
        v = _read(S / "version", "0.0.0")
        if (S / "version-flip").is_file():
            k = int(_read(S / "version-calls", "0") or 0) + 1
            (S / "version-calls").write_text(f"{k}\n", encoding="utf-8")
            if k % 2 == 0:
                v = _read(S / "version-flip")
        print(f"{v} (Claude Code)")
    elif cmd == "agents":
        if env.get("FAKE_CLAUDE_AGENTS_FAIL") == "1":
            print("agents failed (fake)", file=sys.stderr)
            return 1
        if env.get("FAKE_CLAUDE_AGENTS_FAIL_AFTER"):
            n = _bump(S / "agents-reads")
            if n > int(env["FAKE_CLAUDE_AGENTS_FAIL_AFTER"]):
                print("agents failed (fake)", file=sys.stderr)
                return 1
        if (S / "on-agents").is_file():
            k = _bump(S / "on-agents-reads")
            if k >= int(_read(S / "on-agents-at", "1") or 1):
                hook = _read(S / "on-agents")
                (S / "on-agents").unlink(missing_ok=True)
                _run_hook(hook)
        if "--all" in args:
            sys.stdout.write(agents_file.read_text(encoding="utf-8"))
        else:
            print(_dumps([r for r in _agents_of(agents_file) if _is_active(r)]))
    elif cmd == "stop":
        time.sleep(float(env.get("FAKE_CLAUDE_STOP_DELAY") or 0))
        target = args[1]
        if env.get("FAKE_CLAUDE_STOP_KEEPS_STATE") == "1":
            def keep(rows):
                for r in rows:
                    if r.get("id") == target:
                        r.pop("pid", None)
                        r.pop("status", None)
                return rows
            set_agents(keep)
        else:
            def stop(rows):
                for r in rows:
                    if r.get("id") == target:
                        r["state"] = "stopped"
                        r.pop("pid", None)
                return rows
            set_agents(stop)
    elif cmd == "plugin":
        plugins = S / "plugins.json"
        if not plugins.exists():
            plugins.write_text("[]\n", encoding="utf-8")
        sub = args[1] if len(args) > 1 else ""
        if sub == "list":
            if env.get("FAKE_CLAUDE_PLUGIN_FAIL") == "1":
                print("plugin list failed (fake)", file=sys.stderr)
                return 1
            sys.stdout.write(plugins.read_text(encoding="utf-8"))
        elif sub == "install":
            rows = json.loads(plugins.read_text(encoding="utf-8"))
            rows.append({"id": args[2], "version": "1.0.0", "scope": "user", "enabled": True})
            _write_json(plugins, rows)
    elif cmd == "rm":
        set_agents(lambda rows: [r for r in rows if r.get("id") != args[1]])
    elif cmd == "-p":
        prev = ""
        for a in args:
            if prev == "--settings":
                if os.path.isfile(a):
                    text = Path(a).read_text(encoding="utf-8")
                else:
                    text = a
                (S / "p-settings.json").write_text(text, encoding="utf-8")
            prev = a
        print('{"type":"system","subtype":"init"}')
        if "/context" in args:
            print(CONTEXT_REPLY)
        elif "/usage" in args and not env.get("FAKE_CLAUDE_NO_PLAN"):
            print(USAGE_REPLY)
        print('{"type":"result","subtype":"success"}')
    elif cmd == "--bg":
        return _bg(S, args, set_agents)
    sys.stdout.flush()
    return 0


def _bg(S: Path, args: list[str], set_agents) -> int:
    env = os.environ
    if env.get("FAKE_CLAUDE_FAIL_RESUME") == "1" and "--resume" in args:
        print("resume failed (fake)")
        return 1
    if env.get("FAKE_CLAUDE_FAIL_FRESH") == "1" and "--resume" not in args:
        print("new conversation failed (fake)")
        return 1
    time.sleep(float(env.get("FAKE_CLAUDE_BG_DELAY") or 0))  # Claude Code takes a moment to register
    hold = env.get("FAKE_CLAUDE_BG_HOLD")  # a file: the launch waits while it exists (two minutes at most)
    if hold:
        for _ in range(1200):
            if not os.path.exists(hold):
                break
            time.sleep(0.1)
    name = ""
    prev = ""
    for a in args:
        if prev == "-n":
            name = a
        prev = a
    n = _bump(S / "n")
    id = f"{n:08x}"
    job = Path(env.get("HOME") or Path.home()) / ".claude" / "jobs" / id
    job.mkdir(parents=True, exist_ok=True)
    _write_json(job / "state.json", {"sessionId": f"session-{n}"})
    cwd = _cwd()
    # The version the session's process runs: the installed one, or an old spare's (see stale-left).
    v = _read(S / "version", "0.0.0")
    stale = _read(S / "stale-left", "0")
    left = int(stale) if stale.lstrip("-").isdigit() else 0
    if stale == "all" or left > 0:
        v = _read(S / "spawn-version")
        if stale != "all":
            (S / "stale-left").write_text(f"{left - 1}\n", encoding="utf-8")
    row = {"id": id, "name": name, "cwd": cwd, "state": "working",
           "sessionId": f"session-{n}", "startedAt": n, "pid": 40000 + n}
    if (S / "stamp-version").is_file():   # opt in: the list gives the version, where the fake `ps` is not on the PATH
        row["version"] = v
    set_agents(lambda rows: rows + [row])
    # The process listing of the session, as `ps` shows it (see the bash fake for the shape).
    pid = 40000 + n
    ps = S / "ps"
    ps.mkdir(parents=True, exist_ok=True)
    (ps / f"{pid}.ppid").write_text(f"{pid + 1000}\n", encoding="utf-8")
    (ps / f"{pid + 1000}.command").write_text(
        f"claude bg-pty-host --bg-pty-host /tmp/cc/spare/{id}.pty.sock 200 50 -- "
        f"/home/u/.local/share/claude/versions/{v} --bg-spare /tmp/cc/spare/{id}.claim.sock\n",
        encoding="utf-8")
    (ps / f"{pid}.command").write_text("claude bg-spare\n", encoding="utf-8")
    # $S/on-bg holds a command to run once after a launch, with the new session's id in $FAKE_ID.
    if (S / "on-bg").is_file():
        hook = _read(S / "on-bg")
        (S / "on-bg").unlink(missing_ok=True)
        _run_hook(hook, {**env, "FAKE_ID": id})
    print(f"backgrounded · {id} · {name}")
    sys.stdout.flush()
    return 0


def ps(args: list[str]) -> int:
    """The fake `ps`: `ps -o <field>= -p <pid>` from $FAKE_CLAUDE_STATE/ps/<pid>.<field>; `ps -axo ...`
    from ps-list (empty when a test wrote none); any other call goes to the real ps."""
    state = os.environ.get("FAKE_CLAUDE_STATE", "")
    if args and args[0] == "-axo" and state:
        try:
            sys.stdout.write((Path(state) / "ps-list").read_text(encoding="utf-8"))
        except OSError:
            pass
        sys.stdout.flush()
        return 0
    field = pid = prev = ""
    for a in args:
        if prev == "-o":
            field = a[:-1] if a.endswith("=") else a
        elif prev == "-p":
            pid = a
        prev = a
    if state and field and pid:
        f = Path(state) / "ps" / f"{pid}.{field}"
        if f.is_file():
            sys.stdout.write(f.read_text(encoding="utf-8"))
            sys.stdout.flush()
            return 0
    real = shutil.which("ps", path="/usr/bin:/bin")
    if not real:
        print("ps: not available here", file=sys.stderr)
        return 127
    return subprocess.call([real, *args])


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace", newline="\n")
            except (OSError, ValueError, TypeError):
                pass
    if argv and argv[0] == "--fake-ps":
        return ps(argv[1:])
    return claude(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
