"""The pace of a crew on Codex (tares/crew/codex_pace.py and the Codex paths of tares/crew/pace.py, readings.py,
views.py and person.py, TR-1597).

Each case starts stations with `launch.launch` in a throwaway crew home, on the fake `codex`
(tests/factory/fake_codex.py), which answers `account/rateLimits/read` and `account/read` from a file the case
rewrites, and ends a turn at the account's limit when told to. The fake Tares keeps what the crew sends
(PUT /api/crew/codex-limits) and answers GET /api/crew/pace?runtime=codex with the cell's own code
(tares/codex_usage.py and the real `pace_now`), on a clock the case moves. There is no `claude` anywhere: PATH
holds the fake codex and `tares`. The real Codex's reading is taken by hand (see the pull request of TR-1597).

 a. the reading: at the top of a round the step asks a station for the account's limits, sends the windows and
    the account's type and plan (no account id, no address), and reads the pace Tares works out from them; a
    crew on Codex never asks for the Claude pace, and a Claude Code crew asks as it always did;
 b. what is not a reading, each kept apart: no station answers (nothing is sent, said once, and the last
    reading ages until it is not used), an account that refuses the question and is not a ChatGPT one (sent as
    an account with no limits: unsupported), a ChatGPT account whose read fails (nothing is sent), a reading
    older than 30 minutes (stale); none of them slows anything;
 c. the wording: stretch, hold, unhold and release are told to a Codex station with no Cron tool named, and so
    are the prompts of a station revived after a reset; the steps and the pace files are the ones of a Claude
    Code crew;
 d. a limit reached in the middle of a turn: the station is left running with its work, marked, and an event
    records the turn and the reset time; no new builder starts; nothing is handed to it while the limit is
    full, nor before its reset time; after the reset it is carried on once, in the same conversation; a second
    round hands it nothing more;
 e. a turn the limit ended with no reset time to be had: not carried on by the watch, said once; and a mark is
    dropped when the station has been carried on by something else, never because it could not be asked;
 f. a station that is down while the limit is full is not revived into it, and is revived in its own
    conversation after the reset, with the Codex wording;
 g. the nap's tick does not look for Claude's plan on a Codex crew; `tares factory plan` prints the Codex
    account's windows; `crew status` says which kind of unknown the pace is.

Unix only: on Windows the test says so and passes.

Run: python tests/test_crew_codex_pace.py
"""
import io
import json
import os
import signal
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

if sys.platform == "win32":
    print("skipped on Windows: a Codex station listens on a Unix socket\n\n0 passed, 0 failed")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

SCRATCH = Path(tempfile.mkdtemp(prefix="tcp-", dir="/tmp"))   # short: a socket's path holds about a hundred bytes
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "FAKE_CODEX", "CLAUDE", "CODEX"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
BIN = SCRATCH / "bin"
BIN.mkdir()
HOME = SCRATCH / "home"
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
                  FACTORY_DOCKER=str(SCRATCH / "no-docker"), TARES_FAKE_STRAYS_DIR=str(SCRATCH),
                  PATH=str(BIN))   # nothing on it but the fake codex and tares: no claude
(HOME / ".codex").mkdir(parents=True)
(HOME / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": "fake"}\n')
(BIN / "codex").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "factory" / "fake_codex.py"}" "$@"\n')
(BIN / "codex").chmod(0o755)
(BIN / "tares").write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -c '
                           '"import sys; sys.argv[0] = \'tares\'; from tares.cli import main; main()" "$@"\n')
(BIN / "tares").chmod(0o755)   # the station's guard is `tares hook codex-guard`, found on its PATH
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import psutil  # noqa: E402

from tares import codex_usage  # noqa: E402
from tares.factory import pace_now  # noqa: E402
from tares.crew import codex_pace, lifecycle, pace, person, proc, readings, state, stations, views, watch_revive  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-codex-pace-0123456789"
MODEL, EFFORT = "gpt-5.6-luna", "low"
CALLS = SCRATCH / "codex-calls.jsonl"
ACCOUNT = SCRATCH / "account.json"
S, B = "crew-reviewer", "shop-build-m1"
CLOCK = [datetime.now(timezone.utc).replace(microsecond=0)]
_n = [0]
STARTED: list = []


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def later(**kw):
    """Move the clock of the crew and of the cell."""
    CLOCK[0] += timedelta(**kw)
    os.environ["FACTORY_NOW"] = str(int(CLOCK[0].timestamp()))


later(seconds=0)


