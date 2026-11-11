"""The watch lets go of a station's lock when that station's step ends, and says when it can't read the crew
settings (TR-1171).

Bash runs each station's step of the watch in a `( ... )` subshell, so the lock it took is gone when the step
ends; the Python watch is one process, and used to keep every lock until it exited. And bash's prime says why
when Tares can't give the crew settings; the Python watch said nothing.

Checked here: `locks.scope` / `locks.scoped` let go of what was taken inside and keep what was held before; the
watch's per-station units are scoped; a revive that has ended leaves its station free for another command (a
real second process takes the lock while the first loop is still going); `crew watch --once` prints one line with
the reason when Tares answers 500 to the settings, and nothing when they can be read. The whole watch against
bash is in the parity scenarios `watch_locks_*` and `freeze_watch`.

Run: python tests/test_crew_watch_locks.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-watch-locks-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
CREW = SCRATCH / "crew"
(CREW / "stations").mkdir(parents=True)
(CREW / "crew.json").write_text(json.dumps({"crew_name": "t"}), encoding="utf-8")
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), FACTORY_HOME=str(CREW),
                  FACTORY_CREW_HOME=str(CREW), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
(SCRATCH / "home").mkdir()
_crew_guard.install()
sys.path.insert(0, str(ROOT))

from tares.crew import (cc_update, idle_wake, kit, locks, pace, regrant, watch, watch_revive)  # noqa: E402
from tares.crew.errors import CrewError, StationLockedError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, ok, detail=""):
    global P, F
    if ok:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label} {detail}")


paths = CrewPaths.from_env()


def held(station):
    return paths.lock_dir(station).is_dir()


def other_process_takes(station):
    """A second command: a real process that tries the station's lock. True when it got it."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from tares.crew import locks; from tares.crew.errors import StationLockedError;"
            "\ntry:\n    locks.station_lock(sys.argv[2])\nexcept StationLockedError:\n    sys.exit(7)\n")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT), station], capture_output=True, text=True,
                       env={**os.environ})
    return r.returncode == 0


# ---------- the scope ----------

with locks.scope():
    locks.station_lock("a", paths)
    ck("inside the scope the lock is held", held("a"))
ck("the scope lets go of a lock taken inside it", not held("a"))

locks.station_lock("kept", paths)
with locks.scope():
    locks.station_lock("kept", paths)   # held already: taking it again changes nothing
    locks.station_lock("b", paths)
ck("a lock held before the scope stays held", held("kept"))
ck("and the one taken inside goes", not held("b"))
locks.release_lock("kept", paths)

try:
    with locks.scope():
        locks.station_lock("c", paths)
        raise RuntimeError("boom")
except RuntimeError:
    pass
ck("a scope that raises lets go too", not held("c"))


@locks.scoped
def unit(station, fail=False):
    locks.station_lock(station, paths)
    if fail:
        raise CrewError("refused")
    return 5


ck("a scoped function returns its value", unit("d") == 5)
ck("and has let go when it returns", not held("d"))
try:
    unit("e", fail=True)
except CrewError:
    pass
ck("and when it raises", not held("e"))
ck("scoped keeps the function's name (the watch's reports quote it)", unit.__name__ == "unit")

# ---------- the watch's units are scoped ----------

for label, fn in (("revive/restart (_run)", watch_revive._run), ("regrant", regrant.regrant_station),
                  ("idle wake", idle_wake.send), ("pace", pace.send), ("kit upgrade", kit.upgrade_station),
                  ("Claude Code move", cc_update._move)):
    ck(f"the {label} of one station is scoped", hasattr(fn, "__wrapped__"))

# ---------- another command can take the lock mid-loop ----------

seen = []


def revive(argv, env):
    """What cmd_revive does to the lock: take the station's and keep it until the process exits."""
    locks.station_lock(argv[1], paths)
    seen.append(argv[1])
    return 0


