"""A console decision reaches the orchestrator within seconds, not at the next round.

The watch's round is minutes long; its nap ticks every few seconds. Each tick asks Tares one small question
(`/api/desk/answered?since=<the orchestrator's baseline>`): was anything answered since? Only a yes reads the desk
and wakes the orchestrator, and only when it is between turns. A Tares without that route keeps the old behaviour:
the round finds the answer.

Everything runs against a small fake Tares and a throwaway crew home, on a clock the test sets: no sleeping, no
real Claude Code. The restart is a recorder. A fresh HOME with no keyring: nothing of the person's is touched.

Run: python tests/test_crew_wake_console.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-wake-console-"))
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
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run

sys.path.insert(0, str(ROOT))

from tares.crew import claude, idle_wake as IW, launch, lifecycle, state, watch  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


paths = CrewPaths.at(CREW)
ORCH = "crew-orchestrator"
BASE = "2026-05-01T10:00:00Z"
ANSWER = "2026-05-01T10:00:07Z"
world: dict = {}
requests: list = []
restarts: list = []   # (clock, args)
clock = [0.0]


class Clock:
    """`watch.time` for the test: monotonic is the test's clock, sleep moves it, the rest is the real module."""

    @staticmethod
    def monotonic():
        return clock[0]

    def __getattr__(self, name):
        return getattr(time, name)


watch.time = Clock()
IW.time = Clock()


def sleep(seconds):
    clock[0] += seconds


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        requests.append(self.path)
        q = urllib.parse.parse_qs(url.query)
        shown = [a for a in world["answers"] if clock[0] >= world["at"].get(a["id"], 0)]
        if url.path == "/health":
            status, doc = 200, {"ok": True}
        elif url.path == "/api/desk/answered" and world["route"]:
            since = (q.get("since") or [""])[0]
            fresh = [a["closed_at"] for a in shown if a["closed_at"] > since]
            status, doc = 200, {"count": len(fresh), "latest": max(fresh, default=since)}
        elif url.path == "/api/desk":
            status, doc = 200, {"desk": shown}
        elif url.path == "/api/crew/projects":
            status, doc = 200, {"projects": [{"id": "p1", "name": "Shop"}]}
        else:
            status, doc = 404, {"detail": "not found"}
        data = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["TARESD_URL"] = f"http://127.0.0.1:{server.server_address[1]}"


def fake_restart(cl, pth, args, *, claude_bin, **_kw):
    if world.get("restart_fails", 0) > 0:
        world["restart_fails"] -= 1
        raise CrewError("the station would not stop")
    restarts.append((clock[0], args))


lifecycle.restart = fake_restart
launch.idle_wake_state = lambda client: world["setting"]


def answer(label="D7", closed_at=ANSWER, at=0.0):
    a = {"id": f"dk_{label}", "label": label, "command": None, "closed_at": closed_at}
    world["answers"].append(a)
    world["at"][a["id"]] = at