def window(percent, mins, reset_in):
    return {"usedPercent": percent, "windowDurationMins": mins,
            "resetsAt": int((CLOCK[0] + timedelta(seconds=reset_in)).timestamp())}


def account(session=6, weekly=20, *, session_reset=3600, kind="chatgpt", limit_error="", limits=None):
    """What the fake codex answers for the account from now on."""
    doc = {"account": {"type": kind, "planType": "plus", "email": "pat@example.com", "workspaceName": "Pat's"},
           "limits": limits if limits is not None else {
               "accountId": "acct-123",
               "rateLimits": {"limitId": "codex", "primary": window(session, 300, session_reset),
                              "secondary": window(weekly, 10080, 3 * 86400)}}}
    if limit_error:
        doc["limit_error"] = limit_error
    ACCOUNT.write_text(json.dumps(doc))


class Cell:
    """The part of the cell a Codex crew's pace needs, put on a launch test's fake Tares: it keeps the reading
    the crew sends and answers the pace from it with the cell's own code, at the case's clock."""

    def __init__(self, tares, pace_on=True):
        self.s, self.puts, self.gets, self.pace_on, self.refuse = {}, [], [], pace_on, 0
        handler, cell = tares.server.RequestHandlerClass, self
        real_get = handler.do_GET

        def do_put(h):
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
            if urlparse(h.path).path != "/api/crew/codex-limits":
                return h._send(404, {"detail": "no"})
            if cell.refuse:
                return h._send(cell.refuse, {"detail": "refused"})
            cell.puts.append(body)
            kept = codex_usage.save(cell, body, "pat", CLOCK[0])
            return h._send(200, {"windows": len(kept["windows"]), "account": kept["account"]})

        def do_get(h):
            u = urlparse(h.path)
            if u.path != "/api/crew/pace":
                return real_get(h)
            cell.gets.append(h.path)
            if u.query != "runtime=codex":   # the Claude pace: these cases must never ask for it
                return h._send(200, {"on": True, "known": True, "level": 2, "at_limit": True, "state": "limit",
                                     "until": None, "note": "CLAUDE"})
            return h._send(200, json.loads(json.dumps(
                codex_usage.pace(cell, {"pace": cell.pace_on}, CLOCK[0], pace_now), default=str)))

        handler.do_PUT, handler.do_GET = do_put, do_get

    def get_setting(self, k):
        return self.s.get(k)

    def set_setting(self, k, v):
        self.s[k] = v


def crew(world=None, pace_on=True):
    """A fresh crew home and a fake Tares on the Codex runtime; returns (cell, paths, said, make_client, env)."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": FAKE_TOKEN, "stations": {}})
    tares = LS.FakeTares({"runtime": "codex", "projects": [{"id": "p-shop", "name": "shop"}], **(world or {})})
    tares.__enter__()
    cell = Cell(tares, pace_on)
    paths = CrewPaths.at(tmp / "crew")
    said = []
    env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url}
    os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url)
    pace.forget()
    codex_pace.forget()

    def make_client():
        c = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url}, say=said.append)
        c.prime()
        return c
    return cell, paths, said, make_client, env


def fake(**switches):
    for k in [k for k in os.environ if k.startswith("FAKE_CODEX_")]:
        del os.environ[k]
    os.environ.update({f"FAKE_CODEX_{k.upper()}": str(v) for k, v in switches.items()})
    os.environ.update(FAKE_CODEX_LOG=str(CALLS), FAKE_CODEX_DAEMON=str(SCRATCH / "d"), FAKE_CODEX_ACCOUNT=str(ACCOUNT))


def calls(pid=None, method=None) -> list:
    try:
        rows = [json.loads(x) for x in CALLS.read_text().splitlines()]
    except OSError:
        return []
    return [(r["method"], r["params"]) for r in rows if (pid is None or r["pid"] == pid) and (method is None or r["method"] == method)]


def prompts(pid) -> list:
    return ["".join(i.get("text", "") for i in p.get("input") or []) for _, p in calls(pid, "turn/start")]


def quietly(fn, *args, **kw):
    """(error or None, what it printed)."""
    out = io.StringIO()
    try:
        with redirect_stderr(out), redirect_stdout(out):
            fn(*args, **kw)
    except CrewError as e:
        return str(e), out.getvalue()
    return None, out.getvalue()


def start(client, station=S, role="reviewer", project="", prompt="Boot."):
    err, out = quietly(L.launch, client, station, role, "", project, "", str(client.paths.home), MODEL, EFFORT, prompt, "")
    pid = pid_of(client.paths, station)
    if pid and pid not in STARTED:
        STARTED.append(pid)
    return err, out


def row(paths, station=S):
    return state.stations_load(paths).get(station) or {}


def pid_of(paths, station=S):
    return (row(paths, station).get("codex") or {}).get("pid", 0)


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def end(pid, sig=signal.SIGTERM):
    try:
        os.kill(pid, sig)
    except OSError:
        pass
    end_at = time.monotonic() + 5
    while time.monotonic() < end_at and alive(pid):
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        time.sleep(0.05)
    return not alive(pid)


def until(cond, wait=6.0):
    end_at = time.monotonic() + wait
    while time.monotonic() < end_at:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def rested(paths, station=S):
    return until(lambda: cs.turn_state(paths, station) == "done")


def events(paths, name):
    try:
        return [e for e in map(json.loads, paths.events.read_text().splitlines()) if e["event"] == name]
    except OSError:
        return []


def round_(paths, env):
    """One watch round's pace: the top of the round, then the step. What it said."""
    out = io.StringIO()
    with redirect_stderr(out), redirect_stdout(out):
        pace.round_start(paths, env)
        pace.step(paths, env=env)
    return out.getvalue()


