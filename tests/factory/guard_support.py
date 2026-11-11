#!/usr/bin/env python3
"""Support for the `guard` scenario of parity.py: the guard cases in one process.

    guard_support.py

Each case runs `tares hook guard --plugin-root <kit>` (the way hooks.json starts the hook) and checks
whether the answer is a deny. This driver calls tares.crew.hooks.run(["guard", "--plugin-root", <kit>]) for
every case in one process, which keeps the wiring (name, plugin root, the script it runs) in the test while
taking about a second instead of a minute.

The cases are tests/guard_cases.jsonl. It then runs: the cases, the console grants lifting a one-owner rule,
the halted crew refusing a new session however the CLI is spelled, and a session without FACTORY_ROLE being
left alone. It prints a FAIL line per case that decided otherwise, and `guard: all cases pass` when none did.
Exit 0 when all pass.

Standard library and tares.crew.hooks.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CASES = ROOT / "tests" / "guard_cases.jsonl"
KIT = ROOT / "claude-plugin"
ENV_KEYS = ("FACTORY_ROLE", "FACTORY_PARENT", "FACTORY_HOME", "FACTORY_STATION")
HALTED = ("factory spawn x", "/abs/bin/factory spawn x", "/abs/bin/factory revive x", "factory spawn;ls",
          "(factory revive x)")
GRANTED = (("gh pr merge 426 -R o/r --squash", "allow"), ("gh pr merge  426 -R o/r   --squash", "allow"),
           ("gh pr merge 427 -R o/r --squash", "deny"), ("gh pr merge 426 -R o/r --squash && git push", "deny"),
           ("gh pr merge 7", "deny"), ("gh pr merge 9", "deny"))


class _Std:
    """A stand-in for stdin or stdout with the .buffer the guard reads and writes."""

    def __init__(self, data: bytes = b""):
        self.buffer = io.BytesIO(data)


def guard(payload: dict, **env) -> str:
    """The text `tares hook guard --plugin-root <kit>` prints for this payload under this environment."""
    from tares.crew import hooks
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _Std(json.dumps(payload).encode("utf-8")), _Std()
    try:
        hooks.run(["guard", "--plugin-root", str(KIT)])
        return sys.stdout.buffer.getvalue().decode("utf-8", errors="replace")
    finally:
        sys.stdin, sys.stdout = old_in, old_out
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def command_payload(command: str, tool: str = "Bash") -> dict:
    return {"tool_name": tool, "tool_input": {"command": command}}


def main() -> int:
    sys.path.insert(0, str(ROOT))
    fail = 0
    cases = [json.loads(x) for x in CASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    for c in cases:
        env = {"FACTORY_ROLE": c["role"], "FACTORY_HOME": "/nonexistent", "FACTORY_STATION": "test"}
        if c["role"] == "helper":
            env["FACTORY_PARENT"] = c["parent"]
        got = "deny" if '"deny"' in guard(command_payload(c["command"], c["tool"]), **env) else "allow"
        if got != c["expect"]:
            who = f"helper of {c['parent']}" if c["role"] == "helper" else c["role"]
            print(f"FAIL {who} | {c['command']} | expected {c['expect']}, got {got}")
            fail = 1
    # A console grant for the exact command lifts a one-owner rule (TR-644).
    with tempfile.TemporaryDirectory() as home:
        Path(home, "stations").mkdir()
        Path(home, "stations", "crew-releaser.grants").write_text(
            '- "The releaser may run `gh pr merge 426 -R o/r --squash` in every project" (2026-10-21)\n'
            '- "The reviewer may run `gh pr merge 7` in every project" (2026-10-21)\n'
            '- "Builders may merge anything once CI is green" (2026-10-21)\n', encoding="utf-8")
        for cmd, want in GRANTED:
            got = "deny" if '"deny"' in guard(command_payload(cmd), FACTORY_HOME=home, FACTORY_STATION="crew-releaser",
                                              FACTORY_ROLE="releaser") else "allow"
            if got != want:
                print(f"FAIL grant | {cmd} | expected {want}, got {got}")
                fail = 1
    # A halted crew refuses a new session however the CLI is spelled.
    with tempfile.TemporaryDirectory() as halt:
        Path(halt, "HALT").write_text('{"reason":"test"}\n', encoding="utf-8")
        for cmd in HALTED:
            if "crew is halted" not in guard(command_payload(cmd), FACTORY_ROLE="orchestrator", FACTORY_HOME=halt):
                print(f"FAIL halted | {cmd} | expected the halt to refuse it")
                fail = 1
    # A session without FACTORY_ROLE is not touched.
    if guard(command_payload("gh pr merge 1")):
        print("FAIL a session without FACTORY_ROLE was touched")
        fail = 1
    if not fail:
        print("guard: all cases pass")
    return fail


if __name__ == "__main__":
    sys.exit(main())
