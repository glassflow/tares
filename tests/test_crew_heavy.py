"""`tares factory heavy <seconds> <command...>` (tares/crew/heavy.py), the port of the old factory-heavy wrapper.

The port of tests/factory/heavy_test.sh, plus: two commands never overlap; the timeout kills the command
and everything it started (exit 142, the lock folder gone); the command's own exit status comes back; a
lock whose holder is gone is cleared and a live or half-made one is waited for; the wait gives up (75) and
says who it waits for; bad usage is exit 2; a missing command is 127; SIGTERM stops the tree and frees
the lock; the lock is a folder, so a second run waits for the first and a stale folder is cleared;
`tares factory heavy` is routed to Python; a plain run imports no psutil, duckdb or fastapi.

Run: .venv/bin/python tests/test_crew_heavy.py
"""
import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tares.crew import dispatch, heavy  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


WINDOWS = sys.platform == "win32"
PY = sys.executable
RUNNER = ("import os, sys; from tares.crew.heavy import cmd_heavy; "
          "sys.exit(cmd_heavy(['heavy'] + sys.argv[1:], dict(os.environ)))")
TMP = tempfile.mkdtemp(prefix="crew_heavy_")


def env_for(tmp, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("FACTORY_STATION", "TMPDIR")}
    env.update(TMPDIR=tmp, PYTHONPATH=str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""), PYTHONUTF8="1")
    env.update(extra)
    return env


