"""The watch wakes the orchestrator for a stalled builder, from what /api/crew/my-work shows in `in_progress`: a
ticket with no news for 45 minutes wakes it once per stall, and a ticket in changes whose holder is not running wakes
it once per head. A ticket row without `updated_at` (a Tares before that field) gives no stall signal.

Runs the orchestrator's scan, plan and send against a small fake Tares and a throwaway crew home, with Claude Code's
view of the station and the restart replaced by values the test sets. HOME is fresh and there is no keyring.

Run: python tests/test_crew_wake_stall.py
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

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-wake-stall-"))
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
    (CREW / "stations.json").write_text(json.dumps({ORCH: row}, indent=2) + "\n", encoding="utf-8")
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


def fake_restart(cl, pth, args, *, claude_bin):
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


def held(stage="doing", at=None, holder="build-1", head="", project="Shop", ticket="T2", field=True):
    row = {"project": project, "ticket": ticket, "title": "t", "holder": holder, "stage": stage, "head": head}
    if field:
        row["updated_at"] = at
    return row


def doc(*rows):
    return {**EMPTY, "in_progress": list(rows)}


STALE = iso(NOW - 46 * 60)
KEY = f"st:Shop/T2@{STALE}"

print("the first look only sets the baseline")
reset({"released": True, "clock": NOW - 60})
p = round_(doc(held(at=STALE)))
ck("a ticket already stalled does not wake it", p is None and not restarted, str(p))
ck("...and is remembered in the baseline", wake()["baseline"]["work"] == [KEY], str(wake()))

print("a stall wakes once")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
p = round_(doc(held(at=STALE)))
text = "ticket T2 of Shop has had no news for 46 minutes"
ck("46 minutes without news wakes the orchestrator", p and p["work"] and p["keys"] == [KEY] and p["reason"] == f"work: {text}"
   and p["prompt"] == IW.work_prompt("orchestrator", text) + " " + launch.idle_wake_note() + " Carry on from your report.", str(p))
ck("...with one restart", len(restarted) == 1 and restarted[0][0] == ORCH, str(restarted))
ck("...and remembers the key", wake()["seen"] == [KEY] and KEY in wake()["baseline"]["work"], str(wake()))
del restarted[:]
p = round_(doc(held(at=STALE)), NOW + 600)
ck("the same stall does not wake it again, later", p is None and not restarted, str(p))

print("news and a new stall")
at2 = iso(NOW + 600)
p = round_(doc(held(at=at2)), NOW + 700)
ck("news on the ticket is not a stall", p is None and not restarted, str(p))
p = round_(doc(held(at=at2)), NOW + 600 + 46 * 60)
ck("the next stall, after that news, wakes again", p and p["keys"] == [f"st:Shop/T2@{at2}"], str(p))

print("under 45 minutes, and the other stages")
for label, row, now in (
        ("44 minutes is not a stall", held(at=iso(NOW - 44 * 60)), NOW),
        ("45 minutes exactly is not a stall", held(at=iso(NOW - 45 * 60)), NOW),
        ("a ticket in review waits on the reviewer, not the builder", held("review", STALE), NOW),
        ("a blocked ticket is known to be blocked", held("blocked", STALE), NOW),
        ("a stage that is not set is left alone", held("", STALE), NOW)):
    reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
    p = round_(doc(row), now)
    ck(label + ": no wake", p is None and not restarted, str(p))
for stage in ("todo", "changes"):
    reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
    p = round_(doc(held(stage, STALE)))
    ck(f"a ticket in {stage} with no news wakes it", p and p["keys"] == [KEY], str(p))

print("no field, no wake")
for label, row in (("a row without updated_at (a Tares before the field)", held(field=False)),
                   ("an updated_at that is null", held(at=None)),
                   ("an updated_at that is not a time", held(at="yesterday"))):
    reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
    p = round_(doc(row))
    ck(label + " gives no stall", p is None and not restarted, str(p))

print("two projects, the same ticket label")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": [KEY]}})
p = round_(doc(held(at=STALE), held(at=STALE, project="Blog")))
ck("Blog's T2 stalling wakes though Shop's T2 already did", p and p["keys"] == [f"st:Blog/T2@{STALE}"]
   and len(restarted) == 1, str(p))
ck("...naming only the new one", "Blog" in p["reason"] and "of Shop" not in p["reason"], str(p))

print("a ticket in changes whose holder is gone")
GONE = "gone:Shop/T2@abc123"
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
alive.discard("build-1")
p = round_(doc(held("changes", iso(NOW - 60), head="abc123")))
text = "ticket T2 of Shop is in changes and build-1 is not running"
ck("it wakes the orchestrator at once, with no wait for 45 minutes", p and p["work"] and p["keys"] == [GONE]
   and p["reason"] == f"work: {text}", str(p))
ck("...with one restart", len(restarted) == 1, str(restarted))
del restarted[:]
p = round_(doc(held("changes", iso(NOW - 60), head="abc123")), NOW + 60)
ck("the same head does not wake it again", p is None and not restarted, str(p))
p = round_(doc(held("changes", iso(NOW + 100), head="def456")), NOW + 120)
ck("a new head, still with no holder, wakes it again", p and p["keys"] == ["gone:Shop/T2@def456"], str(p))
del restarted[:]

reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
p = round_(doc(held("changes", iso(NOW - 60), head="abc123")))
ck("a holder that is running is not gone", p is None and not restarted, str(p))
alive.discard("build-1")
for stage in ("doing", "review", "todo"):
    p = round_(doc(held(stage, iso(NOW - 60), head="abc123")))
    ck(f"a ticket in {stage} with no running holder is not this signal", p is None and not restarted, str(p))
p = round_(doc(held("changes", iso(NOW - 60), head="")))
ck("a ticket with no head yet is keyed on an empty head", p and p["keys"] == ["gone:Shop/T2@"], str(p))

print("it asks about each holder once")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
asked = []
alive.discard("build-1")
claude.is_alive = lambda station, *a, **k: asked.append(station) or station in alive
p = round_(doc(held("changes", iso(NOW - 60), head="a"), held("changes", iso(NOW - 60), head="b", ticket="T3"),
               held("changes", iso(NOW - 60), head="c", ticket="T4", holder="build-2")))
ck("two tickets of one holder are one question", asked == ["build-1", "build-2"], str(asked))
ck("...and both tickets of the gone holder wake it", p and p["keys"] == ["gone:Shop/T2@a", "gone:Shop/T3@b"], str(p))

print("a session list that cannot be read")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})


def unread(*a, **k):
    raise claude.SessionListUnreadableError("cannot read")


claude.is_alive = unread
p = round_(doc(held("changes", iso(NOW - 60), head="abc123")))
ck("is not taken for a holder that is gone", p is None and not restarted, str(p))
p = round_(doc(held("changes", STALE, head="abc123")), NOW + 60)
ck("...while a stall is still told", p and p["keys"] == [f"st:Shop/T2@{STALE}"], str(p))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
