"""`tares factory runtime`: what the factory starts a new crew on, and what this crew runs on (TR-1600).

    tares factory runtime                  says both, and for Codex the version found
    tares factory runtime codex            new crews start on Codex
    tares factory runtime claude_code      new crews start on Claude Code

The factory's runtime is the cell's runtime slot (`PUT /api/factory/integrations/runtime`, which needs the
admin scope a person's own crew key carries; the console's Setup page sets the same). It decides what a crew
that has started no station yet starts on. A crew keeps the runtime it was started on: its stations record
it, a crew whose stations recorded none is on Claude Code, and a launch for another runtime is refused
(`launch.check_runtime`). So setting the factory's runtime moves no crew; this command says what it means for
the crew of this crew home.

Text only: `tares factory status --json` and `tares factory doctor --json` carry the runtime for a program.
"""
from __future__ import annotations

import sys

from . import launch, proc, state
from .client import TaresClient
from .errors import CrewError
from .paths import ME, CrewPaths

TITLES = launch.RUNTIME_TITLES
USAGE = f"usage: {ME} runtime [claude_code|codex]"


def factory_runtime(client: TaresClient) -> str:
    """The runtime the cell's slot holds now: a new crew starts on it. Dies when Tares does not answer."""
    doc = state.loads_json(client.tares_http("GET", proc.RUNTIME_SLOT))
    for slot in (doc or {}).get("slots") or []:
        if isinstance(slot, dict) and slot.get("slot") == "runtime":
            return str(slot.get("integration") or launch.RUNTIME_DEFAULT)
    return launch.RUNTIME_DEFAULT


def _crew_line(paths: CrewPaths, factory: str) -> str:
    have = launch.crew_runtime(paths)
    name = state.crew_name(paths)
    if not have:
        return f"this crew ({name}): no station started yet; it starts on {TITLES.get(factory, factory)}"
    line = f"this crew ({name}): {TITLES.get(have, have)}, as its stations were started"
    if have != factory:
        line += (f". A crew never moves to another runtime: it stays on {TITLES.get(have, have)}. A crew for "
                 f"{TITLES.get(factory, factory)} starts in a crew home of its own (FACTORY_CREW_HOME)")
    return line


def _codex_line(env: dict) -> str:
    from .views import _codex_line as line   # here: views imports the crew's heavier modules
    return line(env).replace("runtime: Codex", "codex:", 1)


def cmd_runtime(argv: list, env: dict) -> int:
    args = [a for a in argv[1:] if a]
    if len(args) > 1 or (args and args[0] in ("-h", "--help")):
        print(USAGE, file=sys.stderr)
        return 0 if args and args[0] in ("-h", "--help") else 2
    paths = CrewPaths.from_env()
    client = TaresClient(paths, env)
    try:
        client.prime()
        if args:
            want = args[0]
            if want not in TITLES:
                raise CrewError(f"{want} is not a runtime: one of {', '.join(TITLES)}")
            try:
                client.tares_http("PUT", "/api/factory/integrations/runtime", state.dumps_compact({"name": want}))
            except CrewError as e:
                raise CrewError(f"could not set the factory's runtime to {TITLES[want]}: {e}. It needs the admin "
                                "scope; the console's Setup page sets it too") from None
            print(f"factory: new crews now start on {TITLES[want]}")
        now = factory_runtime(client)
        if not args:
            print(f"factory: new crews start on {TITLES.get(now, now)}")
        if paths.home.is_dir():
            print(_crew_line(paths, now))
        if now == proc.CODEX or launch.crew_runtime(paths) == proc.CODEX:
            print(_codex_line(env))
        return 0
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1
    finally:
        client.close()