def start(tmp, *args, **extra):
    return subprocess.Popen([PY, "-c", RUNNER, *map(str, args)], env=env_for(tmp, **extra),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")


def run(tmp, *args, timeout=60, **extra):
    p = start(tmp, *args, **extra)
    out, err = p.communicate(timeout=timeout)
    return p.returncode, out, err


def pycmd(code):
    return [PY, "-c", code]


def wait_for(path, secs=20):
    end = time.time() + secs
    while time.time() < end:
        if os.path.isfile(path) and os.path.getsize(path):
            return True
        time.sleep(0.05)
    return False


def fresh():
    return tempfile.mkdtemp(dir=TMP)


def lockpath(tmp):
    return os.path.join(tmp, "tares-factory-heavy.lock")


def alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return heavy.pid_alive(pid)


def wait_dead(pid, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


# ---------- usage ----------
t = fresh()
for label, args in (("no arguments", []), ("a timeout and no command", ["5"]),
                    ("a timeout that is not a number", ["soon", "true"]), ("a negative timeout", ["-1", "true"]),
                    ("a timeout with a unit", ["5s", "true"])):
    code, out, err = run(t, *args)
    ck(f"usage: {label} is exit 2 with the usage line", code == 2 and "usage: tares factory heavy <timeout-seconds> <command...>" in err, f"{code} {err!r}")
ck("usage: no lock left behind", not os.path.exists(lockpath(t)))

# ---------- exit status ----------
t = fresh()
code, out, err = run(t, 30, *pycmd("print('hello'); import sys; sys.exit(7)"))
ck("the command's exit status comes back", code == 7, f"{code} {err!r}")
ck("its output reaches the caller untouched", out.strip() == "hello", repr(out))
ck("the lock is gone after the command", not os.path.exists(lockpath(t)))
code, out, err = run(t, 30, *pycmd("pass"))
ck("a command that succeeds is exit 0", code == 0, f"{code} {err!r}")
code, out, err = run(t, 0, *pycmd("pass"))
ck("a timeout of 0 means no limit", code == 0, f"{code} {err!r}")
code, out, err = run(t, 30, "tares-no-such-command-zz")
ck("a command that cannot start is exit 127, with a message", code == 127 and "cannot run" in err, f"{code} {err!r}")
ck("... and frees the lock", not os.path.exists(lockpath(t)))
if not WINDOWS:
    code, out, err = run(t, 30, *pycmd("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
    ck("a command killed by a signal is 128 plus the signal", code == 137, f"{code}")

# ---------- the lock names its holder ----------
t = fresh()
started = os.path.join(t, "started")
holder = start(t, 30, *pycmd(f"import time; open({started!r}, 'w').write('x'); time.sleep(30)"), FACTORY_STATION="the-station")
ok = wait_for(os.path.join(lockpath(t), "what")) and wait_for(started)   # the command runs: the runner's SIGTERM handler is in place
what = open(os.path.join(lockpath(t), "what"), encoding="utf-8").read() if ok else ""
pid = open(os.path.join(lockpath(t), "pid"), encoding="utf-8").read().strip() if ok else ""
# on Windows the python.exe a venv hands out can be a launcher whose child is the runner: Popen's pid is not the lock's
ck("the lock holds the runner's pid", pid.isdigit() if WINDOWS else pid == str(holder.pid), f"{pid!r} vs {holder.pid}")
ck("the lock says what runs and for which station", "time.sleep(30)" in what and what.rstrip().endswith("(station the-station)"), repr(what))
if WINDOWS:
    # no SIGTERM handler there (terminate is a hard kill, and may only reach a launcher): stop the runner by its lock pid
    if pid.isdigit():
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
    holder.kill()
    holder.communicate(timeout=30)
else:
    holder.terminate()
    holder.communicate(timeout=30)
    ck("SIGTERM to the runner frees the lock", wait_dead(holder.pid) and not os.path.exists(lockpath(t)),
       os.listdir(t) if os.path.isdir(t) else "")

# ---------- two commands never overlap ----------
t = fresh()
log = os.path.join(t, "log")
step = ("import sys, time; f = open(sys.argv[1], 'a'); f.write(sys.argv[2] + '-start\\n'); f.close(); "
        "time.sleep(float(sys.argv[3])); f = open(sys.argv[1], 'a'); f.write(sys.argv[2] + '-end\\n'); f.close()")
a = start(t, 30, PY, "-c", step, log, "a", "1.5")
ck("the first command takes the lock", wait_for(os.path.join(lockpath(t), "pid")))
t0 = time.time()
code, out, err = run(t, 30, PY, "-c", step, log, "b", "0")
waited = time.time() - t0
a.communicate(timeout=30)
order = open(log, encoding="utf-8").read().split()
ck("the second waits for the first: no overlap", order == ["a-start", "a-end", "b-start", "b-end"], str(order))
ck("... and says who it waits for", "factory-heavy: waiting for" in err and "(station unknown)" in err, repr(err))
ck("... after about the first command's time", waited > 0.5, f"{waited:.2f}s")
ck("both exit 0 and the lock is gone", code == 0 and a.returncode == 0 and not os.path.exists(lockpath(t)), f"{code} {a.returncode}")

# ---------- the timeout kills ----------
t = fresh()
t0 = time.time()
code, out, err = run(t, 1, PY, "-c", "import time; time.sleep(30)")
took = time.time() - t0
ck("a command past the timeout exits 142", code == 142, f"{code} {err!r}")
ck("... and says so", "factory-heavy: killed after 1s:" in err, repr(err))
ck("... in about a second, not thirty", took < 15, f"{took:.1f}s")
ck("... and the lock is not left behind", not os.path.exists(lockpath(t)))

# ... and everything the command started goes with it
t = fresh()
pidfile = os.path.join(t, "child.pid")
parent = ("import subprocess, sys, time\n"
          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
          "open(sys.argv[1], 'w').write(str(c.pid))\n"
          "time.sleep(60)\n")
code, out, err = run(t, 2, PY, "-c", parent, pidfile)
child = int(open(pidfile, encoding="utf-8").read()) if os.path.isfile(pidfile) else 0
ck("the timeout kills the command's children too", code == 142 and child and wait_dead(child), f"{code} child {child} {err!r}")

# ---------- stale, live and half-made locks ----------
t = fresh()
gone = subprocess.Popen(pycmd("pass"))
gone.wait()
os.mkdir(lockpath(t))
open(os.path.join(lockpath(t), "pid"), "w").write(f"{gone.pid}\n")
open(os.path.join(lockpath(t), "what"), "w").write("old thing (station x)\n")
code, out, err = run(t, 30, *pycmd("print('ran')"), timeout=30)
ck("a lock whose holder is gone is cleared and the command runs", code == 0 and out.strip() == "ran", f"{code} {err!r}")

t = fresh()
os.mkdir(lockpath(t))
open(os.path.join(lockpath(t), "pid"), "w").write("not-a-number\n")
code, out, err = run(t, 30, *pycmd("print('ran')"), timeout=30)
ck("a lock whose pid is not a number is stale", code == 0 and out.strip() == "ran", f"{code} {err!r}")

saved = (heavy.WAIT_LIMIT, heavy.SAY_EVERY, heavy.POLL)


def in_process(tmp, *args, **env):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = heavy.cmd_heavy(["heavy", *map(str, args)], {"TMPDIR": tmp, **env})
    return code, out.getvalue(), err.getvalue()


try:
    heavy.WAIT_LIMIT, heavy.SAY_EVERY, heavy.POLL = 0.6, 0.2, 0.05
    t = fresh()
    os.mkdir(lockpath(t))
    open(os.path.join(lockpath(t), "pid"), "w").write(f"{os.getpid()}\n")
    open(os.path.join(lockpath(t), "what"), "w").write("docker build (station other)\n")
    code, out, err = in_process(t, 30, *pycmd("pass"))
    ck("a live holder is waited for, then the wait gives up with exit 75", code == 75, f"{code} {err!r}")
    ck("... naming the holder's command, more than once", err.count("waiting for docker build (station other) to finish") >= 2
       and "waited an hour for docker build (station other)" in err, repr(err))
    ck("... and the holder's lock is left alone", os.path.isdir(lockpath(t)) and os.path.isfile(os.path.join(lockpath(t), "pid")))

    t = fresh()
    os.mkdir(lockpath(t))   # a holder between mkdir and writing its pid file
    code, out, err = in_process(t, 30, *pycmd("pass"))
    ck("a lock with no pid yet is not stale: waited for, not removed", code == 75 and os.path.isdir(lockpath(t)), f"{code} {err!r}")
    ck("... and with no 'what' it says 'another heavy command'", "another heavy command" in err, repr(err))

    t = fresh()
    os.mkdir(lockpath(t))   # TR-1369: only `what` was left, from a release that stopped halfway
    open(os.path.join(lockpath(t), "what"), "w").write("amnesia make test-integration (station x)\n")
    code, out, err = in_process(t, 30, *pycmd("pass"))
    ck("a lock with only a fresh `what` is still waited for", code == 75, f"{code} {err!r}")
    old = time.time() - heavy.NO_PID_GRACE - 30
    os.utime(lockpath(t), (old, old))
    marker = os.path.join(t, "ran")
    code, out, err = in_process(t, 30, *pycmd(f"open({marker!r}, 'w').close()"))
    ck("a lock with no pid file for longer than the grace is taken over, and the command runs",
       code == 0 and os.path.exists(marker) and not os.path.exists(lockpath(t)), f"{code} {err!r}")

    t = fresh()
    os.mkdir(lockpath(t))
    old = time.time() - heavy.NO_PID_GRACE - 30
    os.utime(lockpath(t), (old, old))
    code, out, err = in_process(t, 30, *pycmd("pass"))
    ck("an old empty lock folder is taken over too", code == 0, f"{code} {err!r}")

    t = fresh()
    os.mkdir(lockpath(t))
    for name in ("what", "pid"):
        open(os.path.join(lockpath(t), name), "w").write("x\n")
    removed, real_remove = [], os.remove
    os.remove = lambda path, *a, **k: (removed.append(os.path.basename(path)), real_remove(path, *a, **k))[1]
    try:
        heavy._release(lockpath(t))
    finally:
        os.remove = real_remove
    ck("_release takes the pid file out last", removed == ["what", "pid"] and not os.path.exists(lockpath(t)), repr(removed))

    # TR-1370: two waiters judge one stale lock; the other gets there first and holds a live lock by the time this one clears
    t = fresh()
    os.mkdir(lockpath(t))
    open(os.path.join(lockpath(t), "pid"), "w").write("99999999\n")
    real_gone, judged = heavy._holder_gone, []

    def other_waiter_wins(lock):
        judged.append(real_gone(lock))
        if len(judged) == 1:
            shutil.rmtree(lock)
            os.mkdir(lock)
            open(os.path.join(lock, "pid"), "w").write(f"{os.getpid()}\n")
        return judged[-1]

    heavy._holder_gone = other_waiter_wins
    try:
        slow_judged_stale = heavy._holder_gone(lockpath(t))
        cleared = heavy._clear_stale(lockpath(t))
    finally:
        heavy._holder_gone = real_gone
    ck("two waiters, one stale lock: the slow one does not remove the live lock the other made",
       slow_judged_stale and not cleared and open(os.path.join(lockpath(t), "pid")).read().strip() == str(os.getpid()),
       f"{slow_judged_stale} {cleared} {os.listdir(t)}")
    ck("... and nothing is left beside it", os.listdir(t) == [os.path.basename(lockpath(t))], repr(os.listdir(t)))

    t = fresh()
    os.mkdir(lockpath(t))
    open(os.path.join(lockpath(t), "pid"), "w").write("99999999\n")
    ck("a stale lock is cleared by the first waiter", heavy._clear_stale(lockpath(t)) and os.listdir(t) == [])
    ck("... and a second waiter finding it gone is not in the way", heavy._clear_stale(lockpath(t)))

    t = fresh()
    code, out, err = in_process(t, 0, *pycmd("pass"))
    ck("in process: an idle lock is taken and freed", code == 0 and not os.path.exists(lockpath(t)), f"{code} {err!r}")
    code, out, err = in_process(os.path.join(t, "no", "such", "dir"), 5, *pycmd("pass"))
    ck("a temp folder that does not exist is exit 1 with a message, not a wait", code == 1 and "cannot make" in err, f"{code} {err!r}")
finally:
    heavy.WAIT_LIMIT, heavy.SAY_EVERY, heavy.POLL = saved

# ---------- a second run waits for the first ----------
t = fresh()
holder = start(t, 30, *pycmd("import time; time.sleep(2)"))
wait_for(os.path.join(lockpath(t), "pid"))
t0 = time.time()
code, out, err = run(t, 30, *pycmd("pass"))
took = time.time() - t0
holder.communicate(timeout=30)
ck("a second run waits for the holder, says so, then runs", code == 0 and took > 0.8 and "waiting for" in err, f"{code} {took:.1f}s {err!r}")
ck("... and the lock is gone after both", not os.path.exists(lockpath(t)))

# ---------- routing and imports ----------
ck("`tares factory heavy` is a ported command", dispatch.find(["heavy", "5", "true"]) is not None)
ck("... and the handler is the one in tares.crew.heavy", dispatch.find(["heavy", "5", "true"]).__module__ == "tares.crew.heavy"
   or dispatch.find(["heavy", "5", "true"]).__name__ == "_heavy", str(dispatch.find(["heavy", "5", "true"])))
probe = ("import os, sys; from tares.crew.dispatch import find; "
         "code = find(['heavy'])(['heavy', '30', sys.executable, '-c', 'pass'], dict(os.environ)); "
         "bad = [m for m in ('psutil', 'duckdb', 'fastapi', 'tares.daemon', 'tares.store', 'tares.cli') if m in sys.modules]; "
         "print(code, bad)")
t = fresh()
# PYTHONPATH is the repo alone: the postgres suite puts tests/pg_harness first, whose sitecustomize imports the store
r = subprocess.run([PY, "-c", probe], env=env_for(t, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=60)
ck("a plain run through the dispatcher imports no psutil, duckdb, fastapi or tares.cli", r.stdout.strip() == "0 []", f"{r.stdout!r} {r.stderr!r}")

# ---------- SIGTERM stops the tree ----------
if not WINDOWS:
    t = fresh()
    pidfile = os.path.join(t, "child.pid")
    runner = start(t, 60, PY, "-c", parent, pidfile)
    wait_for(pidfile)
    child = int(open(pidfile, encoding="utf-8").read())
    runner.send_signal(signal.SIGTERM)
    out, err = runner.communicate(timeout=30)
    ck("SIGTERM to the runner is exit 143", runner.returncode == 143, f"{runner.returncode} {err!r}")
    ck("... the command's children are stopped too", wait_dead(child), f"child {child}")
    ck("... and the lock is freed", not os.path.exists(lockpath(t)))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
