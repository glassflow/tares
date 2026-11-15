"""Runs one round of the watch (`cmd_watch --once`) for the parity harness.

    watch_support.py once

The crew home (FACTORY_CREW_HOME) and the fake Claude Code are the scenario's; this script starts a small fake Tares,
points the engine at it and runs one round with `tares.crew.watch.cmd_watch`. In a round the watch runs the steps that
are registered, so a scenario here covers the steps this branch ports (revive and stuck), and its crew must be one the
other steps of the round do nothing for: no pace file, no newer plugin or Claude Code, no grants to give.

The launch of a revived station is the real one (settings file, `claude --bg` in the fake).
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from launch_support import FakeTares, ROOT, make_kit  # noqa: E402


def run_python(env: dict) -> int:
    from tares.crew import watch
    from tares.crew.errors import CrewError
    from tares.crew.paths import ME
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", newline="\n")
    try:
        return watch.cmd_watch(["watch", "--once"], env)
    except CrewError as e:
        print(f"{ME}: {e}", file=sys.stderr)
        return 1


def main(argv: list) -> int:
    if argv != ["once"]:
        print(__doc__, file=sys.stderr)
        return 2
    tmp = Path(os.environ["FACTORY_CREW_HOME"]).parent
    kit = make_kit(tmp)
    with FakeTares({"projects": [{"id": "shop", "name": "shop"}]}) as tares:   # the cell knows the builders' project (TR-870)
        os.environ.update(TARESD_URL=tares.url, TARES_PLUGIN_DIR=str(kit), FACTORY_STOP_WAIT="3")
        sys.path.insert(0, str(ROOT))
        return run_python(dict(os.environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
