"""Which engine runs a `tares factory` command: the Python one when the command is ported, else bash.

Commands move from `claude-plugin/bin/factory` to Python one ticket at a time. A ported command adds
itself to PORTED; every other command still goes to bash, exactly as before.

  TARES_FACTORY_ENGINE   unset or empty  each command on its own engine (Python if ported, else bash)
                         python          Python only: a command that is not ported fails with exit 2
                         bash            bash only, even for a ported command
                         anything else   an error, exit 2

Standard library only (no duckdb, fastapi, tares.daemon or tares.store), so `tares factory roster`
stays quick: nothing here is imported that bash does not need.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

ME = "tares factory"
ENGINE_VAR = "TARES_FACTORY_ENGINE"
ENGINES = ("python", "bash")

# A ported command: its key (the command word, or the command word and its subcommand) to
# fn(argv, env) -> exit code. argv is everything after `tares factory`, the key words included;
# env is the environment the command runs in. Later tickets add entries.
PORTED: dict[tuple[str, ...], Callable[[list[str], dict], int]] = {}


def _crew_up(argv: list[str], env: dict) -> int:
    from . import up  # lazy: the dispatcher stays light for every other command
    return up.entry_crew_up(argv, env)


def _crew_setup(argv: list[str], env: dict) -> int:
    from . import up
    return up.entry_crew_setup(argv, env)


def _tidy(argv: list[str], env: dict) -> int:
    from .tidy import cmd_tidy  # imported when used, so other commands start without it
    return cmd_tidy(argv, env)


PORTED[("crew", "up")] = _crew_up        # TR-956 (runs in Python only with TARES_FACTORY_ENGINE=python)
PORTED[("crew", "setup")] = _crew_setup  # TR-956 (new: no bash twin)
PORTED[("tidy",)] = _tidy


def _view(name: str):
    """A view command (TR-957). views is imported when the command runs, so other commands start without it."""
    def run(argv: list[str], env: dict) -> int:
        from .views import COMMANDS
        return COMMANDS[name](argv, env)
    run.__name__ = name
    return run


for _name in ("status", "desk", "roster", "attach", "logs"):
    PORTED[(_name,)] = _view(_name)
    PORTED[("crew", _name)] = _view(_name)


def is_windows() -> bool:
    """True where bash is not there to fall back on. A function so a test can patch it."""
    return sys.platform == "win32"


def find(argv: list[str]) -> Callable[[list[str], dict], int] | None:
    """The ported function for argv: the longest key that starts argv wins, so `crew status` and
    `crew` can both be keys. None when the command is not ported."""
    for n in (2, 1):
        fn = PORTED.get(tuple(argv[:n])) if len(argv) >= n else None
        if fn is not None:
            return fn
    return None


def command_name(argv: list[str]) -> str:
    """The command as a person typed it, for messages: `crew up`, `roster`, `help` for none."""
    if not argv:
        return "help"
    if len(argv) > 1 and not argv[1].startswith("-"):
        return f"{argv[0]} {argv[1]}"
    return argv[0]


def route(argv: list[str], env: dict) -> tuple[str, Callable[[list[str], dict], int] | None, str]:
    """(kind, function, message). kind is "python" (call the function), "bash" (run the script) or
    "error" (print the message, exit 2)."""
    engine = env.get(ENGINE_VAR) or ""
    if engine not in ("", *ENGINES):
        return "error", None, f'{ENGINE_VAR} must be "python" or "bash" (or unset), not "{engine}"'
    fn = find(argv)
    if engine == "bash" or (fn is None and engine == ""):
        return "bash", None, ""
    if fn is None:  # engine == "python"
        return "error", None, (f'"{command_name(argv)}" is not yet ported to the Python engine '
                               f"({ENGINE_VAR}=python); unset {ENGINE_VAR} to let bash run it")
    return "python", fn, ""


def force_utf8() -> None:
    """Python commands print the same UTF-8 on every platform, whatever the console's code page.
    A stream that cannot be reconfigured (replaced by a test or a wrapper) is left alone."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _fail(message: str) -> None:
    print(f"{ME}: {message}", file=sys.stderr)
    raise SystemExit(2)


def run(argv: list[str], env: dict, script: str | Path) -> None:
    """Run `tares factory <argv>`: call the Python command and exit with its code, or replace this
    process with `bash <script> <argv>` under env. Does not return."""
    kind, fn, message = route(argv, env)
    if kind == "error":
        _fail(message)
    if kind == "python":
        force_utf8()
        code = fn(list(argv), env)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        raise SystemExit(code or 0)
    if is_windows():
        if env.get(ENGINE_VAR) == "bash":
            _fail(f'{ENGINE_VAR}=bash needs bash, which is not available on Windows; '
                  f'unset {ENGINE_VAR} to use the Python engine')
        _fail(f'"{command_name(argv)}" is not yet ported to the Python engine, '
              "and bash is not available on Windows")
    os.execve("/bin/bash", ["bash", str(script), *argv], env)
