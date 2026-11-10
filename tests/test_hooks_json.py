"""The plugin starts every hook, the status line and the MCP header helper through `tares hook <name>` (TR-1068, TR-1103).

Proves: each hooks.json command is `tares hook <known name>` whose script exists in the plugin; the entry
count, matchers and timeouts are kept; `min_tares.txt` names the first release that has `tares hook`;
the session start tells an older install to upgrade (the `||` fallback, and `tares hook compat`); the
shell helpers `tares hook` replaced are gone; `.mcp.json` starts its header helper the same way.

Run: python tests/test_hooks_json.py
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SHIM_PY = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
HOME = tempfile.mkdtemp(prefix="hooks_json_")
ENV = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
ENV.update(HOME=HOME, PYTHONPATH=str(ROOT), TARES_HOME=HOME + "/tares-home")


def tares(args, extra=None, stdin=""):
    return subprocess.run([sys.executable, "-c", SHIM_PY, *args], capture_output=True, text=True, input=stdin,
                          encoding="utf-8", env=dict(ENV, **(extra or {})), cwd=HOME)


plugin = ROOT / "claude-plugin"
hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
entries = [(ev, e.get("matcher", "*"), h) for ev, es in hooks.items() for e in es for h in e["hooks"]]
min_tares = (plugin / "min_tares.txt").read_text(encoding="utf-8").strip()

print("hooks.json")
ck("there are 13 hook commands", len(entries) == 13, len(entries))
ck("every command starts with `tares hook `", all(h["command"].startswith("tares hook ") for _, _, h in entries),
   [h["command"] for _, _, h in entries if not h["command"].startswith("tares hook ")])
ck("none runs python3 or bash itself", not re.search(r"\b(python3?|bash|sh)\b ", json.dumps(hooks).replace("tares hook ", "")), json.dumps(hooks)[:200])
names = sorted({h["command"].split()[2] for _, _, h in entries})
ck("the names are ship, challenger, guard and session-start", names == ["challenger", "guard", "session-start", "ship"], names)
shape = [(ev, m, h["command"].split(" ||")[0], h.get("timeout"), bool(h.get("async"))) for ev, m, h in entries]
want = [
    ("SessionStart", "*", "tares hook ship", 10, False), ("SessionStart", "*", "tares hook session-start", 5, False),
    ("PreToolUse", "Bash|mcp__.*", "tares hook guard", 5, False), ("PreToolUse", "AskUserQuestion", "tares hook ship", None, True),
    ("UserPromptSubmit", "*", "tares hook ship", None, True),
    ("PostToolUse", "*", "tares hook ship", None, True), ("PostToolUse", "Bash|ExitPlanMode", "tares hook challenger", 900, False),
    ("Stop", "*", "tares hook challenger", 10, False), ("Stop", "*", "tares hook ship", None, True),
    ("SubagentStop", "*", "tares hook ship", None, True), ("PermissionRequest", "*", "tares hook ship", None, True),
    ("Notification", "*", "tares hook ship", None, True), ("SessionEnd", "*", "tares hook ship", None, False),
]
ck("events, matchers, timeouts and async flags are the ones the scripts had", shape == want, [s for s in shape if s not in want])

print("tares hook --dry-run")
for name in names + ["statusline", "mcp-headers"]:
    r = tares(["hook", name, "--dry-run"])
    path = Path(r.stdout.strip())
    ck(f"`tares hook {name} --dry-run` prints a script that exists, inside this plugin",
       r.returncode == 0 and path.is_file() and plugin.resolve() in path.resolve().parents,
       (r.returncode, r.stdout, r.stderr[-200:]))
r = tares(["hook", "nope"])
ck("an unknown name exits 2 and lists the known ones", r.returncode == 2 and "unknown hook" in r.stderr and "ship" in r.stderr, (r.returncode, r.stderr))
r = tares(["hook", "ship", "--dry-run"], {"CLAUDE_PLUGIN_ROOT": HOME + "/elsewhere"})
ck("CLAUDE_PLUGIN_ROOT picks the plugin (Claude Code sets it for its hooks)",
   r.stdout.strip() == (HOME + "/elsewhere/scripts/ship.py").replace("\\", "/") and r.returncode == 1, (r.returncode, r.stdout))

print("the first release with `tares hook`")
ck("min_tares.txt is 1.56.0", min_tares == "1.56.0", min_tares)

def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0])[:3])


own = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
ck("this checkout's own version is not older than the plugin asks (else the plugin could not run)", vtuple(own) >= vtuple(min_tares), (own, min_tares))
old = Path(HOME) / "oldplugin"
(old / "hooks").mkdir(parents=True)
(old / "min_tares.txt").write_text("999.0.0\n", encoding="utf-8")
r = tares(["hook", "compat", "--plugin-root", str(old)])
ck("`tares hook compat` for a plugin that wants a newer tares prints the upgrade line",
   r.returncode == 0 and "needs tares 999.0.0 or newer" in r.stdout and "uv tool upgrade tares" in r.stdout, (r.returncode, r.stdout, r.stderr[-200:]))
new = Path(HOME) / "newplugin"
new.mkdir()
(new / "min_tares.txt").write_text("0.0.1\n", encoding="utf-8")
r = tares(["hook", "compat", "--plugin-root", str(new)])
ck("`tares hook compat` for a plugin whose minimum is met prints nothing", r.returncode == 0 and r.stdout.strip() == "", (r.returncode, r.stdout))

print("an install older than 1.56.0 (no `tares hook`)")
fallback = next(h["command"] for _, _, h in entries if "session-start" in h["command"])
ck("the session start entry falls back to an upgrade line naming min_tares.txt's version",
   " || echo " in fallback and f"tares {min_tares} or newer" in fallback and "uv tool upgrade tares" in fallback, fallback)
ck("the fallback has no em dash", "—" not in fallback)
# An old tares answers `tares hook` with a usage error (exit 2) and no hook output: stand one in on PATH.
bindir = Path(HOME) / "oldbin"
bindir.mkdir()
shim = bindir / "tares"
shim.write_text("#!/bin/sh\necho \"usage: tares [-h] {up,mcp,...}\" >&2\nexit 2\n", encoding="utf-8")
shim.chmod(0o755)
if sys.platform == "win32":
    print("  skip  the old-tares shim runs through sh (not on Windows)")
else:
    r = subprocess.run(["sh", "-c", fallback], capture_output=True, text=True, env=dict(ENV, PATH=f"{bindir}{os.pathsep}{ENV['PATH']}"), cwd=HOME)
    ck("with an old tares on PATH the entry prints the upgrade line on stdout (Claude Code shows it as context)",
       r.returncode == 0 and f"tares {min_tares} or newer" in r.stdout, (r.returncode, r.stdout, r.stderr[-200:]))
    ok = subprocess.run(["sh", "-c", "exit 0 || echo SHOULD-NOT-PRINT"], capture_output=True, text=True)
    ck("with a working tares the fallback does not print", ok.stdout == "")

print("the shell helpers are gone, and .mcp.json uses `tares hook`")
for rel in ("hooks/guard.sh", "hooks/session-start.sh", "bin/mcp-headers"):
    ck(f"claude-plugin/{rel} is deleted", not (plugin / rel).exists())
ck("bin/factory-heavy stays (the crew's heavy-command wrapper, not a hook)", (plugin / "bin" / "factory-heavy").is_file())
mcp = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["tares"]
ck(".mcp.json starts its header helper with `tares hook mcp-headers`", mcp["headersHelper"].startswith("tares hook mcp-headers "), mcp["headersHelper"])
words = shlex.split(mcp["headersHelper"].replace("${CLAUDE_PLUGIN_ROOT}", "/p q"))
ck("the plugin root reaches it as one word", words[:4] == ["tares", "hook", "mcp-headers", "--plugin-root"] and words[4:] == ["/p q"], words)
left = []
for f in list(plugin.rglob("*")):
    if f.is_file() and f.suffix in (".json", ".md", ".sh", ".py", "") and "node_modules" not in f.parts:
        try:
            t = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if re.search(r"hooks/guard\.sh|hooks/session-start\.sh|bin/mcp-headers", t):
            left.append(str(f.relative_to(plugin)))
ck("nothing in the plugin still names a deleted helper", not left, left)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
