"""tests/ci_e2e.py run(): a FAIL line fails a suite that exits 0, a clean suite passes, a non-zero exit fails,
and a suite that goes silent is killed at the timeout (it does not wait for the next line of output)."""
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_e2e

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def bounded(fn, seconds):
    """(finished, result): fn runs in a thread and is given `seconds`; a hang is a result, not a hang."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    return (not t.is_alive()), (box[0] if box else None)


def stub(ui, name, js):
    (ui / "tests").mkdir(exist_ok=True)
    (ui / "tests" / f"{name}.e2e.cjs").write_text(js)


if not shutil.which("node"):
    print("node is not installed; this test needs it")
    sys.exit(1)

tmp = Path(tempfile.mkdtemp(prefix="ci-e2e-"))
try:
    stub(tmp, "clean", 'console.log("  ok   something");\n')
    stub(tmp, "failline", 'console.log("  FAIL a check that failed");\n')  # exit 0, FAIL line
    stub(tmp, "badexit", 'console.log("  ok   x"); process.exit(3);\n')
    pidfile = tmp / "silent.pid"
    stub(tmp, "silent", f'require("fs").writeFileSync({str(pidfile)!r}, String(process.pid));\n'
                        'console.log("  ok   start"); setInterval(() => {}, 1000);\n')  # prints once, then nothing
    suites = ["clean", "failline", "badexit", "silent"]

    rc, fails = ci_e2e.run("clean", suites, ui=str(tmp))
    ck("a clean suite passes", rc == 0 and fails == [], f"{rc} {fails}")

    rc, fails = ci_e2e.run("failline", suites, ui=str(tmp))
    ck("a FAIL line fails a suite that exits 0", rc == 0 and len(fails) == 1, f"{rc} {fails}")

    rc, fails = ci_e2e.run("badexit", suites, ui=str(tmp))
    ck("a non-zero exit fails the suite", rc == 3, f"{rc} {fails}")

    # If the kill ever stops working, run() would wait for output that never comes. The test gives it a
    # bound of its own, so that shows up as a failed check and not as a CI job that hangs.
    t = time.time()
    done, got = bounded(lambda: ci_e2e.run("silent", suites, ui=str(tmp), timeout=2), 8)
    took = time.time() - t
    rc, fails = got if done else (None, None)
    ck("a silent suite is killed at the timeout, within the test's own bound",
       done and rc == 124 and took < 8, f"finished {done}, rc {rc}, {took:.1f}s")
    if not done and pidfile.exists():  # clear the suite the broken kill left behind, so the thread ends
        try:
            os.killpg(int(pidfile.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass

    # The race: the timer fires after the suite has ended and before the runner cancels it. The suite's
    # own result stands; it is not turned into a timeout. The fake timer is fired by hand at that moment.
    class HeldTimer:
        last = None
        def __init__(self, interval, fn):
            self.fn = fn
            HeldTimer.last = self
        def start(self):
            pass
        def cancel(self):
            pass

    real_popen = ci_e2e.subprocess.Popen

    class FiresAfterExit(real_popen):
        def wait(self, *a, **kw):
            rc = super().wait(*a, **kw)
            HeldTimer.last.fn()  # the suite is over, the runner has not cancelled the timer yet
            return rc

    real_timer = ci_e2e.threading.Timer
    ci_e2e.threading.Timer, ci_e2e.subprocess.Popen = HeldTimer, FiresAfterExit
    try:
        late_clean = ci_e2e.run("clean", suites, ui=str(tmp))
        late_bad = ci_e2e.run("badexit", suites, ui=str(tmp))
    finally:
        ci_e2e.threading.Timer, ci_e2e.subprocess.Popen = real_timer, real_popen
    ck("a timer that fires after a clean suite ended leaves it passing",
       late_clean[0] == 0 and late_clean[1] == [], f"{late_clean}")
    ck("a timer that fires after a failed suite ended keeps its own exit code", late_bad[0] == 3, f"{late_bad}")

    # main(): a suite fails the run when it exits non-zero or prints a FAIL line, not otherwise
    def main_with(result):
        real_names, real_run, real_argv = ci_e2e.names, ci_e2e.run, sys.argv
        ci_e2e.names = lambda: ["a", "b"]
        ci_e2e.run = lambda suite, everything, **kw: result if suite == "b" else (0, [])
        sys.argv = ["ci_e2e.py"]
        try:
            return ci_e2e.main()
        finally:
            ci_e2e.names, ci_e2e.run, sys.argv = real_names, real_run, real_argv
    ck("main passes when every suite exits 0 with no FAIL line", main_with((0, [])) == 0)
    ck("main fails when a suite exits non-zero", main_with((3, [])) == 1)
    ck("main fails when a suite exits 0 but printed a FAIL line", main_with((0, ["FAIL x"])) == 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# no suite's range (its port, +1, +2 and the MCP port +20) holds a port fetch refuses
many = [f"s{i:02d}" for i in range(60)]
ranges = [(ci_e2e.port(s, many), ci_e2e.port(s, many) + ci_e2e.PORT_STEP) for s in many]
ck("no suite's range holds a blocked port, even with 60 suites",
   not any(lo <= b < hi for lo, hi in ranges for b in {10080}), ranges)
ck("the ranges do not overlap and stay in list order",
   all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:])), ranges)
ck("a short list keeps its first ports as before", [ci_e2e.port(s, many) for s in many[:3]] == [9000, 9040, 9080])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
