#!/usr/bin/env python3
"""The crew tests and the parity harness, run the way the Windows job runs them (TR-1066).

    python tests/ci_windows.py              run everything below, print a summary, exit 0 or 1
    python tests/ci_windows.py --list       print what would run (and what is skipped, and why), run nothing
    python tests/ci_windows.py --root DIR   a tree other than this checkout (test_ci_windows.py uses a fake one)
    python tests/ci_windows.py --timeout S  seconds a single test may take (default 600)

What runs, in this order:
  1. every tests/test_crew_*.py that exists (sorted), as `python <file>`, except the SKIP list below;
  2. `python tests/factory/parity.py --coverage`;
  3. each scenario named by `python tests/factory/parity.py --list`, one run each.

Standard library only, so it runs on any OS and on any Python 3.9 or later: the same script that the
windows-crew workflow runs on Windows runs on a developer's Mac or Linux machine, and the test of it
(tests/test_ci_windows.py) needs no Windows.

A test counts only when it executed. A run whose summary says "0 passed, 0 failed" (a test that printed
`skip:` and left) and a parity scenario whose engines were both skipped are listed as "skipped", not as
passed, and they do not count toward the total. The script exits 1 when

  - any run failed (a non-zero exit, or a timeout), or
  - no test executed ("ran zero tests": a job that tests nothing must not be green; the coverage report
    alone does not count as a test), or
  - tests/test_crew_packaging.py or tests/factory/parity.py is missing (the Windows job is built around them).

It does not install or fix anything; a Windows problem in the engine shows up here as a failed run.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Tests the Windows job does not run, with the reason. Anything not listed here and named test_crew_*.py runs.
SKIP = {
    "test_crew_standup.py": "starts the daemon and needs DuckDB, FastAPI and httpx, which the Windows job does not install",
}

# The Windows job is built around these two: a tree without them is broken, not "nothing to run".
REQUIRED = ["tests/test_crew_packaging.py", "tests/factory/parity.py"]

SUMMARY = re.compile(r"(\d+) passed, (\d+) failed")
TAIL_LINES = 40


class Result:
    def __init__(self, name, code, seconds, last, output, kind="test"):
        self.name = name
        self.code = code
        self.seconds = seconds
        self.last = last
        self.output = output
        self.kind = kind          # "test", "coverage" or "scenario"
        self.executed = code == 0  # settled by judge()
        self.note = ""

    @property
    def failed(self):
        return self.code != 0


def last_line(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def run(argv, root, timeout):
    """(exit code, seconds, combined output). A timeout is exit 124 with a note in the output."""
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    t = time.time()
    try:
        p = subprocess.run(argv, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        code, out = p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        code, out = 124, (e.stdout or b"") + ("\nci_windows: timed out after %ds\n" % timeout).encode()
    return code, time.time() - t, out.decode("utf-8", "replace").replace("\r\n", "\n")


def judge(r):
    """Decide whether a passing run executed anything, from what it printed."""
    if r.failed:
        r.executed = False
        return r
    if r.kind == "test":
        m = None
        for m in SUMMARY.finditer(r.output):
            pass  # the last summary line wins
        if m is not None and int(m.group(1)) + int(m.group(2)) == 0:
            r.executed, r.note = False, "skipped: it ran no checks"
            return r
        r.note = "%s passed, %s failed" % m.groups() if m else ""
    elif r.kind == "scenario":
        if not re.search(r"^(PASS|FAIL) ", r.output, re.M):
            r.executed, r.note = False, "skipped: no engine ran"
            return r
        r.note = ", ".join(ln.strip() for ln in r.output.splitlines() if ln.startswith(("PASS ", "SKIP ")))
    return r


def plan_tests(root):
    """(to run, skipped with reason): the tests/test_crew_*.py files that exist, sorted."""
    found = sorted(p.name for p in (root / "tests").glob("test_crew_*.py"))
    return [n for n in found if n not in SKIP], [(n, SKIP[n]) for n in found if n in SKIP]


def scenarios(root, timeout):
    """(names, error): the scenarios `parity.py --list` prints."""
    code, _, out = run([sys.executable, str(root / "tests" / "factory" / "parity.py"), "--list"], root, timeout)
    if code != 0:
        return [], "parity.py --list exited %d: %s" % (code, last_line(out))
    return [ln.strip() for ln in out.splitlines() if ln.strip()], ""


def show(r):
    print("%-5s %-40s exit %-3d %6.1fs  %s" % ("ok" if not r.failed else "FAIL", r.name, r.code, r.seconds,
                                              r.note or r.last))
    sys.stdout.flush()


def main(argv):
    root, timeout, only_list = HERE.parent, 600, False
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--list":
            only_list = True
        elif a == "--root" and args:
            root = Path(args.pop(0)).resolve()
        elif a == "--timeout" and args:
            timeout = int(args.pop(0))
        else:
            print(__doc__)
            return 2

    missing = [p for p in REQUIRED if not (root / p).is_file()]
    to_run, skipped = plan_tests(root)

    print("ci_windows: root %s" % root)
    print("ci_windows: python %s on %s" % (sys.version.split()[0], sys.platform))
    for n, why in skipped:
        print("skip  %-40s %s" % (n, why))
    if only_list:
        for n in to_run:
            print("test  tests/%s" % n)
        print("test  tests/factory/parity.py --coverage")
        if "tests/factory/parity.py" not in missing:
            names, err = scenarios(root, timeout)
            for n in names:
                print("test  tests/factory/parity.py %s" % n)
            if err:
                print("ci_windows: %s" % err)
        for p in missing:
            print("MISSING %s" % p)
        return 1 if missing else 0

    if missing:
        for p in missing:
            print("ci_windows: FAIL: %s is missing; the Windows job is built around it" % p)
        print("\nci_windows: 0 tests ran")
        return 1

    results = []
    for n in to_run:
        code, secs, out = run([sys.executable, str(root / "tests" / n)], root, timeout)
        r = judge(Result("tests/" + n, code, secs, last_line(out), out))
        show(r)
        results.append(r)

    code, secs, out = run([sys.executable, str(root / "tests" / "factory" / "parity.py"), "--coverage"], root, timeout)
    r = Result("parity.py --coverage", code, secs, last_line(out), out, "coverage")
    show(r)
    results.append(r)

    names, err = scenarios(root, timeout)
    if err:
        r = Result("parity.py --list", 1, 0.0, err, err, "coverage")
        show(r)
        results.append(r)
    for n in names:
        code, secs, out = run([sys.executable, str(root / "tests" / "factory" / "parity.py"), n], root, timeout)
        r = judge(Result("parity.py " + n, code, secs, last_line(out), out, "scenario"))
        show(r)
        results.append(r)

    failed = [r for r in results if r.failed]
    ran = [r for r in results if r.executed]
    ran_tests = [r for r in ran if r.kind != "coverage"]  # the coverage report alone is not a test
    idle = [r for r in results if not r.failed and not r.executed]
    for r in failed:
        print("\n---- %s (exit %d), last %d lines ----" % (r.name, r.code, TAIL_LINES))
        print("\n".join(r.output.rstrip("\n").splitlines()[-TAIL_LINES:]))
    print("\nci_windows: %d ran and passed, %d failed, %d skipped (ran nothing)"
          % (len(ran), len(failed), len(idle)))
    for r in idle:
        print("  skipped %s: %s" % (r.name, r.note))
    if failed:
        print("ci_windows: FAIL: " + ", ".join(r.name for r in failed))
        return 1
    if not ran_tests:
        print("ci_windows: FAIL: ran zero tests; a job that tests nothing must not pass")
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
