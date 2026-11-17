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
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-allow-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY"))]:
    del os.environ[_k]
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
sys.path.insert(0, str(ROOT))

from tares.crew import allow, doctor, launch, state  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

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
write(HOME / ".claude.json", {"mcpServers": {"github": {"command": "x"}, "linear": {"command": "y"}}})
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

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
