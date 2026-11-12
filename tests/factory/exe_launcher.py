"""A claude.exe for the parity scenarios on Windows (TR-1183).

The real Claude Code is a claude.exe, and CreateProcess hands it the command line as it is. A claude.cmd
goes through cmd.exe, which cannot take an argument of several lines or a percent sign, so the scenarios that
start or tell a station with such a prompt could not run on Windows. This builds the executable form:
the console launcher pip puts in front of every console script (pip's vendored distlib: t64.exe, t32.exe,
t64-arm.exe), then a `#!python.exe` line, then a zip holding one `__main__.py`. Running claude.exe starts
python.exe with the executable itself as the script, which Python runs as a zip application; the arguments are
passed on untouched, with no shell in between.

`payload()` and `build_exe()` take the launcher bytes as a parameter, so a test on any platform can check the
file and run it as a zip application (python claude.exe args) without a Windows machine.
"""
from __future__ import annotations

import io
import platform
import sys
import zipfile
from pathlib import Path


def runner_source(fake: Path, extra: list) -> str:
    """The __main__.py of the executable: the fake's own argv, then what the caller passed."""
    return (
        "import runpy, sys\n"
        "fake = %r\n"
        "sys.argv = [fake] + %r + sys.argv[1:]\n"
        "runpy.run_path(fake, run_name='__main__')\n" % (str(fake), list(extra))
    )


def payload(python: str, fake: Path, extra: list) -> bytes:
    """What follows the launcher: the shebang line, then the zip. A python path with a space is quoted."""
    shebang = ('#!"%s"\n' if " " in python else "#!%s\n") % python
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", runner_source(fake, extra))
    return shebang.encode("utf-8") + buf.getvalue()


def launcher_name(machine: str | None = None) -> str:
    m = (machine or platform.machine()).lower()
    if m in ("arm64", "aarch64"):
        return "t64-arm.exe"
    return "t64.exe" if sys.maxsize > 2 ** 32 and m in ("amd64", "x86_64") else "t32.exe"


def find_launcher(machine: str | None = None) -> bytes:
    """The console launcher shipped inside pip. Raises when this pip does not carry it."""
    name = launcher_name(machine)
    try:
        import pip
    except ImportError as e:
        raise RuntimeError("no claude.exe fake: pip is not installed (%s)" % e)
    f = Path(pip.__file__).parent / "_vendor" / "distlib" / name
    if not f.is_file():
        raise RuntimeError("no claude.exe fake: pip's launcher %s is missing (%s)" % (name, f))
    return f.read_bytes()


def build_exe(path: Path, python: str, fake: Path, extra: list, launcher: bytes | None = None) -> Path:
    """Write the executable at path (a .exe) and return it."""
    path.write_bytes((launcher if launcher is not None else find_launcher()) + payload(python, fake, extra))
    return path
