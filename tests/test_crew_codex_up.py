"""`crew up`, the station commands, `roster` and `doctor` for a crew on Codex, on a machine with no Claude Code
(TR-1593, and the doctor of TR-1598).

Each case uses a throwaway crew home, a fake Tares whose runtime slot holds Codex (or Claude Code, where the
case says so) and the fake `codex` (tests/factory/fake_codex.py). There is no `claude` anywhere: PATH holds
the fake codex, a `tares` for the guard's hook, and nothing else. The real Codex is run by hand.

 a. which runtime a crew is on: the one its recorded stations were started on, without asking Tares; for a
    crew that has started nothing, the one the cell's runtime slot holds; "" when Tares does not answer or is
    too old to say;
 b. `crew up` from an empty crew home starts the crew on Codex and never looks for `claude`; a second run
    leaves the stations alone; a Claude model's name in crew.json (its template's) is not handed to Codex; it stops before anything starts when `codex` is missing, too old, or the
    person has no login; a crew for Claude Code is still told that `claude` is missing, with Tares up or down;
 c. the station commands (`revive`, `restart`, `retire`) on a crew that has recorded no station yet: on a
    Codex cell they get past the look for `claude` and give their own answer; on a Claude Code cell, and with
    Tares down, they still say `claude` is missing;
 d. `roster`: one line per station with its turn as its app-server says it and the Codex it runs; as JSON,
    the roster's shape, with `runtime` and `codex`; a station that does not run is `gone`;
 e. `doctor`: for a crew on Codex, no check of Claude Code's (no `claude`, session list, trust, plugins,
    Claude processes); the machine (codex, its version, the login, the kit's guard); each station (its
    version, its own Codex folder, its guard, and its MCP connection asked of its app-server); what it says for a
    broken setup: no codex, an old one, a newer one, no login, a kit without its guard, a role set to a
    Claude model, a station's folder
    with a setup or a login of its own, settings that do not register the guard, a station stopped for its
    guard, a last turn never compared; the document `--json` prints is the doctor's shape.

Unix only: on Windows the test says so and passes.

Run: python tests/test_crew_codex_up.py
"""
import io
import json
import os
import signal
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

if sys.platform == "win32":
    print("skipped on Windows: a Codex station listens on a Unix socket\n\n0 passed, 0 failed")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

SCRATCH = Path(tempfile.mkdtemp(prefix="tcu-", dir="/tmp"))   # short: a socket's path holds about a hundred bytes
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
LOGIN = HOME / ".codex" / "auth.json"
LOGIN.parent.mkdir(parents=True)
LOGIN.write_text('{"auth_mode": "chatgpt", "tokens": "fake"}\n')
CODEX = BIN / "codex"
CODEX_TEXT = f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "factory" / "fake_codex.py"}" "$@"\n'
CODEX.write_text(CODEX_TEXT)
CODEX.chmod(0o755)
(BIN / "tares").write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -c '
                           '"import sys; sys.argv[0] = \'tares\'; from tares.cli import main; main()" "$@"\n')
(BIN / "tares").chmod(0o755)   # the station's guard is `tares hook codex-guard`, found on its PATH
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import psutil  # noqa: E402

from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import doctor, doctor_codex, lifecycle, proc, state, up, views  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-codex-up-0123456789"
ORCH, REV = "crew-orchestrator", "crew-reviewer"
SLOT = "/api/factory/integrations/effective"
_n = [0]
STARTED: list = []
KIT = LS.make_kit(SCRATCH)


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def crew(runtime="codex", world=None):
    """A fresh crew home with no station recorded, and a fake Tares whose runtime slot holds `runtime`;
    returns (tares, paths, env)."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": FAKE_TOKEN})
    tares = LS.FakeTares({"runtime": runtime, **(world or {})})
    tares.__enter__()
    paths = CrewPaths.at(tmp / "crew")
    for k in [k for k in os.environ if k.startswith("FAKE_CODEX_")]:
        del os.environ[k]
    os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_PLUGIN_DIR=str(KIT),
                      TARES_FACTORY_MCP_URL=tares.url + "/mcp", FAKE_CODEX_DAEMON=str(SCRATCH / "d"))
    return tares, paths, dict(os.environ)


def run(fn, argv, env=None):
    """(exit code, stdout, stderr) of a command called as the dispatcher calls it."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv, dict(os.environ) if env is None else env)
    return code, out.getvalue(), err.getvalue()


def row(paths, station):
    return state.stations_load(paths).get(station) or {}


