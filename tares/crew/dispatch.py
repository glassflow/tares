"""`tares factory <command>`: which function runs a command, and what is printed for no command, `help`, a
command that does not exist and a bad `crew` subcommand.

Every command is in PORTED (named for the move from bash), a map from the command word (or the command word and its subcommand) to a
function. The old bash crew (`claude-plugin/bin/factory`) was removed in TR-975; its help text moved to
usage.py and its exit codes are kept: help exits 0, an unknown command prints the help and exits 1, and a
`crew` subcommand that does not exist says which ones there are on stderr and exits 1.

Standard library only (no duckdb, fastapi, tares.daemon or tares.store), so `tares factory roster`
stays quick: each command's module is imported when the command runs.
"""
from __future__ import annotations

import sys
from typing import Callable

ME = "tares factory"

# A command: its key (the command word, or the command word and its subcommand) to
# fn(argv, env) -> exit code. argv is everything after `tares factory`, the key words included;
# env is the environment the command runs in.
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


PORTED[("crew", "up")] = _crew_up        # TR-956
PORTED[("crew", "setup")] = _crew_setup  # TR-956 (new: no bash twin)
PORTED[("tidy",)] = _tidy


def _events(argv: list[str], env: dict) -> int:
    from .events import cmd_events  # imported when used: it is the only command that tails a file
    return cmd_events(argv, env)


PORTED[("events",)] = _events  # new in Python (TR-1067): bash has no such command


def _with_secrets(argv: list[str], env: dict) -> int:
    from .with_secrets import cmd_with_secrets  # imported when used (TR-775)
    return cmd_with_secrets(argv, env)


def _secrets(argv: list[str], env: dict) -> int:
    from .with_secrets import cmd_secrets
    return cmd_secrets(argv, env)


PORTED[("with-secrets",)] = _with_secrets  # new in Python (TR-775): bash has no such command
PORTED[("secrets",)] = _secrets            # new in Python (TR-775)


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


def _kit(name: str) -> Callable[[list[str], dict], int]:
    """A command of kit.py (the freeze, TR-965), imported when it runs."""
    def run_it(argv: list[str], env: dict) -> int:
        from . import kit
        return getattr(kit, name)(argv, env)
    return run_it


# bash has `crew freeze` and `crew unfreeze` only, no one-word forms
for _word in ("freeze", "unfreeze"):
    PORTED[("crew", _word)] = _kit(f"cmd_{_word}")


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


def _mcp_url(argv: list[str], env: dict) -> int:
    from .client import cmd_mcp_url  # imported when used (TR-1155)
    return cmd_mcp_url(argv, env)


PORTED[("_mcp-url",)] = _mcp_url


def _parse_bg_id(argv: list[str], env: dict) -> int:
    from .claude import cmd_parse_bg_id  # imported when used
    return cmd_parse_bg_id(argv, env)


PORTED[("_parse-bg-id",)] = _parse_bg_id


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


def find(argv: list[str]) -> Callable[[list[str], dict], int] | None:
    """The ported function for argv: the longest key that starts argv wins, so `crew status` and
    `crew` can both be keys. None when the command is not ported."""
    for n in (2, 1):
        fn = PORTED.get(tuple(argv[:n])) if len(argv) >= n else None
        if fn is not None:
            return fn
    return None


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


CREW_TAKES = "up, down, status, desk, attach, logs, watch, halt, resume, freeze, unfreeze, update or restart"


def _no_command(argv: list[str]) -> tuple[str, str, int]:
    """(stdout, stderr, exit code) for argv that is no command: what the bash crew printed. `help`, `-h`,
    `--help` and no argument (or an empty one) print the help and exit 0; any other word prints the help and
    exits 1; a `crew` subcommand that does not exist says which ones there are, on stderr, and exits 1."""
    from .usage import USAGE
    word = argv[0] if argv else ""
    if word in ("", "help", "-h", "--help"):
        return USAGE, "", 0
    if word == "crew":
        return "", f"{ME}: {ME} crew takes {CREW_TAKES}\n", 1
    return USAGE, "", 1


def run(argv: list[str], env: dict) -> None:
    """Run `tares factory <argv>`: call the command and exit with its code. A command that does not exist
    (and `help`) prints what the bash crew printed. Does not return."""
    force_utf8()
    if argv == ["crew"]:        # a bare `crew` is `crew status`
        argv = ["crew", "status"]
    fn = find(argv)
    if fn is None:
        out, err, code = _no_command(argv)
        sys.stdout.write(out)
        sys.stderr.write(err)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        raise SystemExit(code)
    code = _call(fn, list(argv), env)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    raise SystemExit(code or 0)
