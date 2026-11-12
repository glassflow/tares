"""`tares factory roster` through the dispatcher answers in under 300 ms (TR-951).

roster goes dispatcher -> tares.crew.views. The whole path is timed from a
fresh interpreter: start Python, import tares.cli, resolve the address, dispatch, read the sessions, print
the roster. Against a throwaway crew (tests/_crew_fake.py): no stations, a fake Claude Code, no Tares.

Five timed runs after one untimed warm-up (the first run reads the modules from disk); the median must
be under 300 ms (or, on a machine where the dispatcher alone is slow, 6.5 times its median) and every run
must exit 0. The five timings are printed, with the same five for the
dispatcher alone (`help`, which prints the usage and does nothing else), so a slow machine shows
where the time goes.

Run: .venv/bin/python tests/test_crew_startup_time.py
"""
import json
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
# A slower machine (a CI runner) takes proportionally longer to start Python and import the engine, so the limit
# grows with the dispatcher-alone median measured in the same run. Measured on Linux CI (TR-1276), the roster
# median is 4.5 to 4.9 times the dispatcher-alone median (186/41, 223/46, 271/56, 272/55, 281/56, 283/59 and
# 310/64 ms); 6.5 keeps about a third of margin over that. On a fast machine the limit stays 300 ms, so the
# M1 check keeps its meaning there, and a roster that got a third slower still fails on CI.
RATIO = 6.5

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
limit = max(LIMIT_MS, RATIO * statistics.median(d_ms))
why = f"{limit:.0f} ms" + ("" if limit == LIMIT_MS else f" ({RATIO} times the dispatcher alone on this slower machine)")
if os.name == "nt":  # roster runs the fake Claude Code: claude.cmd, then a second Python (about 2 s here), so the limit says nothing
    print(f"  skip  median of {RUNS} runs of `tares factory roster` is under {why}: on Windows the fake Claude Code costs a Python start and a cmd.exe")
else:
    ck(f"median of {RUNS} runs of `tares factory roster` is under {why}", statistics.median(ms) < limit,
       f"median {statistics.median(ms):.0f} ms; dispatcher alone {statistics.median(d_ms):.0f} ms")
ck(f"the dispatcher's own share (interpreter, imports, exec) is under {LIMIT_MS // 2} ms", statistics.median(d_ms) < LIMIT_MS // 2,
   f"median {statistics.median(d_ms):.0f} ms")

# What the time is spent on: each call to Claude Code is a process start (the fake one is a second Python), a
# third to half of the roster. One `claude agents --json` per roster, not one for adopting and one for listing.
calls = Path(ENV["FAKE_CLAUDE_STATE"]) / "calls.jsonl"
calls.unlink(missing_ok=True)
subprocess.run(FACTORY, capture_output=True, env=ENV, cwd=str(T))
asked = [json.loads(line)["args"] for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()] if calls.is_file() else []
ck("a roster asks Claude Code for the session list once", asked.count(["agents", "--json"]) == 1, str(asked))

shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
