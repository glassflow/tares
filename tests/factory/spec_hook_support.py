#!/usr/bin/env python3
"""Runs the plugin's session-start hook the way spec_test.sh does, for the parity scenario `spec_hooks`.

    spec_hook_support.py <bash|python> hook <source> [VAR=value ...]    `tares hook session-start`, the context it prints
    spec_hook_support.py <bash|python> bare <source> [VAR=value ...]    hooks/session_start.py by hand, what it prints

The hook is the plugin's own file, the same for both engines: the engine word is only there so the parity
harness can call this script like its other drivers. The hook's stdin is {"source": <source>}. TARES_PROJECT,
FACTORY_ROLE, TARES_SESSION_KIND and TARES_SESSION_TAKEOVER of the caller are dropped, then the VAR=value
words are set. `hook` prints the additionalContext of the answer (as `jq -r` does, "null" when there is none);
`bare` prints the hook's output as it is.

Standard library only.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN = ROOT / "claude-plugin"
DROPPED = ("FACTORY_ROLE", "TARES_PROJECT", "TARES_SESSION_KIND", "TARES_SESSION_TAKEOVER")
MAIN = "import sys; sys.argv = ['tares'] + sys.argv[1:]; from tares.cli import main; main()"


def main(argv: list) -> int:
    if len(argv) < 3 or argv[0] not in ("bash", "python") or argv[1] not in ("hook", "bare"):
        print(__doc__, file=sys.stderr)
        return 2
    op, source, sets = argv[1], argv[2], argv[3:]
    env = {k: v for k, v in os.environ.items() if k not in DROPPED}
    env.update(w.split("=", 1) for w in sets)
    stdin = json.dumps({"source": source})
    if op == "hook":
        cmd = [sys.executable, "-c", MAIN, "hook", "session-start", "--plugin-root", str(PLUGIN)]
    else:
        cmd = [sys.executable, str(PLUGIN / "hooks" / "session_start.py")]
    p = subprocess.run(cmd, input=stdin, env=env, capture_output=True, text=True, encoding="utf-8")
    out = p.stdout
    if op == "hook":
        try:
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            ctx = "null"
        out = ctx + "\n"
    sys.stdout.write(out)
    sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