def pid_of(paths, station):
    pid = (row(paths, station).get("codex") or {}).get("pid", 0)
    if pid and pid not in STARTED:
        STARTED.append(pid)
    return pid


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def end(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(100):
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def until(cond, wait=6.0):
    end_at = time.monotonic() + wait
    while time.monotonic() < end_at:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def asked(tares, path=SLOT):
    return [r for r in tares.log if r["path"].split("?")[0] == path]


def checks(paths, env=None):
    d = doctor.Doctor(dict(os.environ) if env is None else env, paths, root=str(KIT))
    with redirect_stderr(io.StringIO()):
        rows = d.collect()
    return {c.id: c for c in rows}, rows


# ---------------------------------------------------------------------------------------------
print("a. which runtime a crew is on")
tares, paths, env = crew("codex")
with TaresClient(paths, env, say=lambda _m: None) as c:
    ck("a crew that has started nothing is on the runtime the cell's slot holds", proc.planned_runtime(c) == "codex"
       and len(asked(tares)) == 1, asked(tares))
tares.__exit__()
tares, paths, env = crew("claude_code")
with TaresClient(paths, env, say=lambda _m: None) as c:
    ck("and that is Claude Code when the slot holds it", proc.planned_runtime(c) == "claude_code")
tares.__exit__()
tares, paths, env = crew("codex", {"routes": {SLOT: [404, {"detail": "no"}]}})
with TaresClient(paths, env, say=lambda _m: None) as c:
    ck("a Tares too old to say gives no runtime", proc.planned_runtime(c) == "")
tares.__exit__()
tares, paths, env = crew("codex", {"health": False})
with TaresClient(paths, env, say=lambda _m: None) as c:
    ck("a Tares that does not answer gives none, and is not asked for the slot", proc.planned_runtime(c) == ""
       and not asked(tares))
tares.__exit__()
tares, paths, env = crew("codex")
state.stations_put(REV, {"role": "reviewer", "status": "active"}, paths)
with TaresClient(paths, env, say=lambda _m: None) as c:
    ck("a crew with a station recorded for Claude Code stays on it, and Tares is not asked",
       proc.planned_runtime(c) == "claude_code" and not asked(tares))
state.stations_put(REV, {"role": "reviewer", "status": "active", "runtime": "codex"}, paths)
with TaresClient(paths, env, say=lambda _m: None) as c:
    ck("and one recorded for Codex is on Codex", proc.planned_runtime(c) == "codex" and not asked(tares))
tares.__exit__()

# ---------------------------------------------------------------------------------------------
print("b. crew up")
tares, paths, env = crew("codex")
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
orch, rev = pid_of(paths, ORCH), pid_of(paths, REV)
ck("crew up of a Codex crew from an empty crew home ends well, with no claude on PATH", code == 0, err[-600:])
ck("the orchestrator and the reviewer run, each on an app-server of its own, recorded for Codex",
   orch and rev and orch != rev and alive(orch) and alive(rev)
   and row(paths, ORCH).get("runtime") == "codex" and row(paths, REV).get("runtime") == "codex", (orch, rev))
ck("it says so, and sends the person to the desk, not to an attach nobody can make",
   f"started {ORCH} on Codex" in err and f"started {REV} on Codex" in err and "factory desk" in err
   and "crew attach" not in err, err[-600:])
ck("nothing of Claude Code's was asked: no trust of the crew home, no look for claude",
   "claude not found" not in err and "trust" not in err.lower() and "language server" not in err.lower(), err)
sdoc = json.loads(paths.station_file(REV, "settings.json").read_text())
ck("crew.json names Claude's models (its template does): none is handed to Codex, which then gives its own, and "
   "crew up says so for each station", "model" not in sdoc["thread"] and "effort" not in sdoc
   and row(paths, REV).get("model") == "" and row(paths, REV).get("effort") == ""
   and f"{REV} is set to opus, a Claude model" in err and "stations.reviewer.model in crew.json" in err, (sdoc["thread"], err[-500:]))
ck("the stations got their boot prompt as a turn", until(lambda: cs.turn_state(paths, ORCH) == "done")
   and until(lambda: cs.turn_state(paths, REV) == "done"))
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
ck("a second crew up leaves the running stations alone", code == 0 and f"{ORCH} is already running" in err
   and f"{REV} is already running" in err and pid_of(paths, ORCH) == orch and pid_of(paths, REV) == rev, err[-400:])
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch", "--json"])
doc = json.loads(out) if out.strip() else {}
ck("with --json it is one result document and nothing else on stdout", code == 0 and doc.get("schema_version") == 1
   and "error" not in doc, out[:300])
live_tares, live_paths = tares, paths    # kept for the roster and the doctor below

tares, paths, env = crew("codex")
cfg = json.loads(paths.crew_cfg.read_text())
cfg["stations"]["reviewer"] = {"model": "gpt-5.6-luna", "effort": "low"}
paths.crew_cfg.write_text(json.dumps(cfg))
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch", "reviewer"])
sdoc = json.loads(paths.station_file(REV, "settings.json").read_text())
pid_of(paths, REV)
ck("a role set to a Codex model starts on it, with its effort, and nothing is said", code == 0
   and sdoc["thread"].get("model") == "gpt-5.6-luna" and sdoc.get("effort") == "low"
   and row(paths, REV).get("model") == "gpt-5.6-luna" and "a Claude model" not in err
   and not row(paths, ORCH), (sdoc["thread"].get("model"), err[-300:]))
with redirect_stderr(io.StringIO()):
    lifecycle.cmd_down(["crew", "down"], dict(os.environ))
tares.__exit__()

tares, paths, env = crew("codex")
CODEX.unlink()
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
ck("no codex on PATH: crew up says so and starts nothing", code == 1 and "codex not found on PATH" in err
   and not state.stations_load(paths), err[-300:])
CODEX.write_text(CODEX_TEXT)
CODEX.chmod(0o755)
os.environ["FAKE_CODEX_VERSION"] = "0.170.4"
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
ck("a Codex older than the one stations start on: it says so and starts nothing", code == 1
   and "0.170.4 is older than 0.171.0" in err and not state.stations_load(paths), err[-300:])
del os.environ["FAKE_CODEX_VERSION"]
LOGIN.rename(LOGIN.with_suffix(".away"))
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
ck("no Codex login: it says so and starts nothing", code == 1 and "no Codex login" in err and "codex login" in err
   and not state.stations_load(paths), err[-300:])
LOGIN.with_suffix(".away").rename(LOGIN)
tares.__exit__()

tares, paths, env = crew("claude_code")
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
ck("a crew for Claude Code with no claude is told so, as before, and nothing starts", code == 1
   and "claude not found on PATH" in err and not state.stations_load(paths), err[-300:])
tares.__exit__()
tares, paths, env = crew("codex", {"health": False})
code, out, err = run(up.cmd_crew_up, ["crew", "up", "--yes", "--no-watch"])
ck("and with Tares down there is nothing to say Codex: claude is missing, as before", code == 1
   and "claude not found on PATH" in err, err[-300:])
tares.__exit__()

# ---------------------------------------------------------------------------------------------
print("c. the station commands on a crew that has recorded no station")
tares, paths, env = crew("codex")
code, out, err = run(lifecycle.cmd_revive, ["revive", REV])
ck("revive on a Codex cell gets past the look for claude and gives its own answer", code == 1
   and "claude not found" not in err and err.strip(), err[-300:])
code, out, err = run(lifecycle.cmd_restart, ["restart", REV])
ck("so does restart", code == 1 and "claude not found" not in err and err.strip(), err[-300:])
code, out, err = run(lifecycle.cmd_retire, ["retire", REV])
ck("and retire", "claude not found" not in err, err[-300:])
tares.__exit__()
tares, paths, env = crew("claude_code")
code, out, err = run(lifecycle.cmd_revive, ["revive", REV])
ck("on a Claude Code cell revive still says claude is missing", code == 1 and "claude not found" in err, err[-300:])
tares.__exit__()
tares, paths, env = crew("codex", {"health": False})
code, out, err = run(lifecycle.cmd_retire, ["retire", REV])
ck("and so does a command run with Tares down", code == 1 and "claude not found" in err, err[-300:])
tares.__exit__()

# ---------------------------------------------------------------------------------------------
print("d. roster")
tares, paths, env = crew("codex")
(paths.home / "stations.json").unlink()
code, out, err = run(views.cmd_roster, ["roster"])
ck("a crew that has started nothing on a Codex cell: roster says there are no stations, not that claude is missing",
   code == 0 and out.splitlines()[-1] == "no stations" and "claude" not in err, out + err)
code, out, err = run(views.cmd_status, ["status"])
ck("and status says the same", code == 0 and "no stations yet" in out + err and "claude not found" not in out + err, out + err)
tares.__exit__()
tares, paths, env = crew("claude_code")
(paths.home / "stations.json").unlink()
try:
    code, out, err = run(views.cmd_roster, ["roster"])
except Exception as e:   # the command's wrapper turns it into a line; called bare, the error itself comes out
    code, out, err = 1, "", str(e)
ck("on a Claude Code cell roster still says claude is missing", code == 1 and "claude not found" in err, err)
tares.__exit__()
tares, paths = live_tares, live_paths
os.environ.update(FACTORY_CREW_HOME=str(paths.home), TARESD_URL=tares.url, TARES_FACTORY_MCP_URL=tares.url + "/mcp")
code, out, err = run(views.cmd_roster, ["roster"])
lines = [x.split("\t") for x in out.splitlines()[1:]]
ck("one line per station: name, role, project, turn and the Codex it runs", code == 0
   and out.splitlines()[0] == f"crew: crew (orchestrator: {ORCH})"
   and sorted(lines) == [[ORCH, "orchestrator", "", "done", "codex 0.171.0"], [REV, "reviewer", "", "done", "codex 0.171.0"]],
   out)
code, out, err = run(views.cmd_roster, ["roster", "--json"])
doc = json.loads(out)
by = {s["station"]: s for s in doc["stations"]}
ck("as JSON it is the roster's shape, with the runtime and its version beside an empty claude_code",
   code == 0 and doc["schema_version"] == 1 and doc["orchestrator"] == ORCH
   and by[REV] == {"station": REV, "role": "reviewer", "project": "", "turn": "done", "claude_code": "",
                   "runtime": "codex", "codex": "0.171.0"}, out)
try:
    import jsonschema
    schema = json.loads((ROOT / "tares" / "crew" / "schemas" / "roster.json").read_text())
    problems = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(doc)]
