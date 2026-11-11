#!/usr/bin/env python3
"""Runs the watch between its rounds for the parity scenarios `usage_watch_*` (TR-1163).

    readings_support.py <bash|python> watch <ticks>

Starts `python -m tares.crew crew watch --interval 60` (the engine is TARES_FACTORY_ENGINE), which spends its
time napping and serving readings, and waits until the fake Tares says it has answered the asks route <ticks>
times (GET {TARESD_URL}/ticks), so a tick is waited for by what happened and not by a pause. Then it stops the
watch and prints the lines of the watch's own output that say it could not read something.

Standard library only.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

TIMEOUT = 60.0


def ticks(url: str) -> int:
    try:
        with urllib.request.urlopen(url + "/ticks", timeout=5) as r:
            return int(json.loads(r.read().decode("utf-8")).get("n", 0))
    except (OSError, ValueError):
        return 0


def main(argv: list) -> int:
    if len(argv) != 3 or argv[0] not in ("bash", "python") or argv[1] != "watch" or not argv[2].isdigit():
        print(__doc__, file=sys.stderr)
        return 2
    want = int(argv[2])
    url = os.environ["TARESD_URL"]
    env = {**os.environ, "FACTORY_ASK_POLL": os.environ.get("FACTORY_ASK_POLL", "0.2")}
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as log:
        p = subprocess.Popen([sys.executable, "-m", "tares.crew", "crew", "watch", "--interval", "60"],
                             env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        end = time.monotonic() + TIMEOUT
        try:
            while p.poll() is None and time.monotonic() < end and ticks(url) < want:
                time.sleep(0.05)
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        log.seek(0)
        lines = [ln.rstrip("\n") for ln in log if "could not read" in ln]
    reached = ticks(url) >= want
    print("\n".join(lines))
    print(f"the watch served {want} ticks" if reached else f"the watch did not reach {want} ticks")
    return 0 if reached else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
