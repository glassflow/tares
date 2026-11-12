"""Tests that exec or spawn a crew process run on a scratch home (TR-1164).

A watch, crew up, crew restart or a revive that is handed no scratch home attaches to the real ~/factory: a test once
started a real watch from a worktree and the live stations moved onto it. tests/_crew_guard.py refuses such a start in
the tests that can make one. This script checks:

  * what the guard calls the real home (HOME or USERPROFILE, FACTORY_CREW_HOME or FACTORY_HOME, a crew process with
    none of them) and what it lets through (a scratch home, a process that is not the crew);
  * the guard on the real calls: the old environment of tests/test_crew_kit.py (PATH and the engine, no home) handed to
    the real watch.restart_self and watch.start is refused before anything starts, and the refusal cannot be swallowed;
  * that each test file that restarts or starts a watch, or runs crew up, installs the guard.

Nothing here starts a process: a refused start never gets to the exec, and the allowed one is `python -c pass`.

Run: python tests/test_crew_scratch_home.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-scratch-home-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
CREW = SCRATCH / "crew"
CREW.mkdir()
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), FACTORY_HOME=str(CREW),
                  FACTORY_CREW_HOME=str(CREW), PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
                  TARES_HOME=str(SCRATCH / "tares-home"))
(SCRATCH / "home").mkdir()
sys.path.insert(0, str(ROOT))

from tares.crew import watch  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def refused(fn):
    """Whether fn raises RealHomeReached; the violation it noted is cleared (this script provokes them on purpose)."""
    try:
        fn()
    except _crew_guard.RealHomeReached:
        return True
    except Exception as e:   # anything else is not the guard
        print(f"       (raised {type(e).__name__}: {e})")
        return False
    finally:
        _crew_guard.VIOLATIONS.clear()
    return False


REAL = _crew_guard.REAL_HOME
REAL_F = os.path.join(REAL, "factory")
HOME = os.environ["HOME"]
WATCH = [sys.executable, "-m", "tares.crew", "crew", "watch"]
SCRATCH_ENV = {"PATH": os.environ.get("PATH", ""), "HOME": HOME, "USERPROFILE": HOME, "FACTORY_HOME": str(CREW),
               "FACTORY_CREW_HOME": str(CREW)}

print("what counts as the real home")
ck("the real home is not this script's scratch home", os.path.realpath(REAL) != os.path.realpath(HOME), REAL)
ck("a scratch home for a watch is fine", _crew_guard.problems(WATCH, SCRATCH_ENV) == [], str(_crew_guard.problems(WATCH, SCRATCH_ENV)))
ck("...with or without FACTORY_HOME (the Python engine reads FACTORY_CREW_HOME)",
   _crew_guard.problems(WATCH, {k: v for k, v in SCRATCH_ENV.items() if k != "FACTORY_HOME"}) == [])
ck("a watch with no home at all is refused (the environment test_crew_kit.py handed restart_self)",
   len(_crew_guard.problems(WATCH, {"PATH": "/usr/bin"})) == 2)
ck("a watch with a scratch HOME but no FACTORY_CREW_HOME is refused: it would use ~/factory",
   any("FACTORY_CREW_HOME" in p for p in _crew_guard.problems(WATCH, {k: v for k, v in SCRATCH_ENV.items() if "CREW" not in k})))
ck("a watch whose HOME is the real home is refused", any("HOME is the real home" in p for p in _crew_guard.problems(WATCH, {**SCRATCH_ENV, "HOME": REAL})))
ck("...and so is USERPROFILE", any("USERPROFILE is the real home" in p for p in _crew_guard.problems(WATCH, {**SCRATCH_ENV, "USERPROFILE": REAL})))
ck("FACTORY_CREW_HOME on the real ~/factory is refused", any("FACTORY_CREW_HOME" in p for p in _crew_guard.problems(WATCH, {**SCRATCH_ENV, "FACTORY_CREW_HOME": REAL_F})))
ck("...and a folder inside it", any("FACTORY_CREW_HOME" in p for p in _crew_guard.problems(WATCH, {**SCRATCH_ENV, "FACTORY_CREW_HOME": REAL_F + "/sub"})))
ck("...FACTORY_HOME too", any("FACTORY_HOME" in p for p in _crew_guard.problems(WATCH, {**SCRATCH_ENV, "FACTORY_HOME": REAL_F})))
ck("a folder that only starts with the same letters is not the real ~/factory",
   _crew_guard.problems(WATCH, {**SCRATCH_ENV, "FACTORY_CREW_HOME": REAL_F + "-scratch"}) == [])
ck("a process that is not the crew needs no home of its own", _crew_guard.problems([sys.executable, "-c", "pass"], {"PATH": "/usr/bin"}) == [])
ck("...but still may not carry the real one", _crew_guard.problems([sys.executable, "-c", "pass"], {"HOME": REAL}) != [])
ck("a probe that only imports tares.crew in its -c code is not the crew",
   not _crew_guard.crew_like([sys.executable, "-I", "-c", "import tares.crew; print(1)"]))
ck("`python -m tares.crew` and `tares factory` are the crew",
   _crew_guard.crew_like(WATCH) and _crew_guard.crew_like(["tares", "factory", "roster"]))
ck("a bare `factory` command (an old install's) is looked at too", _crew_guard.crew_like(["/usr/local/bin/factory", "roster"]))
ck("no env given: this process's own environment is looked at, and it is scratch", _crew_guard.problems(WATCH) == [])

print("the guard on the real calls")
_crew_guard.install()
_crew_guard.install()   # twice is the same as once
OLD_KIT_ENV = {"PATH": os.environ.get("PATH", "")}   # what test_crew_kit.py handed restart_self before TR-1164
paths = CrewPaths.at(CREW)
ck("the real restart_self handed no home is refused before it execs",
   refused(lambda: watch.restart_self(paths, 7, OLD_KIT_ENV)))
ck("watch.start handed no home is refused before it spawns, and writes no pid file",
   refused(lambda: watch.start(paths, OLD_KIT_ENV)) and not (CREW / "watch.pid").exists())
ck("a Popen of the watch on the real home is refused", refused(lambda: subprocess.Popen(WATCH, env={**SCRATCH_ENV, "FACTORY_CREW_HOME": REAL_F})))
ck("...as is one that inherits a real HOME", refused(lambda: subprocess.Popen(WATCH, env={**SCRATCH_ENV, "HOME": REAL})))


def swallowed():
    try:
        subprocess.Popen(WATCH, env=OLD_KIT_ENV)
    except Exception:   # code under test with a broad handler
        pass


swallowed()
ck("a refusal that code under test swallows is still written down, for the exit hook to fail the run", len(_crew_guard.VIOLATIONS) == 1,
   str(_crew_guard.VIOLATIONS))
_crew_guard.VIOLATIONS.clear()
r = subprocess.run([sys.executable, "-c", "pass"], env=SCRATCH_ENV)
ck("a start on a scratch home goes through", r.returncode == 0 and _crew_guard.VIOLATIONS == [])
r = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; import _crew_guard; _crew_guard.install(); "
                    "import subprocess; "
                    "\ntry:\n subprocess.Popen(['x', '-m', 'tares.crew'], env={'PATH': ''})\nexcept Exception:\n pass\n",
                    str(ROOT / "tests"), str(ROOT)], env=SCRATCH_ENV, capture_output=True, text=True)
ck("a run that swallowed a refusal ends non-zero and says so", r.returncode == 1 and "reached the real home" in r.stderr, str((r.returncode, r.stderr[-200:])))

r = subprocess.run([sys.executable, "-c",
                    "import os, sys; sys.path.insert(0, sys.argv[1]); import _crew_guard as g; before = os.environ['HOME']; "
                    "a = g.scratch_home(); b = g.scratch_home(); e = os.environ; "
                    "print(a == b, e['HOME'] != before, e['HOME'] == e['USERPROFILE'], e['FACTORY_CREW_HOME'] == e['FACTORY_HOME'], "
                    "g.problems([sys.executable, '-m', 'tares.crew']) == [], a)", str(ROOT / "tests")],
                   env=SCRATCH_ENV, capture_output=True, text=True)
out = r.stdout.split()
ck("scratch_home() moves a process off the home it had, once, and the guard is then content",
   r.returncode == 0 and out[:5] == ["True"] * 5, str((r.returncode, r.stdout, r.stderr[-200:])))
ck("...and the folder is gone when the process ends", len(out) >= 5 and not os.path.exists(out[-1]), str(out))

print("every test that restarts or starts a watch, or runs crew up, installs the guard")
TRIGGER = re.compile(r"restart_self\(|watch\.start\(|\"crew\", \"up\"|cmd_crew_up|cmd_crew_restart|watch_revive\.|\.revive\(|start_stations")
INSTALLS = re.compile(r"_crew_guard\.install\(\)|from _crew_fake import|import _crew_fake")
NOT_A_SPAWN = {
    "test_check_ledger.py": "names functions in a ledger fixture; starts nothing",
}
tests = sorted(p for p in (ROOT / "tests").glob("test_*.py"))
bare = []
for t in tests:
    text = t.read_text(encoding="utf-8", errors="replace")
    if TRIGGER.search(text) and not INSTALLS.search(text) and t.name not in NOT_A_SPAWN:
        bare.append(t.name)
ck("no test that can start a crew process runs without the guard (add `_crew_guard.install()` after its scratch home, or the "
   "file to NOT_A_SPAWN with the reason)", bare == [], str(bare))
ck("every file named in NOT_A_SPAWN exists", all((ROOT / "tests" / n).exists() for n in NOT_A_SPAWN))

import shutil  # noqa: E402

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
