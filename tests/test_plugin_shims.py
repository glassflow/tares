"""A plugin update does not turn the guard off in sessions that are already open (TR-1131).

Claude Code reads a plugin's hooks.json when a session starts. A session opened before the plugin moved to
`tares hook` (TR-1103) keeps running the old commands: `bash .../hooks/guard.sh`, `bash .../hooks/session-start.sh`
and the MCP header helper `bin/mcp-headers`. Without those files every Bash and MCP call fails with exit 127,
which Claude Code does not treat as a block, so the guard stops without a sound.

Proves: each hooks.json command from before TR-1103 (tests/old_hooks_commands.json, the same as
`git show 7b57d5d4^:claude-plugin/hooks/hooks.json`) still runs against the current plugin folder. The guard
exits 0 and says nothing for an allowed call, and denies a builder that runs `factory spawn`; the session start
exits 0 and still says who a station is; the header helper still answers the crew's token; the two scripts the
other old commands ran (ship.py, challenger.py) are still there. The two hook shims have no shebang (they are run as
`bash <file>`) and bin/mcp-headers has `#!/bin/sh` (it may be started directly); all are executable. On a tares with
no `hook` command (exit 2) the guard shim exits 1, not 2, which Claude Code would read as a block. Nothing in hooks.json or .mcp.json names them.

Run: python tests/test_plugin_shims.py
"""
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude-plugin"
P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


if sys.platform == "win32":
    print("  skip  the old hooks ran through sh, which a Windows session never had")
    sys.exit(0)

OLD = json.loads((ROOT / "tests" / "old_hooks_commands.json").read_text(encoding="utf-8"))
TMP = Path(tempfile.mkdtemp(prefix="plugin_shims_")).resolve()
BIN = TMP / "bin"
BIN.mkdir()
SHIM_PY = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
(BIN / "tares").write_text(f'#!/bin/sh\nexec "{sys.executable}" -c "{SHIM_PY}" "$@"\n', encoding="utf-8")
(BIN / "tares").chmod(0o755)
BASE = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
BASE.update(HOME=str(TMP / "home"), PYTHONPATH=str(ROOT), TARES_HOME=str(TMP / "tares-home"),
            PATH=f"{BIN}{os.pathsep}{BASE.get('PATH', '/usr/bin:/bin')}", CLAUDE_PLUGIN_ROOT=str(PLUGIN))
BUILDER = dict(FACTORY_ROLE="builder", FACTORY_STATION="shop-build-m1-api", FACTORY_CREW="crew", TARES_PROJECT="p1",
               FACTORY_ORCHESTRATOR="crew-orchestrator", FACTORY_HOME=str(TMP / "factory"))
(TMP / "factory").mkdir()


def run(command, stdin="", **env):
    """The command as Claude Code ran it: through a shell, the payload on stdin."""
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True, input=stdin, encoding="utf-8",
                          env=dict(BASE, **env), cwd=str(TMP), timeout=60)


def payload(command):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def denied(r):
    return r.returncode == 0 and '"permissionDecision": "deny"' in r.stdout.replace('":"', '": "')


hooks = OLD["hooks"]
print("the old hooks.json")
ck("the fixture has the 13 commands hooks.json had", len(hooks) == 13, len(hooks))
git = subprocess.run(["git", "show", "7b57d5d4^:claude-plugin/hooks/hooks.json"], capture_output=True, text=True,
                     cwd=str(ROOT), encoding="utf-8")
if git.returncode == 0:
    real = [(ev, e.get("matcher", "*"), h["command"], h.get("args", [])) for ev, es in json.loads(git.stdout)["hooks"].items()
            for e in es for h in e["hooks"]]
    ck("the fixture is the hooks.json of the commit before TR-1103",
       real == [(h["event"], h["matcher"], h["command"], h["args"]) for h in hooks], real[:2])
else:
    print("  skip  the fixture is not compared with git (that commit is not in this clone)")

