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
    # A venv's python may start the base interpreter as the watch (a hosted Windows runner's does): then the folder the
    # watch really runs from is the one to name, because the venv folder holds only its launcher.
    running_exes = watch._watch_executables(watch.running_pid(paths) or 0)
    if running_exes and not any(watch._under(e, folder) for e in running_exes):
        folder = str(Path(running_exes[0]).parent)
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
