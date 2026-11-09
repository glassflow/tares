"""A hook starts fast: `tares hook ship --dry-run` stays under 400 ms, and starting it loads
nothing heavy (TR-1068).

A hook runs at every tool call, so the start of `tares hook` must not pull in DuckDB, FastAPI, yaml,
httpx or the daemon. This runs the command 7 times in a fresh interpreter, takes the median wall
time and fails above LIMIT_MS, printing the time. It also imports tares.cli and tares.crew.hooks in
one interpreter and fails when any heavy module appears in sys.modules.

Run: python tests/test_hook_start.py
"""
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIMIT_MS = 400
RUNS = 7

P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


HOME = tempfile.mkdtemp(prefix="hook_start_")
ENV = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
ENV.update(HOME=HOME, PYTHONPATH=str(ROOT), TARES_HOME=HOME + "/tares-home")
SHIM_PY = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"

print("start time")
times = []
for _ in range(RUNS):
    t = time.perf_counter()
    r = subprocess.run([sys.executable, "-c", SHIM_PY, "hook", "ship", "--dry-run"], capture_output=True, env=ENV, cwd=HOME)
    times.append((time.perf_counter() - t) * 1000)
    if r.returncode != 0:
        break
med = statistics.median(times)
print(f"  `tares hook ship --dry-run`: median {med:.0f} ms over {len(times)} runs (min {min(times):.0f}, max {max(times):.0f}); limit {LIMIT_MS} ms")
ck(f"the median start is under {LIMIT_MS} ms (it was {med:.0f} ms)", r.returncode == 0 and med < LIMIT_MS, (r.returncode, med))

print("imports")
HEAVY = ["duckdb", "fastapi", "starlette", "uvicorn", "yaml", "httpx", "pydantic", "psutil", "keyring",
         "tares.daemon", "tares.config", "tares.store"]
code = ("import sys\nimport tares, tares.cli, tares.crew.hooks\n"
        f"print(' '.join(m for m in {HEAVY!r} if m in sys.modules))")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=ENV, cwd=HOME)
ck("importing tares, tares.cli and tares.crew.hooks loads no heavy module", r.returncode == 0 and r.stdout.strip() == "",
   (r.returncode, r.stdout.strip(), r.stderr[-200:]))
# the real entry: main() with `hook ship --dry-run`, then what is loaded
code = ("import sys\nsys.argv = ['tares', 'hook', 'ship', '--dry-run']\nfrom tares.cli import main\n"
        "try:\n    main()\nexcept SystemExit:\n    pass\n"
        f"print('LOADED:' + ' '.join(m for m in {HEAVY!r} if m in sys.modules))")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=ENV, cwd=HOME)
loaded = [ln[7:].strip() for ln in r.stdout.splitlines() if ln.startswith("LOADED:")]
ck("running `tares hook ship --dry-run` through main() loads no heavy module (config is not imported first)",
   r.returncode == 0 and loaded == [""], (r.returncode, loaded, r.stderr[-200:]))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
