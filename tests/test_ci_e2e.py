"""tests/ci_e2e.py run(): a FAIL line fails a suite that exits 0, a clean suite passes, a non-zero exit fails,
and a suite that goes silent is killed at the timeout (it does not wait for the next line of output)."""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_e2e

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


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
    stub(tmp, "silent", 'console.log("  ok   start"); setInterval(() => {}, 1000);\n')  # prints once, then nothing
    suites = ["clean", "failline", "badexit", "silent"]

    rc, fails = ci_e2e.run("clean", suites, ui=str(tmp))
    ck("a clean suite passes", rc == 0 and fails == [], f"{rc} {fails}")

    rc, fails = ci_e2e.run("failline", suites, ui=str(tmp))
    ck("a FAIL line fails a suite that exits 0", rc == 0 and len(fails) == 1, f"{rc} {fails}")

    rc, fails = ci_e2e.run("badexit", suites, ui=str(tmp))
    ck("a non-zero exit fails the suite", rc == 3, f"{rc} {fails}")

    t = time.time()
    rc, fails = ci_e2e.run("silent", suites, ui=str(tmp), timeout=2)
    took = time.time() - t
    ck("a silent suite is killed at the timeout", rc == 124 and took < 8, f"rc {rc}, {took:.1f}s")
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
