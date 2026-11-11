"""`tares factory roster` through the dispatcher answers in under 300 ms (TR-951).

roster goes dispatcher -> tares.crew.views. The whole path is timed from a
fresh interpreter: start Python, import tares.cli, resolve the address, dispatch, read the sessions, print
the roster. Against a throwaway crew (tests/_crew_fake.py): no stations, a fake Claude Code, no Tares.

Five timed runs after one untimed warm-up (the first run reads the modules from disk); the median must
be under 300 ms and every run must exit 0. The five timings are printed, with the same five for the
dispatcher alone (`help`, which prints the usage and does nothing else), so a slow machine shows
where the time goes.

Run: .venv/bin/python tests/test_crew_startup_time.py
"""
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _crew_fake import fake_crew_env  # noqa: E402

LIMIT_MS = 300
RUNS = 5

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


T = Path(tempfile.mkdtemp(prefix="tares crew startup "))
ENV = fake_crew_env(T)
ENV["PYTHONPATH"] = str(ROOT)
ENV.pop("PYTHONSTARTUP", None)

# what `tares factory roster` does, from the interpreter up (the installed `tares` is this same entry point)
FACTORY = [sys.executable, "-c", "import sys; sys.argv = ['tares', 'factory'] + sys.argv[1:]; from tares.cli import main; main()", "roster"]
DISPATCH_ONLY = [sys.executable, "-c",
                 "import os; from tares.crew.dispatch import run; run(['help'], dict(os.environ))"]


def timed(cmd, n=RUNS):
    """Wall time in ms of n runs after a warm-up, and the exit codes and the first stdout line."""
    subprocess.run(cmd, capture_output=True, env=ENV, cwd=str(T))
    ms, codes, first = [], [], ""
    for _ in range(n):
        t = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV, cwd=str(T))
        ms.append((time.perf_counter() - t) * 1000)
        codes.append(r.returncode)
        if not first and r.stdout:
            first = r.stdout.splitlines()[0]
    return ms, codes, first


def show(label, ms):
    print(f"  {label}: " + ", ".join(f"{m:.0f}" for m in ms) + f" ms, median {statistics.median(ms):.0f} ms")


ms, codes, first = timed(FACTORY)
show("tares factory roster ", ms)
d_ms, d_codes, _ = timed(DISPATCH_ONLY)
show("dispatcher + help    ", d_ms)

ck("every run exits 0", codes == [0] * RUNS, str(codes))
ck("...and prints the roster", first == "crew: crew (orchestrator: crew-orchestrator)", first)
ck("the dispatcher alone (interpreter, imports, usage) exits 0 every time", d_codes == [0] * RUNS, str(d_codes))
limit, why = LIMIT_MS, f"{LIMIT_MS} ms"
ck(f"median of {RUNS} runs of `tares factory roster` is under {why}", statistics.median(ms) < limit,
   f"median {statistics.median(ms):.0f} ms; dispatcher alone {statistics.median(d_ms):.0f} ms")
ck(f"the dispatcher's own share (interpreter, imports, exec) is under {LIMIT_MS // 2} ms", statistics.median(d_ms) < LIMIT_MS // 2,
   f"median {statistics.median(d_ms):.0f} ms")

shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
