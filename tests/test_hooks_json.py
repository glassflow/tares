"""`tares hook <name>` covers every script hooks.json runs today (TR-1068).

hooks.json itself still runs python3 and bash directly until TR-1103 switches it, after a tares release
has `tares hook` (A15). This test proves the switch is possible: each script a hooks.json command
runs is one `tares hook` knows, `tares hook <name> --dry-run` prints that script, and it exists.
The check that hooks.json uses `tares hook` moves to TR-1103.

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
text = json.dumps(hooks)
ck("there are 13 hook commands", len(entries) == 13, len(entries))
scripts = sorted(set(re.findall(r"(?:scripts|hooks)/([\w-]+\.(?:py|sh))", text)))
ck("hooks.json runs ship.py, challenger.py, guard.sh and session-start.sh", scripts == ["challenger.py", "guard.sh", "session-start.sh", "ship.py"], scripts)

print("tares hook --dry-run")
names = [Path(x).stem for x in scripts]
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

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