# ---------------------------------------------------------------------------------------------
print("a. the reading")
cell, paths, said, mk, env = crew()
fake()
account(86, 20)
err, _ = start(mk())
pid = pid_of(paths)
rested(paths)
turns = len(calls(pid))
got = codex_pace.take(paths)
ck("a station answers for the account's limits: both windows, and the account's type and plan",
   err is None and got and got["station"] == S and got["account"] == {"type": "chatgpt", "planType": "plus"}
   and got["limits"]["rateLimits"]["primary"]["usedPercent"] == 86
   and got["limits"]["rateLimits"]["secondary"]["windowDurationMins"] == 10080, (err, got))
ck("no account id, address or workspace name is in what is taken",
   not any(x in json.dumps(got) for x in ("acct-123", "pat@example.com", "Pat's")), got)
asked = calls(pid)[turns:]
ck("asking costs two questions about the account, and starts no turn and no conversation",
   [m for m, _ in asked] == ["account/read", "account/rateLimits/read"], asked)
out = round_(paths, env)
sent = cell.puts[-1] if cell.puts else {}
ck("at the top of a round the step sends the reading to Tares: limits, account and when it was taken, nothing else",
   len(cell.puts) == 1 and sorted(sent) == ["account", "limits", "taken_at"]
   and "acct-123" not in json.dumps(sent) and "pat@example.com" not in json.dumps(sent), sent)
ck("and reads the pace of a Codex crew, never the Claude one",
   cell.gets and all(p == "/api/crew/pace?runtime=codex" for p in cell.gets), cell.gets)
r = pace.last(paths)
ck("the reading is active, at the level the cell worked out from the windows: slow at 86%",
   r.state == "active" and r.level == 1 and not r.at_limit and r.raw.get("source") == "codex"
   and r.raw.get("session_pct") == 86 and r.raw.get("reading") == "ok", vars(r))
ck("the path a Codex crew asks is its own, and a crew with no station recorded asks the cell's default",
   pace.pace_path(paths) == "/api/crew/pace?runtime=codex"
   and pace.pace_path(CrewPaths.at(SCRATCH / "nowhere" / "crew")) == "/api/crew/pace")
state.stations_put("old-claude", {"role": "reviewer", "status": "active", "runtime": "claude_code", "id": "job1"}, paths)
ck("a station on Claude Code is never asked for the Codex account's limits",
   codex_pace._codex_stations(paths) == [S])
data = state.stations_load(paths)
data.pop("old-claude")
state.write_json(paths.stations, data)
end(pid)

# ---------------------------------------------------------------------------------------------
print("b. what is not a reading")
cell, paths, said, mk, env = crew()
fake()
account(96, 20)
start(mk())
pid = pid_of(paths)
rested(paths)
round_(paths, env)
ck("with a reading at 96% the crew is held", pace.last(paths).level == 2 and len(cell.puts) == 1, vars(pace.last(paths)))
end(pid)
pace.forget()
out = round_(paths, env) + round_(paths, env)
ck("no station answers: nothing is sent, and the watch says so once",
   len(cell.puts) == 1 and out.count("could not read the Codex account's limits (no station answers)") == 1, out)
