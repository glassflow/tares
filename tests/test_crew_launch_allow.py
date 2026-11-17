"""What each role keeps of the person's MCP servers and plugins (stations.<role>.mcp and .plugins in crew.json).

Defaults are empty lists, a helper keeps its parent's role's lists, `tares factory allow` shows and sets them,
and the doctor shows one line per role and warns about a name Claude Code does not have.

A scratch crew home and a fake HOME: nothing of the person's is read or written.
Run: python tests/test_crew_launch_allow.py
"""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-allow-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY"))]:
    del os.environ[_k]
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "factory"))

from tares.crew import allow, dispatch, doctor, launch, state  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(doc, indent=2) + "\n").encode("utf-8"))


HOME = SCRATCH / "home"
MCP_SECRET = "fake-mcp-secret-9911"
write(HOME / ".claude.json", {"mcpServers": {"github": {"command": "x"}, "linear": {"command": "y", "env": {"TOKEN": MCP_SECRET}},
                                              "other": {"command": "z"}}})
write(HOME / ".claude" / "settings.json", {"enabledPlugins": {"slack@claude-plugins": True}})
CREW = SCRATCH / "crew"
paths = CrewPaths.at(CREW)
ENV = {"HOME": str(HOME)}


def crew_json(stations):
    write(CREW / "crew.json", {"crew_name": "crew", "stations": stations})


