#!/usr/bin/env python3
"""Support for the crewname hook scenarios of parity.py: runs the plugin's guard and session-start hooks for a helper, as
crewname_test.sh does with `tares hook guard` and `tares hook session-start`.

    crewname_support.py <bash|python> guard <parent> <command> <crew>   prints deny or allow
    crewname_support.py <bash|python> start <parent> <crew>             prints the station skill a helper is told to load

The hooks are Python scripts of the plugin that both engines share, so the engine word changes nothing here.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN = ROOT / "claude-plugin"


def hook(name: str, payload: dict, parent: str, crew: str, station: str = "") -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_"))}
    env.update(FACTORY_ROLE="helper", FACTORY_CREW=crew, FACTORY_PARENT=parent, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    if station:
        env["FACTORY_STATION"] = station
    p = subprocess.run([sys.executable, "-c", "import sys; from tares.crew.hooks import run; sys.exit(run(sys.argv[1:]))",
                        name, "--plugin-root", str(PLUGIN)],
                       input=json.dumps(payload), env=env, capture_output=True, text=True, encoding="utf-8")
    return p.stdout


def main(argv: list) -> int:
    kind = argv[2] if len(argv) > 2 else ""
    if kind == "guard" and len(argv) == 6:
        out = hook("guard", {"tool_name": "Bash", "tool_input": {"command": argv[4]}}, argv[3], argv[5])
        print("deny" if '"deny"' in out else "allow")
        return 0
    if kind == "start" and len(argv) == 5:
        out = hook("session-start", {"source": "startup"}, argv[3], argv[4], station="h")
        try:
            text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError):
            text = ""
        m = re.search(r"station-[a-z]*", text)
        print(m.group(0) if m else "")
        return 0
    print("usage: crewname_support.py <bash|python> guard <parent> <command> <crew> | start <parent> <crew>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