ck("the last reading still counts while it is young", pace.last(paths).state == "active" and pace.last(paths).level == 2)
later(minutes=31)
round_(paths, env)
r = pace.last(paths)
ck("31 minutes on it is not used: the pace is unknown, which the cell calls stale, and nothing is slowed",
   r.state == "unknown" and r.level == 0 and not r.closed and not r.paced, vars(r))
ck("and a status line says which kind of unknown it is",
   views.pace_status_line(mk()) == "pace: on, the last reading of the Codex account's limits is too old to use",
   views.pace_status_line(mk()))

cell, paths, said, mk, env = crew()
fake()
account(kind="apiKey", limits={"error": "chatgpt authentication required to read rate limits"})
start(mk())
pid = pid_of(paths)
rested(paths)
out = round_(paths, env)
r = pace.last(paths)
ck("an account that is not a ChatGPT one and refuses the question is sent as one with no limits",
   len(cell.puts) == 1 and cell.puts[0]["limits"] == {} and cell.puts[0]["account"]["type"] == "apiKey", cell.puts)
ck("its pace is unknown, and nothing is slowed", r.state == "unknown" and not r.paced and not r.closed, vars(r))
ck("the status line says the account reports no limits",
   views.pace_status_line(mk()) == "pace: on, this Codex account reports no limits", views.pace_status_line(mk()))
ck("with the pace unknown a station is told nothing", pace.plan(mk(), paths, S, r) is None and len(prompts(pid)) == 1, prompts(pid))
account(96, 20)
round_(paths, env)
ck("a reading that works again counts at once", pace.last(paths).level == 2 and len(cell.puts) == 2)
account(kind="chatgpt", limits={"error": "the backend did not answer"})
out = round_(paths, env) + round_(paths, env)
ck("a ChatGPT account whose read fails sends nothing (the last reading is kept), said once",
   len(cell.puts) == 2 and pace.last(paths).level == 2 and out.count("could not read the Codex account's limits") == 1, out)
account(10, 20)
cell.refuse = 500
out = round_(paths, env) + round_(paths, env)
ck("a reading Tares does not take is said once, and the round goes on",
   out.count("could not send the Codex account's limits to Tares") == 1 and pace.last(paths).level == 2, out)
cell.refuse = 0
out = round_(paths, env)
ck("and the next one that is taken counts", pace.last(paths).level == 0 and "could not" not in out, (vars(pace.last(paths)), out))
end(pid)

cell, paths, said, mk, env = crew(pace_on=False)
fake()
account(100, 100)
start(mk())
pid = pid_of(paths)
rested(paths)
round_(paths, env)
ck("with the crew setting pace off a full account changes nothing", pace.last(paths).state == "off"
   and not pace.last(paths).closed and len(prompts(pid)) == 1 and not paths.pace_file.exists())
end(pid)

# ---------------------------------------------------------------------------------------------
print("c. the wording")
cell, paths, said, mk, env = crew()
fake()
account(86, 20)
start(mk())
start(mk(), B, "builder", "shop")
pid, bpid = pid_of(paths), pid_of(paths, B)
rested(paths), rested(paths, B)
out = round_(paths, env)
told = {S: prompts(pid)[-1], B: prompts(bpid)[-1]}
ck("at 86% every station is told to slow down, once, in its own conversation",
   len(prompts(pid)) == 2 and len(prompts(bpid)) == 2 and all(codex_pace.STRETCH_TEXT in t for t in told.values())
   and all("the 5-hour window is at 86%" in t for t in told.values()), (told, out))
ck("the pace files say what each was told, as for a Claude Code crew",
   state.pace_station_read(S, paths) == {"stretched": True, "held": False}
   and state.pace_station_read(B, paths) == {"stretched": True, "held": False})
rested(paths), rested(paths, B)
out = round_(paths, env)
ck("a second round at the same level tells nobody again", len(prompts(pid)) == 2 and len(prompts(bpid)) == 2, out)
account(96, 20)
out = round_(paths, env)
ck("at 96% the builder is told to finish its step and wait; the reviewer is not told again",
   len(prompts(bpid)) == 3 and pace.HOLD_TEXT in prompts(bpid)[-1] and len(prompts(pid)) == 2
   and state.pace_station_read(B, paths)["held"] is True, (prompts(bpid)[-1:], out))
rested(paths, B)
account(86, 20)
out = round_(paths, env)
ck("back at 86% the builder is told it may go on", len(prompts(bpid)) == 4 and prompts(bpid)[-1] == codex_pace.UNHOLD_TEXT, prompts(bpid)[-1:])
rested(paths, B)
account(6, 20)
out = round_(paths, env)
ck("with room again both are released, and the pace files go",
   prompts(pid)[-1] == codex_pace.RELEASE_TEXT and prompts(bpid)[-1] == codex_pace.RELEASE_TEXT
   and not paths.pace_station_file(S).exists() and not paths.pace_station_file(B).exists(), (prompts(pid)[-1:], out))
