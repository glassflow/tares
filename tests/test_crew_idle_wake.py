"""The idle-wake step of the Python watch (tares/crew/idle_wake.py, TR-961) does what bash's does.

Each role's scan, the plan (when a station is woken and with what), the send (the restart and what is written
to the wake file), the launch note and the step's loop run here against a small fake Tares and a throwaway crew
home. What Claude Code says about a station (between turns, attached, last activity, alive) is replaced by
values the test sets, and the restart by a recorder: no real Claude Code, no bash. The same scenarios run on
bash and on Python in the parity harness (`tests/factory/parity.py idle_wake_*`).

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: python tests/test_crew_idle_wake.py
"""
import http.server
import io
import json
import os
import contextlib
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-idle-wake-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
CREW = SCRATCH / "crew"
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1", FACTORY_CREW_HOME=str(CREW), CLAUDE_BIN="claude-not-run",
)
(SCRATCH / "home").mkdir()
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))

from tares.crew import claude, idle_wake as IW, launch, lifecycle, state, watch, watch_revive  # noqa: E402
from tares.crew.client import TaresClient, cksum  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class FakeTares:
    """A Tares that answers by route ({"GET /x": (status, doc)}) and writes down each request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                key = f"GET {self.path}"
                outer.requests.append(key)
                if key == "GET /health":
                    status, doc = 200, {"ok": True}
                else:
                    status, doc = outer.routes.get(key, (404, {"detail": "not found"}))
                data = (doc if isinstance(doc, str) else json.dumps(doc)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["TARESD_URL"] = self.url
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("TARESD_URL", None)


NOW = 1_800_000_000
paths = CrewPaths.at(CREW)
say_lines: list = []


def client() -> TaresClient:
    return TaresClient(paths, dict(os.environ), say_lines.append)


def reset(stations=None, wake=None, report=None):
    shutil.rmtree(CREW, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps(stations or {}, indent=2) + "\n", encoding="utf-8")
    for name, w in (wake or {}).items():
        (CREW / "stations" / f"{name}.wake").write_text(json.dumps(w) + "\n", encoding="utf-8")
    if report is not None:
        (CREW / "reports").mkdir()
        (CREW / "reports" / "crew-orchestrator.md").write_text(report, encoding="utf-8")
    del say_lines[:]
    claude.between_turns = lambda *a, **k: world["quiet"]
    claude.attached = lambda *a, **k: world["attached"]
    claude.station_updated = lambda *a, **k: world["updated"]
    claude.is_alive = lambda *a, **k: world["alive"]
    world.update(quiet=True, attached=False, updated=None, alive=True)


world: dict = {}


def row(role, **over):
    r = {"role": role, "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
         "cwd": str(SCRATCH), "id": "aaaa0001", "sessionId": "sess-1"}
    r.update(over)
    return r


def wake(name):
    f = CREW / "stations" / f"{name}.wake"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def settings(on=True):
    return (200, {"settings": {"idle_wake": on}})


QUEUE = {"queue": [
    {"project": "p1", "project_name": "Shop", "ticket": "T1", "head": "abcdef1234567", "verdict": None},
    {"project": "p1", "ticket": "T2", "head": "1111111222", "verdict": {"current": True, "verdict": "pass"}},
    {"project": "p2", "ticket": "T3", "head": "2222222333", "verdict": {"current": False, "verdict": "changes"}}]}

# ------------------------------------------------------------------------------------------------
print("the small rules")
ck("wake_base_min: reviewer 15, orchestrator 20, the rest 30",
   [IW.wake_base_min(r) for r in ("reviewer", "orchestrator", "builder", "releaser")] == [15, 20, 30, 30])
ck("work_prompt: reviewer", IW.work_prompt("reviewer", "X") == "There is work in the review queue: X have no current verdict. Work the queue.")
ck("work_prompt: builder", IW.work_prompt("builder", "X") == "Your tickets changed: X. Read the reviewer's verdict and CI on the current head and act on it, as your manual says.")
ck("work_prompt: releaser", IW.work_prompt("releaser", "X") == "A RELEASE is waiting for you in the orchestrator's report under Undelivered: X. Take it as your manual says.")
ck("work_prompt: orchestrator", IW.work_prompt("orchestrator", "X") == "Something is waiting for you: X. Follow the Tick section of your manual.")
ck("the after() ladder: base, 30, 45, 45; pacing waits 45 at once",
   [IW._after(n, 15, 0) for n in (0, 1, 2, 9)] == [900, 1800, 2700, 2700] and IW._after(0, 15, 1) == 2700)
ck("the step is registered with the watch", "idle_wake" in watch._STEPS)

# ------------------------------------------------------------------------------------------------
print("the wake file")
reset({"crew-reviewer": row("reviewer")})
w = state.wake_read("crew-reviewer", paths)
ck("no file gives the defaults", w == state.WAKE_DEFAULT and w["seen"] is not state.WAKE_DEFAULT["seen"], str(w))
state.wake_update("crew-reviewer", {"n": 2, "released": True}, paths)
ck("wake_update writes the whole state, compact, defaults first",
   (CREW / "stations" / "crew-reviewer.wake").read_text(encoding="utf-8")
   == '{"seen":[],"n":2,"last_wake":null,"clock":null,"baseline":null,"released":true}\n',
   (CREW / "stations" / "crew-reviewer.wake").read_text(encoding="utf-8"))
state.wake_update("crew-reviewer", {"seen": ["a"]}, paths)
ck("...and keeps the rest", wake("crew-reviewer")["n"] == 2 and wake("crew-reviewer")["seen"] == ["a"])
ck("the defaults are never changed by an update", state.WAKE_DEFAULT["seen"] == [])

# ------------------------------------------------------------------------------------------------
print("the reviewer's scan")
reset({"crew-reviewer": row("reviewer")})
with FakeTares({"GET /api/review-queue?mine=1": (200, QUEUE)}) as t:
    c = client()
    s = IW.scan_reviewer(c, paths, "crew-reviewer")
    c.close()
ck("PRs without a current verdict are pending, once per head", s == {"live": True, "pending": [
    {"key": "rv:p1/T1@abcdef1234567", "text": "Shop T1 at abcdef1"},
    {"key": "rv:p2/T3@2222222333", "text": "p2 T3 at 2222222"}]}, str(s))
with FakeTares({"GET /api/review-queue?mine=1": (200, {})}) as t:
    c = client()
    ck("a queue that is missing is empty", IW.scan_reviewer(c, paths, "x") == {"pending": [], "live": True})
    c.close()
with FakeTares({"GET /api/review-queue?mine=1": (500, "boom")}) as t:
    c = client()
    try:
        IW.scan_reviewer(c, paths, "x")
        failed = False
    except IW.ScanError:
        failed = True
    c.close()
ck("a queue Tares cannot give fails the scan", failed)

# ------------------------------------------------------------------------------------------------
print("the builder's scan")
PROJECTS = {"projects": [{"id": "uc_1", "name": "shop"}, {"id": "uc_2", "name": "other"}]}


def ticket(label, stage, holder="shop-build-m1", verdict=None, pr=None):
    return {"label": label, "stage": stage, "holder": holder, "verdict": verdict, "pr": pr}


TICKETS = {"tickets": [
    ticket("T1", "changes", pr={"head": "aaa", "ci": "success"}),
    ticket("T2", "review", verdict={"current": True, "verdict": "pass"}, pr={"head": "bbb", "ci": "success"}),
    ticket("T3", "review", pr={"head": "ccc", "ci": "failure"}),
    ticket("T4", "review", pr={"head": "ddd", "ci": "pending"}),
    ticket("T5", "review", verdict={"current": False, "verdict": "block"}, pr={"head": "eee", "ci": "success"}),
    ticket("T6", "doing"),
    ticket("T7", "changes", holder="someone-else")]}
reset({"shop-build-m1": row("builder")})
routes = {"GET /api/crew/projects": (200, PROJECTS), "GET /api/projects/uc_1/tickets": (200, TICKETS)}
with FakeTares(routes):
    c = client()
    s = IW.scan_builder(c, paths, "shop-build-m1")
    c.close()
ck("changes, a current verdict and failed CI are pending", s["pending"] == [
    {"key": "bd:T1@aaa||success", "text": "T1 (changes asked for)"},
    {"key": "bd:T2@bbb|pass|success", "text": "T2 (verdict pass)"},
    {"key": "bd:T3@ccc||failure", "text": "T3 (CI failed)"}], str(s))
ck("...a ticket waiting on review is live but not pending; the station's own tickets only", s["live"] is True)
reset({"shop-build-m1": row("builder", project="nowhere")})
with FakeTares(routes) as t:
    c = client()
    s = IW.scan_builder(c, paths, "shop-build-m1")
    c.close()
ck("a project Tares does not know: nothing pending, not live", s == {"pending": [], "live": False}, str(s))
ck("...and its tickets are never asked for", not any("tickets" in r for r in t.requests), str(t.requests))
reset({"shop-build-m1": row("builder")})
with FakeTares({"GET /api/crew/projects": (200, PROJECTS)}):
    c = client()
    try:
        IW.scan_builder(c, paths, "shop-build-m1")
        failed = False
    except IW.ScanError:
        failed = True
    c.close()
ck("tickets Tares cannot give fail the scan", failed)
reset({"shop-build-m1": row("builder")})
with FakeTares({"GET /api/crew/projects": (200, {"projects": [{"id": "uc_1", "name": "shop"}]}),
                "GET /api/projects/uc_1/tickets": (200, {"tickets": [ticket("T9", "review")]})}):
    c = client()
    s = IW.scan_builder(c, paths, "shop-build-m1")
    c.close()
ck("a held ticket in review with nothing to act on: live, nothing pending", s == {"pending": [], "live": True}, str(s))

# ------------------------------------------------------------------------------------------------
print("the releaser's scan")
REPORT = """# report

