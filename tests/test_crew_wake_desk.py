"""The watch wakes the orchestrator to ask an open desk item again, at most once an hour (from the `desk` of
/api/crew/my-work), and once for a project listed under `releasing` in /api/crew/projects (a takeover). Neither
wakes it the moment it appears, and a project taken over again after a release wakes it again.

Runs the orchestrator's scan, plan and send against a small fake Tares and a throwaway crew home, with Claude Code's
view of the station and the restart replaced by values the test sets. HOME is fresh and there is no keyring.

Run: python tests/test_crew_wake_desk.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import time
import threading
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-wake-desk-"))
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
alive: set = set()


def client() -> TaresClient:
    return TaresClient(paths, dict(os.environ), say_lines.append)


def reset(wake=None):
    shutil.rmtree(CREW, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    row = {"role": "orchestrator", "status": "active", "parent": "", "project": "", "repo": "", "cwd": str(SCRATCH),
           "id": "aaaa0001", "sessionId": "sess-1"}
    builders = {b: {**row, "role": "builder", "id": f"bbbb000{i}", "sessionId": f"sess-b{i}"}
                for i, b in enumerate(("build-1", "build-2"), 1)}
    (CREW / "stations.json").write_text(json.dumps({ORCH: row, **builders}, indent=2) + "\n", encoding="utf-8")
    if wake is not None:
        (CREW / "stations" / f"{ORCH}.wake").write_text(json.dumps(wake) + "\n", encoding="utf-8")
    del say_lines[:], restarted[:]
    claude.between_turns = lambda *a, **k: True
    claude.attached = lambda *a, **k: False
    claude.station_updated = lambda *a, **k: None
    alive.clear()
    alive.update({"build-1", "build-2"})
    claude.is_alive = lambda station, *a, **k: station in alive


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
         "GET /api/desk?state=answered&mine=1": (200, DESK), "GET /api/crew/projects": (200, world.get("projects", PROJECTS))}
    if mywork is not None:
        r["GET /api/crew/my-work"] = (200, mywork)
    return r


def round_(mywork, now=NOW):
    """One watch round for the orchestrator: plan, then send what it chose. Returns the plan."""
    os.environ["FACTORY_NOW"] = str(now)
    with FakeTares(routes(mywork)):
        c = client()
        try:
            p = IW.plan(c, paths, ORCH, claude_bin="x", now=now)
            if p is not None:
                IW.send(c, paths, p, claude_bin="x")
            return p
        finally:
            c.close()


def iso(epoch):
    """A server `updated_at`: UTC with microseconds and an offset."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".123456+00:00"


def item(label="D7", created=None, project_name="Shop"):
    return {"label": label, "project_name": project_name, "question": "q", "created_at": created, "blocking": True}


def doc(*items):
    return {**EMPTY, "in_progress": [], "desk": list(items)}


def projects(*releasing):
    world["projects"] = {"projects": PROJECTS["projects"],
                         "releasing": [{"id": r, "name": f"Name {r}", "takeover": {}} for r in releasing]}


def fresh(work=None):
    reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": work or []}})
    world.pop("projects", None)


H = 3600
OPEN_AT = iso(NOW - 3 * H - 60)   # open for three hours and a minute


def key_at(hours):
    return f"dk:D7@{hours}"


print("an open desk item is asked about again once an hour")
fresh()
p = round_(doc(item(created=iso(NOW - 120))))
ck("an item filed two minutes ago does not wake it", p is None and not restarted, str(p))
p = round_(doc(item(created=iso(NOW - H + 60))))
ck("59 minutes open is not an hour", p is None and not restarted, str(p))
fresh()
p = round_(doc(item(created=iso(NOW - H - 30))))
text = "desk item D7 of Shop is still open, ask again"
ck("an hour open wakes the orchestrator", p and p["work"] and p["keys"] == [key_at(1)] and p["reason"] == f"work: {text}"
   and p["prompt"] == IW.work_prompt("orchestrator", text) + " " + launch.idle_wake_note() + " Carry on from your report.",
   str(p))
ck("...with one restart", len(restarted) == 1 and restarted[0][0] == ORCH, str(restarted))
ck("...and remembers the key", wake()["seen"] == [key_at(1)] and key_at(1) in wake()["baseline"]["work"], str(wake()))
del restarted[:]
p = round_(doc(item(created=iso(NOW - H - 30))), NOW + 600)
ck("the same hour does not wake it again", p is None and not restarted, str(p))
p = round_(doc(item(created=iso(NOW - H - 30))), NOW + H)
ck("the next hour wakes it again", p and p["keys"] == [key_at(2)], str(p))
del restarted[:]
p = round_(doc(item(created=iso(NOW - H - 30))), NOW + H + 300)
ck("...and once for that hour", p is None and not restarted, str(p))

