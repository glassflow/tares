"""Processes the crew does not own (TR-1332, tares/crew/strays.py): found, reported, never stopped.

Every case reads a process table made of rows (strays.Proc), the same rows TARES_FAKE_STRAYS_DIR/ps-table.json holds,
so no case reads the machine's real processes. What it checks:

  1 a healthy crew (service, pty host and child per station, a spare, an attach, a plain claude) gives no stray;
  2 a station session left after its station was retired is a stray, listed with pid, start time, session, command;
  3 a second background service is a stray;
  4 the service in session 0, or in another session than the watch, gives the FIX text (Windows only; the flag
    and the session reader are patched, the GitHub runner is itself in session 0);
  5 an orphan is reported past an hour (3601 s yes, 3599 s no) and a process with a live parent is not;
  6 the environment marker beats the path fallback; an unreadable environment uses the roots;
  7 doctor's three checks and the status document validate against their schemas, over a ps-table.json;
  8 the watch step tells the orchestrator once when the count rises, not when it stays, not when it falls, and
    once more after a later rise;
  9 nothing is killed, signalled or run: every way to stop a process is patched to raise, and strays.py holds none.

Run: python tests/test_crew_strays.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-strays-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
                  FACTORY_CREW_HOME=str(SCRATCH / "crew"), FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
                  TARES_FAKE_STRAYS_DIR=str(SCRATCH / "ps"))
(SCRATCH / "home").mkdir()
(SCRATCH / "crew").mkdir()
(SCRATCH / "ps").mkdir()

import psutil  # noqa: E402

from tares.crew import doctor, strays, watch  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
from tares.crew.strays import Proc  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:600]))


PATHS = CrewPaths(str(SCRATCH / "crew"))
NOW = 2_000_000_000.0
OLD = NOW - 86400
(PATHS.home / "crew.json").write_bytes(b'{"crew_name": "crew"}\n')
(PATHS.home / "stations.json").write_bytes(json.dumps({
    "crew-orchestrator": {"status": "active", "sessionId": "s1"},
    "shop-build-m1": {"status": "active", "sessionId": "s2"},
    "shop-build-old": {"status": "retired"},
}).encode("utf-8") + b"\n")
ENV = {"TARES_FAKE_STRAYS_DIR": str(SCRATCH / "ps")}


def P_(pid, ppid, cmd, started=OLD, session=None, cwd="", crew=""):
    return Proc(pid, ppid, started, tuple(cmd.split(" ")), session, cwd, crew)


def healthy() -> list:
    return [
        P_(5, 1, "bash"),
        P_(10, 1, "claude daemon run"),
        P_(11, 10, "claude --bg-pty-host -n crew-orchestrator"),
        P_(12, 11, "claude --model opus -n crew-orchestrator"),
        P_(13, 10, "claude --bg-pty-host -n shop-build-m1"),
        P_(14, 13, "claude --model opus -n shop-build-m1"),
        P_(15, 10, "claude bg-pty-host --bg-spare x --bg-spare /tmp/a.claim.sock"),
        P_(16, 5, "claude attach abc123"),
        P_(17, 5, "claude"),
        P_(18, 5, "claude agents --json"),
    ]


def scan(procs, **kw):
    return strays.scan(PATHS, ENV, now=NOW, procs=procs, **kw)


print("1 a healthy crew")
r = scan(healthy())
ck("no claude stray", r["claude"] == [], [s.line() for s in r["claude"]])
ck("no orphan", r["orphan"] == [], [s.line() for s in r["orphan"]])
ck("no service problem", r["service"] == "")
ck("counts are 0, 0", strays.counts(r) == (0, 0))

print("2 a retired station's session")
left = P_(40, 5, "claude --model opus -n shop-build-old", started=OLD, session=1)
r = scan(healthy() + [left])
ck("one stray", [s.proc.pid for s in r["claude"]] == [40], [s.line() for s in r["claude"]])
line = r["claude"][0].line() if r["claude"] else ""
ck("the line names pid, start time, session and command",
   line.startswith("pid 40, started ") and ", session 1: " in line and "-n shop-build-old" in line, line)
ck("the start time is local, to the minute", len(line.split(", started ")[1].split(", session")[0]) == 16, line)
ghost = P_(41, 5, "claude -n crew-reviewer")
ck("a crew-prefixed name that is not active is a stray",
   [s.proc.pid for s in scan([P_(5, 1, "bash"), ghost])["claude"]] == [41])
ck("a plain interactive claude is ignored", scan([P_(5, 1, "bash"), P_(6, 5, "claude")])["claude"] == [])
ck("a -n of the person's own, no --bg, is ignored",
   scan([P_(5, 1, "bash"), P_(6, 5, "claude -n my-notes")])["claude"] == [])

ck("a --bg-pty-host with no -n is a stray the crew did not start",
   [(s.proc.pid, s.why) for s in scan([P_(5, 1, "bash"), P_(61, 5, "claude --bg-pty-host x")])["claude"]]
   == [(61, "background session the crew did not start")])
ck("a --bg session with a name that is not the crew's is a stray",
   [(s.proc.pid, s.why) for s in scan([P_(5, 1, "bash"), P_(62, 5, "claude --bg -n mine")])["claude"]]
   == [(62, "background session mine is not the crew's")])

print("3 a second service")
r = scan(healthy() + [P_(50, 1, "claude daemon run", started=OLD + 100)])
ck("the later service is the stray", [(s.proc.pid, s.why) for s in r["claude"]] == [(50, "second service")],
   [s.line() for s in r["claude"]])

print("4 the service's session (Windows)")
real_win, real_session = strays._windows, strays.session_of
strays._windows = lambda: True
strays.session_of = lambda pid: 1
real_running = watch.running_pid
watch.running_pid = lambda paths: 999   # a watch runs, in session 1
try:
    def with_service(session):
        procs = healthy()
        procs[1] = P_(10, 1, "claude daemon run", session=session)
        return scan(procs)["service"]
    ck("session 0 gives the FIX text", with_service(0) == strays.SESSION_FIX)
    ck("another session than the watch's gives it", with_service(2) == strays.SESSION_FIX)
    ck("the watch's session is fine", with_service(1) == "")
    ck("an unreadable session says nothing", with_service(None) == "")
    ck("session 0 is the problem even when the watch's session is unknown or 0 too",
       strays.service_session_problem([P_(10, 1, "claude daemon run", session=0)], None) == strays.SESSION_FIX
       and strays.service_session_problem([P_(10, 1, "claude daemon run", session=0)], 0) == strays.SESSION_FIX)
    watch.running_pid = lambda paths: None
    ck("with no watch running, the caller's own session is not held against the service (doctor over SSH)",
       with_service(2) == "" and with_service(1) == "")
    ck("with no watch running, session 0 still is", with_service(0) == strays.SESSION_FIX)
    watch.running_pid = lambda paths: 999
    ck("the text is the agreed one", strays.SESSION_FIX == "Claude's background service was started from an SSH "
       "login (session 0); stations there can't use your GitHub login. Stop it from that SSH login "
       "(`claude daemon stop --any`) and start the crew again from the desktop.")
    ck("a service in session 0 counts as one other",
       strays.counts({"claude": [], "orphan": [], "service": strays.SESSION_FIX}) == (0, 1))
    strays._windows = lambda: False
    ck("off Windows nothing is said", with_service(0) == "")
finally:
    strays._windows, strays.session_of = real_win, real_session
    watch.running_pid = real_running

print("5 orphans and their age")
root = SCRATCH / "wt"
(root / "ticket").mkdir(parents=True)
ROOTS = [str(root)]
inside = str(root / "ticket" / "x")


def orph(procs, **kw):
    return [s.proc.pid for s in strays.orphans(procs, ROOTS, "crew", set(), NOW, **kw)]


base = [P_(70, 1, "bash", started=OLD)]
ck("3601 s old is reported", orph(base + [P_(71, 999, "node " + inside, started=NOW - 3601)]) == [71])
ck("3599 s old is not", orph(base + [P_(71, 999, "node " + inside, started=NOW - 3599)]) == [])
ck("a live parent is not an orphan", orph(base + [P_(72, 70, "node " + inside)]) == [])
ck("a reused parent pid (parent younger than the child) is gone",
   orph([P_(73, 1, "bash", started=NOW - 10), P_(74, 73, "node " + inside, started=OLD)]) == [74])
ck("a protected pid is skipped", [s.proc.pid for s in strays.orphans(
    [P_(75, 999, "node " + inside)], ROOTS, "crew", {75}, NOW)] == [])

ck("a parent that is a reaper (systemd) is gone even though it exists",
   orph([P_(76, 1, "systemd", started=OLD), P_(77, 76, "node " + inside, started=OLD)]) == [77])
ck("a parent that is a plain process is not", orph([P_(78, 1, "bash"), P_(79, 78, "node " + inside)]) == [])

print("6 the marker and the roots")
outside = str(SCRATCH / "elsewhere" / "tool")
ck("the marker beats the path: outside the roots, marked", orph([P_(80, 999, "node " + outside, crew="crew")]) == [80])
ck("another crew's marker inside the roots is not ours", orph([P_(81, 999, "node " + inside, crew="other")]) == [])
ck("no marker, inside the roots (cmdline)", orph([P_(82, 999, "node " + inside)]) == [82])
ck("no marker, cwd inside the roots", orph([P_(83, 999, "node x", cwd=str(root / "ticket"))]) == [83])
ck("no marker, outside the roots", orph([P_(84, 999, "node " + outside)]) == [])
seen = []
ck("fill() is asked only for an old candidate whose parent is gone",
   orph(base + [P_(85, 999, "node " + inside, started=NOW - 5), P_(86, 70, "node")], fill=lambda p: seen.append(p.pid))
   == [] and seen == [70])

print("6b the watch's Windows launcher is not an orphan")
launcher = P_(90, 999, "pythonw -m tares.crew crew watch", crew="crew")
loop = P_(91, 90, "python -m tares.crew crew watch", started=OLD + 5, crew="crew")
watch.running_pid = lambda paths: 91
try:
    ck("the launcher of the watch is protected", scan([P_(5, 1, "bash"), launcher, loop])["orphan"] == [],
       [s.line() for s in scan([P_(5, 1, "bash"), launcher, loop])["orphan"]])
    ck("a crew-made process that is not the watch's parent still is reported",
       [s.proc.pid for s in scan([P_(5, 1, "bash"), loop, P_(92, 999, "pythonw -m other", crew="crew")])["orphan"]] == [92])

    def lone(parent, child):
        return [s.proc.pid for s in scan([P_(5, 1, "bash"), parent, child])["orphan"]]

    ck("a parent that is bash, not the watch, is not protected",
       lone(P_(90, 999, "bash -c crew", crew="crew"), loop) == [90])
    ck("a parent started after the watch (a reused pid) is not protected",
       lone(P_(90, 999, "pythonw -m tares.crew crew watch", started=OLD + 50, crew="crew"), loop) == [90])
    ck("a watch whose start time cannot be read leaves its launcher protected",
       lone(launcher, P_(91, 90, "python -m tares.crew crew watch", started=0.0, crew="crew")) == [])
    ck("a launcher whose start time cannot be read stays protected",
       lone(P_(90, 999, "pythonw -m tares.crew crew watch", started=0.0, crew="crew"), loop) == [])
    ck("a launcher and a watch with equal start times: the launcher stays protected",
       lone(P_(90, 999, "pythonw -m tares.crew crew watch", started=OLD + 5, crew="crew"), loop) == [])
finally:
    watch.running_pid = real_running

print("7 doctor and status over a ps-table.json")
table = []
for p in healthy() + [left]:
    table.append({"pid": p.pid, "ppid": p.ppid, "started": p.started, "cmdline": list(p.cmdline),
                  "session": strays.session_of(os.getpid())})
(SCRATCH / "ps" / "ps-table.json").write_bytes(json.dumps(table).encode("utf-8"))
ck("table() reads the file", {p.pid for p in strays.table(ENV)} >= {10, 40})
ck("a folder without the file is an empty table, not the machine",
   strays.table({"TARES_FAKE_STRAYS_DIR": str(SCRATCH / "none")}) == [])
d = doctor.Doctor(dict(os.environ) | ENV, PATHS)
d.check_strays()
ids = {c.id: c for c in d.checks}
ck("three new checks", list(ids) == ["claude_processes", "claude_service", "orphan_processes"], list(ids))
ck("all marked new", all(c.new for c in d.checks))
ck("claude_processes warns with the stray", ids["claude_processes"].level == "warn"
   and "pid 40" in ids["claude_processes"].message and "shop-build-old" in ids["claude_processes"].message)
ck("its fix is the agreed one", ids["claude_processes"].fix == strays.CLAUDE_FIX)
ck("service and orphans are ok", ids["claude_service"].level == "ok" and ids["orphan_processes"].level == "ok",
   [(c.id, c.level, c.message) for c in d.checks])
ck("every message starts with a NEW_LABELS label",
   all(any(c.message.startswith(lab) for lab in doctor.NEW_LABELS) for c in d.checks))
many = [P_(100 + i, 5, f"claude -n shop-build-old{i}") for i in range(13)]
ck("at most 10 are listed, then 'and N more'",
   strays.listing(scan([P_(5, 1, "bash")] + many)["claude"]).endswith("and 3 more"))
try:
    from jsonschema import Draft202012Validator

    def sch(n):
        return Draft202012Validator(json.loads((ROOT / "tares/crew/schemas" / f"{n}.json").read_text(encoding="utf-8")))

    doc = {"schema_version": 1, "ok": True, "checks": [c.as_json() for c in d.checks]}
    ck("the doctor document validates against doctor.json", not list(sch("doctor").iter_errors(doc)),
       [e.message for e in sch("doctor").iter_errors(doc)])
    fx = json.loads((ROOT / "tests/fixtures/crew_contract/status.json").read_text(encoding="utf-8"))
    fx["strays"] = {"claude": 1, "other": 0}
    ck("a status document with strays validates", not list(sch("status").iter_errors(fx)),
       [e.message for e in sch("status").iter_errors(fx)])
    fx["strays"] = {"claude": -1, "other": 0}
    ck("a negative count does not", bool(list(sch("status").iter_errors(fx))))
    fx["strays"] = {"claude": 1, "other": 0, "x": 1}
    ck("an extra key does not", bool(list(sch("status").iter_errors(fx))))
except ImportError:
    ck("jsonschema is installed (a test-only dependency)", False)
views_src = (ROOT / "tares/crew/views.py").read_text(encoding="utf-8")
ck("status writes the strays line and the key",
   "strays: {n_claude} claude, {n_other} other ({ME} doctor)" in views_src and 'doc["strays"]' in views_src)

print("8 the watch step tells once per rise")
told, said = [], []
real_scan, real_deliver, real_say = strays.scan, strays._deliver, watch._say
count = {"claude": 2, "other": 0}


def fake_scan(paths, env=None, now=None, procs=None):
    return {"claude": [object()] * count["claude"], "orphan": [object()] * count["other"], "service": ""}


def fake_deliver(paths, env, prompt):
    told.append(prompt)
    return True


def told_now():
    return json.loads((PATHS.home / "strays.json").read_text(encoding="utf-8"))["told"]


strays.scan, strays._deliver, watch._say = fake_scan, fake_deliver, said.append
try:
    t = [NOW]

    def go(c, o, dt=700):
        count["claude"], count["other"] = c, o
        t[0] += dt
        before = len(told)
        strays.step(PATHS, {}, t[0])
        return len(told) - before

    ck("two strays: one message", go(2, 0) == 1)
    ck("the prompt says nothing was stopped and names doctor",
       "Nothing was stopped" in told[-1] and "factory doctor" in told[-1], told[-1])
    ck("the log line", "strays: 2 claude, 0 other (tares factory doctor)" in said, said)
    ck("the same count: none", go(2, 0) == 0)
    ck("within ten minutes nothing is looked at", go(5, 0, dt=60) == 0)
    ck("a fall: none", go(1, 0) == 0)
    ck("told dropped to the new count", told_now() == 1)
    ck("a later rise: one again", go(2, 0) == 1)
    ck("a rise in the other kind: one", go(2, 3) == 1)
    ck("none: no message, told 0", go(0, 0) == 0 and told_now() == 0)
    strays._deliver = lambda p, e, pr: False
    ck("a failed send does not raise told", go(1, 0) == 0 and told_now() == 0)
    ck("the step is registered in the watch", "strays" in watch.STEP_NAMES and watch._STEPS.get("strays") is strays.step)
finally:
    strays.scan, strays._deliver, watch._say = real_scan, real_deliver, real_say

print("9 nothing is stopped")
called = []


def boom(name):
    def f(*a, **k):
        called.append(name)
        raise AssertionError(name)
    return f


saved = {}
targets = [(psutil.Process, n) for n in ("kill", "terminate", "send_signal", "suspend")] + \
          [(os, "kill"), (subprocess, "run"), (subprocess, "Popen")]
for obj, n in targets:
    saved[(obj, n)] = getattr(obj, n)
    setattr(obj, n, boom(f"{getattr(obj, '__name__', 'Process')}.{n}"))
try:
    strays.scan(PATHS, ENV, now=NOW, procs=healthy() + [left, P_(50, 1, "claude daemon run", started=OLD + 5),
                                                         P_(90, 999, "node " + inside)])
    strays.scan(PATHS, ENV, now=NOW)
    d2 = doctor.Doctor(dict(os.environ) | ENV, PATHS)
    d2.check_strays()
    strays.step(PATHS, ENV, NOW + 5000)
    strays.step(PATHS, ENV, NOW + 9000)
finally:
    for (obj, n), v in saved.items():
        setattr(obj, n, v)
ck("no kill, terminate, signal, os.kill, run or Popen was called", called == [], called)
src = (ROOT / "tares/crew/strays.py").read_text(encoding="utf-8")
ck("strays.py holds no way to stop a process", not any(w in src for w in ("kill(", "terminate(", "send_signal", "taskkill")))
ck("strays.py does not use locks.pid_alive", "pid_alive" not in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