except ImportError:
    problems = []
ck("and it passes the roster schema", problems == [], problems)
end(pid_of(paths, REV))
os.environ["FAKE_CODEX_TURN"] = "hang"   # the app-server started now keeps its turn going until interrupted
with redirect_stderr(io.StringIO()):
    c = TaresClient(paths, dict(os.environ), say=lambda _m: None)
    c.prime()
    lifecycle.revive(c, paths, [REV, "Look again."], claude_bin="")
    c.close()
code, out, err = run(views.cmd_roster, ["roster"])
ck("a station in a turn is working", f"{REV}\treviewer\t\tworking\tcodex 0.171.0" in out, out)
del os.environ["FAKE_CODEX_TURN"]
end(pid_of(paths, REV))
code, out, err = run(views.cmd_roster, ["roster"])
ck("a station whose app-server is gone is gone, and the other is still listed",
   f"{REV}\treviewer\t\tgone\tcodex 0.171.0" in out and f"{ORCH}\torchestrator\t\tdone" in out, out)
with redirect_stderr(io.StringIO()):
    c = TaresClient(paths, dict(os.environ), say=lambda _m: None)
    c.prime()
    lifecycle.revive(c, paths, [REV], claude_bin="")
    c.close()
ck("revived with no claude, it is listed as running again", until(lambda: cs.turn_state(paths, REV) == "done")
   and pid_of(paths, REV) and alive(pid_of(paths, REV)))
