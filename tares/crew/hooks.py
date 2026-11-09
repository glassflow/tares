"""`tares hook <name> [args]`: the one way the Claude Code plugin starts a hook.

A hook command in `claude-plugin/hooks/hooks.json` is meant to be `tares hook <name>`, never a bare
`python3` (missing on Windows, or the Store alias) or `bash`. hooks.json still runs `python3` and `bash`
until TR-1103 switches it, after a tares release has this command. The name maps to a file under the
plugin root.
A Python script runs in this process with `runpy`, with stdin and stdout passed through and its
exit code kept. A bash script (none is left; the kind stays for a later hook) runs
through the `bash` found on PATH, which on Windows is Git Bash.

Standard library only, and nothing from the rest of the package: a hook starts at every tool call,
so this must stay light (tests/test_hook_start.py holds the limit).

Which plugin: `--plugin-root`, else `CLAUDE_PLUGIN_ROOT` (Claude Code sets it for its hooks), else
`TARES_PLUGIN_DIR`, else the plugin inside the installed package or checkout.
"""
from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

# name -> (kind, path under the plugin root). kind: py runs in process, sh runs through bash,
# compat is the version check, which needs no file.
HOOKS = {
    "ship": ("py", "scripts/ship.py"),
    "challenger": ("py", "scripts/challenger.py"),
    "statusline": ("py", "scripts/statusline.py"),
    "guard": ("py", "hooks/guard.py"),
    "session-start": ("py", "hooks/session_start.py"),
    "mcp-headers": ("py", "scripts/mcp_headers.py"),
    "compat": ("compat", ""),
}


def plugin_root(given: str = "") -> Path:
    """The plugin folder a hook runs from (see the module note for the order)."""
    for cand in (given, os.environ.get("CLAUDE_PLUGIN_ROOT", ""), os.environ.get("TARES_PLUGIN_DIR", "")):
        if cand:
            return Path(cand)
    here = Path(__file__).resolve()
    for d in (here.parents[1] / "claude-plugin", here.parents[2] / "claude-plugin"):
        if (d / "hooks" / "hooks.json").is_file():
            return d
    raise SystemExit("tares hook: the Claude Code plugin is missing from this install "
                     f"(looked in {here.parents[1] / 'claude-plugin'} and {here.parents[2] / 'claude-plugin'})")


def find_bash() -> str:
    """bash for the shell hooks. On Windows prefer Git Bash (next to git.exe) over the WSL launcher
    in System32, which would run the script inside Linux where the plugin's paths mean nothing."""
    if sys.platform == "win32":
        git = shutil.which("git")
        if git:
            for rel in (("..", "..", "bin", "bash.exe"), ("..", "bin", "bash.exe")):
                cand = Path(git).resolve().parent.joinpath(*rel).resolve()
                if cand.is_file():
                    return str(cand)
    found = shutil.which("bash")
    if not found:
        raise SystemExit("tares hook: bash is not on PATH (on Windows, install Git for Windows)")
    return found


def run(argv: list[str]) -> int:
    """`tares hook <name> [--dry-run] [--plugin-root DIR] [args...]`. The two options count only
    straight after the name; everything after them goes to the hook script as its own arguments."""
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: tares hook <name> [--dry-run] [--plugin-root DIR] [args...]\n"
              "known hooks: " + ", ".join(HOOKS))
        return 0 if argv else 2
    name, rest = argv[0], list(argv[1:])
    if name not in HOOKS:
        print(f"tares hook: unknown hook {name!r} (known: {', '.join(HOOKS)})", file=sys.stderr)
        return 2
    dry, root_arg = False, ""
    while rest and rest[0] in ("--dry-run", "--plugin-root"):
        opt = rest.pop(0)
        if opt == "--dry-run":
            dry = True
        elif rest:
            root_arg = rest.pop(0)
        else:
            print("tares hook: --plugin-root needs a folder", file=sys.stderr)
            return 2
    kind, rel = HOOKS[name]
    if kind == "compat":
        return _compat(root_arg, dry)
    script = plugin_root(root_arg) / rel
    if dry:
        print(script.as_posix())
        return 0 if script.is_file() else 1
    if not script.is_file():
        print(f"tares hook: {name} has no script at {script.as_posix()}", file=sys.stderr)
        return 1
    if kind == "sh":
        return subprocess.call([find_bash(), script.as_posix(), *rest])
    return _run_py(script, rest)


def _run_py(script: Path, args: list[str]) -> int:
    """Run a hook script as __main__ in this process; its sys.exit code is ours."""
    saved = sys.argv
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    finally:
        sys.argv = saved
    return 0


def _compat(root_arg: str, dry: bool) -> int:
    """`tares hook compat`: one line when the installed tares is older than the plugin's
    min_tares, else nothing; always exit 0, so a hook can call it at every start."""
    if dry:
        print("(builtin: tares.crew.version.check_plugin_compat)")
        return 0
    root = root_arg or os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    line = ""
    if root:
        try:
            from .version import check_plugin_compat
            line = check_plugin_compat(root)
        except Exception:
            line = ""
    if line:
        print(line)
    return 0