def reset(baseline=True, **over):
    shutil.rmtree(CREW, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    (CREW / "stations.json").write_text(json.dumps({ORCH: {
        "role": "orchestrator", "status": "active", "parent": "", "project": "", "repo": "", "cwd": str(SCRATCH),
        "id": "aaaa0001", "sessionId": "sess-1"}}, indent=2) + "\n", encoding="utf-8")
    if baseline:
        state.wake_update(ORCH, {"baseline": {"desk": BASE, "projects": ["p1"]}}, paths)
    world.clear()
    world.update(answers=[], at={}, route=True, setting="on", quiet=True, attached=False, updated=None, alive=True,
                 busy_until=0.0)
    world.update(over)
    claude.between_turns = lambda *a, **k: world["quiet"] and clock[0] >= world["busy_until"]
    claude.attached = lambda *a, **k: world["attached"]
    claude.station_updated = lambda *a, **k: world["updated"]
    claude.is_alive = lambda *a, **k: world["alive"]
    IW._ANSWERED_OFF.clear()
    IW._ANSWERED_DONE.clear()
    IW._ANSWERED_WAIT.clear()
    del requests[:], restarts[:]
    clock[0] = 0.0


def routes_asked(prefix):
    return [r for r in requests if r.split("?")[0] == prefix]


def nap(seconds):
    watch.nap(paths, seconds, 5.0, sleep=sleep)


print("the step is registered with the watch's nap")
ck("the console tick is a nap step", watch.CONSOLE_TICK_NAME in watch._STEPS)

print("an answer in the console wakes the orchestrator within 15 seconds")
reset()
answer(at=7.0)
nap(180)
ck("it is woken once", len(restarts) == 1 and restarts[0][1][0] == ORCH, str(restarts))
ck("within 15 seconds of the answer, not at the round (180 s)", restarts and restarts[0][0] <= 15.0,
   str(restarts))
ck("the wake names the answer", restarts and "D7" in restarts[0][1][1], str(restarts))
ck("the answer is not told twice", len(restarts) == 1)

print("nothing answered: only the small call is made")
reset()
nap(60)
ck("no wake", restarts == [], str(restarts))
ck("each tick asked the small question", len(routes_asked("/api/desk/answered")) == 12,
   str(len(routes_asked("/api/desk/answered"))))
ck("the question carries the baseline", all("since=" in r for r in routes_asked("/api/desk/answered")), str(requests[:3]))
ck("nothing else was read", routes_asked("/api/desk") == [] and routes_asked("/api/crew/projects") == [], str(requests[:5]))

print("an orchestrator in a turn is not woken, and is the moment it is between turns")
reset(busy_until=22.0)
answer(at=3.0)
nap(60)
ck("woken once", len(restarts) == 1, str(restarts))
ck("not while it was in a turn", restarts and restarts[0][0] >= 22.0, str(restarts))
ck("and at the first tick after", restarts and restarts[0][0] <= 27.0, str(restarts))
ck("the desk was not read while it was busy", len(routes_asked("/api/desk")) == 1, str(routes_asked("/api/desk")))

print("an attached orchestrator is left alone")
reset(attached=True)
answer(at=0.0)
nap(30)
ck("no wake, nothing read", restarts == [] and routes_asked("/api/desk") == [], str(restarts))

print("a Tares without the route keeps the round's behaviour")
reset(route=False)
answer(at=0.0)
nap(60)
ck("it asked once and stopped asking", len(routes_asked("/api/desk/answered")) == 1,
   str(len(routes_asked("/api/desk/answered"))))
ck("and woke nobody and read nothing else", restarts == [] and routes_asked("/api/desk") == [], str(restarts))

print("a Tares upgraded later is asked again")
reset(route=False)
answer(at=0.0)
nap(60)
ck("it did not ask again within the wait", len(routes_asked("/api/desk/answered")) == 1,
   str(len(routes_asked("/api/desk/answered"))))
world["route"] = True
nap(IW._OFF_FOR)
ck("it asks again after the wait and wakes the orchestrator", len(restarts) == 1 and restarts[0][0] >= IW._OFF_FOR,
   str(restarts))

print("a wake that did not happen is tried again soon, not forgotten and not every tick")
reset(restart_fails=1)
answer(at=0.0)
nap(20)
ck("the first try failed and nothing was restarted yet", restarts == [], str(restarts))
ck("it did not hammer the restart every tick", len(routes_asked("/api/desk")) == 1, str(routes_asked("/api/desk")))
nap(40)
ck("the retry after the wait woke it once", len(restarts) == 1 and restarts[0][0] >= IW._RETRY_AFTER, str(restarts))
nap(60)
ck("and then it is told no more", len(restarts) == 1, str(restarts))

print("a wake step that raises a CrewError is looked at again soon, not every tick")
reset()
answer(at=0.0)
real_plan = IW.plan
planned = []
def failing_plan(*a, **k):
    planned.append(clock[0])
    raise CrewError("the desk could not be read")
IW.plan = failing_plan
nap(20)
ck("the step failed once and was not repeated every tick", len(planned) == 1, str(planned))
nap(40)
IW.plan = real_plan
ck("it tried again after the wait, not before", len(planned) == 2 and planned[1] >= IW._RETRY_AFTER, str(planned))
nap(60)
ck("and a step that works then wakes the orchestrator once", len(restarts) == 1, str(restarts))

print("an answer already acted on is not asked about again")
reset(updated=IW._secs(ANSWER) + 60)   # the orchestrator recorded the answer itself: nothing to tell it
answer(at=0.0)
nap(60)
asked = routes_asked("/api/desk/answered")
ck("the first question carries the baseline", asked and f"since={BASE}" in asked[0].replace("%3A", ":"), str(asked[:2]))
ck("every later question carries the answer already seen",
   len(asked) > 2 and all(f"since={ANSWER}" in r.replace("%3A", ":") for r in asked[2:]), str(asked[:4]))

print("the first look is the round's: no baseline, no call")
reset(baseline=False)
answer(at=0.0)
nap(30)
ck("nothing asked, nobody woken", routes_asked("/api/desk/answered") == [] and routes_asked("/api/desk") == []
   and restarts == [], str(requests))

print("an answer the orchestrator recorded itself is not news, and is not read again every tick")
reset(updated=IW._secs(ANSWER) + 60)   # the orchestrator was last active after the answer closed
answer(at=0.0)
nap(60)
ck("no wake", restarts == [], str(restarts))
ck("the desk was read once, not every tick", len(routes_asked("/api/desk")) == 1, str(routes_asked("/api/desk")))

print("with idle_wake off the console tick wakes nobody")
reset(setting="off")
answer(at=0.0)
nap(30)
ck("no wake", restarts == [], str(restarts))

print("a halted crew")
reset()
answer(at=0.0)
state_halted = state.halted
state.halted = lambda *a, **k: True
nap(30)
state.halted = state_halted
ck("a halted crew is not woken", restarts == [], str(restarts))

server.shutdown()
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