everything = prompts(pid) + prompts(bpid) + [codex_pace.resume_prompt(mk()), codex_pace.fresh_prompt(mk(), paths, S),
                                              codex_pace.limit_prompt(mk()), pace.resume_prompt(mk()),
                                              pace.fresh_prompt(mk(), paths, S)]
ck("nothing a Codex station is told about the pace names a Cron tool or a Claude plan",
   not any(w in t for t in everything for w in ("Cron", "cron", "the plan's limit", "Claude")), [t for t in everything if "ron" in t])
ck("the prompts of a station revived after a reset are the Codex ones through the pace's own calls",
   pace.resume_prompt(mk()) == codex_pace.resume_prompt(mk()) and pace.Seam(env).resume_prompt(paths).startswith("The account's limit was reset.")
   and "could not be resumed" in pace.Seam(env).fresh_prompt(paths, S))
ck("the Claude Code wording is what it was", "CronCreate" in pace.STRETCH_TEXT)
end(pid), end(bpid)

# ---------------------------------------------------------------------------------------------
print("d. a limit reached in the middle of a turn")
cell, paths, said, mk, env = crew({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True}})
fake()
account(100, 20, session_reset=3600, limit_error="You have hit your usage limit. Try again later.")
err, _ = start(mk(), B, "builder", "shop", prompt="Build the thing.")
pid, thread = pid_of(paths, B), row(paths, B).get("sessionId")
rested(paths, B)
hit = codex_pace.limit_turn(paths, B)
ck("the turn the limit ended is kept as failed, and the station is still running, between turns",
   err is None and hit and hit["message"].startswith("You have hit your usage limit") and alive(pid)
   and proc.at_rest(B, None, paths) and cs.is_alive(paths, B), (err, hit))
out = round_(paths, env)
mark = codex_pace.mark_read(paths, B)
reset_at = int((CLOCK[0] + timedelta(seconds=3600)).timestamp())
ck("the pace step marks the station: the turn, and when the limit resets",
   mark and mark["turn"] == hit["turn"] and mark["until"] == reset_at, mark)
ev = events(paths, "limit")
ck("an event records the turn, the reset time and what Codex said",
   len(ev) == 1 and ev[0]["station"] == B and hit["turn"] in ev[0]["detail"]
   and codex_pace._stamp(reset_at) in ev[0]["detail"] and "usage limit" in ev[0]["detail"], ev)
ck("the watch says so, and that the work is kept", "the account's limit ended a turn of " + B in out and "its work is kept" in out, out)
ck("the station is handed nothing and left running: its work is where the turn left it",
   prompts(pid) == ["Build the thing."] and alive(pid) and row(paths, B).get("sessionId") == thread, prompts(pid))
err, _ = quietly(stations._builder_plan, mk(), paths, ["shop", "m2", "do", "it"])
ck("no new builder starts while the limit is full", "no new builder starts now" in (err or ""), err)
out = round_(paths, env) + round_(paths, env)
ck("further rounds while the limit is full hand it nothing, and record and say nothing more",
   prompts(pid) == ["Build the thing."] and len(events(paths, "limit")) == 1 and "ended a turn" not in out, out)
later(minutes=50)
account(100, 20, session_reset=600, limit_error="You have hit your usage limit. Try again later.")
round_(paths, env)
ck("ten minutes before the reset it still waits", prompts(pid) == ["Build the thing."] and codex_pace.mark_read(paths, B))
later(minutes=11)
account(2, 21)   # the window started over
out = round_(paths, env)
ck("after the reset it is carried on, in the same conversation, with one prompt that says what happened",
   len(prompts(pid)) == 2 and prompts(pid)[-1] == codex_pace.limit_prompt(mk()) and "ended by the account's limit" in prompts(pid)[-1]
   and calls(pid, "turn/start")[-1][1]["threadId"] == thread and pid_of(paths, B) == pid and alive(pid), (prompts(pid), out))
ck("the station's lock is not left held by the watch after the prompt", not paths.lock_dir(B).exists(), paths.lock_dir(B))
ck("the mark is gone and an event says it was resumed",
   codex_pace.mark_read(paths, B) is None and [e["detail"] for e in events(paths, "pace")][-1:] == ["resumed after the limit's reset"])
