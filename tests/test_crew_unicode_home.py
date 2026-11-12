"""The Python crew engine in a crew home whose path has a space and a letter outside ASCII, on a console that
is not UTF-8 (TR-976). A Windows user name can have both (C:\\Users\\Jane Do\u00e9), and a console there often
uses a legacy code page. Each command must run, print UTF-8 whatever the console says, and never raise
UnicodeEncodeError.

Real `python -m tares.crew` subprocesses against the throwaway crew of tests/_crew_fake.py (its fake Claude Code
swapped for a launcher that also starts on Windows), with
PYTHONUTF8=0 and PYTHONIOENCODING=cp1252 so the console is the hostile one. Nothing reaches a real crew home.

Run: python tests/test_crew_unicode_home.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _crew_fake import fake_crew_env  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {str(detail)[-1500:]}"))


TOP = Path(tempfile.mkdtemp(prefix="Jane Do\u00e9 ")).resolve()
PROJECT = "Caf\u00e9 \u65e5\u672c"  # an e-acute and two CJK characters: none fits cp1252 but the first

env = fake_crew_env(TOP)
crew = Path(env["FACTORY_CREW_HOME"])
station = {"role": "builder", "status": "active", "project": PROJECT, "repo": "", "cwd": "", "model": "opus",
           "effort": "high", "id": "aaaa0001"}
(crew / "stations.json").write_text(json.dumps({"shop-build-m1": station}, ensure_ascii=False), encoding="utf-8")
env.update(PYTHONPATH=str(ROOT), PYTHONUTF8="0", PYTHONIOENCODING="cp1252")
env.pop("PYTHONSTARTUP", None)


def make_launcher(folder, script):
    """A `claude` that runs `python script args`: a sh script, or claude.cmd on Windows, where tests/factory/fake-claude
    (a sh script) cannot start. The same launcher test_crew_claude.py uses."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import ctypes
        page = "cp%d" % ctypes.windll.kernel32.GetOEMCP()  # cmd.exe reads a batch file in the OEM code page
        f = folder / "claude.cmd"
        f.write_bytes(('@"%s" "%s" %%*\r\n' % (sys.executable, script)).encode(page))
    else:
        f = folder / "claude"
        f.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script), encoding="utf-8")
        f.chmod(0o755)
    return str(f)


env["CLAUDE_BIN"] = make_launcher(TOP / "launcher", ROOT / "tests" / "factory" / "fake_claude.py")


def tares(*argv):
    p = subprocess.run([sys.executable, "-m", "tares.crew"] + list(argv), env=env, cwd=str(ROOT),
                       capture_output=True, timeout=120)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8", "replace")


print("the crew home has a space and a non-ASCII letter")
ck("the crew home path has both", " " in str(crew) and "\u00e9" in str(crew), str(crew))

print("roster")
code, out, err = tares("roster")
ck("exit 0", code == 0, (code, err))
ck("it prints the project name as UTF-8, whatever the console code page", PROJECT in out, out)
ck("no UnicodeEncodeError", "UnicodeEncodeError" not in err and "Traceback" not in err, err)

print("doctor --json")
code, out, err = tares("doctor", "--json")
try:
    doc = json.loads(out)
except ValueError as e:
    doc = {}
    ck("stdout is JSON", False, "%s: %r %s" % (e, out[:200], err))
home = [c for c in doc.get("checks", []) if c.get("id") == "crew_home"]
ck("the crew_home check names the folder with its space and its letter", bool(home) and str(crew) in home[0]["message"], home)
ck("no UnicodeEncodeError", "UnicodeEncodeError" not in err and "Traceback" not in err, err)

print("a message with the path")
gone = TOP / "not there \u65e5"
code, out, err = tares("tidy", str(gone))
ck("tidy of a path that is not there says so, naming the path whole, and exits 0", code == 0 and str(gone) in err and "already gone" in err, (code, err))
ck("no UnicodeEncodeError", "UnicodeEncodeError" not in err and "Traceback" not in err, err)

shutil.rmtree(TOP, ignore_errors=True)
print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