print("the first look only sets the baseline")
reset({"released": True, "clock": NOW - 60})
p = round_(doc(item(created=OPEN_AT)))
ck("an item open for hours does not wake it", p is None and not restarted, str(p))
ck("...and is remembered in the baseline", wake()["baseline"]["work"] == [key_at(3)], str(wake()))

print("the items are told apart")
fresh()
p = round_(doc(item("D7", OPEN_AT), item("D8", OPEN_AT, "Blog"), item("D9", iso(NOW - 60))))
ck("each item open for an hour wakes it, a new one does not", p and p["keys"] == [key_at(3), "dk:D8@3"], str(p))
ck("...naming each item and its project", "D7 of Shop" in p["reason"] and "D8 of Blog" in p["reason"] and "D9" not in p["reason"],
   str(p))
fresh()
p = round_(doc(item("D7", OPEN_AT, None)))
ck("an item with no project name is named alone", p and "desk item D7 is still open" in p["reason"], str(p))
fresh()
p = round_(doc(item("", OPEN_AT)))
ck("an item with no label gives no wake", p is None and not restarted, str(p))

print("a created_at that is not a time")
fresh()
p = round_(doc(item(created=None)))
ck("is keyed on the clock hour", p and p["keys"] == [key_at(NOW // H)], str(p))
fresh()
p = round_(doc(item(created="yesterday")))
ck("...also when it is text", p and p["keys"] == [key_at(NOW // H)], str(p))

print("an answered item is forgotten")
fresh([key_at(3)])
p = round_(doc(), NOW)
ck("no open item: no wake", p is None and not restarted, str(p))
ck("...and its key leaves the baseline", wake()["baseline"]["work"] == [], str(wake()))

print("no desk, no field")
fresh()
p = round_({**EMPTY, "in_progress": []})
ck("a my-work without a desk gives no wake", p is None and not restarted, str(p))
fresh()
p = round_({**EMPTY, "in_progress": [], "desk": None})
ck("a null desk gives no wake", p is None and not restarted, str(p))

print("a takeover wakes it once")
fresh()
projects("p1")
p = round_(doc())
text = "project Name p1 was taken over, release it"
ck("a project under releasing wakes the orchestrator", p and p["work"] and p["keys"] == ["rl:p1"] and p["reason"] == f"work: {text}"
   and p["prompt"] == IW.work_prompt("orchestrator", text) + " " + launch.idle_wake_note() + " Carry on from your report.",
   str(p))
ck("...with one restart", len(restarted) == 1 and restarted[0][0] == ORCH, str(restarted))
del restarted[:]
p = round_(doc(), NOW + 600)
ck("the same takeover does not wake it again", p is None and not restarted, str(p))
ck("...and stays in the baseline while it is listed", "rl:p1" in wake()["baseline"]["work"], str(wake()))
projects()
p = round_(doc(), NOW + 700)
ck("released: no wake, and the key is forgotten", p is None and wake()["baseline"]["work"] == [], str(wake()))
projects("p1")
p = round_(doc(), NOW + 800)
ck("taken over again, it wakes it again", p and p["keys"] == ["rl:p1"], str(p))
del restarted[:]

fresh()
projects("p1", "p2")
p = round_(doc())
ck("two projects are two keys, in one wake", p and p["keys"] == ["rl:p1", "rl:p2"] and len(restarted) == 1, str(p))

fresh(["rl:p1"])
projects("p1", "p2")
p = round_(doc())
ck("a project already told is left out; the new one wakes it", p and p["keys"] == ["rl:p2"], str(p))

print("the first look sets the takeover baseline")
reset({"released": True, "clock": NOW - 60})
projects("p1")
p = round_(doc())
ck("a project already under releasing does not wake it", p is None and not restarted, str(p))
ck("...and is remembered in the baseline", wake()["baseline"]["work"] == ["rl:p1"], str(wake()))

print("a project list without releasing")
fresh()
world["projects"] = PROJECTS
p = round_(doc())
ck("an older Tares gives no takeover wake", p is None and not restarted, str(p))

print("an older Tares without my-work")
fresh()
projects("p1")
p = round_(None)
ck("skips both signals", p is None and not restarted, str(p))

print("a desk item and a takeover together")
fresh()
projects("p1")
p = round_(doc(item(created=OPEN_AT)))
ck("wake once, naming both", p and sorted(p["keys"]) == sorted([key_at(3), "rl:p1"]) and len(restarted) == 1, str(p))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
