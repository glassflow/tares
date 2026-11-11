#!/usr/bin/env python3
"""Runs the station's status line the way Claude Code does, for the parity scenario `usage_statusline`.

    usage_support.py <bash|python> crew <args...>               `python -m tares.crew <args...>` (the engine is TARES_FACTORY_ENGINE)
    usage_support.py <bash|python> status <used%> <five-hour%>  the crew-orchestrator's status line, fed the state usage_test.sh feeds it
    usage_support.py <bash|python> raw                          the same command with stdin as given (not JSON)
    usage_support.py <bash|python> notares <used%> <five-hour%> the same state, with a Tares that does not answer

The command is the one in the station's settings file (statusLine.command), run with the settings' env on top
of this process's. `tares hook ...` at its start is run from this checkout (`python -c`), not from whatever
`tares` is on PATH. It prints what the command prints and exits as it does.

Standard library only.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MAIN = "import sys; sys.argv = ['tares'] + sys.argv[1:]; from tares.cli import main; main()"
STATION = "crew-orchestrator"


def state_for(sid: str, used: int, five: int) -> str:
    return json.dumps({
        "session_id": sid, "session_name": STATION,
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "context_window": {"total_input_tokens": 95300, "total_output_tokens": 800, "context_window_size": 1000000,
                           "used_percentage": used},
        "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": 4102444800},
                        "seven_day": {"used_percentage": 6, "resets_at": 4102444800}},
        "cost": {"total_cost_usd": 1.5}})


def command_of(settings: dict) -> list:
    words = shlex.split(settings["statusLine"]["command"], posix=(os.name != "nt"))
    words = [w[1:-1] if len(w) > 1 and w[0] == w[-1] == '"' else w for w in words]
    if words[:2] != ["tares", "hook"]:
        raise SystemExit(f"usage_support: the status line command is not `tares hook ...`: {words}")
    return [sys.executable, "-c", MAIN, *words[1:]]


def main(argv: list) -> int:
    if len(argv) < 2 or argv[0] not in ("bash", "python"):
        print(__doc__, file=sys.stderr)
        return 2
    op, rest = argv[1], argv[2:]
    if op == "crew":
        return subprocess.run([sys.executable, "-m", "tares.crew", op, *rest]).returncode
    home = Path(os.environ["FACTORY_CREW_HOME"])
    settings = json.loads((home / "stations" / f"{STATION}.settings.json").read_text(encoding="utf-8"))
    sid = json.loads((home / "stations.json").read_text(encoding="utf-8"))[STATION]["sessionId"]
    env = {**os.environ, **{k: str(v) for k, v in settings["env"].items()}}
    if op == "raw":
        stdin = sys.stdin.read()
    elif op in ("status", "notares") and len(rest) == 2:
        stdin = state_for(sid, int(rest[0]), int(rest[1]))
        if op == "notares":
            env["TARES_FACTORY_URL"] = "http://127.0.0.1:9"
    else:
        print(__doc__, file=sys.stderr)
        return 2
    p = subprocess.run(command_of(settings), input=stdin, env=env, capture_output=True, text=True, encoding="utf-8")
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