try:
    code, out, err = run(views.cmd_attach, ["attach", "reviewer"])
except Exception as e:
    code, out, err = 1, "", str(e)
ck("attach says a Codex station has no session to open, and names the desk and its log", code == 1
   and "runs on Codex" in err and "factory desk" in err and "codex.log" in err and "claude not found" not in err, err)

# ---------------------------------------------------------------------------------------------
print("e. doctor")
by, rows = checks(paths)
ids = set(by)
ck("no check of Claude Code's is made for a crew on Codex",
   not ids & {"claude", "sessions", "home_trusted", "claude_processes", "claude_service", "claude_update", "cmux"}
   and not any(i.startswith(("cc_", "allow_", "lsp_", "job_")) for i in ids)
   and not any("claude" in c.message.lower() for c in rows                 # a setting's name, and what is said
               if c.id != "settings" and not c.id.startswith("model_")), sorted(ids))   # of a Claude model in crew.json
ck("the machine: codex with its version, the login, the kit's guard", by["codex"].level == "ok"
   and "0.171.0" in by["codex"].message and by["codex_login"].level == "ok" and str(LOGIN) in by["codex_login"].message
   and by["plugin"].level == "ok" and str(KIT) in by["plugin"].message, [by[k].message for k in ("codex", "codex_login", "plugin")])
