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


def run(*args):
    p = subprocess.run([sys.executable, "-m", "tares.crew", "crew", "watch", *args], env=ENV, capture_output=True,
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
    ck("...the pid file names a live watch", wait_for(lambda: watch.running_pid(paths) is not None))
    pid1 = watch.running_pid(paths)

    print("restart")
    code, out, err = run("--restart", "--json")
    d = doc_of(out)
    pid2 = (d or {}).get("watch", {}).get("pid")
    ck("--restart --json: restarted with a new pid",
       code == 0 and d and d["watch"]["action"] == "restarted" and isinstance(pid2, int) and pid2 != pid1,
       (code, out, err, pid1))
    ck("...the old watch process is gone", wait_for(lambda: gone(pid1)), pid1)
    ck("...the new one runs and the pid file names it", wait_for(lambda: watch.running_pid(paths) == pid2),
       (watch.running_pid(paths), pid2))
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
    ck("...the watch was left alone (same pid, still running)", watch.running_pid(paths) == pid2, watch.running_pid(paths))
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
    ck("...the second old watch is gone", wait_for(lambda: gone(pid2)), pid2)
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
finally:
    cleanup()

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