def run(args, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = allow.run(args, {**ENV, **(env or {})}, paths, out)
        except Exception as e:  # CrewError: what cmd_allow prints
            print(getattr(e, "line", str(e)), file=err)
            code = 1
    return code, out.getvalue(), err.getvalue()


def doctor_rows():
    d = doctor.Doctor(ENV, paths)
    d.check_allow()
    return {c.id: c for c in d.checks}


crew_json({"orchestrator": {"model": "opus"}, "builder": {"model": "sonnet"}})
print("defaults")
ck("a role with no keys keeps no MCP server and no plugin", launch.allow_lists(paths, "builder") == ([], []))
ck("a role that is not in crew.json keeps none", launch.allow_lists(paths, "releaser") == ([], []))
ck("a crew.json without a stations key keeps none", (write(CREW / "crew.json", {}), launch.allow_lists(paths, "builder"))[1] == ([], []))
ck("no crew.json keeps none", (( CREW / "crew.json").unlink(), launch.allow_lists(paths, "builder"))[1] == ([], []))

print("reading")
crew_json({"builder": {"mcp": ["github", " linear ", "github", "", 3], "plugins": ["slack@claude-plugins"]},
           "reviewer": {"mcp": "github", "plugins": None}})
ck("a builder keeps the names listed, trimmed once each", launch.allow_lists(paths, "builder") == (["github", "linear"], ["slack@claude-plugins"]))
ck("a value that is not a list keeps none", launch.allow_lists(paths, "reviewer") == ([], []))
ck("a helper keeps its builder parent's lists", launch.allow_lists(paths, "helper", "proj-build-m1") == (["github", "linear"], ["slack@claude-plugins"]))
ck("a helper under the reviewer keeps the reviewer's", launch.allow_lists(paths, "helper", "crew-reviewer") == ([], []))
ck("a helper whose parent is no station keeps none", launch.allow_lists(paths, "helper", "stranger") == ([], []))

print("the command")
code, out, err = run([])
ck("allow shows the four roles, one line each", code == 0 and len(out.splitlines()) == 4 and out.splitlines()[3] == "builder keeps mcp github, linear; plugins slack@claude-plugins", out)
code, out, err = run(["releaser"])
ck("allow <role> shows one role", code == 0 and out == "releaser keeps mcp none; plugins none\n", out)
code, out, err = run(["releaser", "mcp", "github"])
ck("allow <role> mcp <name> sets the list", code == 0 and launch.allow_lists(paths, "releaser") == (["github"], []) and err == "", (out, err))
ck("setting keeps what else the role has", state.ccfg("stations", "builder", "plugins", paths=paths) != "" and launch.allow_lists(paths, "builder")[0] == ["github", "linear"])
code, out, err = run(["releaser", "mcp", "--none"])
ck("--none empties the list", code == 0 and launch.allow_lists(paths, "releaser") == ([], []), (out, err))
code, out, err = run(["builder", "mcp", "nosuch"])
ck("an unknown name is kept and warned about", code == 0 and launch.allow_lists(paths, "builder")[0] == ["nosuch"] and "nosuch" in err, (out, err))
code, out, err = run(["builder", "mcp", "github"], {"FACTORY_ROLE": "builder"})
ck("a builder cannot set a list", code == 1 and launch.allow_lists(paths, "builder")[0] == ["nosuch"] and "orchestrator" in err, (out, err))
code, out, err = run(["builder", "mcp"], {"FACTORY_ROLE": "builder"})
ck("a builder can read one", code == 0 and out.startswith("builder keeps"), (out, err))
ck("a helper role is not a role to set", run(["helper", "mcp", "x"])[0] == 1)
ck("a kind that is not mcp or plugins is refused", run(["builder", "tools", "x"])[0] == 1)
ck("a name that looks like a flag is refused", run(["builder", "mcp", "--all"])[0] == 1)
crew_json({"builder": {"model": "sonnet"}})
code, out, err = run(["builder", "plugins", "slack@claude-plugins"])
ck("it keeps the role's model", code == 0 and state.ccfg("stations", "builder", "model", paths=paths) == "sonnet" and launch.allow_lists(paths, "builder")[1] == ["slack@claude-plugins"], (out, err))
write(CREW / "crew.json", {"stations": []})
code, out, err = run(["builder", "mcp", "github"])
ck("stations that is a list is refused, not a traceback", code == 1 and "stations" in err and "Traceback" not in err, (out, err))
write(CREW / "crew.json", {"stations": None})
code, out, err = run(["builder", "mcp", "github"])
ck("stations that is null is made into an object", code == 0 and launch.allow_lists(paths, "builder")[0] == ["github"], (out, err))
crew_json({"builder": {"mcp": ["github"]}})
os.environ["FACTORY_CREW_HOME"] = str(CREW)
_cmd = dispatch.find(["allow", "builder"])
_out, _err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(_out), contextlib.redirect_stderr(_err):
    _code = _cmd(["allow", "builder"], ENV) if _cmd else None
del os.environ["FACTORY_CREW_HOME"]
ck("tares factory allow is routed by the dispatcher", _code == 0 and _out.getvalue() == "builder keeps mcp github; plugins none\n", (_code, _out.getvalue(), _err.getvalue()))
(CREW / "crew.json").unlink()
ck("without a crew it says so", run([])[0] == 1 and "no crew" in run([])[2])

print("the doctor")
crew_json({"orchestrator": {"mcp": ["github"]}, "builder": {"mcp": ["github", "nosuch"], "plugins": ["gone@x"]}})
rows = doctor_rows()
ck("one line per role", sorted(rows) == ["allow_builder", "allow_orchestrator", "allow_releaser", "allow_reviewer"], sorted(rows))
ck("a role with known names is ok", rows["allow_orchestrator"].level == "ok" and "mcp github; plugins none" in rows["allow_orchestrator"].message)
ck("a role with no lists is ok", rows["allow_reviewer"].level == "ok" and rows["allow_reviewer"].message == "reviewer keeps mcp none; plugins none")
ck("an unknown name is a warning that names it", rows["allow_builder"].level == "warn" and "nosuch" in rows["allow_builder"].message and "gone@x" in rows["allow_builder"].message, rows["allow_builder"].message)
ck("an unknown name does not fail the doctor", all(c.ok for c in rows.values()))
(HOME / ".claude.json").unlink()
ck("with no ~/.claude.json an MCP name is not called unknown", doctor_rows()["allow_orchestrator"].level == "ok")
(CREW / "crew.json").unlink()
ck("no crew.json, no lines", doctor_rows() == {})

print("the launch")
write(HOME / ".claude.json", {"mcpServers": {"github": {"command": "x"}, "linear": {"command": "y", "env": {"TOKEN": MCP_SECRET}},
                                              "other": {"command": "z"}}})
_n = [0]


def plan_of(role, lists, station="proj-build-m1"):
    """(plan, the station's folder, what was printed, the crew home) of launch_plan and write_settings for a role."""
    _n[0] += 1
    tmp = SCRATCH / f"l{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": "tok-fake-allow-0123456789", "crew": {"stations": {role: lists}}})
    os.environ["TARES_PLUGIN_DIR"] = str(LS.make_kit(tmp))
    tares = LS.FakeTares({"projects": []})
    tares.__enter__()
    c = TaresClient(CrewPaths.at(tmp / "crew"), env={**os.environ, "TARESD_URL": tares.url})
    out, err = io.StringIO(), io.StringIO()
    try:
        c.prime(offline_ok=True)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            plan = launch.launch_plan(c, station, role, "", "", str(tmp / "repo"))
            launch.write_settings(c, station, role, "", "", str(tmp / "repo"), plan)
    finally:
        c.close()
        tares.__exit__(None, None, None)
        os.environ.pop("TARES_PLUGIN_DIR", None)
    return plan, tmp / "crew" / "stations", out.getvalue() + err.getvalue(), tmp


