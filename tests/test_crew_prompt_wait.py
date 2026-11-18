"""A station waiting on a question or a permission prompt is not restarted by a wake, a pace note or an unrelated grant.

A restart loses the prompt. The real `claude.quiet` runs here over fake job files (HOME/.claude/jobs/<id>/state.json)
and a fake session list; only the stop, the revive and the regrant's own restart are recorders. No real Claude
Code, nothing of the person's is read or written.

  * an idle wake and a pace note leave a blocked station alone, however long it has waited;
  * a grants change restarts it at once only when an added grant names the station (the answer to its own refused
    command on the desk), otherwise only after the quiet wait of 30 minutes, as the kit move does;
  * a station that is done is woken and regranted as before.

Run: python tests/test_crew_prompt_wait.py
"""
import contextlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-prompt-wait-"))
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

from tares.crew import claude, idle_wake as IW, launch, lifecycle, locks, pace, regrant, strays  # noqa: E402
from tares.crew.client import TaresClient, grants_sum  # noqa: E402
from tares.crew.errors import NotBetweenTurnsError  # noqa: E402
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
HOUR = 3600
world = {"status": "idle", "updated": ""}
stopped: list = []
revived: list = []
restarts: list = []
say_lines: list = []
OLD = "Bash(git status:*)\nBash(ls:*)"


