"""`tares factory runtime` and the runtime in `tares factory status` (tares/crew/runtime_cmd.py, views.py, TR-1600).

 a. with no word it says what new crews start on (the cell's runtime slot) and what this crew runs on;
 b. `runtime codex` sets the slot; a crew that runs on Claude Code is told it stays there, and how a crew for
    Codex starts; a crew whose stations recorded no runtime is on Claude Code; for Codex, the version found;
 c. a word that is not a runtime is refused and sets nothing; a key without the admin scope is told so;
 d. status --json names the crew's runtime and each station's; a Codex crew's text says what it runs on and lists
    a station the crew stopped (its guard was off), with how to start it again; a Claude Code crew's text has
    neither line.

Run: python tests/test_crew_runtime_cmd.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402

for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "CODEX", "CLAUDE"))]:
    del os.environ[_k]
SCRATCH = Path(tempfile.mkdtemp(prefix="tcrt-"))
BIN = SCRATCH / "bin"
BIN.mkdir()
if sys.platform == "win32":   # Windows runs no shell script: a .cmd that prints the same
    CODEX = BIN / "codex.cmd"
    CODEX.write_bytes(b"@echo codex-cli 0.171.0\r\n")
else:
    CODEX = BIN / "codex"
    CODEX.write_text("#!/bin/sh\necho 'codex-cli 0.171.0'\n")
    CODEX.chmod(0o755)
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), TARES_HOME=str(SCRATCH / "tares-home"), FACTORY_NO_WATCH="1",
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", CODEX_BIN=str(CODEX))
_crew_guard.install()
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "factory"))
from tares.crew import codex_station, runtime_cmd, views  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import _crew_fake  # noqa: E402
import fake_claude  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
TOKEN = "tok-runtime-cmd-0123456789"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def run(fn, argv, env):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv, env)
    return code, out.getvalue(), err.getvalue()


def crew(name, stations, runtime="claude_code"):
    tmp = SCRATCH / name
    tmp.mkdir()
    LS.make_crew(tmp, {"token": TOKEN, "stations": stations})
    tares = LS.FakeTares({"token": TOKEN, "runtime": runtime})
    tares.__enter__()
    home = tmp / "crew"
    env = {**os.environ, "FACTORY_CREW_HOME": str(home), "FACTORY_HOME": str(home), "TARESD_URL": tares.url,
           "TARES_FACTORY_URL": tares.url}
    os.environ.update(FACTORY_CREW_HOME=str(home), FACTORY_HOME=str(home), TARESD_URL=tares.url,
                      TARES_FACTORY_URL=tares.url)
    return tares, CrewPaths.at(home), env


print("a. what new crews start on, and this crew")
old = {"shop-orchestrator": {"role": "orchestrator", "status": "active"}}   # recorded before runtimes were
tares, paths, env = crew("old", old)
code, out, err = run(runtime_cmd.cmd_runtime, ["runtime"], env)
ck("it says the factory's runtime and that this crew, with no runtime recorded, is on Claude Code",
   code == 0 and "factory: new crews start on Claude Code" in out
   and "this crew (crew): Claude Code, as its stations were started" in out and "codex:" not in out, (code, out, err))

print("b. setting it")
code, out, err = run(runtime_cmd.cmd_runtime, ["runtime", "codex"], env)
put = [x for x in tares.log if x.get("method") == "PUT"]
ck("`runtime codex` sets the cell's slot, and the crew is told it stays on Claude Code and how a Codex crew starts",
   code == 0 and tares.world["runtime"] == "codex" and json.loads(put[-1]["body"]) == {"name": "codex"}
   and "factory: new crews now start on Codex" in out and "it stays on Claude Code" in out
   and "crew home of its own (FACTORY_CREW_HOME)" in out, (code, out, err))
ck("and the Codex found is named with its version", "codex: 0.171.0" in out, out)
code, out, err = run(runtime_cmd.cmd_runtime, ["runtime"], env)
ck("asked again, it says new crews start on Codex", "factory: new crews start on Codex" in out, out)
tares.__exit__(None, None, None)

print("c. refusals")
tares, paths, env = crew("bad", {})
n = len(tares.log)
code, out, err = run(runtime_cmd.cmd_runtime, ["runtime", "copilot"], env)
ck("a word that is not a runtime is refused, and nothing is set",
   code == 1 and "copilot is not a runtime: one of claude_code, codex" in err
   and not [x for x in tares.log[n:] if x.get("method") == "PUT"], (code, err))
tares.world["admin"] = False
code, out, err = run(runtime_cmd.cmd_runtime, ["runtime", "codex"], env)
ck("a key without the admin scope is told so, and where else it is set",
   code == 1 and "could not set the factory's runtime to Codex" in err and "Setup page" in err
   and tares.world["runtime"] == "claude_code", err)
code, out, err = run(runtime_cmd.cmd_runtime, ["runtime"], env)
ck("a crew that has started no station starts on the factory's", "no station started yet; it starts on Claude Code" in out, out)
tares.__exit__(None, None, None)

print("d. status")
cx = {"shop-orchestrator": {"role": "orchestrator", "runtime": "codex", "status": "active"},
      "shop-reviewer": {"role": "reviewer", "runtime": "codex", "status": "active",
                        "codex": {"guard_off": {"why": "it ran `git push` with no guard record", "turn": "t1"}}}}
tares, paths, env = crew("cx", cx, runtime="codex")
code, out, err = run(views.COMMANDS["status"], ["status", "--json"], env)
doc = json.loads(out) if out.strip().startswith("{") else {}
ck("status --json names the crew's runtime and the stations the crew stopped",
   code == 0 and doc.get("runtime") == "codex"
   and doc.get("stopped") == [{"station": "shop-reviewer", "why": "it ran `git push` with no guard record"}], (code, out[-600:], err[-400:]))
code, out, err = run(views.COMMANDS["status"], ["status"], env)
ck("its text says it runs on Codex, with the version, and how to start the stopped station again",
   "runtime: Codex 0.171.0" in out and "STOPPED shop-reviewer: it ran `git push` with no guard record;" in out
   and "revive shop-reviewer" in out and "STOPPED by the crew: it ran `git push`" in out, out[-800:])
tares.__exit__(None, None, None)
tares, paths, env = crew("cc", {"shop-orchestrator": {"role": "orchestrator", "status": "active"}})
fake = _crew_fake.fake_crew_env(SCRATCH / "cc-fake")   # a Claude Code crew asks claude: the fake one, as CI has none
fake_claude.write_agents(Path(fake["FAKE_CLAUDE_STATE"]), [])
(Path(fake["FAKE_CLAUDE_STATE"]) / "version").write_text("2.1.310\n", encoding="utf-8")
env = {**env, **{k: fake[k] for k in ("CLAUDE_BIN", "FAKE_CLAUDE_STATE", "PATH")}}
os.environ.update({k: fake[k] for k in ("CLAUDE_BIN", "FAKE_CLAUDE_STATE", "PATH")})
code, out, err = run(views.COMMANDS["status"], ["status", "--json"], env)
doc = json.loads(out) if out.strip().startswith("{") else {}
ck("a Claude Code crew's document says claude_code and has no stopped list", doc.get("runtime") == "claude_code"
   and "stopped" not in doc, (out[-400:], err[-300:]))
tares.__exit__(None, None, None)
ck("guard_off reads the mark the status shows", codex_station.guard_off.__doc__ is not None)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