## Progress
RELEASE in progress here is not it

## Undelivered
- [TF:RELEASE] ship shop 1.2 to staging
not a match: RELEASED already
   RELEASE ship the api
Prerelease notes
## Later
RELEASE after the section
"""
reset({"crew-releaser": row("releaser")}, report=REPORT)
c = client()
s = IW.scan_releaser(c, paths, "crew-releaser")
c.close()
ck("RELEASE lines under Undelivered, stripped, as whole words", [p["text"] for p in s["pending"]] == [
    "- [TF:RELEASE] ship shop 1.2 to staging", "RELEASE ship the api"], str(s))
ck("...keyed by the checksum of the stripped line",
   s["pending"][0]["key"] == "rl:" + str(cksum(b"- [TF:RELEASE] ship shop 1.2 to staging")) and s["live"] is True, str(s))
reset({"crew-releaser": row("releaser")})
c = client()
ck("no report: nothing pending, live", IW.scan_releaser(c, paths, "crew-releaser") == {"pending": [], "live": True})
c.close()

# ------------------------------------------------------------------------------------------------
print("the orchestrator's scan")
DESK = {"desk": [
    {"id": "d1", "label": "D1", "command": None, "closed_at": "2026-05-01T10:00:00Z"},
    {"id": "d2", "label": None, "command": None, "closed_at": "2026-05-01T12:00:00.500Z"},
    {"id": "d3", "label": "D3", "command": "run", "closed_at": "2026-05-01T13:00:00Z"},
    {"id": "d4", "label": "D4", "command": None, "closed_at": None}]}
OP = {"projects": [{"id": "p1", "name": "Shop"}, {"id": "p2", "name": None}]}
reset({"crew-orchestrator": row("orchestrator")})
routes = {"GET /api/desk?state=answered&mine=1": (200, DESK), "GET /api/crew/projects": (200, OP)}
with FakeTares(routes):
    c = client()
    s = IW.scan_orchestrator(c, paths, "crew-orchestrator")
    c.close()
ck("the first look only sets the baseline", s == {"first": True, "pending": [], "live": True,
   "baseline": {"desk": "2026-05-01T12:00:00.500Z", "projects": ["p1", "p2"]}}, str(s))
state.wake_update("crew-orchestrator", {"baseline": {"desk": "2026-05-01T10:00:00Z", "projects": ["p1"]}}, paths)
with FakeTares(routes):
    c = client()
    s = IW.scan_orchestrator(c, paths, "crew-orchestrator")
    c.close()
ck("then a newer answer and a new project are pending", s["pending"] == [
    {"key": "dk:d2", "text": "desk answer d2 is closed"}, {"key": "ph:p2", "text": "project p2 was handed over"}], str(s))
ck("...and the baseline moves to the newest answer and every project",
   s["baseline"] == {"desk": "2026-05-01T12:00:00.500Z", "projects": ["p1", "p2"]} and s["live"] is True, str(s))
world["updated"] = None
claude.station_updated = lambda *a, **k: 1777629600 + 3 * 3600   # 2026-05-01T13:00:00Z: after the answer closed
with FakeTares(routes):
    c = client()
    s = IW.scan_orchestrator(c, paths, "crew-orchestrator")
    c.close()
ck("an answer the orchestrator itself recorded after it closed is not news", [p["key"] for p in s["pending"]] == ["ph:p2"], str(s))
claude.station_updated = lambda *a, **k: 1777629600 + 3600   # 2026-05-01T11:00:00Z: before it closed
with FakeTares(routes):
    c = client()
    s = IW.scan_orchestrator(c, paths, "crew-orchestrator")
    c.close()
ck("an answer closed after its last activity is", [p["key"] for p in s["pending"]] == ["dk:d2", "ph:p2"], str(s))
with FakeTares({"GET /api/desk?state=answered&mine=1": (200, DESK)}):
    c = client()
    try:
        IW.scan_orchestrator(c, paths, "crew-orchestrator")
        failed = False
    except IW.ScanError:
        failed = True
    c.close()
ck("projects Tares cannot give fail the scan", failed)
ck("a closed_at with a positive offset is read in UTC; a negative one is not read, as in bash (its pattern takes the minus into the time)",
   IW._secs("2026-05-01T12:00:00+02:00") == IW._secs("2026-05-01T10:00:00Z") and IW._secs("2026-05-01T12:00:00-01:30") is None
   and IW._secs("soon") is None)

# ------------------------------------------------------------------------------------------------
print("the plan")
# pace_level itself, on a remembered reading (pace.current gives it back while it is young)
from tares.crew import pace as PACE
real_pace_level = IW.pace_level
for state_, level, want in (("active", 2, 2), ("active", 1, 1), ("active", 0, 0), ("off", 2, 0), ("unknown", 2, 0)):
    PACE.forget()
    PACE.remember(paths, PACE.Reading(state_, level))
    ck(f"pace_level: a {state_} reading at level {level} is {want}", real_pace_level(paths) == want)
PACE.forget()

pace = {"level": 0}
IW.pace_level = lambda paths_=None, env=None: pace["level"]


def planned(role="reviewer", on=True, routes=None, **kw):
    name = f"crew-{role}"
    r = {"GET /api/crew/settings": settings(on), "GET /api/review-queue?mine=1": (200, QUEUE)}
    r.update(routes or {})
    with FakeTares(r):
        c = client()
        try:
            return IW.plan(c, paths, name, claude_bin="x", now=NOW)
        finally:
            c.close()


reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 60}})
p = planned()
ck("new work wakes a quiet reviewer at once", p and p["reason"] == "work: Shop T1 at abcdef1; p2 T3 at 2222222" and p["work"] and p["n"] == 0
   and p["prompt"] == "There is work in the review queue: Shop T1 at abcdef1; p2 T3 at 2222222 have no current verdict. Work the queue. "
   + launch.idle_wake_note() + " Carry on from your report.", str(p))
ck("...and tells send the keys and the setting", p["keys"] == ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"] and p["state"] == "on", str(p))
ck("the plan writes the clock and the pruned seen list", wake("crew-reviewer")["clock"] == NOW - 60 and wake("crew-reviewer")["seen"] == [])
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 60,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333", "rv:gone"]}})
p = planned()
ck("work already told is not told again, and not yet due: no wake", p is None, str(p))
ck("...and what is no longer pending leaves the seen list", wake("crew-reviewer")["seen"] == ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"], str(wake("crew-reviewer")))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 899,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
ck("14 minutes 59 seconds of quiet is not due", planned() is None)
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 900,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
p = planned()
ck("15 minutes of quiet is a liveness wake for a reviewer", p and p["reason"] == "liveness: wake 1 with nothing pending" and p["n"] == 1
   and p["prompt"] == "Liveness check, nothing is known to be pending: check your queue once, then end the turn. "
   + launch.idle_wake_note() + " Carry on from your report." and not p["work"], str(p))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 1799, "n": 1,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
ck("after one liveness wake the next waits 30 minutes", planned() is None)
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 1800, "n": 1,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
p = planned()
ck("...and then it is wake 2", p and p["reason"] == "liveness: wake 2 with nothing pending" and p["n"] == 2, str(p))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 2000, "n": 2,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
ck("later wakes wait 45 minutes", planned() is None)
pace["level"] = 1
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 2000,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
ck("while the crew is paced the first wake also waits 45 minutes", planned() is None)
pace["level"] = 0
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 1000, "n": 3,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
world["updated"] = NOW - 100
p = planned()
ck("a session that did something since the last wake starts the clock over", p is None
   and wake("crew-reviewer")["n"] == 0 and wake("crew-reviewer")["clock"] == NOW - 100, str(wake("crew-reviewer")))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "clock": NOW - 2000, "n": 2, "last_wake": NOW - 900,
      "seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
world["updated"] = NOW - 900 + 600
planned()
ck("...but not for activity within the settle time of the wake itself", wake("crew-reviewer")["n"] == 2, str(wake("crew-reviewer")))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"seen": ["rv:p1/T1@abcdef1234567", "rv:p2/T3@2222222333"]}})
p = planned()
ck("switched on under a station that still holds its tick: it is told to drop it", p and p["reason"].startswith("release: ")
   and p["prompt"] == "idle_wake was switched on. " + launch.idle_wake_note() + " Carry on from your report." and p["n"] == 0, str(p))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
ck("with no clock the quiet time starts now: not due", planned(routes={"GET /api/review-queue?mine=1": (200, {})}) is None)
ck("...and the clock is written", wake("crew-reviewer")["clock"] == NOW, str(wake("crew-reviewer")))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
ck("a scan that fails is no wake", planned(routes={"GET /api/review-queue?mine=1": (500, "x")}) is None)
ck("...and writes nothing", wake("crew-reviewer") == {"released": True}, str(wake("crew-reviewer")))

print("...off, and the guards")
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
p = planned(on=False)
ck("off after being on: told once to arm its tick again", p and p["reason"] == "rearm" and p["state"] == "off"
   and p["prompt"] == "idle_wake is off again: re-arm your tick as your manual says.", str(p))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": False}})
ck("off and never released: nothing", planned(on=False) is None)
reset({"crew-reviewer": row("reviewer")})
ck("off with no wake file: nothing, and no file is written", planned(on=False) is None and wake("crew-reviewer") is None)
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
ck("settings Tares cannot give change nothing", planned(routes={"GET /api/crew/settings": (500, "x")}) is None)
for label, set_up in (("a halted crew", lambda: (CREW / "HALT").write_text("{}")),
                      ("a station someone is attached to", lambda: world.update(attached=True)),
                      ("a station mid-turn", lambda: world.update(quiet=False))):
    reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
    set_up()
    ck(f"{label} is not woken", planned() is None)
reset({"crew-reviewer": row("reviewer", sessionId="")}, {"crew-reviewer": {"released": True}})
ck("a station with no conversation to resume is not woken", planned() is None)
reset({"crew-helper": row("helper")})
ck("a helper is not woken", IW.plan(client(), paths, "crew-helper", claude_bin="x", now=NOW) is None)

print("...a builder")
bwake = {"released": True, "clock": NOW - 60}
reset({"shop-build-m1": row("builder")}, {"shop-build-m1": bwake})
r = {"GET /api/crew/projects": (200, PROJECTS), "GET /api/projects/uc_1/tickets": (200, TICKETS)}
with FakeTares({"GET /api/crew/settings": settings(), **r}):
    c = client()
    p = IW.plan(c, paths, "shop-build-m1", claude_bin="x", now=NOW)
    c.close()
ck("a builder is woken for its tickets", p and p["reason"] == "work: T1 (changes asked for); T2 (verdict pass); T3 (CI failed)" and p["prompt"].startswith("Your tickets changed: T1"), str(p))
pace["level"] = 2
reset({"shop-build-m1": row("builder")}, {"shop-build-m1": bwake})
with FakeTares({"GET /api/crew/settings": settings(), **r}):
    c = client()
    p = IW.plan(c, paths, "shop-build-m1", claude_bin="x", now=NOW)
    c.close()
ck("...but not while told to hold (pace level 2)", p is None, str(p))
ck("...and it is still not marked as told", wake("shop-build-m1")["seen"] == [])
pace["level"] = 0
reset({"shop-build-m1": row("builder", project="nowhere")}, {"shop-build-m1": {"clock": NOW - 60}})
with FakeTares({"GET /api/crew/settings": settings(), **r}):
    c = client()
    p = IW.plan(c, paths, "shop-build-m1", claude_bin="x", now=NOW)
    c.close()
ck("a builder with no tickets holds no tick: it counts as released and never gets a liveness wake",
   p is None and wake("shop-build-m1")["released"] is True and wake("shop-build-m1")["clock"] == NOW, str(wake("shop-build-m1")))

# ------------------------------------------------------------------------------------------------
print("the send")
restarted: list = []
real_restart = lifecycle.restart


def fake_restart(cl, pth, args, *, claude_bin):
    restarted.append((args, cl.crew_settings().get("idle_wake")))
    if world.get("restart_fails"):
        raise CrewError("boom")
    if world.get("restart_disk"):
        raise OSError("disk full")


lifecycle.restart = fake_restart
PLAN = {"station": "crew-reviewer", "state": "on", "now": NOW, "reason": "work: Shop T1 at abcdef1", "prompt": "PROMPT",
        "keys": ["k1"], "baseline": None, "n": 0, "work": True}


def sent(plan, on=True, **kw):
    del restarted[:]
    out, err = io.StringIO(), io.StringIO()
    with FakeTares({"GET /api/crew/settings": settings(on)}), contextlib.redirect_stderr(err):
        c = client()
        try:
            IW.send(c, paths, plan, claude_bin="x")
        finally:
            c.close()
    return err.getvalue()


reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True, "seen": ["old"], "baseline": {"b": 1}}})
sent(PLAN)
ck("the station is restarted with the prompt, on settings read afresh", restarted == [(["crew-reviewer", "PROMPT"], True)], str(restarted))
ck("it says what it does, with the reason's first word", say_lines == ["waking crew-reviewer (work); restarting it in the same conversation"], str(say_lines))
ck("the event names the station and the reason", [e for e in map(json.loads, (CREW / "events.jsonl").read_text().splitlines())][-1]
   == {**json.loads((CREW / "events.jsonl").read_text().splitlines()[-1]), "event": "idle_wake", "station": "crew-reviewer", "detail": "work: Shop T1 at abcdef1"})
ck("the wake file records the wake, and what it told", wake("crew-reviewer") == {"seen": ["k1"], "n": 0, "last_wake": NOW, "clock": NOW,
   "baseline": {"b": 1}, "released": True}, str(wake("crew-reviewer")))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": False, "seen": ["old"], "baseline": {"b": 1}}})
sent({**PLAN, "work": False, "n": 2, "baseline": {"b": 2}, "reason": "liveness: wake 2 with nothing pending"})
ck("a wake that is not about work leaves seen alone; a baseline moves", wake("crew-reviewer")["seen"] == ["old"]
   and wake("crew-reviewer")["n"] == 2 and wake("crew-reviewer")["baseline"] == {"b": 2} and wake("crew-reviewer")["released"] is True, str(wake("crew-reviewer")))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
sent({**PLAN, "state": "off", "reason": "rearm"}, on=False)
ck("a rearm wake sets released false", wake("crew-reviewer")["released"] is False and restarted, str(wake("crew-reviewer")))
for label, change in (("the setting changed since the round began", lambda: None), ("someone attached", lambda: world.update(attached=True)),
                      ("the station started a turn", lambda: world.update(quiet=False)), ("the crew was halted", lambda: (CREW / "HALT").write_text("{}"))):
    reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
    change()
    err = sent(PLAN, on=(label != "the setting changed since the round began") or False)
    ck(f"nothing is sent when {label}", not restarted and wake("crew-reviewer").get("last_wake") is None and say_lines == [], str(restarted))
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
world["restart_fails"] = True
err = sent(PLAN)
ck("a restart that fails is said and tried again next round", "boom" in err and say_lines[-1] == "could not wake crew-reviewer; will try again next round"
   and wake("crew-reviewer").get("last_wake") is None, f"{err} {say_lines}")
ck("...and is no event", not (CREW / "events.jsonl").is_file() or "idle_wake" not in (CREW / "events.jsonl").read_text())
world["restart_fails"] = False
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
world["restart_disk"] = True
err = sent(PLAN)
ck("a restart that hits the disk is said the same way, and no wake is recorded", "disk full" in err
   and say_lines[-1] == "could not wake crew-reviewer; will try again next round" and wake("crew-reviewer").get("last_wake") is None, f"{err} {say_lines}")
world["restart_disk"] = False
reset({"crew-reviewer": row("reviewer")}, {"crew-reviewer": {"released": True}})
del restarted[:]
with FakeTares({"GET /api/crew/settings": (500, "x")}), contextlib.redirect_stderr(io.StringIO()):
    c = client()
    IW.send(c, paths, PLAN, claude_bin="x")
    c.close()
ck("settings Tares cannot give at send time: no wake", not restarted and wake("crew-reviewer").get("last_wake") is None, str(restarted))
lifecycle.restart = real_restart

# ------------------------------------------------------------------------------------------------
print("the launch note")
reset({"crew-reviewer": row("reviewer"), "shop-build-m1": row("builder", id="a2"), "crew-reviewer-h": row("helper")})
for label, on, st in (("on", True, "on"), ("off", False, "off")):
    with FakeTares({"GET /api/crew/settings": settings(on)}):
        c = client()
        ck(f"idle_wake_state is {st} when the setting is {label}", launch.idle_wake_state(c) == st)
        c.close()
with FakeTares({"GET /api/crew/settings": (500, "x")}):
    c = client()
    ck("idle_wake_state is unknown when Tares gives no settings", launch.idle_wake_state(c) == "unknown")
    launch.iw_launch_note(c, "crew-reviewer")
    c.close()
ck("...and a start then writes no wake file", wake("crew-reviewer") is None)
os.environ["FACTORY_NOW"] = str(NOW)
with FakeTares({"GET /api/crew/settings": settings(True)}):
    c = client()
    launch.iw_launch_note(c, "crew-reviewer")
    launch.iw_launch_note(c, "crew-reviewer-h")
    c.close()
ck("a start with idle_wake on is a start with its tick released", wake("crew-reviewer") == {**state.WAKE_DEFAULT, "released": True, "n": 0, "clock": NOW}, str(wake("crew-reviewer")))
ck("...but a helper has no wake file", wake("crew-reviewer-h") is None)
state.wake_update("crew-reviewer", {"n": 3, "seen": ["k"]}, paths)
with FakeTares({"GET /api/crew/settings": settings(True)}):
    c = client()
    launch.iw_launch_note(c, "crew-reviewer")
    c.close()
ck("...which starts the count over", wake("crew-reviewer")["n"] == 0 and wake("crew-reviewer")["seen"] == ["k"])
with FakeTares({"GET /api/crew/settings": settings(False)}):
    c = client()
    launch.iw_launch_note(c, "crew-reviewer")
    launch.iw_launch_note(c, "shop-build-m1")
    c.close()
ck("with it off the tick is back, for a station that has a wake file", wake("crew-reviewer")["released"] is False and wake("shop-build-m1") is None)
del os.environ["FACTORY_NOW"]

# ------------------------------------------------------------------------------------------------
print("the step")
woken: list = []


def fake_plan(c, pth, s, **kw):
    woken.append(("plan", s))
    return {"station": s} if s in world.get("wake", ()) else None


def fake_send(c, pth, p, **kw):
    woken.append(("send", p["station"]))


real_plan, real_send = IW.plan, IW.send
IW.plan, IW.send = fake_plan, fake_send
reset({"crew-reviewer": row("reviewer"), "crew-orchestrator": row("orchestrator", id="a2"), "shop-build-m1": row("builder", id="a3"),
       "shop-build-m2": row("builder", id="a4"), "crew-reviewer-x": row("helper", id="a5"), "old-build": row("builder", status="retired"),
       "shop-build-m3": row("builder", id="a6")})
(CREW / "stations" / "shop-build-m2.done").write_text("", encoding="utf-8")
claude.is_alive = lambda s, *a, **k: s != "shop-build-m3"
world["wake"] = ("crew-reviewer", "shop-build-m1")
IW.step(paths, env=dict(os.environ), claude_bin="x")
ck("every active station of a crew role that is up is looked at; a helper, a finished builder, a retired one and a stopped one are not",
   [x for x in woken if x[0] == "plan"] == [("plan", "crew-reviewer"), ("plan", "crew-orchestrator"), ("plan", "shop-build-m1")], str(woken))
ck("...and only those with a plan are sent", [x for x in woken if x[0] == "send"] == [("send", "crew-reviewer"), ("send", "shop-build-m1")], str(woken))


def unreadable(*a, **k):
    raise claude.SessionListUnreadableError("cannot read")


claude.is_alive = unreadable
del woken[:]
err = io.StringIO()
with contextlib.redirect_stderr(err):
    IW.step(paths, env=dict(os.environ), claude_bin="x")
ck("a session list that cannot be read skips the station, said", not woken and "skipping crew-reviewer this round" in err.getvalue()
   and "skipping shop-build-m1 this round" in err.getvalue() and "shop-build-m2" not in err.getvalue(), f"{woken} {err.getvalue()}")
# What the revive step found this round, which it hands over as watch_revive.last (a new object every round).
claude.is_alive = lambda s, *a, **k: s != "shop-build-m3"
del woken[:]
watch_revive.last = rnd = watch_revive.Round()
rnd.started.add("crew-reviewer")
rnd.handled.add("crew-reviewer")
IW.step(paths, env=dict(os.environ), claude_bin="x")
ck("a station the revive step handled this round is left alone: not planned, not woken",
   [x for x in woken if x[0] == "plan"] == [("plan", "crew-orchestrator"), ("plan", "shop-build-m1")]
   and [x for x in woken if x[0] == "send"] == [("send", "shop-build-m1")], str(woken))
del woken[:]
watch_revive.last = rnd = watch_revive.Round()
rnd.handled.add("crew-orchestrator")   # skipped, or a stuck restart that failed: handled, not started
IW.step(paths, env=dict(os.environ), claude_bin="x")
ck("...and so is one it skipped or failed to restart, which `started` does not list",
   [x for x in woken if x[0] == "plan"] == [("plan", "crew-reviewer"), ("plan", "shop-build-m1")], str(woken))
del woken[:]
watch_revive.last = rnd = watch_revive.Round()
rnd.unread = True
err = io.StringIO()
with contextlib.redirect_stderr(err):
    IW.step(paths, env=dict(os.environ), claude_bin="x")
ck("a round the revive step could not read the session list for does nothing here, and says nothing more",
   not woken and err.getvalue() == "", f"{woken} {err.getvalue()}")
del woken[:]
watch_revive.last = watch_revive.Round()
IW.step(paths, env=dict(os.environ), claude_bin="x")
ck("the next round (a new Round) looks at every station again", [x for x in woken if x[0] == "send"] == [("send", "crew-reviewer"), ("send", "shop-build-m1")], str(woken))
IW.plan, IW.send = real_plan, real_send
reset({})
IW.step(paths, env={}, claude_bin=None)
ck("a crew with no stations does nothing, and needs no claude", True)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
