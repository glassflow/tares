"""tares.crew.regrant: the pieces of the watch's regrant step that need no running station (TR-960).

The whole step, against bash, is in the parity scenarios `watch_regrant_*` (tests/factory/parity.py). This script
checks what they cannot say alone: the fingerprint is the cksum bash uses, a change already acted on or an
unreadable Tares is never a change, the prompt quotes only the added grants, and the step is registered with
the watch under the name the skeleton reserves for it.

Run: python tests/test_crew_regrant.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-regrant-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), FACTORY_HOME=str(SCRATCH / "crew"),
                  FACTORY_CREW_HOME=str(SCRATCH / "crew"), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
(SCRATCH / "home").mkdir()
_crew_guard.install()   # a restart the step makes must not reach the real ~/factory (TR-1164)
sys.path.insert(0, str(ROOT))

from tares.crew import claude, launch, lifecycle, regrant, state, watch, watch_revive  # noqa: E402
from tares.crew.client import grants_sum  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def check(label, ok):
    global P, F
    if ok:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}")


class Client:
    """The part of TaresClient the fingerprint reads: the grants text, or an error."""

    def __init__(self, text=None):
        self.text = text

    def grants_read(self, project=""):
        if self.text is None:
            raise CrewError("no Tares")
        return self.text


def crew(rows):
    tmp = Path(tempfile.mkdtemp(prefix="regrant-"))
    (tmp / "stations.json").write_text(json.dumps(rows), encoding="utf-8")
    return CrewPaths.at(tmp)


A = '- "may label pull requests" (2026-02-03)\n'
B = '- "may merge in shop" (2026-02-04)\n'

# the fingerprint
check("the fingerprint is the cksum of the grants and a newline",
      regrant.grants_now(crew({"s": {}}), Client(A), "s") == str(grants_sum(A)))
check("a grants text with extra trailing newlines gives the same fingerprint",
      regrant.grants_now(crew({"s": {}}), Client(A + "\n\n"), "s") == str(grants_sum(A)))
check("a Tares that does not answer gives no fingerprint", regrant.grants_now(crew({"s": {}}), Client(None), "s") is None)

# what counts as a change
was = str(grants_sum(A))
paths = crew({"s": {"grants": was}})
check("unchanged grants are no change", not regrant.regranted(paths, Client(A), "s"))
check("grants that differ are a change", regrant.regranted(paths, Client(A + B), "s"))
check("a Tares that does not answer is no change", not regrant.regranted(paths, Client(None), "s"))
paths = crew({"s": {"grants": was, "grants_tried": str(grants_sum(A + B))}})
check("a change already acted on is not acted on again", not regrant.regranted(paths, Client(A + B), "s"))
check("a different change after it is", regrant.regranted(paths, Client(B), "s"))
paths = crew({"s": {}})
check("a station started before grants were tracked is never regranted", not regrant.regranted(paths, Client(A + B), "s"))

# when a restart is due
paths = crew({"s": {"grants": was, "sessionId": "abc"}})
check("a station with a session and new grants is due", regrant.regrant_due(paths, Client(A + B), "s"))
paths = crew({"s": {"grants": was}})
check("no session to resume: not due", not regrant.regrant_due(paths, Client(A + B), "s"))
paths = crew({"s": {"grants": was, "sessionId": "abc"}})
(paths.home / "stations").mkdir()
paths.done_file("s").write_text("", encoding="utf-8")
check("a done station is not due for a grant that does not name it", not regrant.regrant_due(paths, Client(A + B), "s"))
events = lambda p: [json.loads(l) for l in p.events.read_text(encoding="utf-8").splitlines()]
check("the skip is one line in the event log",
      [(e["event"], e["station"], e["detail"]) for e in events(paths)] == [("regrant-skipped", "s", "done")])
regrant.regrant_due(paths, Client(A + B), "s")
check("and only once for the same change", len(events(paths)) == 1)
regrant.regrant_due(paths, Client(A + B + '- "may read in shop" (2026-02-05)\n'), "s")
check("a further change is noted again", len(events(paths)) == 2)
NAMED = '- "s may run `gh pr merge 1` in shop" (2026-02-05)\n'
check("a done station is due for a new grant that names it", regrant.regrant_due(paths, Client(A + NAMED), "s"))
paths.station_file("s", "grants").write_bytes((A + NAMED).encode())   # LF, as launch writes it
check("a grant that named it before it started does not wake it",
      not regrant.regrant_due(paths, Client(A + NAMED + B), "s"))
check("the name is matched whole", not regrant.names_station('- "s-10 may run x"', "s-1")
      and not regrant.names_station('- "xs-1 may run x"', "s-1") and regrant.names_station('- "s-1 may run x"', "s-1"))
check("the role's name wakes nobody", not regrant.names_station('- "The builder may run x"', "shop-build-m1"))

# what the prompt quotes
check("added grants are the lines not already whole lines of the kept file", regrant.added_grants(A + B, A) == B.rstrip("\n"))
check("a kept line that only contains a new line does not hide it",
      regrant.added_grants("- a b\n- a\n", "- a b\n") == "- a")
check("without a kept file every grant is added", regrant.added_grants(A + B, None) == A + B)
check("a kept file with no final newline still counts its last line", regrant.added_grants(A + B, A.rstrip("\n")) == B.rstrip("\n"))
check("nothing added gives nothing", regrant.added_grants(A, A) == "")
p = regrant._prompt("Kiran", "- x", "read the rules")
check("the prompt with rules quotes them and says to run the refused command again",
      "Kiran allowed new permissions in Tares" in p and "\n- x\n" in p and "run it again now" in p)
p = regrant._prompt("Kiran", "", "read the rules")
check("the prompt without rules says what changed and points at read_grants",
      "changed what the crew is allowed in Tares" in p and "read_grants shows them" in p)

# the step against the revive step's round (reviewer F3, F4 on TR-959)
class StepClient:
    def tares_on(self):
        pass

    def close(self):
        pass


def run_step(round_, stations, listed_cwd="/w", recorded_cwd="/w", same_id=True):
    """regrant.step over `stations` (all running, between turns, due), with the revive step's `round_` as its last."""
    acted = []
    saved = (claude.claude_bin, claude.active_json, claude.between_turns, claude.gone, regrant.TaresClient,
             regrant.regrant_due, regrant.regrant_station, watch_revive.stuck_on_context, watch_revive.last)
    try:
        claude.claude_bin = lambda env: "claude"
        claude.active_json = lambda binary: [{"id": f"id-{s}" if same_id else f"new-{s}", "name": s, "cwd": listed_cwd} for s in stations]
        claude.between_turns = lambda *a, **k: True
        claude.gone = lambda row: False
        regrant.TaresClient = lambda paths, env: StepClient()
        regrant.regrant_due = lambda *a, **k: True
        regrant.regrant_station = lambda paths, station, **k: acted.append(station)
        watch_revive.stuck_on_context = lambda *a, **k: False
        watch_revive.last = round_
        rows = {s: {"status": "active", "role": "reviewer", "cwd": recorded_cwd, "id": f"id-{s}"} for s in stations}
        regrant.step(crew(rows), env={})
    finally:
        (claude.claude_bin, claude.active_json, claude.between_turns, claude.gone, regrant.TaresClient,
         regrant.regrant_due, regrant.regrant_station, watch_revive.stuck_on_context, watch_revive.last) = saved
    return acted