rested(paths, B)
out = round_(paths, env) + round_(paths, env)
ck("once: later rounds hand it nothing more", len(prompts(pid)) == 2 and len(events(paths, "limit")) == 1, prompts(pid))
err, _ = quietly(stations._builder_plan, mk(), paths, ["shop", "m2", "do", "it"])
ck("and the pace no longer keeps a new builder from starting", "no new builder" not in (err or ""), err)
end(pid)

# a reset time that has passed while the reading is unknown is not enough
cell, paths, said, mk, env = crew({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True}})
fake()
account(100, 20, session_reset=600, limit_error="limit")
start(mk(), B, "builder", "shop", prompt="Build.")
pid = pid_of(paths, B)
rested(paths, B)
round_(paths, env)
later(minutes=45)   # past the reset, and the last reading is now too old
account(kind="chatgpt", limits={"error": "the backend did not answer"})
out = round_(paths, env)
ck("past its reset time but with no reading to go by, it still waits: a missing reading changes nothing",
   pace.last(paths).state == "unknown" and len(prompts(pid)) == 1 and codex_pace.mark_read(paths, B), (vars(pace.last(paths)), out))
account(3, 20)
round_(paths, env)
ck("and is carried on at the first reading that says there is room", len(prompts(pid)) == 2 and not codex_pace.mark_read(paths, B))
end(pid)

# ---------------------------------------------------------------------------------------------
# every automatic prompt checks the hold, not only the pace step's (the review of PR 942)
cell, paths, said, mk, env = crew({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True}})
fake()
account(100, 20, session_reset=3600, limit_error="You have hit your usage limit. Try again later.")
start(mk(), B, "builder", "shop", prompt="Build the thing.")
pid = pid_of(paths, B)
rested(paths, B)
ck("before the pace step has looked, the turn itself holds the station", codex_pace.mark_read(paths, B) is None
   and "ended its turn" in codex_pace.held(paths, B))
for who, how in (("an idle wake", lambda: lifecycle.restart(mk(), paths, [B, "A planned wake: the next ticket."], claude_bin="",
                                                             only_between_turns=True)),
                 ("a grant", lambda: lifecycle.restart(mk(), paths, [B, "New grants."], claude_bin="", only_between_turns=True,
                                                       prompt_wait=0))):
    err, _ = quietly(how)
    ck(f"{who} before the pace step is refused, and nothing reaches the station", "held until the account's limit resets" in (err or "")
       and prompts(pid) == ["Build the thing."] and alive(pid), (err, prompts(pid)))
account(None, None, kind="chatgpt", limits={"error": "the backend did not answer"})   # a reading that is missing
round_(paths, env)
for model_only in (False, True):
    if model_only:   # a limit of one model's own: no account-wide window is full
        account(10, 20)
    err, _ = quietly(lifecycle.restart, mk(), paths, [B, "A planned wake."], claude_bin="", only_between_turns=True)
    ck("with the station marked, a wake is refused " + ("with the account's windows open (a model's own limit)" if model_only
                                                     else "whatever the reading says"),
       "held until the account's limit resets" in (err or "") and prompts(pid) == ["Build the thing."], (err, prompts(pid)))
err, _ = quietly(lifecycle.restart, mk(), paths, [B, "The person's own prompt."], claude_bin="")
ck("a person's own restart is not held: they decide", err is None and prompts(pid)[-1] == "The person's own prompt.", (err, prompts(pid)))
end(pid_of(paths, B))

# Turning pace off releases an earlier limit hold, including the idle-wake path that
# runs before pace.step and would otherwise see the old failed turn forever.
from tares.crew import idle_wake  # noqa: E402

cell, paths, said, mk, env = crew({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True}})
fake()
account(100, 20, session_reset=60, limit_error="usage limit")
start(mk(), B, "builder", "shop", prompt="Build before the limit.")
pid, thread = pid_of(paths, B), row(paths, B)["sessionId"]
rested(paths, B)
round_(paths, env)
ck("a limited station is marked before pacing is disabled", codex_pace.mark_read(paths, B) is not None)
cell.pace_on = False
later(minutes=2)
account(1, 20)
pace.round_start(paths, env)
wake = {"station": B, "state": "on", "now": int(CLOCK[0].timestamp()), "reason": "work: next ticket",
        "prompt": "Continue with the next ticket.", "work": True, "keys": ["ticket:next"], "baseline": None, "n": 0}
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    idle_wake.send(mk(), paths, wake, claude_bin="")
    pace.step(paths, env=env)
