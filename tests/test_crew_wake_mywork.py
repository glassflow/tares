"""The watch wakes the orchestrator for what /api/crew/my-work shows: a ticket ready to start, a ticket that came
back from review to a new ladder step, and a milestone ready to check. Each wakes once; a Tares without the route
(404) wakes nothing and leaves the baseline as it was.

Runs the orchestrator's scan, plan and send against a small fake Tares and a throwaway crew home, with Claude Code's
view of the station and the restart replaced by values the test sets. HOME is fresh and there is no keyring.

Run: python tests/test_crew_wake_mywork.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-wake-mywork-"))
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

from tares.crew import claude, idle_wake as IW, launch, lifecycle  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
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
ORCH = "crew-orchestrator"
paths = CrewPaths.at(CREW)
say_lines: list = []
world: dict = {}
restarted: list = []


def client() -> TaresClient:
    return TaresClient(paths, dict(os.environ), say_lines.append)


def reset(wake=None):
    shutil.rmtree(CREW, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    row = {"role": "orchestrator", "status": "active", "parent": "", "project": "", "repo": "", "cwd": str(SCRATCH),
           "id": "aaaa0001", "sessionId": "sess-1"}
    (CREW / "stations.json").write_text(json.dumps({ORCH: row}, indent=2) + "\n", encoding="utf-8")
    if wake is not None:
        (CREW / "stations" / f"{ORCH}.wake").write_text(json.dumps(wake) + "\n", encoding="utf-8")
    del say_lines[:], restarted[:]
    claude.between_turns = lambda *a, **k: True
    claude.attached = lambda *a, **k: False
    claude.station_updated = lambda *a, **k: None
    claude.is_alive = lambda *a, **k: True


def wake():
    f = CREW / "stations" / f"{ORCH}.wake"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def fake_restart(cl, pth, args, *, claude_bin, **_kw):
    restarted.append(args)


lifecycle.restart = fake_restart
IW.pace_level = lambda paths_=None, env=None: 0

DESK = {"desk": []}
PROJECTS = {"projects": [{"id": "p1", "name": "Shop"}]}
BASE = {"desk": "", "projects": ["p1"]}
EMPTY = {"ready": [], "bouncing": [], "led_projects": []}
READY = {"project": "Shop", "ticket": "T5", "title": "x", "repo": ""}
BOUNCE = {"project": "Shop", "ticket": "T3", "title": "y", "holder": "b", "stage": "changes", "count": 3, "ladder": 3, "reason": "r"}
LED = {"project": "Shop", "milestones_ready_to_check": ["M1 Core"]}


def routes(mywork=None):
    r = {"GET /api/crew/settings": (200, {"settings": {"idle_wake": True}}),
         "GET /api/desk?state=answered&mine=1": (200, DESK), "GET /api/crew/projects": (200, PROJECTS)}
    if mywork is not None:
        r["GET /api/crew/my-work"] = (200, mywork)
    return r


def round_(mywork, now=NOW):
    """One watch round for the orchestrator: plan, then send what it chose. Returns the plan."""
    with FakeTares(routes(mywork)):
        c = client()
        try:
            p = IW.plan(c, paths, ORCH, claude_bin="x", now=now)
            if p is not None:
                IW.send(c, paths, p, claude_bin="x")
            return p
        finally:
            c.close()


print("the first look only sets the baseline")
reset({"released": True, "clock": NOW - 60})
p = round_({**EMPTY, "ready": [READY], "bouncing": [BOUNCE], "led_projects": [LED]})
ck("what is already waiting does not wake it", p is None and not restarted, str(p))
ck("...and is remembered in the baseline", wake()["baseline"] == {"desk": "", "projects": ["p1"],
   "work": ["bn:Shop/T3@3", "ms:Shop/M1 Core", "rd:Shop/T5"]}, str(wake()))

for label, doc, key, text in (
        ("a ticket that became ready", {**EMPTY, "ready": [READY]}, "rd:Shop/T5", "ticket T5 of Shop is ready to start"),
        ("a ticket bouncing to a new ladder step", {**EMPTY, "bouncing": [BOUNCE]}, "bn:Shop/T3@3",
         "ticket T3 of Shop bounced again (ladder step 3)"),
        ("a milestone ready to check", {**EMPTY, "led_projects": [LED]}, "ms:Shop/M1 Core",
         "milestone M1 Core of Shop is ready to check")):
    print(label)
    reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
    p = round_(doc)
    ck("it wakes the orchestrator", p and p["work"] and p["keys"] == [key] and p["reason"] == f"work: {text}"
       and p["prompt"] == IW.work_prompt("orchestrator", text) + " " + IW.woke_on(p["keys"]) + " " + launch.idle_wake_note() + " Carry on from your report.", str(p))
    ck("...with one restart", len(restarted) == 1 and restarted[0][0] == ORCH, str(restarted))
    ck("...and remembers the key", wake()["seen"] == [key] and key in wake()["baseline"]["work"], str(wake()))
    del restarted[:]
    p = round_(doc, NOW + 60)
    ck("the same signal does not wake it again", p is None and not restarted, str(p))

print("a new ladder step is a new signal")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": ["bn:Shop/T3@3"]}})
p = round_({**EMPTY, "bouncing": [{**BOUNCE, "ladder": 5, "count": 5}]})
ck("ticket T3 at step 5 wakes though it woke at step 3", p and p["keys"] == ["bn:Shop/T3@5"], str(p))

print("two projects, the same ticket label")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
p = round_({**EMPTY, "bouncing": [BOUNCE]})
ck("project Shop's T3 wakes at step 3", p and p["keys"] == ["bn:Shop/T3@3"], str(p))
p = round_({**EMPTY, "bouncing": [BOUNCE, {**BOUNCE, "project": "Blog"}]}, NOW + 60)
ck("project Blog's T3 at step 3 wakes though Shop's T3 already did", p and p["keys"] == ["bn:Blog/T3@3"], str(p))
p = round_({**EMPTY, "ready": [READY, {**READY, "project": "Blog"}]}, NOW + 120)
ck("...and a ready T5 of another project wakes too", p and p["keys"] == ["rd:Shop/T5", "rd:Blog/T5"], str(p))

print("a baseline from before this signal")
reset({"released": True, "clock": NOW - 60, "baseline": BASE})
p = round_({**EMPTY, "ready": [READY]})
ck("only sets the work baseline", p is None and wake()["baseline"] is not None and not restarted, str(p))
ck("...so the next new ticket wakes", round_({**EMPTY, "ready": [READY, {**READY, "ticket": "T6"}]})["keys"] == ["rd:Shop/T6"])

print("a Tares without the route")
reset({"released": True, "clock": NOW - 60, "baseline": BASE})
p = round_(None)
ck("a 404 on my-work wakes nothing", p is None and not restarted, str(p))
with FakeTares(routes(None)):
    c = client()
    s = IW.scan_orchestrator(c, paths, ORCH)
    c.close()
ck("...and leaves the baseline without a work list", s["pending"] == [] and s["baseline"] == BASE, str(s))
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": ["rd:Shop/T5"]}})
p = round_(None)
ck("a 404 with a work baseline wakes nothing and keeps the work list", p is None and not restarted
   and wake()["baseline"] == {**BASE, "work": ["rd:Shop/T5"]}, str(wake()))
with FakeTares(routes(None)):
    c = client()
    s = IW.scan_orchestrator(c, paths, ORCH)
    c.close()
ck("...in the scan's baseline too", s["pending"] == [] and s["baseline"] == {**BASE, "work": ["rd:Shop/T5"]}, str(s))
reset()
with FakeTares(routes(None)):
    c = client()
    s = IW.scan_orchestrator(c, paths, ORCH)
    c.close()
ck("...on the first look too", s["baseline"] == BASE and s["first"] is True, str(s))

print("any other failure is a failed scan")
reset({"released": True, "clock": NOW - 60, "baseline": BASE})
with FakeTares({**routes(None), "GET /api/crew/my-work": (500, "boom")}):
    c = client()
    try:
        IW.scan_orchestrator(c, paths, ORCH)
        failed = False
    except IW.ScanError:
        failed = True
    c.close()
ck("a 500 on my-work fails the scan, so nothing wakes", failed)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