plan, sdir, said, tmp = plan_of("builder", {"mcp": ["linear", "ghost"], "plugins": ["pyright-lsp@claude-plugins"]})
mcp_file = sdir / "proj-build-m1.mcp.json"
doc = json.loads(mcp_file.read_text(encoding="utf-8")) if mcp_file.exists() else {}
args = plan["args"]
at = args.index("--mcp-config") if "--mcp-config" in args else -1
ck("a role with an MCP server starts with --mcp-config and the file", at > 0 and args[at + 1].endswith("/stations/proj-build-m1.mcp.json"), str(args))
ck("the flag is followed by another flag, so it never takes the prompt", at > 0 and args[at + 2].startswith("--"), str(args))
ck("the file holds only the named server", sorted(doc.get("mcpServers", {})) == ["linear"], str(doc)[:80])
ck("the file is for its owner only", mcp_file.exists() and stat.S_IMODE(mcp_file.stat().st_mode) == 0o600 if os.name != "nt" else True)
ck("a name ~/.claude.json lacks is skipped with one line", said.count("'ghost' is not in ~/.claude.json") == 1, said)
ck("no server config text reaches the output", MCP_SECRET not in said and "TOKEN" not in said, said)
settings = json.loads((sdir / "proj-build-m1.settings.json").read_text(encoding="utf-8"))
ck("an allow-listed plugin is enabled in the station's settings", settings.get("enabledPlugins", {}).get("pyright-lsp@claude-plugins") is True, str(settings.get("enabledPlugins")))
ck("the Tares plugin stays enabled beside it", settings.get("enabledPlugins", {}).get("tares@inline") is True, str(settings.get("enabledPlugins")))
ck("the settings file holds no server config", MCP_SECRET not in json.dumps(settings))

plan, sdir, said, tmp = plan_of("builder", {})
ck("a role with no list has no --mcp-config and no file", "--mcp-config" not in plan["args"] and not any("--mcp-config=" in a for a in plan["args"])
   and not (sdir / "proj-build-m1.mcp.json").exists() and plan["mcp"] == {}, str(plan["args"]))
ck("a role with no list enables only the Tares plugin", list(plan["settings"].get("enabledPlugins", {})) == ["tares@inline"], str(plan["settings"].get("enabledPlugins")))

mcp_file = sdir / "proj-build-m1.mcp.json"
mcp_file.write_text("{}", encoding="utf-8")
plan, sdir, said, tmp = plan_of("builder", {"mcp": ["ghost"]})
ck("a list whose names are all missing writes no file and no flag", "--mcp-config" not in plan["args"] and not (sdir / "proj-build-m1.mcp.json").exists(), str(plan["args"]))

print("an older file goes when the list empties")
_n[0] += 1
t2 = SCRATCH / f"l{_n[0]}"
t2.mkdir()
(t2 / "crew" / "stations").mkdir(parents=True)
(t2 / "crew" / "stations" / "x.mcp.json").write_text("{}", encoding="utf-8")
c2 = type("C", (), {"paths": CrewPaths.at(t2 / "crew")})()
launch.write_settings(c2, "x", "builder", "", "", "", {"env": {}, "settings": {}, "mcp": {}})
ck("write_settings removes a stale MCP file", not (t2 / "crew" / "stations" / "x.mcp.json").exists())

print("with_mcp_flag")
ck("after the --settings value", launch.with_mcp_flag(["-p", "--settings", "s", "--setting-sources", "project,local"], "f")
   == ["-p", "--settings", "s", "--mcp-config", "f", "--setting-sources", "project,local"])
ck("before --plugin-dir without --settings", launch.with_mcp_flag(["--plugin-dir", "k"], "f") == ["--mcp-config", "f", "--plugin-dir", "k"])
ck("the = form when it has neither", launch.with_mcp_flag(["-p"], "f") == ["-p", "--mcp-config=f"])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
