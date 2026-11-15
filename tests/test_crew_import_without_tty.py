"""The daemon's modules import where the terminal modules do not exist (Windows has no termios or tty).

tares/guardrails_api.py imports factory_setup, and the daemon imports guardrails_api, so a top-level
`import termios` there stopped the daemon from starting on Windows (TR-862). The check runs in a child
python that has termios and tty blocked, so it fails the same way on any machine.

Run: python tests/test_crew_import_without_tty.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def child(code):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TARES")}
    env["PYTHONPATH"] = str(ROOT)
    block = "import sys\nsys.modules['termios'] = None\nsys.modules['tty'] = None\n"
    return subprocess.run([sys.executable, "-c", block + code], capture_output=True, text=True, env=env, cwd=str(ROOT))


r = child("import tares.factory_setup")
ck("factory_setup imports with termios and tty missing", r.returncode == 0, r.stderr[-300:])
r = child("import tares.guardrails_api")
ck("guardrails_api imports with termios and tty missing", r.returncode == 0, r.stderr[-300:])
r = child("import tares.factory_setup as f\nf.PROD_PATTERN")
ck("what the rules use from factory_setup is there", r.returncode == 0, r.stderr[-300:])
r = child("import tares.factory_setup as f\nimport inspect\nsrc = inspect.getsource(f.run_form)\nassert 'import termios' in src and 'import tty' in src")
ck("run_form brings the terminal modules in itself, when the box is shown", r.returncode == 0, r.stderr[-300:])

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