ck("first station: the revive step's unit returns 0", watch_revive._run(revive, ["revive", "s1"], {}) == 0)
ck("its lock is gone while the loop is still going", not held("s1"))
ck("a second command takes it (a real process)", other_process_takes("s1"))
paths.lock_dir("s1").joinpath("pid").unlink(missing_ok=True)
with contextlib.suppress(OSError):
    paths.lock_dir("s1").rmdir()
ck("the second station goes after it", watch_revive._run(revive, ["revive", "s2"], {}) == 0 and seen == ["s1", "s2"])
ck("and its lock is gone as well", not held("s2"))


def refuses(argv, env):
    locks.station_lock(argv[1], paths)
    raise CrewError("could not start")


ck("a refused revive is counted", watch_revive._run(refuses, ["revive", "s3"], {}) == 1)
ck("and its lock goes", not held("s3"))

# ---------- the lock another command holds is still refused ----------

holder = subprocess.Popen([sys.executable, "-I", "-c",
                           "import sys,time; sys.path.insert(0, sys.argv[1]);"
                           "from tares.crew import locks; locks.station_lock('busy'); print('held', flush=True); time.sleep(60)",
                           str(ROOT)], stdout=subprocess.PIPE, text=True, env={**os.environ})
try:
    holder.stdout.readline()
    try:
        with locks.scope():
            locks.station_lock("busy", paths)
        refused = False
    except StationLockedError:
        refused = True
    ck("a station another command holds is still refused inside a scope", refused)
    ck("and the scope does not touch the other command's lock", held("busy"))
finally:
    holder.kill()
    holder.wait()

# ---------- unreadable crew settings ----------


class Tares(BaseHTTPRequestHandler):
    settings_ok = False
    asked = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Tares.asked.append(self.path)
        if self.path == "/health":
            body, code = b"{}", 200
        elif self.path.startswith("/api/crew/settings"):
            body, code = (json.dumps({"settings": {"autonomy": "L4-ship"}}).encode(), 200) if Tares.settings_ok \
                else (b"{}", 500)
        else:
            body, code = b"{}", 404
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Tares)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
ENV = {**os.environ, "TARES_FACTORY_URL": BASE, "TARES_AUTH_TOKEN": "k"}


def once(env):
    """`crew watch --once` with no steps (the rounds are not what is checked): its stderr and exit code."""
    err = io.StringIO()
    saved = dict(watch._STEPS)
    watch._STEPS.clear()
    try:
        with contextlib.redirect_stderr(err):
            rc = watch.cmd_watch(["crew", "watch", "--once"], env)
    finally:
        watch._STEPS.update(saved)
    return rc, err.getvalue()


Tares.settings_ok = False
rc, text = once(ENV)
lines = [x for x in text.splitlines() if x]
ck("watch --once says why when the settings can't be read: one line", len(lines) == 1, repr(text))
ck("and the line names the reason and where Tares is",
   lines == [f"tares factory: could not read the crew settings from Tares at {BASE}"], repr(text))
ck("the round still runs and the exit code is 0", rc == 0)

Tares.settings_ok = True
rc, text = once(ENV)
ck("settings that can be read: nothing is said", text == "" and rc == 0, repr(text))

Tares.settings_ok = False
Tares.asked.clear()
(CREW / "crew.json").rename(CREW / "crew.json.off")
try:
    # no crew.json: require_crew refuses first, as before; bash primes only when there is a crew config
    try:
        once(ENV)
        refused = False
    except CrewError:
        refused = True
finally:
    (CREW / "crew.json.off").rename(CREW / "crew.json")
ck("without a crew config the watch still refuses and asks Tares nothing", refused and Tares.asked == [], Tares.asked)

rc, text = once({**os.environ, "TARES_FACTORY_URL": "http://127.0.0.1:9"})
ck("a Tares that does not answer at all adds no settings line (the steps say so)", text == "" and rc == 0, repr(text))

srv.shutdown()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
