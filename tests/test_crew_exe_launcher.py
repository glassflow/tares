"""The claude.exe fake the parity scenarios use on Windows (tests/factory/exe_launcher.py, TR-1183).

Checks: the file is the launcher, then a shebang line, then a zip whose __main__.py runs the fake with the
arguments the caller gave and then the ones the process got; a python path with a space is quoted; run as a zip
application (on any platform), it hands the fake an argument of several lines and one with a percent sign
whole, and passes the fake's exit code on. On Windows it also builds the real executable from pip's launcher and
runs that, which is what a claude.cmd could not do: no cmd.exe stands between the caller and the fake.

Run: .venv/bin/python tests/test_crew_exe_launcher.py
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORY = ROOT / "tests" / "factory"
sys.path.insert(0, str(FACTORY))

import exe_launcher  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


FAKE = FACTORY / "fake_claude.py"
T = Path(tempfile.mkdtemp(prefix="exe-launcher-"))
STUB = b"MZ" + b"\0" * 62   # stands in for the launcher on a platform that has no use for it


def run(exe: Path, *args: str, state: Path):
    env = {**os.environ, "FAKE_CLAUDE_STATE": str(state), "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, str(exe), *args]   # the stub launcher is no program: run the file as a zip application
    return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def calls(state: Path) -> list:
    f = state / "calls.jsonl"
    return [json.loads(x)["args"] for x in f.read_text(encoding="utf-8").splitlines()] if f.is_file() else []


# --- the file ---------------------------------------------------------------------------------------
exe = exe_launcher.build_exe(T / "claude.exe", sys.executable, FAKE, ["--extra"], launcher=STUB)
data = exe.read_bytes()
ck("the file starts with the launcher", data.startswith(STUB), repr(data[:8]))
tail = data[len(STUB):]
nl = tail.index(b"\n")
ck("a shebang line follows, naming the python that runs it", tail[:nl].decode("utf-8").lstrip("#!").strip('"') == sys.executable, tail[:nl])
with zipfile.ZipFile(exe) as z:
    ck("a zip ends the file, with one __main__.py", z.namelist() == ["__main__.py"], str(z.namelist()))
    src = z.read("__main__.py").decode("utf-8")
ck("...that runs the fake with the caller's arguments, then the process's own", "sys.argv = [fake] + ['--extra'] + sys.argv[1:]" in src, src)
spaced = exe_launcher.payload(r"C:\Program Files\Python\python.exe", FAKE, [])
ck("a python path with a space is quoted in the shebang", spaced.startswith(b'#!"C:\\Program Files\\Python\\python.exe"\n'), repr(spaced[:60]))
ck("...and one without is not", exe_launcher.payload(r"C:\Py\python.exe", FAKE, []).startswith(b"#!C:\\Py\\python.exe\n"))
ck("the launcher is chosen by the machine",
   [exe_launcher.launcher_name(m) for m in ("ARM64", "aarch64")] == ["t64-arm.exe"] * 2
   and exe_launcher.launcher_name("AMD64") in ("t64.exe", "t32.exe"))

# --- the launcher is found even where the venv has no pip (uv venv) ----------------------------------------
want = exe_launcher.launcher_name()
ck("ensurepip's bundled wheel carries the launcher (MZ)", (exe_launcher._from_bundled_wheel(want) or b"")[:2] == b"MZ")
saved = sys.modules.get("pip", False)
sys.modules["pip"] = None  # makes `import pip` raise ImportError, as in a venv made by uv venv
try:
    ck("with no pip to import, the launcher still comes from the bundled wheel", exe_launcher.find_launcher()[:2] == b"MZ")
finally:
    if saved is False:
        sys.modules.pop("pip", None)
    else:
        sys.modules["pip"] = saved

# --- run as a zip application: the arguments arrive whole ---------------------------------------------
plain = exe_launcher.build_exe(T / "plain.exe", sys.executable, FAKE, [], launcher=STUB)
state = T / "state"
state.mkdir()
multi = "You are the builder.\nRead your report.\n  Then the ticket TR-1183."
pct = "the 5-hour window is at 85%"
r = run(plain, "--version", state=state)
ck("--version prints the fake's version and exits 0", r.returncode == 0 and "Claude Code" in r.stdout, r.stdout + r.stderr)
r = run(plain, "-p", multi, state=state)
ck("an argument of several lines reaches the fake whole", ["-p", multi] in calls(state), str(calls(state)))
r = run(plain, "-p", pct, state=state)
ck("an argument with a percent sign reaches the fake whole", ["-p", pct] in calls(state), str(calls(state)))
r = run(plain, "-p", '"quoted" and \\back', state=state)
ck("quotes and a backslash arrive as typed", ["-p", '"quoted" and \\back'] in calls(state), str(calls(state)))
extra = exe_launcher.build_exe(T / "extra.exe", sys.executable, FAKE, ["--version"], launcher=STUB)
r = run(extra, state=state)
ck("the caller's own arguments come first", r.returncode == 0 and "Claude Code" in r.stdout, r.stdout + r.stderr)
r = run(plain, state=state)
ck("the fake's exit code is the process's (no arguments: it exits 1)", r.returncode == 1, str(r.returncode))

# --- on Windows: the real executable ---------------------------------------------------------------------
if sys.platform == "win32":
    real = exe_launcher.build_exe(T / "claude-real.exe", sys.executable, FAKE, [])
    ck("pip's launcher is found and the file starts with MZ", real.read_bytes()[:2] == b"MZ")
    st = T / "state-win"
    st.mkdir()
    env = {**os.environ, "FAKE_CLAUDE_STATE": str(st), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([str(real), "--version"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    ck("claude.exe --version prints the fake's version", r.returncode == 0 and "Claude Code" in r.stdout, r.stdout + r.stderr)
    for label, arg in (("several lines", multi), ("a percent sign", pct), ("quotes and a backslash", '"quoted" and \\back')):
        subprocess.run([str(real), "-p", arg], env=env, capture_output=True, text=True, timeout=120)
        ck("claude.exe is given %s whole, with no cmd.exe in between" % label, ["-p", arg] in calls(st), str(calls(st)))
    r = subprocess.run([str(real)], env=env, capture_output=True, text=True, timeout=120)
    ck("claude.exe passes the fake's exit code on", r.returncode == 1, str(r.returncode))
else:
    print("  skip the real executable: it needs Windows")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