ck("a role crew.json sets to a Claude model gets a warning that says what Codex does with it, and how to choose",
   by["model_reviewer"].level == "warn" and "opus, a Claude model" in by["model_reviewer"].message
   and "stations.reviewer.model" in by["model_reviewer"].fix, by["model_reviewer"])
ck("the checks that are every crew's are still made", {"crew_home", "tares", "mcp", "settings"} <= ids, sorted(ids))
ck("each station: the Codex it runs, its own folder, its guard", all(
   by[f"codex_{s}"].level == "ok" and by[f"codex_home_{s}"].level == "ok" and by[f"codex_guard_{s}"].level == "ok"
   for s in (ORCH, REV)), [(i, by[i].level, by[i].message) for i in sorted(ids) if i.startswith("codex_")])
ck("the guard's line says when it last ran", "ran at the start of its last turn" in by[f"codex_guard_{REV}"].message
   and "s ago" in by[f"codex_guard_{REV}"].message, by[f"codex_guard_{REV}"].message)
ck("the MCP connection of a running station is asked of its app-server: connected, and its read goes through",
   by[f"codex_mcp_{REV}"].level == "ok" and "connected to tares" in by[f"codex_mcp_{REV}"].message
   and "list_crew goes through" in by[f"codex_mcp_{REV}"].message, by[f"codex_mcp_{REV}"])
tares.world.update(token=FAKE_TOKEN, mcp_key="another-key")   # the cell no longer takes the crew's key
level, rid, said, fix = doctor_codex.station_mcp(paths, REV)
ck("a key the cell refuses is a fix, in the cell's words: the endpoint took the connection, the read is refused",
   level == "fix" and rid == f"codex_mcp_{REV}" and "would work without Tares" in said and "401" in said
   and f"restart {REV}" in fix, (level, said, fix))
tares.world.update(token="", mcp_key=None)
code, out, err = run(doctor.cmd_doctor, ["doctor", "--json"])
doc = json.loads(out)
try:
    schema = json.loads((ROOT / "tares" / "crew" / "schemas" / "doctor.json").read_text())
    problems = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(doc)]
except (ImportError, NameError):
    problems = []
ck("doctor --json prints the doctor's shape for a Codex crew", problems == [] and doc["schema_version"] == 1
   and any(c["id"] == "codex" for c in doc["checks"]) and not any(c["id"] == "claude" for c in doc["checks"]), problems)
code, out, err = run(doctor.cmd_doctor, ["doctor"])
ck("the text has one line per check and no line about claude", f"{REV} runs Codex 0.171.0" in out
   and not any("claude" in x.lower() for x in out.splitlines()
               if "crew settings on Tares" not in x and "a Claude model" not in x), out)

# the machine, broken
CODEX.unlink()
by, _ = checks(paths)
ck("no codex: a FIX that says how", by["codex"].level == "fix" and "codex not found on PATH" in by["codex"].message
   and "CODEX_BIN" in by["codex"].fix, by["codex"])
CODEX.write_text(CODEX_TEXT)
CODEX.chmod(0o755)
os.environ["FAKE_CODEX_VERSION"] = "0.170.4"
by, _ = checks(paths)
ck("an old codex: a FIX with both versions", by["codex"].level == "fix" and "0.170.4 is older than 0.171.0" in by["codex"].message,
   by["codex"])
os.environ["FAKE_CODEX_VERSION"] = "0.180.0"
by, _ = checks(paths)
ck("a newer codex: a warning that it was not tested, and each station is said to run an older one",
   by["codex"].level == "warn" and "newer than 0.171.0" in by["codex"].message
   and by[f"codex_{REV}"].level == "warn" and "runs Codex 0.171.0, and 0.180.0 is installed" in by[f"codex_{REV}"].message,
   (by["codex"], by[f"codex_{REV}"]))
del os.environ["FAKE_CODEX_VERSION"]
LOGIN.rename(LOGIN.with_suffix(".away"))
by, _ = checks(paths)
ck("no login: a FIX that says how to make one", by["codex_login"].level == "fix"
   and "codex login" == by["codex_login"].fix, by["codex_login"])
LOGIN.with_suffix(".away").rename(LOGIN)
d = doctor.Doctor(dict(os.environ), paths, root=str(SCRATCH / "no-kit"))
with redirect_stderr(io.StringIO()):
    by = {c.id: c for c in d.collect()}
