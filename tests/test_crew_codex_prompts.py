"""What the factory tells a station after its first start, per runtime (TR-1598).

A Codex station has no skills to load, no schedule tool and no way to message a station; what a restart, a
revive, a helper's start, a plugin update, a grant and an idle wake tell it must not ask for any of those.
Claude Code's words stay as they were.

 a. Claude Code: the reload step, the schedule step, the revive prompts (resumed and fresh), a helper's first
    prompt and the idle wake that tells a station to re-arm its tick are word for word what they were;
 b. Codex: the same prompts name nothing of role_text.CLAUDE_ONLY, no skill, no Cron tool, no schedule to
    re-arm and no SendMessage, and say where its rules are;
 c. the idle wake: a Codex station holds no tick, so the factory wakes it whatever the idle_wake setting.
    With the setting off it is planned as with the setting on (a liveness wake, with the Codex note), never
    told to re-arm a tick; the send step checks the setting the same way, so the wake goes through; a
    station on Claude Code with the setting off is planned as before;
 d. `tares factory outcome` as the dispatcher calls it (the command word first): it lists the calls with an
    unknown outcome, and takes one off by its call id, for the station or a person naming it.

Run: python tests/test_crew_codex_prompts.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# a short path on macOS and Linux (/tmp); Windows has no /tmp, so there the default temp folder
SCRATCH = Path(tempfile.mkdtemp(prefix="tcp-", dir=None if os.name == "nt" else "/tmp"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "FAKE_CODEX", "CLAUDE", "CODEX"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
HOME = SCRATCH / "home"
HOME.mkdir()
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))
import _crew_guard  # noqa: E402

_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

from tares.crew import codex_station, idle_wake, lifecycle, proc, state, stations  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew import role_text as rt  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
_n = [0]
CC, CX, HELPER = "p1-build-cc", "p1-build-cx", "p1-build-cx-h1"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def named(text):
    """What a Codex station cannot do that `text` asks of it: names of CLAUDE_ONLY, and the words for them."""
    words = [n for n in rt.CLAUDE_ONLY if n.lower() in text.lower()]
    return words + [w for w in ("skill", "Cron", "re-arm", "tick", "Claude Code", "SendMessage")
                    if w.lower() in text.lower()]


def crew(idle_wake_on=False):
    """A crew with a builder on Claude Code (CC), one on Codex (CX) and a helper of CX, and a fake Tares."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    rows = {CC: {"role": "builder", "runtime": "claude_code", "sessionId": "s-cc", "project": "p1"},
            CX: {"role": "builder", "runtime": "codex", "sessionId": "thr-cx", "project": "p1"},
            HELPER: {"role": "helper", "parent": CX, "runtime": "codex", "sessionId": "thr-h", "project": "p1"}}
    LS.make_crew(tmp, {"token": "tok-fake-codex-prompts-0123456789", "stations": rows})
    tares = LS.FakeTares({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": idle_wake_on}})
    tares.__enter__()
    paths = CrewPaths.at(tmp / "crew")
    client = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url}, say=lambda *_: None)
    client.prime()
    return tares, paths, client


tares, paths, client = crew()

print("a. Claude Code's words are as they were")
SCHEDULE = lifecycle.rearm_text(client)
ck("the schedule step of a Claude Code station is the one every caller had", lifecycle.rearm_text(client, CC) == SCHEDULE
   and SCHEDULE == "re-arm your schedule if you have one", SCHEDULE)
ck("the reload step names its skills", lifecycle.reload_text(paths, CC, "builder")
   == "Reload the skills factory-protocol and station-builder" and lifecycle.reload_text(paths, CC, "builder", may_change=True)
   == "Reload the skills factory-protocol and station-builder (they may have changed)")
AGAIN = ("Reload the skills factory-protocol and station-builder, " + lifecycle.REPORT_READ + " and your work on Tares, "
         "re-arm your schedule if you have one, then carry on where your report left off.")
