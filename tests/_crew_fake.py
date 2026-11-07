"""A throwaway crew for tests that run `tares factory` for real, the way tests/factory/*_test.sh do:
a crew home with a crew.json and no stations, a fake Claude Code (tests/factory/fake-claude) and a
fake ps, everything under one temp folder. Nothing reaches the real crew home, the real Claude Code
or a running Tares.

Use: sys.path.insert(0, os.path.dirname(__file__)); from _crew_fake import fake_crew_env
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORY_DIR = ROOT / "tests" / "factory"


def fake_crew_env(tmp: Path, base: dict | None = None) -> dict:
    """An environment for `tares factory roster` against an empty crew in tmp (made if missing).
    base is the environment to start from (default os.environ)."""
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
    for f in ("fake-claude", "fake-ps"):
        (FACTORY_DIR / f).chmod(0o755)
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_PLUGIN_OPTION_"))
           and k not in ("TARESD_URL", "NAVFLOW_DB")}
    env.update(
        HOME=str(home), FACTORY_HOME=str(fhome), FACTORY_CREW_HOME=str(crew),
        FAKE_CLAUDE_STATE=str(state), CLAUDE_BIN=str(FACTORY_DIR / "fake-claude"),
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
