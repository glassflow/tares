"""The watch's readings step in Python (tares/crew/readings.py, TR-1163).

What the Crew page asks for between rounds: the plan's limits, read every FACTORY_PLAN_EVERY seconds and said once
when they cannot be read, and the asks for a station's /context, answered only for the session this watch's own
station plays. Runs in a throwaway crew home and HOME with a small fake Tares that lists the asks; `plan` and
`context` (the two commands the step calls) are replaced by recorders, so nothing here needs bash, a real Claude
Code or the person's files. The same rounds run on bash and on Python side by side in the parity harness
(`parity.py 'usage_watch_*'`).

Run: .venv/bin/python tests/test_crew_readings.py
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-readings-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
CREW = SCRATCH / "crew"
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1", FACTORY_CREW_HOME=str(CREW),
)
(SCRATCH / "home").mkdir()
CREW.mkdir()

sys.path.insert(0, str(ROOT))

from tares.crew import person, readings, state, watch  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class FakeTares:
    """Lists the asks on GET /api/crew/context/asks; `up` False answers 503."""

    def __init__(self):
        self.asks: list = []
        self.up = True
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/api/crew/context/asks" and outer.up:
                    status, doc = 200, {"asks": outer.asks}
                else:
                    status, doc = (503 if not outer.up else 404), {"detail": "no"}
                data = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["TARESD_URL"] = self.url

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("TARESD_URL", None)


paths = CrewPaths.from_env()
tares = FakeTares()
said: list = []
plan_calls: list = []
context_calls: list = []
plan_code = [0]
context_code = [0]


def fake_plan(argv, env=None):
    plan_calls.append(list(argv))
    return plan_code[0]


def fake_context(argv, env=None):
    context_calls.append(argv[1])
    return context_code[0]


person.cmd_plan = fake_plan
person.cmd_context = fake_context


def fresh():
    readings.MEMORY.plan_read_at = 0.0
    readings.MEMORY.plan_ok = True
    said.clear()
    plan_calls.clear()
    context_calls.clear()
    plan_code[0] = 0
    context_code[0] = 0
    tares.asks = []
    tares.up = True
    shutil.rmtree(CREW, ignore_errors=True)
    CREW.mkdir()
    shutil.rmtree(SCRATCH / "home" / ".claude", ignore_errors=True)


def tick(env=None, now=1000.0):
    readings.step(paths, env={**os.environ, **(env or {})}, say=said.append, now=lambda: now)


def job(job_id, session):
    d = SCRATCH / "home" / ".claude" / "jobs" / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"sessionId": session}), encoding="utf-8")


def station(name, **over):
    state.stations_put(name, {"role": "orchestrator", "id": "", "sessionId": "", **over}, paths)


print("plan_every")
ck("unset is 600", readings.plan_every({}) == 600)
ck("empty is 600", readings.plan_every({"FACTORY_PLAN_EVERY": ""}) == 600)
ck("a number is that many seconds", readings.plan_every({"FACTORY_PLAN_EVERY": "45"}) == 45)
ck("0 is never", readings.plan_every({"FACTORY_PLAN_EVERY": "0"}) == 0)
ck("negative is never", readings.plan_every({"FACTORY_PLAN_EVERY": "-5"}) == 0)
ck("not a number is never", readings.plan_every({"FACTORY_PLAN_EVERY": "soon"}) == 0)

print("the plan")
fresh()
tick(now=1000)
ck("the first tick reads the plan", plan_calls == [["plan"]], plan_calls)
tick(now=1100)
ck("the next tick inside the interval does not", len(plan_calls) == 1, plan_calls)
tick(now=1600)
ck("a tick when the interval has passed does", len(plan_calls) == 2, plan_calls)
ck("and says nothing", said == [], said)

fresh()
env = {"FACTORY_PLAN_EVERY": "0"}
tick(env, now=1000)
tick(env, now=99999)
ck("FACTORY_PLAN_EVERY=0 never reads", plan_calls == [], plan_calls)

fresh()
env = {"FACTORY_PLAN_EVERY": "10"}
plan_code[0] = 1
tick(env, now=1000)
ck("a failed read says so once, with the interval",
   said == [f"could not read the plan's limits ({readings.ME} plan says why); trying again every 10s"], said)
tick(env, now=1020)
tick(env, now=1040)
ck("and tries again but does not repeat itself", len(plan_calls) == 3 and len(said) == 1, (plan_calls, said))
plan_code[0] = 0
tick(env, now=1060)
ck("a read that works is quiet", len(said) == 1, said)
plan_code[0] = 1
tick(env, now=1080)
ck("a failure after a recovery is said again", len(said) == 2, said)

fresh()
env = {"FACTORY_PLAN_EVERY": "10"}


def boom(argv, env=None):
    plan_calls.append(argv)
    raise RuntimeError("no")


person.cmd_plan = boom
tick(env, now=1000)
ck("a plan that raises counts as a failure, not a crash", len(said) == 1, said)
person.cmd_plan = fake_plan

print("the asks")
ZERO = {"FACTORY_PLAN_EVERY": "0"}
fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
job("job-1", "job-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "job-sess"}]
tick(ZERO)
ck("an ask for the session the job plays is answered", context_calls == ["crew-orchestrator"], context_calls)

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
job("job-1", "job-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "roster-sess"}]
tick(ZERO)
ck("an ask for the session the roster keeps is answered too", context_calls == ["crew-orchestrator"], context_calls)

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
job("job-1", "job-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "s-other-crew"}]
tick(ZERO)
ck("an ask for the same name in another crew is left alone", context_calls == [], context_calls)

fresh()
tares.asks = [{"station": "foreign-station", "session": "s-foreign"}]
tick(ZERO)
ck("an ask for a station that is not on the roster is left alone", context_calls == [], context_calls)

fresh()
station("crew-orchestrator", id="", sessionId="roster-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "roster-sess"}]
tick(ZERO)
ck("a station with no job: the roster's session", context_calls == [], context_calls)

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "roster-sess"}]
tick(ZERO)
ck("a job with no state file: the roster's session", context_calls == ["crew-orchestrator"], context_calls)

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
bad = SCRATCH / "home" / ".claude" / "jobs" / "job-1"
bad.mkdir(parents=True)
(bad / "state.json").write_text("{not json", encoding="utf-8")
tares.asks = [{"station": "crew-orchestrator", "session": "roster-sess"}]
tick(ZERO)
ck("a state file that is not JSON: the roster's session", context_calls == ["crew-orchestrator"], context_calls)

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
station("crew-reviewer", id="job-2", sessionId="rev-sess")
job("job-1", "job-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "job-sess"}, {"station": "crew-reviewer", "session": "x"},
              {"station": "crew-reviewer", "session": "rev-sess"}, {"station": "", "session": "job-sess"},
              "junk", {"session": "job-sess"}]
tick(ZERO)
ck("each ask is judged alone; blanks and junk are skipped",
   context_calls == ["crew-orchestrator", "crew-reviewer"], context_calls)

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "roster-sess"}]
tares.up = False
tick(ZERO)
ck("Tares that cannot be read: nothing is read, nothing is said, no crash", context_calls == [] and said == [], (context_calls, said))

fresh()
station("crew-orchestrator", id="job-1", sessionId="roster-sess")
tares.asks = [{"station": "crew-orchestrator", "session": "roster-sess"}]
context_code[0] = 1
tick(ZERO)
ck("a context that cannot be read is said, with the station",
   said == ["could not read the context of crew-orchestrator"], said)

print("the watch runs it as a tick step")
ck("readings is the watch's tick step", watch._STEPS.get(watch.TICK_NAME) is readings.step, watch._STEPS)
ck("and it is not one of the round's steps", watch.TICK_NAME not in watch.STEP_NAMES)

tares.close()
print(f"{P} passed, {F} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if F else 0)
