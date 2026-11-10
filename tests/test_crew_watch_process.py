"""The watch as a process (tares/crew/watch.py, TR-958): it starts detached, outlives the process that started it,
stops through its stop file, and a stale pid file names nothing.

The real loop runs in a real child process (`python -m tares.crew crew watch`, TARES_FACTORY_WATCH=python) in a
throwaway crew home. Its steps are the empty ones: nothing is revived. The loop's rounds, the registered steps
and the nap are checked in this process. The same code stops a plain pid file (bash's) at once, as TR-955 did.

On Windows the processes that start the real watch are not run (the creation flags and pythonw.exe are untested
there, TR-946); the checks that need no such process run.

Run: .venv/bin/python tests/test_crew_watch_process.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-watch-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
CREW = SCRATCH / "crew"
CREW.mkdir()
(CREW / "crew.json").write_text(json.dumps({"crew_name": "t"}), encoding="utf-8")
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), FACTORY_CREW_HOME=str(CREW),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"))
(SCRATCH / "home").mkdir()

sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
from tares.crew import dispatch, doctor, lifecycle as LC, watch  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

WINDOWS = os.name == "nt"
P = F = 0
SPAWNED = []


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


paths = CrewPaths.at(str(CREW))


def child_env(**more):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "TARES_FACTORY_WATCH": "python", "FACTORY_WATCH_INTERVAL": "1",
           "FACTORY_ASK_POLL": "0.1", "PYTHONUNBUFFERED": "1", **more}
    return env


def wait_for(cond, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def sleeper(*argv_tail, ignore_term=False):
    """A process that looks like the watch (its command line has `crew watch`) and does nothing."""
    code = "import time" + (", signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else "") \
           + "; time.sleep(120)"
    proc = subprocess.Popen([sys.executable, "-c", code, "crew watch", *argv_tail])
    SPAWNED.append(proc)
    return proc


def reap():
    for proc in SPAWNED:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    for name in ("watch.pid", "watch.pid.meta", "watch.stop"):
        (CREW / name).unlink(missing_ok=True)


def log_text():
    try:
        return (CREW / "watch.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


try:
    print("the steps and the loop")
    calls = []
    watch.register("revive", lambda p: calls.append("revive"))
    watch.register("pace", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))   # a step that fails
    watch.register("updates", lambda p: calls.append("updates"))
    watch.register("readings", lambda p: calls.append("tick"))
    try:
        watch.register("revivee", len)
        refused = False
    except ValueError:
        refused = True
    ck("a step name that is not one is refused", refused)
    t = threading.Thread(target=watch.watch, args=(paths, 0.3), kwargs={"poll": 0.05}, daemon=True)
    t.start()
    ck("rounds run the steps in order", wait_for(lambda: calls.count("updates") >= 2), str(calls))
    ck("...a step that raises does not end the round or the watch", "updates" in calls and t.is_alive(), str(calls))
    ck("...and a nap serves the readings tick", "tick" in calls, str(calls))
    ck("...revive runs before updates", calls.index("revive") < calls.index("updates"), str(calls))
    (CREW / "watch.stop").write_text("stop\n", encoding="utf-8")
    t.join(5)
    ck("the stop file ends the loop and the loop removes it", not t.is_alive() and not (CREW / "watch.stop").exists())
    for name in ("revive", "pace", "updates", "readings"):
        watch.unregister(name)
    calls.clear()
    watch.watch(paths, 0, once=True)
    ck("--once does one round and no more, with no step registered", calls == [])
    (CREW / "watch.stop").write_text("x", encoding="utf-8")
    started = time.monotonic()
    ck("a nap with the stop file already there returns at once",
       watch.nap(paths, 60, 0.05) is True and time.monotonic() - started < 2)
    (CREW / "watch.stop").unlink()
    ck("a nap with no stop file waits its time and says no", watch.nap(paths, 0.2, 0.05) is False)

    print("the pid file and who is the watch")
    ck("no pid file: no watch", watch.running_pid(paths) is None)
    (CREW / "watch.pid").write_text("not a number\n", encoding="utf-8")
    ck("a pid file that is not a number: no watch", watch.running_pid(paths) is None)
    (CREW / "watch.pid").write_text("99999999\n", encoding="utf-8")
    ck("a pid that cannot exist: no watch", watch.running_pid(paths) is None)
    proc = sleeper()
    created = psutil.Process(proc.pid).create_time()
    watch.write_pidfile(paths, proc.pid, created, "python")
    ck("the pid file is the pid alone, as bash writes it",
       (CREW / "watch.pid").read_text(encoding="utf-8") == f"{proc.pid}\n")
    ck("...and the meta file has the pid, the create time and the engine",
       (CREW / "watch.pid.meta").read_text(encoding="utf-8").split() == [str(proc.pid), f"{created:.3f}", "python"])
    if not WINDOWS:
        r = subprocess.run(["bash", "-c", f'pid=$(cat "{CREW}/watch.pid"); kill -0 "$pid" && echo ok'],
                           capture_output=True, text=True)
        ck("...bash's watch_pid reads it: `kill -0 $(cat watch.pid)` works", r.stdout.strip() == "ok", r.stderr)
    ck("...doctor's watch_running reads it", doctor.watch_running(paths) is True)
    ck("...read_pidfile gives the pid, create time and engine", watch.read_pidfile(paths) == (proc.pid, float(f"{created:.3f}"), "python"))
    (CREW / "watch.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    (CREW / "watch.pid.meta").write_text(f"{proc.pid + 1} 1.000 python\n", encoding="utf-8")
    ck("a meta file that names another pid is ignored (bash rewrote the pid file)",
       watch.read_pidfile(paths) == (proc.pid, None, "") and watch.running_pid(paths) == proc.pid)
    watch.write_pidfile(paths, proc.pid, created, "python")
    ck("...a live process with `crew watch` and that create time is the watch", watch.running_pid(paths) == proc.pid)
    ck("...LC.watch_pid says the same", LC.watch_pid(paths) == proc.pid)
    real_cmdline = psutil.Process.cmdline
    def leak(self):
        raise SystemError("returned a result with an exception set")   # what psutil can leak on macOS
    psutil.Process.cmdline = leak
    try:
        ck("psutil leaking SystemError from cmdline() means not the watch, not a crash", not watch.is_the_watch(proc.pid, created))
    finally:
        psutil.Process.cmdline = real_cmdline
    watch.write_pidfile(paths, proc.pid, created - 3600, "python")
    ck("a stale pid file (a pid a reboot gave to another process: the create time differs) names nothing",
       watch.running_pid(paths) is None and LC.watch_pid(paths) is None)
    ck("...doctor says the same (it reads through running_pid, so the two cannot drift apart)",
       doctor.watch_running(paths) is False)
    lines = []
    watch.stop(paths, lines.append, timeout=0.2)
    ck("...stopping it removes the file, says nothing and leaves that process alone",
       not (CREW / "watch.pid").exists() and lines == [] and alive(proc.pid))
    plain = sleeper()
    (CREW / "watch.pid").write_text(f"{plain.pid}\n", encoding="utf-8")
    ck("a pid file with the pid alone (bash's) is the watch while the command line says so",
       watch.running_pid(paths) == plain.pid)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    SPAWNED.append(other)
    watch.write_pidfile(paths, other.pid, psutil.Process(other.pid).create_time(), "python")
    ck("a live process whose command line has no `crew watch` is not the watch", watch.running_pid(paths) is None)
    proc.kill()
    proc.wait()
    plain.kill()
    plain.wait()
    other.kill()
    other.wait()
    (CREW / "watch.pid").unlink(missing_ok=True)

    print("stopping a watch that does not read the stop file")
    started = time.monotonic()
    victim = sleeper()
    watch.write_pidfile(paths, victim.pid, psutil.Process(victim.pid).create_time(), "bash")
    lines = []
    watch.stop(paths, lines.append, timeout=30)
    ck("a bash watch is terminated at once, not after the timeout",
       victim.wait(10) is not None and time.monotonic() - started < 10 and lines == ["stopped the watch"]
       and not (CREW / "watch.pid").exists(), str(lines))
    victim = sleeper()
    watch.write_pidfile(paths, victim.pid, psutil.Process(victim.pid).create_time(), "python")
    lines = []
    started = time.monotonic()
    watch.stop(paths, lines.append, timeout=0.5)
    ck("a Python watch that does not read the stop file is terminated after the timeout",
       victim.wait(10) is not None and 0.4 <= time.monotonic() - started < 15 and lines == ["stopped the watch"],
       f"{victim.poll()} {time.monotonic() - started:.1f} {lines}")
    ck("...and the stop file and pid file are gone", not (CREW / "watch.stop").exists() and not (CREW / "watch.pid").exists())
    if not WINDOWS:
        victim = sleeper(ignore_term=True)
        watch.write_pidfile(paths, victim.pid, psutil.Process(victim.pid).create_time(), "python")
        lines = []
        watch.stop(paths, lines.append, timeout=0.3)
        ck("a watch that ignores the terminate is killed", victim.wait(15) is not None and lines == ["stopped the watch"],
           str(lines))

    print("a watch that cannot be stopped keeps its pid file")
    victim = sleeper(ignore_term=True) if not WINDOWS else sleeper()
    vcreated = psutil.Process(victim.pid).create_time()
    watch.write_pidfile(paths, victim.pid, vcreated, "python")
    real_term, real_kill = psutil.Process.terminate, psutil.Process.kill
    def refuse(self, *a):
        if self.pid == victim.pid:
            raise psutil.AccessDenied(self.pid)
        return None
    psutil.Process.terminate = psutil.Process.kill = refuse
    lines = []
    try:
        watch.stop(paths, lines.append, timeout=0.3)
    finally:
        psutil.Process.terminate, psutil.Process.kill = real_term, real_kill
    ck("stop says it could not, and keeps the pid file and the process",
       len(lines) == 1 and lines[0].startswith("could not stop the watch") and (CREW / "watch.pid").exists()
       and alive(victim.pid), str(lines))
    watch.stop(paths, lines.append, timeout=0.3)
    ck("...a later stop that is allowed ends it and says stopped", lines[-1] == "stopped the watch" and victim.wait(15) is not None
       and not (CREW / "watch.pid").exists(), str(lines))

    if not WINDOWS:
        print("the launcher claim is retried every round (the pid file may be written after the loop starts)")
        # P stands for the launcher: it starts the loop C, and only later writes the pid file naming itself, as start() does.
        pcode = ("import os, subprocess, sys, time, psutil; sys.path.insert(0, sys.argv[1]); from tares.crew import watch; "
                 "from tares.crew.paths import CrewPaths; p = CrewPaths.at(sys.argv[2]); "
                 "ccode = 'import sys; sys.path.insert(0, sys.argv[1]); from tares.crew import watch; "
                 "from tares.crew.paths import CrewPaths; watch.watch(CrewPaths.at(sys.argv[2]), 0.2, poll=0.05)'; "
                 "c = subprocess.Popen([sys.executable, '-c', ccode, sys.argv[1], sys.argv[2], 'crew watch']); "
                 "time.sleep(1.0); watch.write_pidfile(p, os.getpid(), psutil.Process().create_time(), 'python'); "
                 "end = time.time() + 15\n"
                 "while time.time() < end:\n"
                 "    row = watch.read_pidfile(p)\n"
                 "    if row and row[0] == c.pid: break\n"
                 "    time.sleep(0.1)\n"
                 "ok = bool(row and row[0] == c.pid); (p.home / 'watch.stop').write_text('x'); c.wait(20); print('claimed' if ok else 'not claimed')")
        r = subprocess.run([sys.executable, "-c", pcode, str(ROOT), str(CREW)], env=child_env(), capture_output=True,
                           text=True, timeout=90)
        ck("a loop that started before the pid file existed takes it once the launcher writes its own pid",
           r.stdout.strip() == "claimed", f"{r.stdout} {r.stderr[-300:]}")
        for name in ("watch.pid", "watch.pid.meta", "watch.stop"):
            (CREW / name).unlink(missing_ok=True)

    if not WINDOWS:
        print("a zombie is not the watch")
        zcode = "import sys; sys.exit(0)"
        z = subprocess.Popen([sys.executable, "-c", zcode, "crew watch"])
        SPAWNED.append(z)
        zp = psutil.Process(z.pid)
        wait_for(lambda: zp.status() == psutil.STATUS_ZOMBIE, 10)   # exited, not yet reaped by us
        ck("the child is a zombie whose command line still says `crew watch`",
           zp.status() == psutil.STATUS_ZOMBIE, zp.status())
        ck("...is_the_watch says no", watch.is_the_watch(z.pid, zp.create_time()) is False)
        # A zombie has no command line on most systems, so that alone would reject it; hand it one to test the status check.
        real_cmdline = psutil.Process.cmdline
        psutil.Process.cmdline = lambda self: ["python", "crew watch"] if self.pid == z.pid else real_cmdline(self)
        try:
            ck("...even when its command line is still readable", watch.is_the_watch(z.pid, zp.create_time()) is False)
        finally:
            psutil.Process.cmdline = real_cmdline
        watch.write_pidfile(paths, z.pid, zp.create_time(), "python")
        ck("...and running_pid and doctor say no", watch.running_pid(paths) is None and doctor.watch_running(paths) is False)
        remove = watch.remove_pidfile
        remove(paths)
        ck("remove_pidfile takes both files", not (CREW / "watch.pid").exists() and not (CREW / "watch.pid.meta").exists())
        z.wait()

    print("the stop timeout comes from the env given to stop")
    victim = sleeper(ignore_term=True) if not WINDOWS else sleeper()
    watch.write_pidfile(paths, victim.pid, psutil.Process(victim.pid).create_time(), "python")
    started = time.monotonic()
    watch.stop(paths, lambda m: None, env={"FACTORY_WATCH_STOP_TIMEOUT": "0.5"})
    ck("FACTORY_WATCH_STOP_TIMEOUT in the given env ends the wait", victim.wait(15) is not None
       and time.monotonic() - started < 12, f"{time.monotonic() - started:.1f}")

    print("the loop leaves another watch's files alone")
    other = sleeper()
    watch.write_pidfile(paths, other.pid, psutil.Process(other.pid).create_time(), "python")
    (CREW / "watch.stop").write_text("x\n", encoding="utf-8")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from tares.crew import watch; "
            "from tares.crew.paths import CrewPaths; watch.watch(CrewPaths.at(sys.argv[2]), 0.1, poll=0.05)")
    subprocess.run([sys.executable, "-c", code, str(ROOT), str(CREW)], env=child_env(), capture_output=True, text=True, timeout=60)
    ck("a loop that is not the one in the pid file leaves its pid file, meta file and stop file",
       watch.read_pidfile(paths)[0] == other.pid and (CREW / "watch.stop").exists() and (CREW / "watch.pid.meta").exists())
    other.kill()
    other.wait()
    for name in ("watch.pid", "watch.pid.meta", "watch.stop"):
        (CREW / name).unlink(missing_ok=True)

    print("which command starts the watch, and which engine runs `watch`")
    argv, engine = watch._watch_command({"TARES_FACTORY_WATCH": "python"})
    ck("opted in: the Python module, no shell", engine == "python" and argv[1:] == ["-m", "tares.crew", "crew", "watch"], str(argv))
    if not WINDOWS:
        argv, engine = watch._watch_command({})
        ck("not opted in: the bash watch stays the default", engine == "bash" and argv[0] == "bash" and argv[-2:] == ["crew", "watch"], str(argv))
    argv, engine = watch._watch_command({"TARES_FACTORY_WATCH": "python", "TARES_FACTORY_ENGINE": "bash"})
    ck("TARES_FACTORY_ENGINE=bash wins over TARES_FACTORY_WATCH=python (on POSIX; Windows has no bash and runs Python)",
       engine == ("python" if WINDOWS else "bash"), str(argv))
    argv, engine = watch._watch_command({"TARES_FACTORY_ENGINE": "bash"})
    ck("TARES_FACTORY_ENGINE=bash alone: bash on POSIX, Python on Windows",
       engine == ("python" if WINDOWS else "bash") and (WINDOWS or argv[0] == "bash"), str(argv))
    for words in (["watch"], ["crew", "watch"]):
        kind = dispatch.route(words, {})[0]
        ck(f"`{' '.join(words)}` goes to bash unless opted in" if not WINDOWS else f"`{' '.join(words)}` runs in Python on Windows",
           kind == ("python" if WINDOWS else "bash"), kind)
        ck(f"`{' '.join(words)}` goes to Python with TARES_FACTORY_WATCH=python",
           dispatch.route(words, {"TARES_FACTORY_WATCH": "python"})[0] == "python")
        ck(f"`{' '.join(words)}` goes to Python with TARES_FACTORY_ENGINE=python",
           dispatch.route(words, {"TARES_FACTORY_ENGINE": "python"})[0] == "python")
    ck("...and `status` is not affected by the switch", dispatch.route(["status"], {})[0] == "python")

    print("options")
    for bad in (["--bogus"], ["--interval", "soon"]):
        try:
            watch.cmd_watch(["crew", "watch", *bad], {})
            raised = False
        except CrewError:
            raised = True
        ck(f"{bad[0]} {'with a bad value ' if len(bad) > 1 else ''}is refused", raised)
    ck("--once runs one round and returns 0", watch.cmd_watch(["crew", "watch", "--once"], {}) == 0)

    print("the claim: a launcher's pid in the pid file becomes the loop's own")
    # The child writes the file naming its own parent (the launcher, on Windows: a venv's python.exe starts the real
    # one as its child, so the test process is not the parent there), then claims it.
    code = ("import os, sys, psutil; sys.path.insert(0, sys.argv[1]); from tares.crew import watch; "
            "from tares.crew.paths import CrewPaths; p = CrewPaths.at(sys.argv[2]); "
            "par = psutil.Process().parent(); watch.write_pidfile(p, par.pid, par.create_time(), 'python'); "
            "watch.claim_pidfile(p); print(os.getpid())")
    r = subprocess.run([sys.executable, "-c", code, str(ROOT), str(CREW)], capture_output=True, text=True, timeout=60)
    row = watch.read_pidfile(paths)
    ck("the child that the pid file names as its parent takes the file", row is not None and str(row[0]) == r.stdout.strip(),
       f"{row} {r.stdout} {r.stderr[-200:]}")
    watch.write_pidfile(paths, 99999999, 1.0, "python")
    plain = ("import sys; sys.path.insert(0, sys.argv[1]); from tares.crew import watch; "
             "from tares.crew.paths import CrewPaths; watch.claim_pidfile(CrewPaths.at(sys.argv[2]))")
    subprocess.run([sys.executable, "-c", plain, str(ROOT), str(CREW)], capture_output=True, text=True, timeout=60)
    ck("a pid file that names someone else's process is left alone", watch.read_pidfile(paths)[0] == 99999999)
    (CREW / "watch.pid").unlink(missing_ok=True)

    if WINDOWS:
        print("starting the real watch is not run on Windows (untested there, TR-946)")
    else:
        print("starting the real watch")
        notes = []
        env = child_env()
        watch.start(paths, env, note=notes.append)
        row = watch.read_pidfile(paths)
        ck("start writes the pid file and says where the log is", row is not None and notes and "started the watch" in notes[-1]
           and "watch.log" in notes[-1], str(notes))
        pid = row[0]
        ck("...the process runs, and is the watch", wait_for(lambda: watch.running_pid(paths) == pid))
        ck("...its engine is python", row[2] == "python")
        ck("...it is in a session of its own (it does not die with our terminal)", os.getsid(pid) == pid != os.getsid(0))
        ck("...it prints that it is watching", wait_for(lambda: "watching the crew every 1s" in log_text()), log_text()[-300:])
        notes.clear()
        watch.start(paths, env, note=notes.append)
        ck("starting again changes nothing and names the pid", watch.read_pidfile(paths)[0] == pid
           and notes == [f"  ✓ the watch keeps the crew alive (already running, pid {pid})"], str(notes))
        notes.clear()
        watch.start(paths, {**env, "FACTORY_NO_WATCH": "1"}, note=notes.append)
        ck("FACTORY_NO_WATCH starts nothing and says nothing", notes == [])
        lines = []
        started = time.monotonic()
        watch.stop(paths, lines.append, timeout=20)
        ck("stop: the watch is gone, by its stop file well before the timeout",
           time.monotonic() - started < 10 and lines == ["stopped the watch"] and wait_for(lambda: not alive(pid)), f"{lines}")
        ck("...it left on its own (the log says so), and the files are gone",
           "asked to stop; leaving" in log_text() and not (CREW / "watch.pid").exists() and not (CREW / "watch.stop").exists(),
           log_text()[-300:])

        print("a SIGTERM straight to the watch")
        (CREW / "watch.log").write_text("", encoding="utf-8")   # so the banner we wait for is this watch's own
        watch.start(paths, env, note=lambda m: None)
        pid = watch.read_pidfile(paths)[0]
        wait_for(lambda: "watching the crew every 1s" in log_text())
        os.kill(pid, 15)
        gone = wait_for(lambda: not alive(pid))
        ck("the watch leaves and removes its pid file and meta file",
           gone and not (CREW / "watch.pid").exists() and not (CREW / "watch.pid.meta").exists(),
           f"gone={gone} pid={(CREW / 'watch.pid').exists()} meta={(CREW / 'watch.pid.meta').exists()} {log_text()[-300:]}")

        print("it outlives the process that started it")
        helper = ("import sys; sys.path.insert(0, sys.argv[1]); from tares.crew import watch; "
                  "from tares.crew.paths import CrewPaths; import os; "
                  "watch.start(CrewPaths.at(sys.argv[2]), dict(os.environ), note=lambda m: None)")
        r = subprocess.run([sys.executable, "-c", helper, str(ROOT), str(CREW)], env=child_env(), capture_output=True,
                           text=True, timeout=60)
        row = watch.read_pidfile(paths)
        ck("the starter exits 0", r.returncode == 0, r.stderr[-300:])
        ck("...and the watch it started still runs after the starter is gone",
           row is not None and wait_for(lambda: watch.running_pid(paths) == row[0]))
        time.sleep(1.5)   # a few rounds
        ck("...a second later still", row is not None and watch.running_pid(paths) == row[0])
        watch.stop(paths, lambda m: None, timeout=20)   # the process may still be finishing when stop returns
        ck("...and it still answers to the stop file", wait_for(lambda: not alive(row[0])))

        print("a start after a reboot: the pid file names a pid some other process has")
        stale = sleeper()
        watch.write_pidfile(paths, stale.pid, psutil.Process(stale.pid).create_time() - 86400, "python")
        (CREW / "watch.stop").write_text("old\n", encoding="utf-8")
        notes = []
        watch.start(paths, child_env(), note=notes.append)
        row = watch.read_pidfile(paths)
        ck("start ignores the stale pid file and starts a new watch", row is not None and row[0] != stale.pid
           and "started the watch" in notes[-1] and wait_for(lambda: watch.running_pid(paths) == row[0]), str(notes))
        ck("...and a stop file left from before does not stop it", wait_for(lambda: "watching the crew" in log_text())
           and watch.running_pid(paths) == row[0] and not (CREW / "watch.stop").exists())
        ck("...the stale process was not touched", alive(stale.pid))
        watch.stop(paths, lambda m: None, timeout=20)
finally:
    reap()
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
