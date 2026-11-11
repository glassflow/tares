"""A guard for tests that exec or spawn a crew process (a watch, crew up, crew restart, a revive).

Such a process that is handed no scratch home attaches to the real ~/factory: a test once started a real watch
from a worktree and moved the live stations onto it (TR-1164). A test that can spawn one pins HOME, USERPROFILE and
FACTORY_CREW_HOME (and FACTORY_HOME, when it sets one) to a throwaway folder, and calls install() once that is done.
From then on every exec and every subprocess.Popen is looked at before it happens, and one that would reach the real
home is refused:

  * any process whose HOME or USERPROFILE is the real home, or whose FACTORY_CREW_HOME or FACTORY_HOME is the real
    ~/factory or inside it;
  * a process that looks like the crew (`python -m tares.crew`, `tares factory`) and
    gets no HOME, or no FACTORY_CREW_HOME: it would fall back to the real home's folders.

The refusal raises RealHomeReached and is also written down, and an exit hook then ends the run non-zero, so a
broad `except Exception` in the code under test cannot hide it. A test that replaces os.execve or the spawn with a
recorder calls check_exec() from it, so the environment it would have started is checked too.

Import this module before the test changes HOME: the real home is read once, here (on POSIX from the password file; on
Windows from USERPROFILE, so the guard is blind to a real home if USERPROFILE was already scratch when it was imported).

A test that builds no scratch home of its own and only runs `python -c` probes with os.environ calls scratch_home() first:
it pins HOME, USERPROFILE, FACTORY_HOME and FACTORY_CREW_HOME of this process to a throwaway folder, removed at exit.

What counts as the crew is textual (see crew_like): `python -m tares.crew`, a program named `factory`, `tares factory`.
A start by the file (`python .../tares/crew/__main__.py`) is not recognised, though the real-home checks still apply to it.

Use: sys.path.insert(0, os.path.dirname(__file__)); import _crew_guard; _crew_guard.install()
"""
import atexit
import os
import subprocess
import sys

try:
    import pwd
    REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
except (ImportError, KeyError):   # Windows: the profile folder as the process was started with it
    REAL_HOME = os.path.expanduser("~")


_scratch = None


def scratch_home() -> str:
    """Point this process's HOME, USERPROFILE, FACTORY_HOME and FACTORY_CREW_HOME at a throwaway folder (once); returns it."""
    global _scratch
    if _scratch is None:
        import shutil
        import tempfile
        _scratch = tempfile.mkdtemp(prefix="tares-crew-guard-")
        atexit.register(shutil.rmtree, _scratch, True)
        home, crew = os.path.join(_scratch, "home"), os.path.join(_scratch, "crew")
        os.makedirs(home)
        os.makedirs(crew)
        os.environ.update(HOME=home, USERPROFILE=home, FACTORY_HOME=crew, FACTORY_CREW_HOME=crew)
    return _scratch


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


REAL_FACTORY = _norm(os.path.join(REAL_HOME, "factory"))
VIOLATIONS: list = []


class RealHomeReached(AssertionError):
    """A process was about to start with the real home."""


def crew_like(argv) -> bool:
    """Whether a command line is a crew process: the module (`-m tares.crew`), a command named `factory` or
    the tares command (`tares factory`). The code after `-c` is not looked at: a probe that imports tares.crew is not one."""
    parts = [os.fspath(a) if isinstance(a, (str, os.PathLike)) else "" for a in (argv if isinstance(argv, (list, tuple)) else [argv])]
    for i, a in enumerate(parts):
        if i and parts[i - 1] == "-c":
            continue
        if a == "tares.crew" or os.path.basename(a.replace("\\", "/")) == "factory":
            return True
        if os.path.basename(a.replace("\\", "/")) in ("tares", "tares.exe") and parts[i + 1:i + 2] == ["factory"]:
            return True
    return False


def problems(argv, env=None) -> list:
    """What is wrong with starting `argv` with `env` (None: this process's environment); [] when nothing."""
    env = os.environ if env is None else env
    found = []
    like = crew_like(argv)
    home = env.get("HOME") or env.get("USERPROFILE")
    if like and not home:
        found.append("no HOME or USERPROFILE: the process would use the real home")
    for name in ("HOME", "USERPROFILE"):
        if env.get(name) and _norm(env[name]) == _norm(REAL_HOME):
            found.append(f"{name} is the real home")
    if like and not env.get("FACTORY_CREW_HOME"):
        found.append("no FACTORY_CREW_HOME: the process would use ~/factory")
    for name in ("FACTORY_CREW_HOME", "FACTORY_HOME"):
        v = env.get(name)
        if v and (_norm(v) == REAL_FACTORY or _norm(v).startswith(REAL_FACTORY + os.sep)):
            found.append(f"{name} is the real ~/factory")
    return found


def check_exec(argv, env=None) -> None:
    """Refuse (RealHomeReached) a start of `argv` with `env` that would reach the real home."""
    found = problems(argv, env)
    if found:
        text = f"{' '.join(str(a) for a in (argv if isinstance(argv, (list, tuple)) else [argv]))[:120]}: " + "; ".join(found)
        VIOLATIONS.append(text)
        raise RealHomeReached(text)


_installed = False


def install() -> None:
    """Look at every exec and every Popen from here on. Safe to call twice."""
    global _installed
    if _installed:
        return
    _installed = True
    real = {n: getattr(os, n) for n in ("execve", "execv", "execvp", "execvpe") if hasattr(os, n)}

    def execve(path, argv, env):
        check_exec([path, *argv], env)
        return real["execve"](path, argv, env)

    def execvpe(file, argv, env):
        check_exec([file, *argv], env)
        return real["execvpe"](file, argv, env)

    def execv(path, argv):
        check_exec([path, *argv])
        return real["execv"](path, argv)

    def execvp(file, argv):
        check_exec([file, *argv])
        return real["execvp"](file, argv)

    for fn in (execve, execvpe, execv, execvp):
        if fn.__name__ in real:
            setattr(os, fn.__name__, fn)

    real_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        check_exec(args, kw.get("env"))
        real_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = popen_init
    atexit.register(_at_exit)


def _at_exit() -> None:
    if VIOLATIONS:
        sys.stdout.flush()
        print("FAIL  a test reached the real home:\n  " + "\n  ".join(VIOLATIONS), file=sys.stderr, flush=True)
        os._exit(1)