ck("turning pacing off lets an idle wake reach the formerly limited station in the same conversation",
   len(prompts(pid)) == 2 and prompts(pid)[-1] == wake["prompt"] and row(paths, B)["sessionId"] == thread,
   (prompts(pid), said))
ck("the wake is acknowledged and the old hold is removed when pacing is off",
   state.wake_read(B, paths)["seen"] == ["ticket:next"] and codex_pace.mark_read(paths, B) is None)
end(pid_of(paths, B))

print("e. no reset time, and a mark that is no longer true")
cell, paths, said, mk, env = crew({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True}})
fake()
account(40, 20, limit_error="You have hit your usage limit for this model.")   # no window the reading has is full
start(mk(), B, "builder", "shop", prompt="Build.")
pid = pid_of(paths, B)
rested(paths, B)
out = round_(paths, env) + round_(paths, env) + round_(paths, env)
mark = codex_pace.mark_read(paths, B)
ck("a turn the limit ended, with no window full in the reading: marked with no reset time",
   mark and mark["until"] is None and "no reading says when it resets" in events(paths, "limit")[0]["detail"], mark)
ck("it is not carried on by the watch, which says so once and names the command that does",
   len(prompts(pid)) == 1 and out.count(f"{B} is not carried on by the watch") == 1
   and f"tares factory restart {B}" in out, out)
account(40, 20)
cs.hand(paths, B, "A person carries it on.")
rested(paths, B)
out = round_(paths, env)
ck("carried on by something else, its mark is dropped and the watch hands it nothing",
   codex_pace.mark_read(paths, B) is None and prompts(pid)[-1] == "A person carries it on." and len(prompts(pid)) == 2, out)


def one_model_full():
    """The account has room, and a bucket of one model is full for the next half hour."""
    return {"accountId": "a", "rateLimits": {"limitId": "codex", "primary": window(40, 300, 3600), "secondary": window(20, 10080, 86400)},
            "rateLimitsByLimitId": {"codex_other": {"limitId": "codex_other", "limitName": "Another model",
                                                   "primary": window(100, 300, 1800)}}}


account(limits=one_model_full(), limit_error="You have hit your usage limit for this model.")
cs.hand(paths, B, "Go on.")
rested(paths, B)
out = round_(paths, env)
mark = codex_pace.mark_read(paths, B)
ck("a limit of one model, which the pace does not know, still gives the reset time of the turn it ended",
   mark and mark["until"] == int((CLOCK[0] + timedelta(seconds=1800)).timestamp()) and pace.last(paths).level == 0
   and not pace.last(paths).closed, (mark, vars(pace.last(paths))))
ck("and the crew is not slowed for it", pace.plan(mk(), paths, B, pace.last(paths)) is None and len(prompts(pid)) == 3)
real_turn = codex_pace._newest_turn


def no_answer(*a, **kw):
    raise codex_pace._Unasked("the station does not answer")


codex_pace._newest_turn = no_answer
out = io.StringIO()
with redirect_stderr(out), redirect_stdout(out):
    held = codex_pace.limit_step(mk(), paths, B, pace.last(paths))
    asked = codex_pace.limit_turn(paths, B)
codex_pace._newest_turn = real_turn
ck("a station whose turns cannot be listed keeps its mark and is handed nothing: silence is not an answer",
   held is True and asked is None and codex_pace.mark_read(paths, B) == mark and len(prompts(pid)) == 3
   and codex_pace.limit_turn(paths, B) is not None, (held, asked))
account(kind="apiKey", limits={"error": "chatgpt authentication required to read rate limits"},
        limit_error="Rate limit reached for this key.")
later(minutes=31)   # the reading of a moment ago is too old by now, and this account gives none
cs.hand(paths, B, "Once more.")
rested(paths, B)
n = len(events(paths, "limit"))
out = round_(paths, env) + round_(paths, env)
mark = codex_pace.mark_read(paths, B)
ck("with no reading at all (an API key account), a turn the limit ended is still recorded, once, and the station held",
   pace.last(paths).state == "unknown" and len(events(paths, "limit")) == n + 1 and mark and mark["until"] is None
   and "no reading says when it resets" in events(paths, "limit")[-1]["detail"]
   and prompts(pid)[-1] == "Once more." and out.count("ended a turn of") == 1, (mark, out))