ck("a revive with no prompt", lifecycle.revive_prompt(client, paths, CC, "builder", False)
   == "You were revived by the factory under the same name. " + AGAIN, lifecycle.revive_prompt(client, paths, CC, "builder", False))
ck("a fresh revive", lifecycle.revive_prompt(client, paths, CC, "builder", True)
   == "You were started again under the same name, in a new conversation: the last one kept refilling its context right "
   "after each compaction, so Claude Code stopped it. Nothing from it carries over except your report and what is on "
   "Tares. " + AGAIN + " Read big files, logs and diffs in parts.")
ck("a helper's first prompt", stations.helper_prompt(paths, CC, "Look at X.")
   == f"Boot as a helper of the station {CC}: load the skills factory-protocol and station-builder, do only the task "
   f"below, write results to your report, and send {CC} DONE when it is finished.\n\nYour task:\nLook at X.")

print("b. a Codex station is asked for nothing it does not have")
said = {"schedule": lifecycle.rearm_text(client, CX),
        "revive": lifecycle.revive_prompt(client, paths, CX, "builder", False),
        "fresh revive": lifecycle.revive_prompt(client, paths, CX, "builder", True),
        "helper revive": lifecycle.revive_prompt(client, paths, HELPER, "builder", False),
        "plugin update": lifecycle.reload_text(paths, CX, "builder", may_change=True),
        "helper": stations.helper_prompt(paths, CX, "Look at X.")}
bad = {k: named(v) for k, v in said.items() if named(v)}
ck("no skill, no Cron tool, no tick to re-arm, no message to send, nothing of Claude Code's", not bad, bad)
ck("its schedule step says it holds none and is woken",
   said["schedule"] == "keep no schedule of your own (the factory wakes you when there is work)", said["schedule"])
ck("a revive and a plugin update point at its instructions", all(
   "Your rules are in your instructions" in said[k] for k in ("revive", "fresh revive", "helper revive", "plugin update"))
   and said["plugin update"].endswith("(they may have changed)"), said)
ck("a fresh revive says only what carries over", said["fresh revive"].startswith(
   "You were started again under the same name, in a new conversation. Nothing from the last one carries over except "
   "your report and what is on Tares."), said["fresh revive"])
ck("a helper on Codex sends its parent DONE with send_to_station (its report's DONE line where the tool is missing)",
   f"send {CX} DONE with the Tares tool `send_to_station`" in said["helper"] and "end your report with the line DONE" in said["helper"]
   and said["helper"].endswith("Your task:\nLook at X."), said["helper"])

print("c. the idle wake of a Codex station")
real_rest, real_scan = codex_station.at_rest, dict(idle_wake.SCANS)
codex_station.at_rest = lambda _p, _s: True   # between turns, with nobody connected
idle_wake.SCANS["builder"] = lambda *_a: {"live": True, "pending": [], "baseline": None}
try:
    ck("the setting is off for the crew", L.idle_wake_state(client) == "off")
    ck("for a Codex station it is on: the factory is what wakes it", idle_wake.state_for(client, paths, CX) == "on"
       and idle_wake.state_for(client, paths, CC) == "off")
    state.wake_update(CX, {"released": True, "clock": 0, "n": 0}, paths)
    p = idle_wake.plan(client, paths, CX, now=10 ** 6)
    ck("with the setting off it is planned a liveness wake, with the Codex note, and not told to re-arm a tick",
       p is not None and p["state"] == "on" and p["reason"].startswith("liveness") and named(p["prompt"]) == []
       and L.idle_wake_note("codex") in p["prompt"], p)
    sent = []
    real_restart = lifecycle.restart
    lifecycle.restart = lambda _c, _p, argv, **_k: sent.append(argv)
    try:
        idle_wake.send(client, paths, p, claude_bin="")
    finally:
        lifecycle.restart = real_restart
    ck("and the send step lets it through: the setting it checks again is the station's", sent == [[CX, p["prompt"]]], sent)
    ck("the station's wake file says it holds no tick", state.wake_read(CX, paths)["released"] is True)
    state.wake_update(CX, {"released": False, "clock": 0, "n": 0}, paths)   # a station started before this version
    old = idle_wake.plan(client, paths, CX, now=10 ** 6)
    ck("a Codex station started before, with its wake file not released, is not told idle_wake was switched on",
       old is not None and old["reason"].startswith("liveness") and "switched on" not in old["prompt"], old)
    L.iw_launch_note(client, CX)
    ck("a start on Codex with the setting off releases the tick it does not hold", state.wake_read(CX, paths)["released"] is True)
    real_cc = proc.at_rest
    proc.at_rest = lambda *_a, **_k: True
    try:
        state.wake_update(CC, {"released": True}, paths)
        q = idle_wake.plan(client, paths, CC, now=10 ** 6)
    finally:
        proc.at_rest = real_cc
    ck("a Claude Code station with the setting off is told to re-arm its tick, as before", q is not None
       and q["reason"] == "rearm" and q["prompt"] == "idle_wake is off again: re-arm your tick as your manual says.", q)
