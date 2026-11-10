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


def _events(argv: list[str], env: dict) -> int:
    from .events import cmd_events  # imported when used: it is the only command that tails a file
    return cmd_events(argv, env)


PORTED[("events",)] = _events  # new in Python (TR-1067): bash has no such command


def _spawn(argv: list[str], env: dict) -> int:
    from .stations import cmd_spawn  # imported when used (TR-954)
    return cmd_spawn(argv, env)


PORTED[("spawn",)] = _spawn


def _settings(argv: list[str], env: dict) -> int:
    from .stations import cmd_settings  # imported when used (TR-1122)
    return cmd_settings(argv, env)


PORTED[("settings",)] = _settings


def _lifecycle(name: str) -> Callable[[list[str], dict], int]:
    """A lifecycle command (TR-955), imported when it runs."""
    def run_it(argv: list[str], env: dict) -> int:
        from . import lifecycle
        return getattr(lifecycle, name)(argv, env)
    return run_it


# The top-level `restart <station>` is here; `crew restart` is below with `crew update`: bash runs it as
# `crew update --force`
for _word in ("revive", "restart", "retire", "down", "halt", "resume"):
    PORTED[(_word,)] = _lifecycle(f"cmd_{_word}")
for _word in ("down", "halt", "resume"):
    PORTED[("crew", _word)] = _lifecycle(f"cmd_{_word}")


def _cc_update(name: str) -> Callable[[list[str], dict], int]:
    """crew update and crew restart (TR-964), imported when they run."""
    def run_it(argv: list[str], env: dict) -> int:
        from . import cc_update
        return getattr(cc_update, name)(argv, env)
    return run_it


PORTED[("crew", "update")] = _cc_update("cmd_crew_update")
PORTED[("crew", "restart")] = _cc_update("cmd_crew_restart")


def _watch(argv: list[str], env: dict) -> int:
    from .watch import cmd_watch  # imported when used (TR-958)
    return cmd_watch(argv, env)


PORTED[("watch",)] = _watch
PORTED[("crew", "watch")] = _watch
WATCH_VAR = "TARES_FACTORY_WATCH"  # "python" opts the watch in; bash stays the default one


def _bash_watch(argv: list[str], env: dict) -> bool:
    """True for `watch` / `crew watch` that is not opted in: the bash watch stays the default where there is
    bash. TARES_FACTORY_ENGINE=python (asked for outright) and Windows (no bash) run the Python one."""
    if argv[:1] == ["crew"]:
        argv = argv[1:]
    return (argv[:1] == ["watch"] and (env.get(ENGINE_VAR) or "") == ""
            and env.get(WATCH_VAR) != "python" and not is_windows())


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


def _person(name: str) -> Callable[[list[str], dict], int]:
    """A command of person.py (projects, finish, context, plan), imported when it runs."""
    def run_it(argv: list[str], env: dict) -> int:
        from . import person
        return getattr(person, name)(argv, env)
    return run_it


for _word in ("projects", "finish", "context", "plan", "spec", "session"):
    PORTED[(_word,)] = _person(f"cmd_{_word}")


def _heavy(argv: list[str], env: dict) -> int:
    from .heavy import cmd_heavy
    return cmd_heavy(argv, env)


PORTED[("heavy",)] = _heavy


def _doctor(argv: list[str], env: dict) -> int:
    from .doctor import cmd_doctor  # imported when used, so other commands start without it
    return cmd_doctor(argv, env)


PORTED[("doctor",)] = _doctor


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
    if engine == "bash" or (fn is None and engine == "") or (fn is not None and _bash_watch(argv, env)):
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


def _call(fn: Callable[[list[str], dict], int], argv: list[str], env: dict) -> int:
    """fn(argv, env). With --json, a failure nobody caught prints the error shape (contract.py) and exits 1
    instead of a traceback or a text line. Without --json it is left alone, as it was."""
    if "--json" not in argv:
        return fn(argv, env)
    from . import contract
    from .errors import CrewError
    try:
        return fn(argv, env)
    except CrewError as e:
        contract.refuse(e, argv)
        return contract.EXIT_FAILED
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:  # a bug: still one shape for the caller, the exception named
        contract.refuse(CrewError(f"{type(e).__name__}: {e}"), argv)
        return contract.EXIT_FAILED


def run(argv: list[str], env: dict, script: str | Path) -> None:
    """Run `tares factory <argv>`: call the Python command and exit with its code, or replace this
    process with `bash <script> <argv>` under env. Does not return."""
    kind, fn, message = route(argv, env)
    if kind == "error":
        _fail(message)
    if kind == "python":
        force_utf8()
        code = _call(fn, list(argv), env)
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
