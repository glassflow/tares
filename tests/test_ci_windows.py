"""tests/ci_windows.py, the runner of the Windows job, against a fake tree (TR-1066).

The runner must pass when the tests pass, fail when one fails, fail when it ran zero tests, fail when
tests/test_crew_packaging.py is missing, and cope with a folder whose name has a space and a letter outside
ASCII (a Windows user name can). Each case builds a small tree in a temp folder and runs the real runner on
it with --root; nothing here needs Windows.

Run: python tests/test_ci_windows.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "ci_windows.py"

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else "  " + str(detail)[-1500:]))


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

# Everything lives under a folder with a space and an e-acute, as a Windows user's profile can.
BASE = Path(tempfile.mkdtemp(prefix="Jane Do\u00e9 ")).resolve()
N = [0]

PASSING = 'print("  ok   one")\nprint("\\n2 passed, 0 failed")\n'
FAILING = 'print("  FAIL one")\nprint("\\n0 passed, 1 failed")\nimport sys\nsys.exit(1)\n'
SKIPPING = 'print("skip: needs bash")\nprint("\\n0 passed, 0 failed")\n'


def parity(scenarios=("alpha", "beta"), coverage_code=0, bad=(), both_skip=False):
    """A stand-in for tests/factory/parity.py: --list, --coverage and one scenario by name."""
    return (
        "import sys\n"
        "a = sys.argv[1:]\n"
        "if a == ['--list']:\n    print(%r)\n    sys.exit(0)\n" % "\n".join(scenarios)
        + "if a == ['--coverage']:\n    print('coverage: 1 of 2 scenarios pass against Python (50.0%%) in 1 suites')\n"
        "    sys.exit(%d)\n" % coverage_code
        + "if a[0] in %r:\n    print('FAIL python: 1 of 1 checks failed')\n    sys.exit(1)\n" % (list(bad),)
        + ("print('SKIP bash: no bash')\nprint('SKIP python: not ported')\n" if both_skip
           else "print('SKIP bash: no bash')\nprint('PASS python: 2 checks')\n")
    )


def tree(tests=None, par=None, packaging=PASSING, extra=None):
    """A fake checkout: tests/test_crew_packaging.py, the given tests/test_crew_*.py, a fake parity.py."""
    N[0] += 1
    root = BASE / ("tree %d" % N[0])
    (root / "tests" / "factory").mkdir(parents=True)
    files = {}
    if packaging is not None:
        files["test_crew_packaging.py"] = packaging
    files.update(tests or {})
    files.update(extra or {})
    for name, body in files.items():
        (root / "tests" / name).write_text(body, encoding="utf-8")
    if par is not None:
        (root / "tests" / "factory" / "parity.py").write_text(par, encoding="utf-8")
    return root


def runner(root, *flags):
    p = subprocess.run([sys.executable, str(RUNNER), "--root", str(root)] + list(flags), capture_output=True,
                       env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
    return p.returncode, p.stdout.decode("utf-8", "replace").replace("\r\n", "\n") + p.stderr.decode("utf-8", "replace")


print("all tests pass")
code, out = runner(tree({"test_crew_state.py": PASSING}, parity()))
ck("exit 0", code == 0, out)
ck("it names each test it ran", "tests/test_crew_packaging.py" in out and "tests/test_crew_state.py" in out, out)
ck("it names the coverage run and each scenario", "parity.py --coverage" in out and "parity.py alpha" in out and "parity.py beta" in out, out)
ck("the summary counts what ran: 2 tests and 2 scenarios, plus the coverage report", "5 ran and passed, 0 failed, 0 skipped" in out, out)
ck("the last line of each test is shown", "2 passed, 0 failed" in out, out)
ck("the folder with a space and a non-ASCII letter is handled", "Jane Do\u00e9 " in out, out)

print("a test fails")
code, out = runner(tree({"test_crew_state.py": FAILING}, parity()))
ck("exit 1", code == 1, out)
ck("the failing test is named, with its output", "FAIL  tests/test_crew_state.py" in out and "0 passed, 1 failed" in out, out)
ck("the others still ran", "tests/test_crew_packaging.py" in out and "parity.py alpha" in out, out)
ck("the summary names the failure", "ci_windows: FAIL: tests/test_crew_state.py" in out, out)

print("a failure early in a long output is not lost in the tail")
LONG = "print('  FAIL the early one  detail-xyz')\nfor i in range(120):\n    print('  ok   filler %d' % i)\nprint('0 passed, 1 failed')\nraise SystemExit(1)\n"
code, out = runner(tree({"test_crew_state.py": LONG}, parity()))
ck("its FAIL line is printed on its own", code == 1 and "its 1 FAIL lines" in out and out.count("FAIL the early one  detail-xyz") == 1, out)

print("a failure is printed when it happens, not only at the end")
code, out = runner(tree({"test_crew_state.py": PASSING}, parity(), packaging=FAILING))
ck("its details come before the next test's line, so a job killed by its timeout still shows them",
   code == 1 and 0 <= out.index("---- tests/test_crew_packaging.py") < out.index("ok    tests/test_crew_state.py"), out)

print("a test that crashes (a traceback, not a clean failure)")
code, out = runner(tree({"test_crew_state.py": "raise RuntimeError('boom')\n"}, parity()))
ck("exit 1 and the traceback is shown", code == 1 and "RuntimeError: boom" in out, out)

print("a parity scenario fails, and the coverage report fails")
code, out = runner(tree({}, parity(bad=["beta"])))
ck("a failing scenario fails the run", code == 1 and "FAIL  parity.py beta" in out, out)
code, out = runner(tree({}, parity(coverage_code=1)))
ck("a failing coverage run fails the run", code == 1 and "FAIL  parity.py --coverage" in out, out)

print("a test that never finishes")
code, out = runner(tree({"test_crew_state.py": "import time\ntime.sleep(60)\n"}, parity()), "--timeout", "2")
ck("it is stopped, reported as exit 124, and fails the run", code == 1 and "exit 124" in out and "timed out after 2s" in out, out)

print("zero tests")
code, out = runner(tree({}, parity(scenarios=(), both_skip=True), packaging=SKIPPING))
ck("a packaging test that ran no checks and no scenarios: exit 1 with a clear message", code == 1 and "ran zero tests" in out, out)
ck("...and the skipped test is listed as skipped, not as passed", "1 ran and passed" in out and "skipped tests/test_crew_packaging.py" in out, out)
code, out = runner(tree({"test_crew_state.py": SKIPPING}, parity(both_skip=True), packaging=SKIPPING))
ck("every scenario with both engines skipped does not count either", code == 1 and "ran zero tests" in out and "skipped parity.py alpha" in out, out)
code, out = runner(tree({}, parity(both_skip=True)))
ck("one test that ran is enough, scenarios that ran nothing are only listed", code == 0 and "2 ran and passed" in out and "skipped parity.py alpha" in out, out)

print("tests/test_crew_packaging.py or parity.py missing")
code, out = runner(tree({"test_crew_state.py": PASSING}, parity(), packaging=None))
ck("no packaging test: exit 1 naming it, even though another test would pass", code == 1 and "tests/test_crew_packaging.py is missing" in out, out)
ck("...and nothing ran", "0 tests ran" in out and "passed, 0 failed" not in out, out)
code, out = runner(tree({"test_crew_state.py": PASSING}, None))
ck("no parity.py: exit 1 naming it", code == 1 and "tests/factory/parity.py is missing" in out, out)
code, out = runner(BASE / "does not exist")
ck("a root that is not there: exit 1", code == 1, out)

print("the skip list")
marker = BASE / "standup ran"
t = tree({"test_crew_standup.py": "open(%r, 'w').close()\nimport sys\nsys.exit(1)\n" % str(marker)}, parity())
code, out = runner(t)
ck("test_crew_standup.py is not run, and the output says why", code == 0 and not marker.exists() and "skip  test_crew_standup.py" in out and "DuckDB" in out, out)

print("a test that exits 0 and prints no summary line")
code, out = runner(tree({"test_crew_state.py": "print('all fine')\n"}, parity()))
ck("it fails the run: a test that cannot say what it checked is not a pass",
   code == 1 and "FAIL  tests/test_crew_state.py" in out and "printed no" in out, out)

print("the floors")
code, out = runner(tree({"test_crew_state.py": PASSING}, parity()), "--floor", "test_crew_state.py=3")
ck("a test below its floor fails the run, and the note says so",
   code == 1 and "FAIL  tests/test_crew_state.py" in out and "fewer than the floor of 3" in out, out)
code, out = runner(tree({"test_crew_state.py": PASSING}, parity()), "--floor", "test_crew_state.py=2")
ck("a test exactly at its floor passes", code == 0, out)
code, out = runner(tree({"test_crew_state.py": PASSING}, parity()))
ck("without --floors nothing is checked against a floor (the fake trees pass 2 checks)", code == 0, out)
code, out = runner(tree({"test_crew_packaging.py": PASSING}, parity()), "--floors")
ck("--floors applies the built-in table: the fake packaging test passes fewer checks than its floor",
   code == 1 and "tests/test_crew_packaging.py" in out and "fewer than the floor of" in out, out)
fl = Path(RUNNER).read_text(encoding="utf-8")
names = sorted(p.name for p in (ROOT / "tests").glob("test_crew_*.py"))
import re as _re
table = set(_re.findall(r'^\s+"(test_crew_\w+\.py)": \d+,', fl, _re.M))
ck("every floor names a test that exists", table <= set(names), sorted(table - set(names)))

print("the extra tests")
marker = BASE / "extra ran"
code, out = runner(tree({}, parity(), extra={"test_ci_windows.py": "open(%r, 'w').close()\nprint('2 passed, 0 failed')\n" % str(marker)}))
ck("test_ci_windows.py runs after the crew tests when it exists", code == 0 and marker.exists() and "tests/test_ci_windows.py" in out, out)
marker.unlink()
code, out = runner(tree({}, parity(), extra={"test_parity.py": "open(%r, 'w').close()\n" % str(marker)}))
ck("test_parity.py is not run", code == 0 and not marker.exists(), out)
code, out = runner(tree({}, parity(), extra={"test_ci_windows.py": FAILING}))
ck("a failing extra test fails the run", code == 1 and "FAIL  tests/test_ci_windows.py" in out, out)

print("--list runs nothing")
marker = BASE / "list ran"
t = tree({"test_crew_state.py": "open(%r, 'w').close()\n" % str(marker)}, parity())
code, out = runner(t, "--list")
ck("it prints the tests, the coverage run and the scenarios, and exits 0", code == 0 and "test  tests/test_crew_state.py" in out
   and "test  tests/factory/parity.py --coverage" in out and "test  tests/factory/parity.py alpha" in out, out)
ck("...without running a test", not marker.exists())
code, out = runner(tree({}, parity(), packaging=None), "--list")
ck("--list on a tree with no packaging test says MISSING and exits 1", code == 1 and "MISSING tests/test_crew_packaging.py" in out, out)

print("the real checkout")
code, out = runner(ROOT, "--list")
ck("it would run tests/test_crew_packaging.py and the parity coverage here", code == 0 and "test  tests/test_crew_packaging.py" in out
   and "test  tests/factory/parity.py --coverage" in out, out)
ck("test_crew_standup.py is the only crew test it skips", [ln for ln in out.splitlines() if ln.startswith("skip ")] != []
   and len([ln for ln in out.splitlines() if ln.startswith("skip ")]) == 1, out)

shutil.rmtree(BASE, ignore_errors=True)
print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
