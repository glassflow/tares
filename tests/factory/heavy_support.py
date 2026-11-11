#!/usr/bin/env python3
"""Support for the `heavy` scenario of parity.py: heavy_test.sh on the bash script or the Python port.

    heavy_support.py <bash|python> [run]

heavy_test.sh starts two heavy commands at once and checks they never overlap, then checks that a command
past its timeout is killed (exit 142) and that the lock folder is gone afterwards. `factory heavy` is not a
command of the bash `factory` (it is the stand-alone claude-plugin/bin/factory-heavy), so the scenario
cannot run it as a step: this script runs the whole suite against one engine, in a throwaway TMPDIR.

  bash    claude-plugin/bin/factory-heavy <seconds> <command...>
  python  python -m tares.crew heavy <seconds> <command...>

The commands under test are Python one-liners instead of `bash -c`, so the same script runs on Windows.
It prints what the bash suite prints: a FAIL line per broken rule and, when none broke,
`factory-heavy: serialises and times out`. Exit 0 when all hold.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LOCK = "tares-factory-heavy.lock"
WAIT_FOR_LOCK = 10.0     # seconds the first command gets to take the lock (the suite waits up to 10 s too)


def heavy_command(engine: str, seconds: int, command: list) -> list:
    if engine == "bash":
        return [str(ROOT / "claude-plugin" / "bin" / "factory-heavy"), str(seconds)] + command
    return [sys.executable, "-m", "tares.crew", "heavy", str(seconds)] + command


def append(log: Path, *lines: str) -> str:
    """A Python program that appends these lines to the log, with a pause after the first when given."""
    return "; ".join(f"open({str(log)!r}, 'a').write({x + chr(10)!r})" if not x.startswith("sleep ")
                     else f"__import__('time').sleep({x.split()[1]})" for x in lines)


def main(argv: list) -> int:
    if not argv or argv[0] not in ("bash", "python"):
        print(__doc__, file=sys.stderr)
        return 2
    engine = argv[0]
    tmp = Path(tempfile.mkdtemp(prefix="heavy-")).resolve()
    env = {**os.environ, "TMPDIR": str(tmp), "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop("FACTORY_STATION", None)
    log = tmp / "log"
    lock = tmp / LOCK
    fail = 0
    try:
        first = subprocess.Popen(
            heavy_command(engine, 30, [sys.executable, "-c", append(log, "start-a", "sleep 2", "end-a")]), env=env)
        deadline = time.monotonic() + WAIT_FOR_LOCK
        while time.monotonic() < deadline:   # a holds the lock once its pid file has content
            try:
                if (lock / "pid").stat().st_size > 0:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        subprocess.run(heavy_command(engine, 30, [sys.executable, "-c", append(log, "start-b", "end-b")]), env=env,
                       stderr=subprocess.DEVNULL)   # it says who it waits for, which the suite does not read
        first.wait()
        order = " ".join(log.read_text(encoding="utf-8").split()) + " "
        if order != "start-a end-a start-b end-b ":
            print(f"FAIL overlap: {order}")
            fail = 1
        code = subprocess.run(heavy_command(engine, 1, [sys.executable, "-c", "__import__('time').sleep(5)"]),
                              env=env, stderr=subprocess.DEVNULL).returncode
        if code != 142:
            print("FAIL timeout did not kill")
            fail = 1
        if lock.is_dir():
            print("FAIL lock left behind")
            fail = 1
        if not fail:
            print("factory-heavy: serialises and times out")
        return fail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
