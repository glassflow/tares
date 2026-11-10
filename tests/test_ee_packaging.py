"""ee/ ships in the tares wheel: the package tares_ee, both licence files, the licence expression, and the Dockerfile copies ee/."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _env import clean_env

ROOT = Path(__file__).resolve().parent.parent

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def first_line(path):
    return path.read_text().splitlines()[0] if path.read_text() else ""


# static checks, no build
ck("ee/LICENSE starts with its title", first_line(ROOT / "ee" / "LICENSE") == "Tares Enterprise Licence")
ck("tares_ee/__init__.py starts with the licence header",
   first_line(ROOT / "ee" / "tares_ee" / "__init__.py") == "# Tares enterprise licence, see ee/LICENSE")
ck("the Dockerfile copies ee/", "COPY ee/ ./ee/" in (ROOT / "deploy" / "docker" / "Dockerfile").read_text().splitlines())

# the wheel
tmp = Path(tempfile.mkdtemp(prefix="tares-ee-wheel-"))
try:
    env = clean_env()
    if not (ROOT / "ui" / "dist").is_dir():
        # the wheel force-includes the built console, so it must exist
        for cmd in (["npm", "ci"], ["npm", "run", "build"]):
            r = subprocess.run(cmd, cwd=ROOT / "ui", capture_output=True, text=True, env=env)
            ck(f"console: {' '.join(cmd)}", r.returncode == 0, (r.stdout + r.stderr)[-500:])
    if importlib.util.find_spec("pip"):
        build = [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(tmp)]
    else:  # an environment without pip (a uv tool env): uv builds the same wheel with the same backend
        build = ["uv", "build", "--wheel", "--out-dir", str(tmp)]
    r = subprocess.run(build, cwd=ROOT, capture_output=True, text=True, env=env)
    wheels = sorted(tmp.glob("tares-*.whl"))
    ck("the wheel builds", r.returncode == 0 and len(wheels) == 1, (r.stdout + r.stderr)[-800:])
    if wheels:
        with zipfile.ZipFile(wheels[0]) as z:
            names = z.namelist()
            ck("the wheel holds tares_ee/__init__.py", "tares_ee/__init__.py" in names)
            ck("the wheel has no ee/ folder of its own", not any(n.startswith("ee/") for n in names))
            ck("the wheel still holds the tares package", "tares/__init__.py" in names or "tares/cli.py" in names)
            lic = [n for n in names if ".dist-info/" in n and "LICENSE" in n]
            ck("dist-info holds the top-level LICENSE", any(n.endswith("licenses/LICENSE") for n in lic), str(lic))
            ck("dist-info holds ee/LICENSE", any(n.endswith("licenses/ee/LICENSE") for n in lic), str(lic))
            meta = [n for n in names if n.endswith(".dist-info/METADATA")]
            text = z.read(meta[0]).decode() if meta else ""
            ck("METADATA has the licence expression",
               "License-Expression: MIT AND LicenseRef-Tares-EE" in text.splitlines(), text[:400])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
