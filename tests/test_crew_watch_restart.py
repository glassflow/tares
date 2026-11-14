"""`tares factory crew watch --restart` (TR-1279): stop the background watch and start a new one, touching no station.

The real watch runs in a real child process in a throwaway crew home, started and restarted through the command
(`python -m tares.crew crew watch --restart ...`). Checked: the pid changes, the old process is gone, stations.json
is byte for byte the same, `--if-running` starts nothing, `--only-under` leaves a watch from another install alone, and
`--json` prints the document each time.

Run: .venv/bin/python tests/test_crew_watch_restart.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-watch-restart-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
CREW = SCRATCH / "crew"
CREW.mkdir()
(CREW / "crew.json").write_text(json.dumps({"crew_name": "t"}), encoding="utf-8", newline="\n")
STATIONS = b'{"t-orchestrator":{"role":"orchestrator","pid":1}}\n'
(CREW / "stations.json").write_bytes(STATIONS)
(SCRATCH / "home").mkdir()
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), FACTORY_CREW_HOME=str(CREW),
                  FACTORY_HOME=str(CREW), PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
                  TARES_HOME=str(SCRATCH / "tares-home"))
_crew_guard.install()
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
from tares.crew import watch  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


paths = CrewPaths.at(str(CREW))
ENV = {**os.environ, "PYTHONPATH": str(ROOT), "FACTORY_WATCH_INTERVAL": "1", "FACTORY_ASK_POLL": "0.1",
       "PYTHONUNBUFFERED": "1", "TARESD_URL": "http://127.0.0.1:9"}


def run(*args, extra_env=None):
    p = subprocess.run([sys.executable, "-m", "tares.crew", "crew", "watch", *args], env={**ENV, **(extra_env or {})}, capture_output=True,
                       text=True, timeout=120, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout, p.stderr


def doc_of(out):
    lines = out.splitlines()
    try:
        return json.loads(lines[0]) if len(lines) == 1 else None
    except ValueError:
        return None


def gone(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.Error:
        return True


def wait_for(cond, seconds=20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def parent_of(pid):
    try:
        return psutil.Process(pid).ppid()
    except psutil.Error:
        return None


def settled(seconds=20.0):
    """The watch's pid once the pid file names the loop itself. On Windows a start first writes the venv launcher's
    pid, and the loop writes its own over it a moment later (`claim_pidfile`), so the pid changes with no restart."""
    out = []

    def read():
        pid = watch.running_pid(paths)
        if pid is None:
            return None
        try:
            if any("crew watch" in " ".join(c.cmdline()) for c in psutil.Process(pid).children(recursive=True)):
                return None
        except psutil.Error:
            return None
        return pid

    def done():
        pid = read()
        if pid is None:
            return False
        time.sleep(0.3)   # a launcher that has not yet spawned its loop has no child to see: the same pid twice
        if read() != pid:
            return False
        out.append(pid)
        return True
    return out[0] if wait_for(done, seconds) else None


def watch_trees():
    """The `crew watch` loops of the scratch crew home, as a list of root pids (a root is a watch process whose parent
    is not one too): a launcher with its loop is one tree, a leftover second watch or an orphaned loop is another."""
    mine = {}
    for pr in psutil.process_iter():
        try:
            if "crew watch" not in " ".join(pr.cmdline()) or "--restart" in pr.cmdline():
                continue
            if os.path.normcase(pr.environ().get("FACTORY_CREW_HOME", "")) != os.path.normcase(str(CREW)):
                continue
            mine[pr.pid] = pr.ppid()
        except (psutil.Error, OSError, SystemError):
            continue
    return sorted(p for p, pp in mine.items() if pp not in mine)


def one_tree(label):
    ok = wait_for(lambda: len(watch_trees()) == 1, 15)
    ck(label + ": exactly one watch tree for the scratch crew home", ok, watch_trees())


def cleanup():
    pid = watch.running_pid(paths)
    if pid is not None:
        watch.stop(paths, lambda m: None, timeout=5, env=ENV)


try:
    print("no watch is running")
    code, out, err = run("--restart", "--if-running", "--json")
    d = doc_of(out)
    ck("--restart --if-running --json: none_running, no pid, exit 0",
       code == 0 and d == {"schema_version": 1, "watch": {"action": "none_running"}}, (code, out, err))
    code, out, err = run("--restart", "--if-running")
    ck("...and as text: no watch is running", code == 0 and out.strip() == "no watch is running", (code, out, err))
    ck("...and nothing was started", watch.running_pid(paths) is None)

    code, out, err = run("--restart", "--json")
    d = doc_of(out)
    pid1 = (d or {}).get("watch", {}).get("pid")
    ck("--restart with none running starts one: action started and a pid",
       code == 0 and d and list(d)[0] == "schema_version" and d["watch"]["action"] == "started" and isinstance(pid1, int),
       (code, out, err))
    pid1 = settled()
    ck("...the pid file names a live watch", pid1 is not None)
    one_tree("after the first start")

    print("restart")
    code, out, err = run("--restart", "--json")
    d = doc_of(out)
    pid2 = (d or {}).get("watch", {}).get("pid")
    ck("--restart --json: restarted with a new pid",
       code == 0 and d and d["watch"]["action"] == "restarted" and isinstance(pid2, int) and pid2 != pid1,
       (code, out, err, pid1))
    ck("...the old watch process is gone", wait_for(lambda: gone(pid1)), pid1)
    w2 = settled()
    ck("...the new one runs and the pid file names it (or the loop it started)",
       w2 is not None and (w2 == pid2 or parent_of(w2) == pid2), (w2, pid2))
    ck("...stations.json is byte for byte the same", (CREW / "stations.json").read_bytes() == STATIONS)
    ck("...no stop file is left behind", not (CREW / "watch.stop").exists())
    one_tree("after the first restart")

    print("--only-under")
    exe = Path(sys.executable)
    elsewhere = SCRATCH / "elsewhere"
    elsewhere.mkdir()
    code, out, err = run("--restart", "--only-under", str(elsewhere), "--json")
    d = doc_of(out)
    ck("a folder the watch is not in: other_install, the executable named, no pid",
       code == 0 and d and d["watch"]["action"] == "other_install" and "pid" not in d["watch"]
       and d["watch"]["executable"], (code, out, err))
    ck("...the watch was left alone (same pid, still running)", watch.running_pid(paths) == w2, (watch.running_pid(paths), w2))
    code, out, err = run("--restart", "--only-under", str(elsewhere))
    ck("...as text: names the install and says it was left alone",
       code == 0 and out.startswith("the watch runs from another install (") and out.strip().endswith("): left alone"), out)
    # the folder the interpreter lives in, spelled with another case on Windows
    folder = str(exe.parent)
    if os.name == "nt":
        folder = folder.swapcase()
    code, out, err = run("--restart", "--only-under", folder, "--json")
    d = doc_of(out)
    pid3 = (d or {}).get("watch", {}).get("pid")
    ck("the folder it runs from: restarted with a new pid",
       code == 0 and d and d["watch"]["action"] == "restarted" and isinstance(pid3, int) and pid3 != pid2,
       (code, out, err, folder))
    ck("...the second old watch is gone", wait_for(lambda: gone(pid2) and gone(w2)), (pid2, w2))
    ck("...a restart without --json prints one line",
       run("--restart")[1].startswith("watch restarted, pid "))
    one_tree("after the restarts with --only-under and without --json")

    print("refusals")
    code, out, err = run("--if-running")
    ck("--if-running without --restart: usage, exit 2, text on stderr", code == 2 and out == "" and "usage:" in err, (code, out, err))
    code, out, err = run("--json", "--only-under", "x")
    d = doc_of(out)
    ck("--json without --restart: the error shape, code usage, exit 2",
       code == 2 and d and d["error"]["code"] == "usage", (code, out, err))
    code, out, err = run("--restart", "--only-under")
    ck("--only-under without a folder: usage, exit 2", code == 2 and "needs a folder" in err, (code, out, err))
    ck("a refused restart left the watch running", watch.running_pid(paths) is not None)
    live = settled()
    code, out, err = run("--restart", "--only-under", "")
    ck("--only-under with an empty folder: usage, exit 2, not a restart", code == 2 and "needs a folder" in err, (code, out, err))
    code, out, err = run("--restart", "--only-under", "--json")
    d = doc_of(out)
    ck("--only-under does not swallow a following --json: the usage error comes as JSON",
       code == 2 and d and d["error"]["code"] == "usage", (code, out, err))
    ck("...and the watch was not touched", watch.running_pid(paths) == live)

    print("FACTORY_NO_WATCH")
    code, out, err = run("--restart", "--json", extra_env={"FACTORY_NO_WATCH": "1"})
    ck("a restart that could not start a new watch fails before stopping the old one",
       code == 1 and "FACTORY_NO_WATCH" in (out + err), (code, out, err))
    ck("...the running watch is still the same one", watch.running_pid(paths) == live, (watch.running_pid(paths), live))
    one_tree("after the FACTORY_NO_WATCH step")

    print("restart_watch: a stop that did not work is not reported as restarted (TR-1307)")
    real_stop = watch.stop
    watch.stop = lambda paths, say, **kw: None
    try:
        try:
            doc, err = watch.restart_watch(paths, ENV), None
        except CrewError as e:
            doc, err = None, str(e)
    finally:
        watch.stop = real_stop
    ck("a stop that left the watch running raises, it does not return restarted",
       err is not None and "could not stop the watch" in err, (doc, err))
    ck("...the old watch is still the one recorded", watch.running_pid(paths) == live, (watch.running_pid(paths), live))
    ck("...and its process is still running", not gone(live))

    print("_watch_executables: a watch started by a launcher (TR-1319)")
    # A Windows venv's python.exe is a launcher that runs the base interpreter as the loop, so the loop alone is
    # never under the venv. Stand-ins: a "launcher" with `crew watch` in its command line and its own argv[0], and
    # a child that has another argv[0].
    lfolder = SCRATCH / "venv" / "Scripts"
    bfolder = SCRATCH / "base"
    BASEPY = getattr(sys, "_base_executable", sys.executable)   # a venv's own python.exe may itself be a launcher
    child_code = "import time; time.sleep(60)"
    launch_code = ("import subprocess, sys, time; "
                   f"subprocess.Popen([{str(bfolder / 'pythonw')!r}, '-c', {child_code!r}], executable={BASEPY!r}); "
                   "time.sleep(60)")

    def start_pair(launcher_args):
        lp = subprocess.Popen([str(lfolder / "pythonw"), "-c", launch_code, *launcher_args], executable=BASEPY,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kid = []

        def seen():
            try:
                kid[:] = [c.pid for c in psutil.Process(lp.pid).children()]
            except psutil.Error:
                pass
            return bool(kid)
        wait_for(seen)
        return lp, (kid[0] if kid else None)

    pair = []
    try:
        lp, cp = start_pair(["crew", "watch"])
        pair.append(lp)
        ck("a launcher and its loop child started", cp is not None)
        ck("the loop alone is not under the launcher's folder",
           not any(watch._under(e, str(lfolder)) for e in watch._program_paths(psutil.Process(cp))), watch._program_paths(psutil.Process(cp)))
        ck("...but its `crew watch` launcher is: the folder is accepted",
           any(watch._under(e, str(lfolder)) for e in watch._watch_executables(cp)), watch._watch_executables(cp))
        ck("...and a folder that neither is in is still not",
           not any(watch._under(e, str(elsewhere)) for e in watch._watch_executables(cp)), watch._watch_executables(cp))
        lp2, cp2 = start_pair(["serve"])
        pair.append(lp2)
        ck("a parent that is not a `crew watch` launcher is not counted",
           cp2 is not None and not any(watch._under(e, str(lfolder)) for e in watch._watch_executables(cp2)),
           watch._watch_executables(cp2) if cp2 else None)
        # F4: the parent's pid reused by a process that started after the watch is not its launcher
        real_process = psutil.Process
        late = real_process(lp.pid).create_time() + 3600

        class LateParent(real_process):
            def create_time(self):
                return late if self.pid == lp.pid else super().create_time()
        watch.psutil.Process = LateParent
        try:
            ck("a `crew watch` parent younger than the watch is not its launcher",
               not any(watch._under(e, str(lfolder)) for e in watch._watch_executables(cp)), watch._watch_executables(cp))
        finally:
            watch.psutil.Process = real_process
    finally:
        for q in pair:
            for c in psutil.Process(q.pid).children(recursive=True) if q.poll() is None else []:
                c.kill()
            q.kill()

    print("_watch_executables: a relative program path is ignored (TR-1307)")
    class _Stand:
        argv0 = "python"
        def __init__(self, pid):
            pass
        def exe(self):
            raise psutil.AccessDenied()
        def cmdline(self):
            return [_Stand.argv0, "-m", "tares.crew"]
        def ppid(self):
            raise psutil.NoSuchProcess(1)
    real_process = watch.psutil.Process
    watch.psutil.Process = _Stand
    try:
        ck("a bare program name in the command line is not a program path", watch._watch_executables(1) == [], watch._watch_executables(1))
        _Stand.argv0 = sys.executable
        ck("an absolute one is kept", watch._watch_executables(1) == [sys.executable], watch._watch_executables(1))
    finally:
        watch.psutil.Process = real_process

    print("_under")
    base = SCRATCH / "install"
    (base / "bin").mkdir(parents=True)
    ck("a path inside the folder", watch._under(str(base / "bin" / "python"), str(base)))
    ck("the folder itself", watch._under(str(base), str(base)))
    ck("a sibling that only shares the name as a prefix is not inside", not watch._under(str(base) + "-old/bin/python", str(base)))
    if os.name != "nt":
        outside = SCRATCH / "base-python"
        outside.write_text("", encoding="utf-8")
        link = base / "bin" / "python3"
        link.symlink_to(outside)
        ck("a link inside the folder that points outside it still counts as inside", watch._under(str(link), str(base)))
finally:
    cleanup()

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
