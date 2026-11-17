"""A wake never stops a station that is in a turn.

`idle_wake.send` looks at the station, then `lifecycle.restart` takes the station's lock and stops the session.
A turn that starts between those two looks (a SendMessage arriving, say) must not be cut off: the restart looks
again under the lock, right before the stop, and leaves the station alone when it is no longer between turns.

The real `claude.quiet` and `claude.between_turns` run here over fake job files (HOME/.claude/jobs/<id>/state.json)
and a fake session list; only the stop and the revive are recorders. No real Claude Code, nothing of the
person's is read or written.

Run: python tests/test_crew_wake_midturn.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-wake-midturn-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
HOME = SCRATCH / "home"
CREW = SCRATCH / "crew"
os.environ.update(
    HOME=str(HOME), USERPROFILE=str(HOME), PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    TARES_HOME=str(SCRATCH / "tares-home"), FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
    FACTORY_CREW_HOME=str(CREW), CLAUDE_BIN="claude-not-run", FACTORY_STOP_WAIT="0",
)
HOME.mkdir()
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run

sys.path.insert(0, str(ROOT))

from tares.crew import claude, idle_wake as IW, launch, lifecycle, locks  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


paths = CrewPaths.at(CREW)
STATION = "crew-reviewer"
SID = "aaaa0001"
NOW = 1_800_000_000
world = {"status": "idle", "inflight": {"queued": 0, "tasks": 0, "kinds": []}, "attached": False}
stopped: list = []
revived: list = []
say_lines: list = []


def reset():
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(HOME / ".claude", ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps({STATION: {
        "role": "reviewer", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
        "cwd": str(SCRATCH), "id": SID, "sessionId": "sess-1"}}, indent=2) + "\n", encoding="utf-8")
    world.update(status="idle", inflight={"queued": 0, "tasks": 0, "kinds": []}, attached=False)
    write_job()
    del stopped[:], revived[:], say_lines[:]


def write_job():
    d = HOME / ".claude" / "jobs" / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_bytes(json.dumps({"sessionId": "sess-1", "inFlight": world["inflight"]}).encode("utf-8"))


def start_a_turn():
    world["status"] = "busy"
    world["inflight"] = {"queued": 1, "tasks": 0, "kinds": []}
    write_job()


claude.active_json = lambda *a, **k: [] if stopped else [
    {"id": SID, "name": STATION, "cwd": str(SCRATCH), "state": "working", "status": world["status"], "pid": 4242}]
claude.attached = lambda *a, **k: world["attached"]
lifecycle._stop_session = lambda job_id, binary: stopped.append(job_id)
lifecycle.revive = lambda cl, pth, args, *, claude_bin: revived.append(args)
launch.idle_wake_state = lambda client: "on"
TaresClient.tares_on = lambda self: True

real_lock = locks.station_lock
hook: dict = {"in_lock": None}


def lock_then_hook(station, pth=None):
    real_lock(station, pth)
    fn, hook["in_lock"] = hook["in_lock"], None
    if fn:
        fn()   # what happens in the moment between send's own look and the stop


locks.station_lock = lock_then_hook

PLAN = {"station": STATION, "state": "on", "now": NOW, "reason": "work: Shop T1 at abcdef1", "prompt": "PROMPT",
        "keys": ["k1"], "baseline": None, "n": 0, "work": True}


def send():
    err = io.StringIO()
    c = TaresClient(paths, dict(os.environ), say_lines.append)
    try:
        with contextlib.redirect_stderr(err):
            IW.send(c, paths, dict(PLAN), claude_bin="x")
    finally:
        c.close()
    return err.getvalue()


def wake_file():
    f = CREW / "stations" / f"{STATION}.wake"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


print("the real between_turns over job files")
reset()
ck("a session done with nothing in flight is between turns", claude.between_turns(STATION, "x", paths) is True)
start_a_turn()
ck("a busy session with a message queued is not", claude.between_turns(STATION, "x", paths) is False)

print("a station between turns is woken")
reset()
send()
ck("it is stopped once", stopped == [SID], str(stopped))
ck("and revived with the prompt", revived == [[STATION, "PROMPT"]], str(revived))
ck("the wake is recorded", wake_file().get("last_wake") == NOW and wake_file().get("seen") == ["k1"], str(wake_file()))

print("a turn that starts after send's look and before the stop is not stopped")
reset()
hook["in_lock"] = start_a_turn
send()
ck("the session is never stopped", stopped == [], str(stopped))
ck("and never revived", revived == [], str(revived))
ck("it says it left the station alone", any("started a turn" in s for s in say_lines), str(say_lines))
ck("the wake file keeps its key for the next round", wake_file().get("seen", []) == [], str(wake_file()))
ck("no idle_wake event is written", not (CREW / "events.jsonl").is_file()
   or "idle_wake" not in (CREW / "events.jsonl").read_text(encoding="utf-8"))

print("someone attaching in that moment is not cut off either")
reset()


def attach():
    world["attached"] = True


hook["in_lock"] = attach
send()
ck("the attached session is not stopped", stopped == [] and revived == [], f"{stopped} {revived}")

print("the next round, between turns again, wakes it")
reset()
hook["in_lock"] = start_a_turn
send()
world.update(status="idle", inflight={"queued": 0, "tasks": 0, "kinds": []})
write_job()
send()
ck("the second try stops and revives it", stopped == [SID] and revived == [[STATION, "PROMPT"]], f"{stopped} {revived}")

print("a plain restart, not a wake, still stops a busy station")
reset()
start_a_turn()
c = TaresClient(paths, dict(os.environ), say_lines.append)
try:
    lifecycle.restart(c, paths, [STATION, "P"], claude_bin="x")
finally:
    c.close()
ck("restart without the guard is unchanged", stopped == [SID], str(stopped))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
