"""The factory guard as a Codex PreToolUse hook: a thin adapter over claude-plugin/hooks/guard.py (TR-1576).

Codex hands a PreToolUse hook the same payload Claude Code does (`tool_name` "Bash" or `mcp__server__tool`,
`tool_input.command`) and takes the same deny answer, so the rules and their decision are guard.py's own,
unchanged. The adapter adds one thing Codex needs and Claude Code does not:

Codex can start a command and type into it later (`write_stdin`), and what it types is not shown to any hook.
A station could start `bash` and then type `git push` past a halt. So a command the guard lets through is
rewritten to run with its standard input closed (`exec </dev/null` first): a shell that waits for input ends
at once, and nothing typed later reaches anything. A station then has what a Claude Code station has, one
checked command per call.

Reads the hook payload on stdin; prints a deny decision, or an allow decision with the rewritten command, or
nothing. Standard library only: a hook starts at every tool call.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.environ.get("TARES_GUARD_PY") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "claude-plugin", "hooks", "guard.py")
CLOSE_STDIN = "exec </dev/null\n"


def load_guard():
    spec = importlib.util.spec_from_file_location("tares_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    env = os.environ
    if not env.get("FACTORY_ROLE"):
        return 0
    guard = load_guard()
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    tool, cmd = guard.parse(raw)
    try:
        guard.decide(env, tool, cmd)
    except guard.Deny as d:
        reason = str(d)
        sys.stdout.buffer.write(guard.deny_output(reason))
        sys.stdout.buffer.flush()
        guard.log_deny(env, tool, reason)
        return 0
    if tool == "Bash" and cmd and not cmd.startswith(CLOSE_STDIN):
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                      "updatedInput": {"command": CLOSE_STDIN + cmd}}}
        sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