check("every due station is restarted when the revive step started none",
      run_step(watch_revive.Round(), ["a", "b"]) == ["a", "b"])
r = watch_revive.Round()
r.started.add("a")
check("a station the revive step started this round is left alone, the others go on",
      run_step(r, ["a", "b"]) == ["b"])
r = watch_revive.Round()
r.unread = True
check("a round whose session list the revive step could not read restarts nothing", run_step(r, ["a", "b"]) == [])

# TR-1305: a running station is found by its folder spelled another way (a trailing separator here; on Windows
# also the slash and the letter case), when the session list's id is not the recorded one
check("a station whose listed folder has a trailing separator is running, so it is restarted",
      run_step(watch_revive.Round(), ["a"], listed_cwd="/w/", same_id=False) == ["a"])
check("a station whose recorded folder has a trailing separator is running too",
      run_step(watch_revive.Round(), ["a"], recorded_cwd="/w/", same_id=False) == ["a"])
check("a session in another folder under another id is not the station: nothing is restarted",
      run_step(watch_revive.Round(), ["a"], listed_cwd="/w/x", same_id=False) == [])

# the steps after this one leave a restarted station alone this round (bash's elif chain; TR-961 idle wake reads it)
r = watch_revive.Round()
r.started.add("a")
run_step(r, ["a", "b", "c"])
check("a station restarted for new grants is recorded as handled, one the revive step started is not",
      r.handled == {"b", "c"})
r = watch_revive.Round()
r.unread = True
run_step(r, ["a"])
check("a round restarting nothing records nothing as handled", r.handled == set())

# a restart that fails is not tried again every round (reviewer F1, F2): grants_tried is written before it
class RestartClient(StepClient):
    def __init__(self, text):
        self.text = text
        self.said = []

    def tares_on(self):
        return True

    def crew_settings(self):
        pass

    def grants_read(self, project=""):
        return self.text

    def say(self, line):
        self.said.append(line)


def run_failing_restart(error):
    paths = crew({"s": {"grants": str(grants_sum(A)), "sessionId": "abc", "status": "active"}})
    client = RestartClient(A + B)
    saved = (regrant.TaresClient, claude.between_turns, launch._ccfg_or, regrant._new_rules, lifecycle.restart,
             lifecycle.rearm_text)
    try:
        regrant.TaresClient = lambda p, e: client
        claude.between_turns = lambda *a, **k: True
        launch._ccfg_or = lambda *a, **k: "Kiran"
        regrant._new_rules = lambda *a, **k: ["may merge"]
        lifecycle.rearm_text = lambda c: ""

        def boom(*a, **k):
            raise error
        lifecycle.restart = boom
        regrant.regrant_station(paths, "s", claude_bin="claude", env={})
    finally:
        (regrant.TaresClient, claude.between_turns, launch._ccfg_or, regrant._new_rules, lifecycle.restart,
         lifecycle.rearm_text) = saved
    return paths, client


for what, err in (("a refused restart", CrewError("no room")), ("a restart that hits the disk", OSError("disk full"))):
    paths, client = run_failing_restart(err)
    check(f"{what} is said, and the station gets the grants when it next starts",
          any("could not restart s with its new permissions" in line for line in client.said))
    check(f"{what} has written grants_tried first, so the next round does not try again",
          state.stations_get("s", "grants_tried", paths=paths) == str(grants_sum(A + B))
          and not regrant.regranted(paths, Client(A + B), "s"))

# registered with the skeleton
check("the watch runs this step under the name the skeleton reserves", watch._STEPS.get("regrant") is regrant.step)
check("that name is one of the skeleton's", "regrant" in watch.STEP_NAMES)

import shutil  # noqa: E402

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
