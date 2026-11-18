"""The wake prompt names the keys that woke the station in one short line, and the orchestrator's prompts and
skill say that a wake, a liveness wake included, runs the whole Tick section.

Runs the orchestrator's plan against a small fake Tares and a throwaway crew home, with Claude Code's view of the
station replaced by values the test sets. HOME is fresh and there is no keyring.

Run: python tests/test_crew_wake_prompt.py
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

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-wake-prompt-"))
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

from tares.crew import claude, idle_wake as IW, launch  # noqa: E402
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
EMPTY = {"ready": [], "bouncing": [], "led_projects": [], "in_progress": [], "desk": []}
BASE = {"desk": "", "projects": ["p1"]}


def reset(wake):
    shutil.rmtree(CREW, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    row = {"role": "orchestrator", "status": "active", "parent": "", "project": "", "repo": "", "cwd": str(SCRATCH),
           "id": "aaaa0001", "sessionId": "sess-1"}
    (CREW / "stations.json").write_text(json.dumps({ORCH: row}, indent=2) + "\n", encoding="utf-8")
    (CREW / "stations" / f"{ORCH}.wake").write_text(json.dumps(wake) + "\n", encoding="utf-8")
    claude.between_turns = lambda *a, **k: True
    claude.attached = lambda *a, **k: False
    claude.station_updated = lambda *a, **k: None
    claude.is_alive = lambda station, *a, **k: False


IW.pace_level = lambda paths_=None, env=None: 0


def planned(mywork):
    routes = {"GET /api/crew/settings": (200, {"settings": {"idle_wake": True}}),
              "GET /api/desk?state=answered&mine=1": (200, {"desk": []}),
              "GET /api/crew/projects": (200, {"projects": [{"id": "p1", "name": "Shop"}]}),
              "GET /api/crew/my-work": (200, mywork)}
    os.environ["FACTORY_NOW"] = str(NOW)
    with FakeTares(routes):
        c = TaresClient(paths, dict(os.environ), lambda *_: None)
        try:
            return IW.plan(c, paths, ORCH, claude_bin="x", now=NOW)
        finally:
            c.close()


NOTE = launch.idle_wake_note()

print("the pieces")
ck("one key", IW.woke_on(["rd:Shop/T5"]) == "Woke on: rd:Shop/T5.")
ck("several keys, in one line", IW.woke_on(["a", "b", "c"]) == "Woke on: a, b, c.")
ck("five keys are all shown", IW.woke_on(list("abcde")) == "Woke on: a, b, c, d, e.")
ck("more than five are counted", IW.woke_on(list("abcdefg")) == "Woke on: a, b, c, d, e and 2 more.")
ck("the orchestrator is told to run the whole tick",
   IW.work_prompt("orchestrator", "X") == "Something is waiting for you: X. Run the whole Tick section of your manual, not only that.")
ck("its liveness prompt says the same",
   IW.liveness_prompt("orchestrator") == "Liveness check, nothing is known to be pending: run the whole Tick section of your manual once, then end the turn.")
ck("a reviewer's liveness prompt stays a queue check",
   IW.liveness_prompt("reviewer") == "Liveness check, nothing is known to be pending: check your queue once, then end the turn.")

print("a work wake")
reset({"released": True, "clock": NOW - 60, "baseline": {**BASE, "work": []}})
p = planned({**EMPTY, "ready": [{"project": "Shop", "ticket": "T5", "title": "x", "repo": ""}]})
ck("the prompt names the key that woke it", p and p["keys"] == ["rd:Shop/T5"] and p["prompt"] == (
    "Something is waiting for you: ticket T5 of Shop is ready to start. Run the whole Tick section of your manual, not only that. "
    "Woke on: rd:Shop/T5. " + NOTE + " Carry on from your report."), str(p))

print("a liveness wake")
reset({"released": True, "clock": NOW - 1300, "baseline": {**BASE, "work": []}})
p = planned(EMPTY)
ck("the orchestrator's liveness prompt runs the whole tick", p and not p["work"] and p["prompt"] == (
    "Liveness check, nothing is known to be pending: run the whole Tick section of your manual once, then end the turn. "
    + NOTE + " Carry on from your report."), str(p))

print("the skill")
skill = (ROOT / "claude-plugin" / "skills" / "station-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
tick = skill[skill.index("## Tick"):skill.index("## Status for the person")]
ck("the Tick section says every wake runs the whole tick, liveness included",
   "With idle_wake on, every wake (a liveness wake too) runs the whole tick" in tick, tick[:400])

print(f"\n{P} passed, {F} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if F else 0)
