"""The disposable world a Codex probe runs in (TR-1576): nothing here touches a real repository or service.

    <root>/crew/            the crew home (FACTORY_HOME): HALT, events.jsonl, stations/<station>.grants
    <root>/work/            a git repository with one commit, the station's working folder
    <root>/remote.git/      a bare repository, the only remote of work/ (a `git push` lands here)
    <root>/bin/             fake gh, trivy and tares: each appends its arguments to side-effects.log
    <root>/side-effects.log one line per fake command or MCP write that really ran

A side effect that was denied leaves no line and no commit, which is what a probe checks.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
GUARD = os.path.join(REPO, "claude-plugin", "hooks", "guard.py")

FAKE = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "{log}"
exit 0
"""


def _git(cwd: str, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="probe", GIT_AUTHOR_EMAIL="probe@example.invalid",
               GIT_COMMITTER_NAME="probe", GIT_COMMITTER_EMAIL="probe@example.invalid")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class World:
    def __init__(self, root: str | None = None):
        self.root = os.path.realpath(root or tempfile.mkdtemp(prefix="tares-codex-probe-"))
        self.crew = os.path.join(self.root, "crew")
        self.work = os.path.join(self.root, "work")
        self.remote = os.path.join(self.root, "remote.git")
        self.bin = os.path.join(self.root, "bin")
        self.log = os.path.join(self.root, "side-effects.log")
        # the folders a real crew home has (state.ensure_crew_home): a station's sandbox is given the reports
        # folder as writable, and on Linux it starts no command when that folder does not exist
        for d in (self.crew, *(os.path.join(self.crew, x) for x in ("stations", "reports", "worktrees")), self.work,
                  self.bin):
            os.makedirs(d, exist_ok=True)
        for name in ("gh", "trivy"):
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(FAKE.format(log=self.log))
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if not os.path.isdir(os.path.join(self.work, ".git")):
            _git(self.root, "init", "-q", "--bare", self.remote)
            _git(self.root, "init", "-q", "-b", "main", self.work)
            with open(os.path.join(self.work, "README.md"), "w", encoding="utf-8") as f:
                f.write("probe\n")
            _git(self.work, "add", "README.md")
            _git(self.work, "commit", "-q", "-m", "first")
            _git(self.work, "remote", "add", "origin", self.remote)

    # -- what a probe reads back ------------------------------------------------------------------------------
    def side_effects(self) -> list[str]:
        try:
            with open(self.log, encoding="utf-8") as f:
                return [line.rstrip("\n") for line in f if line.strip()]
        except OSError:
            return []

    def remote_commits(self, ref: str = "main") -> int:
        r = subprocess.run(["git", "rev-list", "--count", ref], cwd=self.remote, capture_output=True, text=True)
        return int(r.stdout.strip()) if r.returncode == 0 else 0

    def remote_tags(self) -> list[str]:
        r = subprocess.run(["git", "tag"], cwd=self.remote, capture_output=True, text=True)
        return [t for t in r.stdout.split() if t]

    def guard_denies(self) -> list[dict]:
        out = []
        try:
            with open(os.path.join(self.crew, "events.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") == "guard-deny":
                        out.append(ev)
        except OSError:
            pass
        return out

    # -- what a probe sets ------------------------------------------------------------------------------------
    def halt(self, reason: str = "probe halt") -> None:
        with open(os.path.join(self.crew, "HALT"), "w", encoding="utf-8") as f:
            json.dump({"at": "probe", "reason": reason}, f)

    def lift_halt(self) -> None:
        try:
            os.remove(os.path.join(self.crew, "HALT"))
        except OSError:
            pass

    def env(self, role: str, station: str | None = None, parent: str = "", extra: dict | None = None) -> dict:
        """The environment a station of this role runs in: the FACTORY_* names the Claude runtime sets
        (tares/integrations/builtin/runtime_claude_code.py), and the fakes first on PATH."""
        env = dict(os.environ)
        # `tares factory heavy` must be this checkout's: the interpreter's folder goes before the person's PATH
        # when it holds a `tares` (a virtual environment of the checkout)
        mine = os.path.dirname(sys.executable)
        ours = [mine] if os.access(os.path.join(mine, "tares"), os.X_OK) else []
        env.update({
            "FACTORY_ROLE": role, "FACTORY_STATION": station or f"crew-{role}", "FACTORY_CREW": "crew",
            "FACTORY_HOME": self.crew, "FACTORY_PARENT": parent,
            "PATH": os.pathsep.join([self.bin, *ours, env.get("PATH", "")]),
            "PROBE_SIDE_EFFECTS": self.log,
        })
        env.update(extra or {})
        return env


def adapter_command() -> str:
    """The guard as a Codex station runs it (TR-1594): `tares hook codex-guard`, the product's own, from this
    checkout. The `tares` next to the interpreter when there is one (a virtual environment of the checkout),
    else the package run by this interpreter."""
    import shlex
    tares = os.path.join(os.path.dirname(sys.executable), "tares")
    if os.access(tares, os.X_OK):
        run = shlex.quote(tares)
    else:
        shim = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
        run = f"PYTHONPATH={shlex.quote(REPO)} {shlex.quote(sys.executable)} -c {shlex.quote(shim)}"
    return f"{run} hook codex-guard --plugin-root {shlex.quote(os.path.join(REPO, 'claude-plugin'))}"


def adapter_hooks(timeout: int = 5) -> dict:
    """The `hooks` table a Codex station is started with: the adapter before every tool call and at the start
    of every turn (tares/integrations/builtin/runtime_codex.py)."""
    guard = {"type": "command", "command": adapter_command(), "timeout": timeout}
    return {"PreToolUse": [{"matcher": "", "hooks": [dict(guard)]}], "UserPromptSubmit": [{"hooks": [dict(guard)]}]}


def guard_hooks(command: str | None = None, timeout: int = 5, matcher: str = "") -> dict:
    """The `hooks` table that runs the repository's own guard before every tool call. The Claude plugin
    registers it for `Bash|mcp__.*` (claude-plugin/hooks/hooks.json); an empty matcher is every tool, and
    the guard itself ignores what it has no rule for."""
    command = command or f"{sys.executable} {GUARD}"
    return {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}]}