state.halt_write(paths=paths)
account(limits=one_model_full(), limit_error="You have hit your usage limit for this model.")
round_(paths, env)   # a reading again: the limit of that model, which resets in half an hour
later(minutes=31)
account(40, 20)
out = round_(paths, env)
ck("a halted crew carries nobody on", state.halted(paths) and prompts(pid)[-1] == "Once more."
   and (codex_pace.mark_read(paths, B) or {}).get("until"), (codex_pace.mark_read(paths, B), out))
end(pid)

# ---------------------------------------------------------------------------------------------
print("f. a station that is down while the limit is full")
cell, paths, said, mk, env = crew({"settings": {**LS.DEFAULT_WORLD["settings"], "idle_wake": True}})
fake()
account(100, 20, session_reset=3600)
start(mk())
pid, thread = pid_of(paths), row(paths).get("sessionId")
rested(paths)
other_err, _ = start(mk(), "crew-releaser", "releaser")
other = pid_of(paths, "crew-releaser")
rested(paths, "crew-releaser")
end(pid, signal.SIGKILL)
watch_revive.pace = pace.Seam(env)
out = io.StringIO()
with redirect_stderr(out), redirect_stdout(out):
    pace.round_start(paths, env)
    watch_revive.step(paths, env)
    pace.step(paths, env=env)
ck("the limit is read from the station that still runs", pace.last(paths).closed and len(cell.puts) == 1, vars(pace.last(paths)))
ck("the dead station is not revived into the full limit, and the pace file remembers it",
   not cs.is_alive(paths, S) and pace.json_read(paths)["stopped"] == [S] and "is not revived now" in out.getvalue(), out.getvalue())
later(minutes=61)
account(1, 21)
out = io.StringIO()
with redirect_stderr(out), redirect_stdout(out):
    pace.round_start(paths, env)
    watch_revive.step(paths, env)
    pace.step(paths, env=env)
new = pid_of(paths)
STARTED.append(new)
ck("after the reset it is revived in its own conversation",
   new != pid and alive(new) and row(paths).get("sessionId") == thread
   and [m for m, _ in calls(new) if m.startswith(("thread/", "turn/"))][:2] == ["thread/resume", "turn/start"], (out.getvalue(), calls(new)))
ck("with the Codex wording: the account's limit, and no Cron tool",
   prompts(new) and "The account's limit was reset." in prompts(new)[0] and "Cron" not in prompts(new)[0]
   and "keep no schedule of your own" in prompts(new)[0], prompts(new))
end(new), end(other)

# ---------------------------------------------------------------------------------------------
print("g. the tick, the plan command and the status line")
cell, paths, said, mk, env = crew()
fake()
account(86, 20)
start(mk())
pid = pid_of(paths)
rested(paths)
asked = []
real_plan, person.cmd_plan = person.cmd_plan, lambda argv, env_: asked.append(argv) or 1
readings.MEMORY.plan_read_at, readings.MEMORY.plan_ok = 0.0, True
told = []
readings.read_plan(env, told.append, paths=paths)
ck("the nap's tick does not look for Claude's plan on a Codex crew, and says nothing", asked == [] and told == [], (asked, told))
readings.read_plan(env, told.append, paths=CrewPaths.at(SCRATCH / "nowhere" / "crew"))
ck("for a crew that is not on Codex it reads the plan as it did", asked == [["plan"]] and len(told) == 1, (asked, told))
person.cmd_plan = real_plan
out = io.StringIO()
with redirect_stdout(out), redirect_stderr(io.StringIO()):
    code = person.cmd_plan(["plan"], env)
ck("`tares factory plan` on a Codex crew prints the account's windows, and asks no claude",
   code == 0 and "300 minutes: 86% used, resets 20" in out.getvalue() and "10080 minutes: 20% used" in out.getvalue(), out.getvalue())
ck("and sends them to Tares", cell.puts and cell.puts[-1]["limits"]["rateLimits"]["primary"]["usedPercent"] == 86)
ck("the status line of a Codex crew shows its own windows",
   views.pace_status_line(mk()) == f"pace: slow, no new builders and the longest ticks, until {pace.read(mk()).until} (5-hour 86%, week 20%)",
   views.pace_status_line(mk()))
ck("it never asked for the Claude pace", all(p == "/api/crew/pace?runtime=codex" for p in cell.gets), cell.gets)
end(pid)
err = io.StringIO()
with redirect_stdout(io.StringIO()), redirect_stderr(err):
    code = person.cmd_plan(["plan"], env)
ck("with no station running the plan command says a running station is needed", code == 1 and "no station of this crew answers" in err.getvalue(), err.getvalue())

for p_ in STARTED:
    end(p_, signal.SIGKILL)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