bash_cmds = [h for h in hooks if h["command"].startswith("bash ")]
py_cmds = [h for h in hooks if h["command"] == "python3"]
ck("two old commands ran a bash hook and the rest ran a python script", len(bash_cmds) == 2 and len(bash_cmds) + len(py_cmds) == 13,
   (len(bash_cmds), len(py_cmds)))
guard = next(h["command"] for h in bash_cmds if h["command"].endswith('guard.sh"'))
start = next(h["command"] for h in bash_cmds if h["command"].endswith('session-start.sh"'))
ck("the old guard command is `bash \"${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh\"`", guard == 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh"', guard)

print("the old guard command, on the current plugin")
r = run(guard, payload("ls -la"), **BUILDER)
ck("an allowed call exits 0 and says nothing", r.returncode == 0 and r.stdout.strip() == "", (r.returncode, r.stdout, r.stderr[-200:]))
r = run(guard, payload('factory spawn builder x "y"'), **BUILDER)
ck("a builder that runs `factory spawn` is denied", denied(r), (r.returncode, r.stdout, r.stderr[-200:]))
r = run(guard, payload('factory spawn builder x "y"'), FACTORY_ROLE="orchestrator", FACTORY_STATION="crew-orchestrator", FACTORY_CREW="crew",
        FACTORY_HOME=str(TMP / "factory"))
ck("the orchestrator may spawn", r.returncode == 0 and r.stdout.strip() == "", (r.returncode, r.stdout, r.stderr[-200:]))
r = run(guard, payload('factory spawn builder x "y"'))
ck("a session outside the factory is never touched", r.returncode == 0 and r.stdout.strip() == "", (r.returncode, r.stdout, r.stderr[-200:]))
r = run(guard, json.dumps({"tool_name": "mcp__slack__slack_send_message", "tool_input": {}}), **BUILDER)
ck("an MCP call is judged too (the matcher was Bash|mcp__.*): a Slack post is denied", denied(r), (r.returncode, r.stdout, r.stderr[-200:]))

print("the old guard and session start commands, on a tares that has no `hook` (it exits 2 for the usage error)")
OLDBIN = TMP / "oldbin"
OLDBIN.mkdir()
(OLDBIN / "tares").write_text("#!/bin/sh\necho \"tares: error: argument command: invalid choice: 'hook'\" >&2\nexit 2\n", encoding="utf-8")
(OLDBIN / "tares").chmod(0o755)
OLDPATH = f"{OLDBIN}{os.pathsep}/usr/bin{os.pathsep}/bin"
r = run(guard, payload("ls -la"), PATH=OLDPATH, **BUILDER)
ck("the guard shim does not exit 2 (Claude Code would block every call); it exits 1", r.returncode == 1, (r.returncode, r.stdout, r.stderr[-200:]))
r = run(start, json.dumps({"source": "startup"}), PATH=OLDPATH, **BUILDER)
ck("the session start shim does not exit 2 either", r.returncode != 2, (r.returncode, r.stdout, r.stderr[-200:]))
r = run(guard, payload("ls -la"), PATH="/usr/bin:/bin", **BUILDER)
ck("with no tares on PATH at all it does not block either (as hooks.json: only exit 2 is changed)", r.returncode not in (2,), (r.returncode, r.stderr[-200:]))

print("the old session start command, on the current plugin")
r = run(start, json.dumps({"source": "startup"}), **BUILDER)
ck("a station is told who it is, exit 0", r.returncode == 0 and "shop-build-m1-api" in r.stdout, (r.returncode, r.stdout[:200], r.stderr[-200:]))
r = run(start, json.dumps({"source": "compact"}))
ck("a session outside the factory hears nothing and exits 0", r.returncode == 0 and "tares-factory" not in r.stdout, (r.returncode, r.stdout[:200]))

print("the old MCP header helper, started by path")
helper = OLD["headersHelper"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
cred = TMP / "cred"
cred.write_text("tok-123\n", encoding="utf-8")
r = run(shlex.quote(helper), TARES_FACTORY_CRED_FILE=str(cred))
ck("it answers the crew's token", r.returncode == 0 and json.loads(r.stdout) == {"Authorization": "Bearer tok-123"}, (r.returncode, r.stdout, r.stderr[-200:]))
r = run(shlex.quote(helper))
ck("with no token it answers {} and exits 0", r.returncode == 0 and json.loads(r.stdout) == {}, (r.returncode, r.stdout, r.stderr[-200:]))
env = {k: v for k, v in BASE.items() if k != "CLAUDE_PLUGIN_ROOT"}
r = subprocess.run(["sh", "-c", shlex.quote(helper)], capture_output=True, text=True, env=dict(env, TARES_FACTORY_CRED_FILE=str(cred)), cwd=str(TMP))
ck("...also without CLAUDE_PLUGIN_ROOT (it finds the plugin from its own path)", r.returncode == 0 and "tok-123" in r.stdout, (r.returncode, r.stdout, r.stderr[-200:]))

print("the plugin root the header helper passes on")
ARGVBIN = TMP / "argvbin"
ARGVBIN.mkdir()
(ARGVBIN / "tares").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
(ARGVBIN / "tares").chmod(0o755)
for label, extra in (("from CLAUDE_PLUGIN_ROOT", {"CLAUDE_PLUGIN_ROOT": "/some/where else"}), ("from its own path when CLAUDE_PLUGIN_ROOT is unset", {})):
    e = {k: v for k, v in BASE.items() if k != "CLAUDE_PLUGIN_ROOT"}
    e.update(extra, PATH=f"{ARGVBIN}{os.pathsep}/usr/bin{os.pathsep}/bin")
    r = subprocess.run(["sh", "-c", shlex.quote(helper)], capture_output=True, text=True, env=e, cwd=str(TMP))
    want = ["hook", "mcp-headers", "--plugin-root", extra.get("CLAUDE_PLUGIN_ROOT", str(PLUGIN))]
    ck(f"--plugin-root comes {label}", r.returncode == 0 and r.stdout.split("\n")[:4] == want, (r.returncode, r.stdout, r.stderr[-200:]))

print("the other old commands")
for h in py_cmds:
    script = h["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    ck(f"{h['event']}: {Path(script).name} is still there", Path(script).is_file(), script)

print("the shims")
shims = [PLUGIN / "hooks" / "guard.sh", PLUGIN / "hooks" / "session-start.sh", PLUGIN / "bin" / "mcp-headers"]
for s in shims:
    text = s.read_text(encoding="utf-8")
    rel = s.relative_to(PLUGIN).as_posix()
    if rel == "bin/mcp-headers":  # started by path, so it needs a shebang to be run directly; /bin/sh, never bash
        ck(f"{rel} starts with #!/bin/sh (a direct start works; it is not bash)", text.startswith("#!/bin/sh\n"), text[:20])
        st = subprocess.run([str(s)], capture_output=True, text=True, env=dict(BASE, TARES_FACTORY_CRED_FILE=str(cred)), cwd=str(TMP))
        ck(f"{rel} runs when started directly, not through a shell", st.returncode == 0 and "tok-123" in st.stdout, (st.returncode, st.stdout, st.stderr[-200:]))
    else:
        ck(f"{rel} has no shebang (it is run as `bash <file>`)", not text.startswith("#!"), text[:20])
    ck(f"{rel} is executable", bool(s.stat().st_mode & stat.S_IXUSR), oct(s.stat().st_mode))
    ck(f"{rel} is one `tares hook` call and says when it goes", re.search(r"^(exec )?tares hook [a-z-]+", text, re.M) and "TR-975" in text, text)
    ck(f"{rel} has no em dash", "—" not in text)
now = json.dumps(json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))) + (PLUGIN / ".mcp.json").read_text(encoding="utf-8")
ck("hooks.json and .mcp.json do not name them (new sessions use `tares hook`)", not re.search(r"guard\.sh|session-start\.sh|bin/mcp-headers", now), now[:200])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