finally:
    codex_station.at_rest = real_rest
    idle_wake.SCANS.clear()
    idle_wake.SCANS.update(real_scan)
tares.__exit__(None, None, None)

print("d. `tares factory outcome`, as the dispatcher calls it")
import contextlib  # noqa: E402
import io  # noqa: E402

from tares.crew import codex_guard, dispatch  # noqa: E402

tares, paths, client = crew()
state.write_json(paths.station_file(CX, codex_guard.UNKNOWN), [
    {"call": "call_a1", "command": "git push origin factory/T1", "cwd": "/w"},
    {"call": "call_b2", "command": "sleep 60", "cwd": "/w"}], mode=0o600)
env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "FACTORY_STATION": CX}


def factory(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = dispatch.find(list(argv))(list(argv), env)
    return code, out.getvalue()


code, out = factory("outcome")
ck("with nothing after it, it lists the station's calls by call id", code == 0 and "call_a1  git push" in out
   and "call_b2  sleep 60" in out, out)
code, out = factory("outcome", "call_b2", "not-done", "the sleep was killed with its app-server")
left = [r.get("call") for r in codex_station.unknown(paths, CX)]
ck("a station's account takes that call off the list, and only that one", code == 0 and "recorded: not-done for sleep 60"
   in out and left == ["call_a1"], (out, left))
code, out = factory("outcome", "--station", CX, "call_a1", "done", "the branch is on the remote at the same head")
ck("a person names the station with --station", code == 0 and codex_station.unknown(paths, CX) == [], out)
err = io.StringIO()
with contextlib.redirect_stderr(err):
    code, out = factory("outcome", "--station", CX, "call_a1", "done", "settled twice")
ck("a call already settled (here by the station itself) is refused in one line, exit 1, not a traceback",
   code == 1 and "has no call call_a1 with an unknown outcome" in err.getvalue() and "Traceback" not in err.getvalue(),
   (code, err.getvalue()))
tares.__exit__(None, None, None)

print("e. a Codex station's command that cannot reach Tares")
from tares.crew.errors import CrewError as _CE  # noqa: E402

tares, paths, client = crew()
dead = "http://127.0.0.1:9"   # nothing listens there


def refusal(env_extra):
    c = TaresClient(paths, env={**os.environ, "TARESD_URL": dead, **env_extra}, say=lambda *_: None)
    try:
        c.require_tares()
    except _CE as e:
        return str(e)
    return ""


ck("from a Codex station (its FACTORY_STATION runs on Codex), it says the sandbox has no network, and what to do",
   "Codex sandbox, which has no network, Tares is not down" in refusal({"FACTORY_STATION": CX}))
ck("so it does with the sandbox's own mark", "ask for approval" in refusal({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}))
ck("anywhere else it says what it always said", "Start one with `tares up`" in refusal({"FACTORY_STATION": CC})
   and "Start one with `tares up`" in refusal({}))
tares.__exit__(None, None, None)

print(json.dumps({"passed": P, "failed": F}) if os.environ.get("TEST_JSON") else f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
