"""A throwaway crew for tests that run `tares factory` for real, the way the parity scenarios do:
a crew home with a crew.json and no stations, a fake Claude Code (tests/factory/fake_claude.py) and a
fake ps, everything under one temp folder. Nothing reaches the real crew home, the real Claude Code
or a running Tares.

Use: sys.path.insert(0, os.path.dirname(__file__)); from _crew_fake import fake_crew_env
"""
import os
import shutil
import sys
from pathlib import Path

import _crew_guard

ROOT = Path(__file__).resolve().parent.parent
FACTORY_DIR = ROOT / "tests" / "factory"


def _claude_launcher(path: Path) -> Path:
    """`claude` for the Python fake (tests/factory/fake_claude.py): a shell script, or claude.cmd on Windows, where the
    bash fake cannot run. The paths in a .cmd must be ASCII (cmd.exe reads it in the console's code page)."""
    fake = FACTORY_DIR / "fake_claude.py"
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
        lines = ['@"%s" "%s" %%*' % (sys.executable, fake), "@exit /b %ERRORLEVEL%"]
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    else:
        path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, fake), encoding="utf-8")
        path.chmod(0o755)
    return path


def fake_crew_env(tmp: Path, base: dict | None = None) -> dict:
    """An environment for `tares factory roster` against an empty crew in tmp (made if missing).
    base is the environment to start from (default os.environ)."""
    _crew_guard.install()   # a test that builds a fake crew must not spawn or exec on the real ~/factory (TR-1164)
    tmp = Path(tmp)
    crew, home, fhome, state, bindir = (tmp / n for n in ("crew", "home", "fhome", "claude", "bin"))
    for d in (crew, home, fhome, state, bindir):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", crew / "crew.json")
    (crew / "stations.json").write_text("{}\n", encoding="utf-8")
    ps = bindir / "ps"
    if not ps.exists():
        try:
            ps.symlink_to(FACTORY_DIR / "fake-ps")
        except OSError:  # Windows: a symlink needs a privilege a runner or a person often lacks
            shutil.copy(FACTORY_DIR / "fake-ps", ps)
    claude = _claude_launcher(bindir / "claude")
    (FACTORY_DIR / "fake-ps").chmod(0o755)
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_PLUGIN_OPTION_"))
           and k not in ("TARESD_URL", "NAVFLOW_DB")}
    env.update(
        HOME=str(home), USERPROFILE=str(home), FACTORY_HOME=str(fhome), FACTORY_CREW_HOME=str(crew),
        FAKE_CLAUDE_STATE=str(state), CLAUDE_BIN=str(claude),
        FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
        GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
        PATH=str(bindir) + os.pathsep + env.get("PATH", ""),
        TARESD_URL="http://127.0.0.1:9",  # nothing listens: roster works without Tares
        TARES_HOME=str(tmp / "tares-home"),
        # a regression in the TARES_KEYRING gate must fail a test, never reach a person's OS keychain
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    )
    (tmp / "gitconfig").write_text("[user]\n\tname = Test Crew\n", encoding="utf-8")
    return env
