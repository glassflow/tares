"""Run the console's browser e2e suites, or one shard of them.

    python tests/ci_e2e.py                       # every suite, in order
    python tests/ci_e2e.py --shard 2 --of 3      # the 2nd of 3 shards (1-based)
    python tests/ci_e2e.py --list                # print the shard's suites, run nothing

The suites are the ones named in ui/tests/e2e_suites.txt (ui/tests/<name>.e2e.cjs). Each gets its own
TARES_E2E_PORT, from its place in the whole list, so a suite keeps its port whichever shard runs it.
A suite passes when node exits 0 and prints no line that starts with FAIL. Every suite runs, then
the failures are listed and the exit code is 1. With E2E_SHOTS set, each suite keeps its screenshots
in $E2E_SHOTS/<name> (CI uploads that folder only when a run fails).

Needs ui/dist built, tares installed (the suites start their own scratch daemons) and Playwright's
Chromium. Shards are balanced by WEIGHT (seconds a suite took on a CI runner); a suite that
is not listed there counts as DEFAULT_WEIGHT."""
import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(os.path.dirname(HERE), "ui")
LIST = os.path.join(UI, "tests", "e2e_suites.txt")

# Each suite starts up to three daemons on its port, +1 and +2, and an MCP port +20 from it, so
# ports are 40 apart. The ones that set their own port (login-handoff, session-picker) ignore it.
BASE_PORT = 9000
PORT_STEP = 40
# Ports fetch (undici) refuses as "bad port". A suite whose range holds one gets the next free
# slot, so its page loads can reach its own daemon.
BLOCKED = {10080}

DEFAULT_WEIGHT = 30
WEIGHT = {
    "factory-reviews": 49, "factory-home": 47, "factory-project": 44, "sre-runbooks": 36, "edition-routes": 24,
    "factory-docs": 23, "factory-projects": 23, "factory-settings": 23, "factory-crew": 22, "projects-list": 19,
    "edition-shell": 16, "capped-runs": 8, "project-idle": 7, "component-gallery": 5, "raise-limit": 4,
    "login-handoff": 3, "session-picker": 2,
}

TIMEOUT = 600  # seconds for one suite; a hung browser fails the suite instead of the whole job
FAIL_LINE = re.compile(r"^\s*FAIL\b")


def names(path=LIST):
    """The suite names. A list that is missing or empty stops the run: a loop over nothing would pass."""
    if not os.path.isfile(path):
        sys.exit(f"The e2e suite list {path} is missing (was it renamed or deleted?). CI reads it to know which suites to run.")
    with open(path) as f:
        got = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    if not got:
        sys.exit(f"The e2e suite list {path} has no suite names. CI would run nothing and pass.")
    return got


def slots(n):
    """The first port of each of n suites' ranges, in order, skipping a range that holds a blocked port."""
    out, p = [], BASE_PORT
    while len(out) < n:
        if not any(p <= b < p + PORT_STEP for b in BLOCKED):
            out.append(p)
        p += PORT_STEP
    return out


def port(suite, all_suites=None):
    all_suites = all_suites or names()
    return slots(len(all_suites))[all_suites.index(suite)]


def shard(all_items, n, of):
    """Longest first, each onto the lightest shard; then back in list order within the shard."""
    order = {t: i for i, t in enumerate(all_items)}
    bins = [[0, []] for _ in range(of)]
    for t in sorted(all_items, key=lambda t: -WEIGHT.get(t, DEFAULT_WEIGHT)):
        b = min(bins, key=lambda b: b[0])
        b[0] += WEIGHT.get(t, DEFAULT_WEIGHT)
        b[1].append(t)
    return sorted(bins[n - 1][1], key=order.get)


def run(suite, all_suites, ui=UI, timeout=None):
    """(exit code, FAIL lines) for one suite. Its output goes to the log as it comes.

    A timer kills the suite's whole process group when the time is up, so a suite that goes silent
    (a request with no timeout, a browser that will not close) still ends; it does not wait for the next
    output line."""
    timeout = TIMEOUT if timeout is None else timeout
    env = dict(os.environ, TARES_E2E_PORT=str(port(suite, all_suites)))
    shots = os.environ.get("E2E_SHOTS")
    if shots:
        env["SHOTS_DIR"] = os.path.join(shots, suite)
        os.makedirs(env["SHOTS_DIR"], exist_ok=True)
    else:
        env.pop("SHOTS_DIR", None)
    print(f"== {suite} (port {env['TARES_E2E_PORT']}) ==", flush=True)
    fails = []
    started = time.time()
    p = subprocess.Popen(["node", os.path.join("tests", f"{suite}.e2e.cjs")], cwd=ui, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                         start_new_session=True)
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        try:
            os.killpg(p.pid, signal.SIGKILL)  # node and the daemons and browsers it started
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        for line in p.stdout:
            print(line, end="", flush=True)
            if FAIL_LINE.match(line):
                fails.append(line.strip())
        rc = p.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        print(f"TIMEOUT: {suite} ran over {timeout}s", flush=True)
        rc = 124
    print(f"-- {suite}: exit {rc}, {len(fails)} FAIL line(s), {time.time() - started:.0f}s", flush=True)
    return rc, fails


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=1)
    p.add_argument("--of", type=int, default=1)
    p.add_argument("--list", action="store_true", help="print the shard's suites, run nothing")
    a = p.parse_args()
    if not 1 <= a.shard <= a.of:
        p.error("--shard must be between 1 and --of")
    everything = names()
    mine = shard(everything, a.shard, a.of)
    if a.list:
        print("\n".join(mine))
        return 0
    bad = []
    for suite in mine:
        rc, fails = run(suite, everything)
        if rc or fails:
            bad.append(suite)
    print(f"\n{len(mine) - len(bad)} of {len(mine)} suites passed (shard {a.shard} of {a.of})", flush=True)
    for suite in bad:
        print(f"FAILED: {suite}", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