ck("a kit without its guard: a FIX", by["plugin"].level == "fix" and "no hooks/guard.py" in by["plugin"].message, by["plugin"])
ck("no tares on PATH for the hook: a FIX", doctor_codex.kit(str(KIT), {"PATH": str(SCRATCH / "empty")})[0][0] == "fix")

# a station, broken
home = Path(row(paths, REV)["codex"]["home"])
config = home / "config.toml"
kept = config.read_text() if config.exists() else None
config.write_text((kept or "") + '[mcp_servers.other]\ncommand = "x"\n')
by, _ = checks(paths)
ck("a station's folder with a setup of its own: a FIX that names it", by[f"codex_home_{REV}"].level == "fix"
   and "setup of its own" in by[f"codex_home_{REV}"].message and "mcp_servers" in by[f"codex_home_{REV}"].message,
   by[f"codex_home_{REV}"])
config.unlink() if kept is None else config.write_text(kept)
link = home / "auth.json"
link.unlink()
link.write_text("{}")
by, _ = checks(paths)
ck("a login of its own where the link belongs: a FIX", by[f"codex_home_{REV}"].level == "fix"
   and "not a link to the person's login" in by[f"codex_home_{REV}"].message, by[f"codex_home_{REV}"])
link.unlink()
os.symlink(LOGIN, link)
home.chmod(0o755)
by, _ = checks(paths)
ck("a folder others can open: a FIX", by[f"codex_home_{REV}"].level == "fix" and "open to others" in by[f"codex_home_{REV}"].message)
home.chmod(0o700)
ck("and in order again, the folder is ok", checks(paths)[0][f"codex_home_{REV}"].level == "ok")

sfile = paths.station_file(REV, "settings.json")
sdoc = json.loads(sfile.read_text())
broken = json.loads(json.dumps(sdoc))
del broken["thread"]["config"]["hooks"]["PreToolUse"]
sfile.write_text(json.dumps(broken))
by, _ = checks(paths)
ck("settings that register no guard for tool calls: a FIX", by[f"codex_guard_{REV}"].level == "fix"
   and "no guard for PreToolUse" in by[f"codex_guard_{REV}"].message, by[f"codex_guard_{REV}"])
broken = json.loads(json.dumps(sdoc))
broken["thread"]["config"]["bypass_hook_trust"] = False
sfile.write_text(json.dumps(broken))
ck("settings without the thread's trust for hooks: a FIX", "trust for hooks" in checks(paths)[0][f"codex_guard_{REV}"].message)
sfile.write_text(json.dumps(sdoc))
cs.mark_unverified(paths, REV, row(paths, REV).get("sessionId") or "t", "the station did not answer")
by, _ = checks(paths)
ck("a last turn never compared: a warning that says how it is compared", by[f"codex_guard_{REV}"].level == "warn"
   and "never compared" in by[f"codex_guard_{REV}"].message and f"revive {REV}" in by[f"codex_guard_{REV}"].fix,
   by[f"codex_guard_{REV}"])
cs._clear_unverified(paths, REV)
cs.mark_off(paths, REV, "a command ran with no run of the guard before it")
by, _ = checks(paths)
ck("a station stopped for its guard: a FIX with the reason", by[f"codex_guard_{REV}"].level == "fix"
   and "a command ran with no run of the guard" in by[f"codex_guard_{REV}"].message, by[f"codex_guard_{REV}"])
rec = dict(row(paths, REV)["codex"])
rec.pop("guard_off", None)
state.stations_put(REV, {"codex": rec}, paths)
end(pid_of(paths, ORCH))
by, _ = checks(paths)
ck("a station that does not run: a warning, and the watch is named", by[f"codex_{ORCH}"].level == "warn"
   and "is not running" in by[f"codex_{ORCH}"].message, by[f"codex_{ORCH}"])
tares.__exit__()

# a crew that has started nothing, on a Codex cell and on a Claude Code cell
tares, paths, env = crew("codex")
by, rows = checks(paths)
ck("a crew that has started nothing on a Codex cell gets the machine's Codex checks and no FIX for claude",
   by["codex"].level == "ok" and "claude" not in by and not any(i.startswith("codex_guard_") for i in by), sorted(by))
tares.__exit__()
tares, paths, env = crew("claude_code")
by, rows = checks(paths)
ck("on a Claude Code cell the doctor still asks for claude, and makes no Codex check",
   by["claude"].level == "fix" and "codex_login" not in by and "sessions" in by, sorted(by))
tares.__exit__()

for pid in STARTED:
    end(pid)
import shutil  # noqa: E402
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
