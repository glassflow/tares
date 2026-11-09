"""Every hook of the Claude Code plugin starts through `tares hook <name>` (TR-1068).

Checks claude-plugin/hooks/hooks.json: every command string starts with `tares hook `, none is a
bare python3, python or bash, none carries an args list, every hook name is one `tares hook` knows,
and `tares hook <name> --dry-run` prints the script it would run, which exists. Also that the
/tares:challenger-waive command no longer calls python3.

Run: python tests/test_hooks_json.py
"""
import json
import os
import re
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


def tares(args, extra=None):
    return subprocess.run([sys.executable, "-c", SHIM_PY, *args], capture_output=True, text=True,
                          encoding="utf-8", env=dict(ENV, **(extra or {})), cwd=HOME)


plugin = ROOT / "claude-plugin"
hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
entries = [(ev, e.get("matcher", "*"), h) for ev, es in hooks.items() for e in es for h in e["hooks"]]

print("hooks.json")
cmds = [h["command"] for _, _, h in entries]
ck("there are 13 hook commands", len(cmds) == 13, len(cmds))
bad = [c for c in cmds if not c.startswith("tares hook ")]
ck("every command starts with `tares hook `", not bad, bad)
ck("no command names python, python3, py or bash first",
   not [c for c in cmds if re.match(r"(python3?|py|bash|sh)\b", c)], cmds)
ck("no hook carries an args list (the command is the whole line)", not [h for _, _, h in entries if "args" in h],
   [h for _, _, h in entries if "args" in h])
ck("no command needs ${CLAUDE_PLUGIN_ROOT} quoted (the hook finds the plugin itself)",
   not [c for c in cmds if "CLAUDE_PLUGIN_ROOT" in c], cmds)

print("tares hook --dry-run")
names = sorted({c.split()[2] for c in cmds})
ck("the plugin uses ship, challenger, guard and session-start", names == ["challenger", "guard", "session-start", "ship"], names)
for name in names:
    r = tares(["hook", name, "--dry-run"])
    path = Path(r.stdout.strip())
    ck(f"`tares hook {name} --dry-run` prints a script that exists, inside this plugin",
       r.returncode == 0 and path.is_file() and plugin.resolve() in path.resolve().parents,
       (r.returncode, r.stdout, r.stderr[-200:]))
r = tares(["hook", "statusline", "--dry-run"])
ck("statusline is a known hook too", r.returncode == 0 and r.stdout.strip().endswith("scripts/statusline.py"), (r.returncode, r.stdout))
r = tares(["hook", "nope"])
ck("an unknown name exits 2 and lists the known ones", r.returncode == 2 and "unknown hook" in r.stderr and "ship" in r.stderr, (r.returncode, r.stderr))
r = tares(["hook", "ship", "--dry-run"], {"CLAUDE_PLUGIN_ROOT": HOME + "/elsewhere"})
ck("CLAUDE_PLUGIN_ROOT picks the plugin (Claude Code sets it for its hooks)",
   r.stdout.strip() == (HOME + "/elsewhere/scripts/ship.py").replace("\\", "/") and r.returncode == 1, (r.returncode, r.stdout))

print("the waive command")
waive = (plugin / "commands" / "challenger-waive.md").read_text(encoding="utf-8")
ck("/tares:challenger-waive runs `tares hook challenger waive`", "tares hook challenger waive $ARGUMENTS" in waive and "python3" not in waive)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