def iso(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def reset(status="idle", waited=0):
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(HOME / ".claude", ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps({STATION: {
        "role": "reviewer", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
        "cwd": str(SCRATCH), "id": SID, "sessionId": "sess-1", "grants": str(grants_sum(OLD + "\n"))}},
        indent=2) + "\n", encoding="utf-8")
    (CREW / "stations" / f"{STATION}.grants").write_bytes((OLD + "\n").encode("utf-8"))
    world.update(status=status, updated=iso(NOW - waited))
    d = HOME / ".claude" / "jobs" / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_bytes(json.dumps({
        "sessionId": "sess-1", "updatedAt": world["updated"],
        "inFlight": {"queued": 0, "tasks": 0, "kinds": []}}).encode("utf-8"))
    del stopped[:], revived[:], restarts[:], say_lines[:]


real_quiet = claude.quiet
claude.quiet = lambda station, wait=None, cl=None, pth=None, now=None: real_quiet(station, wait, cl, pth, NOW if now is None else now)
claude.active_json = lambda *a, **k: [] if stopped else [
    {"id": SID, "name": STATION, "cwd": str(SCRATCH), "state": "working", "status": world["status"], "pid": 4242}]
claude.attached = lambda *a, **k: False
lifecycle._stop_session = lambda job_id, binary: stopped.append(job_id)
lifecycle.revive = lambda cl, pth, args, *, claude_bin: revived.append(args)
launch.idle_wake_state = lambda client: "on"
pace.idle_wake_state = lambda client: "off"
TaresClient.tares_on = lambda self: True


class Grants:
    """A TaresClient stand-in for the regrant: Tares, with these grants."""

    def __init__(self, text, say=None):
        self.text, self.said = text, []

    def tares_on(self):
        return True

    def crew_settings(self):
        pass

    def grants_read(self, project=""):
        return self.text

    def say(self, line):
        self.said.append(line)

    def close(self):
        pass


PLAN = {"station": STATION, "state": "on", "now": NOW, "reason": "work: Shop T1 at abcdef1", "prompt": "PROMPT",
        "keys": ["k1"], "baseline": None, "n": 0, "work": True}
PACE = {"station": STATION, "want": "hold", "prompt": "PACE NOTE", "stretched": True, "held": True}


def wake():
    c = TaresClient(paths, dict(os.environ), say_lines.append)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            IW.send(c, paths, dict(PLAN), claude_bin="x")
    finally:
        c.close()


def pace_note():
    c = TaresClient(paths, dict(os.environ), say_lines.append)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            pace.send(c, paths, dict(PACE), claude_bin="x")
    finally:
        c.close()


def regrant_with(added):
    """regrant_station over the real quiet, Tares giving OLD plus `added`; the restart is a recorder."""
    client = Grants(OLD + added)
    saved = (regrant.TaresClient, launch._ccfg_or, regrant._new_rules, lifecycle.restart, lifecycle.rearm_text)
    try:
        regrant.TaresClient = lambda p, e: client
        launch._ccfg_or = lambda *a, **k: "Kiran"
        regrant._new_rules = lambda *a, **k: []
        lifecycle.rearm_text = lambda c: ""

        def record(c, pth, args, *, claude_bin, only_between_turns=False, prompt_wait=0):
            restarts.append({"guard": only_between_turns, "wait": prompt_wait})
            if not claude.between_turns(STATION, claude_bin, pth, wait=prompt_wait):
                raise NotBetweenTurnsError("not now")
        lifecycle.restart = record
        regrant.regrant_station(paths, STATION, claude_bin="x", env={})
    finally:
        (regrant.TaresClient, launch._ccfg_or, regrant._new_rules, lifecycle.restart, lifecycle.rearm_text) = saved
    return client


def due(added):
    return regrant._may_restart(paths, Grants(OLD + added), STATION, "x")


print("done is at rest; blocked is between turns only when asked to wait 0")
reset()
ck("a done station is at rest", claude.at_rest(STATION, "x", paths) is True)
reset("waiting", waited=5 * HOUR)
ck("a station blocked for hours is not at rest", claude.at_rest(STATION, "x", paths) is False)
ck("it is between turns with no wait", claude.between_turns(STATION, "x", paths) is True)
ck("and with the 30 minute wait, having waited 5 hours", claude.between_turns(STATION, "x", paths, wait=None) is True)
reset("waiting", waited=60)
ck("blocked for a minute is not between turns with the 30 minute wait",
   claude.between_turns(STATION, "x", paths, wait=None) is False)

print("an idle wake")
reset()
wake()
ck("wakes a done station", stopped == [SID] and revived == [[STATION, "PROMPT"]], f"{stopped} {revived}")
reset("waiting", waited=60)
wake()
ck("leaves a station blocked on a prompt alone", stopped == [] and revived == [], f"{stopped} {revived}")
reset("waiting", waited=5 * HOUR)
wake()
ck("even one that has waited for hours", stopped == [] and revived == [], f"{stopped} {revived}")

print("the restart itself, under the lock, looks at the prompt again")
reset("waiting", waited=5 * HOUR)
c = TaresClient(paths, dict(os.environ), say_lines.append)
try:
    try:
        lifecycle.restart(c, paths, [STATION, "P"], claude_bin="x", only_between_turns=True, prompt_wait=math.inf)
        refused = False
    except NotBetweenTurnsError:
        refused = True
    ck("a guarded restart with prompt_wait=inf refuses a blocked station", refused and stopped == [], f"{stopped}")
    lifecycle.restart(c, paths, [STATION, "P"], claude_bin="x", only_between_turns=True)
    ck("and a guarded restart with the default wait still stops it", stopped == [SID], f"{stopped}")
finally:
    c.close()

print("the plan of a wake or a pace note")


def wake_plan():
    launch.idle_wake_state = lambda client: "off"
    try:
        c = TaresClient(paths, dict(os.environ), say_lines.append)
        try:
            return IW.plan(c, paths, STATION, claude_bin="x", now=NOW)
        finally:
            c.close()
    finally:
        launch.idle_wake_state = lambda client: "on"


def pace_plan():
    pace.idle_wake_state = lambda client: "off"
    try:
        c = TaresClient(paths, dict(os.environ), say_lines.append)
        try:
            return pace.plan(c, paths, STATION, pace.Reading("active", 1, note="NOTE"), claude_bin="x")
        finally:
            c.close()
    finally:
        pace.idle_wake_state = lambda client: "off"


def released():
    (CREW / "stations" / f"{STATION}.wake").write_bytes(b'{"released": true}\n')


reset()
released()
ck("an idle wake plans a note for a done station", (wake_plan() or {}).get("reason") == "rearm", str(wake_plan()))
ck("a pace plan plans a note for a done station", (pace_plan() or {}).get("want") == "stretch", str(pace_plan()))
reset("waiting", waited=5 * HOUR)
released()
ck("an idle wake plans nothing for a station blocked on a prompt", wake_plan() is None, str(wake_plan()))
ck("a pace plan plans nothing for it either", pace_plan() is None, str(pace_plan()))

print("a pace note")
reset()
pace_note()
ck("reaches a done station", stopped == [SID] and revived == [[STATION, "PACE NOTE"]], f"{stopped} {revived}")
reset("waiting", waited=5 * HOUR)
pace_note()
ck("leaves a station blocked on a prompt alone", stopped == [] and revived == [], f"{stopped} {revived}")

print("a message from the strays scan to the orchestrator")
reset("waiting", waited=5 * HOUR)
(CREW / "stations.json").write_text(json.dumps({
    "crew-orchestrator": {"role": "orchestrator", "status": "active", "project": "", "repo": "", "cwd": str(SCRATCH),
                          "id": SID, "sessionId": "sess-1"}}, indent=2) + "\n", encoding="utf-8")
sent = strays._deliver(paths, dict(os.environ), "STRAYS")
ck("is not delivered while the orchestrator waits on a prompt", sent is False and stopped == [] and revived == [],
   f"{sent} {stopped} {revived}")

print("a desk answer waking the orchestrator")
reset("waiting", waited=5 * HOUR)
(CREW / "stations.json").write_text(json.dumps({
    "crew-orchestrator": {"role": "orchestrator", "status": "active", "project": "", "repo": "", "cwd": str(SCRATCH),
                          "id": SID, "sessionId": "sess-1"}}, indent=2) + "\n", encoding="utf-8")
(CREW / "stations" / "crew-orchestrator.wake").write_bytes(b'{"baseline": {"desk": "2026-01-01T00:00:00Z"}}\n')
asked: list = []
real_http = TaresClient.tares_http
TaresClient.tares_http = lambda self, method, path, *a, **k: (asked.append(path) or '{"latest": "2026-02-01T00:00:00Z", "count": 1}')
try:
    IW._ANSWERED_DONE.clear()
    IW._ANSWERED_WAIT.clear()
    IW.console_tick(paths, env=dict(os.environ), claude_bin="x")
finally:
    TaresClient.tares_http = real_http
ck("it asks about the answer", len(asked) == 1, str(asked))
ck("a tick leaves an orchestrator blocked on a prompt alone", stopped == [] and revived == [], f"{stopped} {revived}")
ck("and does not count the answer as handled: the next tick asks again", IW._ANSWERED_DONE == {}, str(IW._ANSWERED_DONE))

ADDED_OTHER = "\nBash(make test:*)"
ADDED_NAMED = f"\nBash(git -C /x push origin {STATION}:*)"

print("a grants change")
reset()
regrant_with(ADDED_OTHER)
ck("restarts a done station", len(restarts) == 1, str(restarts))
reset("waiting", waited=60)
ck("is not due for a blocked station when it names nobody", due(ADDED_OTHER) is False)
regrant_with(ADDED_OTHER)
ck("and does not restart it", restarts == [], str(restarts))
ck("so the grants are tried again, not marked as given",
   json.loads((CREW / "stations.json").read_text(encoding="utf-8"))[STATION].get("grants_tried") is None)
reset("waiting", waited=5 * HOUR)
regrant_with(ADDED_OTHER)
ck("restarts a blocked station that has waited the 30 minutes, as the kit move does", len(restarts) == 1
   and restarts[0]["wait"] is None, str(restarts))
reset("waiting", waited=60)
ck("is due for a blocked station when an added grant names it", due(ADDED_NAMED) is True)
regrant_with(ADDED_NAMED)
ck("and restarts it at once, the answer to its own refused command",
   len(restarts) == 1 and restarts[0]["wait"] == 0 and restarts[0]["guard"] is True, str(restarts))
reset("waiting", waited=60)
regrant_with(f"\nBash(git push origin {STATION}-2:*)")
ck("a grant that names another station whose name starts with this one is not this station's", restarts == [],
   str(restarts))
reset("waiting", waited=60)
(CREW / "stations" / f"{STATION}.grants").write_bytes((OLD + ADDED_NAMED + "\n").encode("utf-8"))
regrant_with(ADDED_NAMED)
ck("a named grant the station already started with is not new", restarts == [], str(restarts))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
