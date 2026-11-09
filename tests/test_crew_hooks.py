"""`tares hook <name>` runs the plugin's hooks with stdin, stdout, arguments and exit codes passed through (TR-1068).

Uses a throwaway plugin folder (--plugin-root) whose scripts/ship.py echoes what it got and exits with
a chosen code, so the pass-through is proven without the real shipper; then the real ship.py with a
sample hook payload (it must exit 0 and print nothing, with Tares not reachable), the shell hooks
through bash (skipped, and said so, when bash is not on PATH), and the failure messages.

Run: python tests/test_crew_hooks.py
"""
import json
import os
import shutil
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


T = Path(tempfile.mkdtemp(prefix="crew_hooks_"))
(T / "home").mkdir()
SHIM_PY = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
ENV = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
ENV.update(HOME=str(T / "home"), USERPROFILE=str(T / "home"), PYTHONPATH=str(ROOT), TARES_HOME=str(T / "tares-home"),
           PYTHONIOENCODING="utf-8")


def tares(args, stdin="", extra=None):
    return subprocess.run([sys.executable, "-c", SHIM_PY, *args], input=stdin, capture_output=True, text=True,
                          encoding="utf-8", env=dict(ENV, **(extra or {})), cwd=str(T))


# A plugin whose ship.py echoes stdin and its arguments, and exits with the code in its first argument.
fake = T / "fake plugin"                      # a space in the path, as on a Windows user folder
(fake / "scripts").mkdir(parents=True)
(fake / "hooks").mkdir()
(fake / "scripts" / "ship.py").write_text(
    "import sys\n"
    "data = sys.stdin.read()\n"
    "print(__name__, 'argv=' + repr(sys.argv[1:]), 'stdin=' + data.strip())\n"
    "sys.stderr.write('to stderr\\n')\n"
    "sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0)\n", encoding="utf-8", newline="\n")
(fake / "hooks" / "guard.sh").write_text("#!/usr/bin/env bash\ncat\necho\necho \"args=$*\"\nexit 3\n", encoding="utf-8", newline="\n")

print("a Python hook")
r = tares(["hook", "ship", "--plugin-root", str(fake)], stdin='{"a": 1}')
ck("it runs as __main__, gets stdin and no arguments, exit 0",
   r.returncode == 0 and r.stdout.strip() == "__main__ argv=[] stdin={\"a\": 1}" and r.stderr == "to stderr\n", (r.returncode, r.stdout, r.stderr))
r = tares(["hook", "ship", "--plugin-root", str(fake), "7", "x y"], stdin="in")
ck("arguments after the options reach the script untouched, and its exit code is ours",
   r.returncode == 7 and "argv=['7', 'x y']" in r.stdout, (r.returncode, r.stdout))
r = tares(["hook", "ship", "7", "--dry-run"], extra={"CLAUDE_PLUGIN_ROOT": str(fake)})
ck("--dry-run after the first argument is the script's own argument, not ours",
   r.returncode == 7 and "argv=['7', '--dry-run']" in r.stdout, (r.returncode, r.stdout))
r = tares(["hook", "ship"], stdin="e", extra={"CLAUDE_PLUGIN_ROOT": str(fake)})
ck("the plugin folder comes from CLAUDE_PLUGIN_ROOT, a path with a space included", r.returncode == 0 and "stdin=e" in r.stdout, (r.returncode, r.stdout, r.stderr))
r = tares(["hook", "ship", "--plugin-root", str(T / "nowhere")])
ck("a plugin folder without the script: exit 1 and a message naming it",
   r.returncode == 1 and "no script" in r.stderr and "nowhere" in r.stderr, (r.returncode, r.stderr))
r = tares(["hook", "ship", "--plugin-root"])
ck("--plugin-root without a folder: exit 2", r.returncode == 2 and "needs a folder" in r.stderr, (r.returncode, r.stderr))

print("the real shipper")
payload = json.dumps({"hook_event_name": "Notification", "session_id": "s-hooks-1", "message": "hello"})
r = tares(["hook", "ship"], stdin=payload, extra={"CLAUDE_PLUGIN_ROOT": str(ROOT / "claude-plugin"),
                                                    "TARES_FACTORY_URL": "http://127.0.0.1:9", "TARESD_URL": "http://127.0.0.1:9"})
ck("`tares hook ship` with a sample payload exits 0 and prints nothing", r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout, r.stderr[-300:]))
r = tares(["hook", "ship"], stdin="not json", extra={"CLAUDE_PLUGIN_ROOT": str(ROOT / "claude-plugin"),
                                                      "TARES_FACTORY_URL": "http://127.0.0.1:9", "TARESD_URL": "http://127.0.0.1:9"})
ck("...and so does a payload that is not JSON", r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout, r.stderr[-300:]))

print("a shell hook")
if shutil.which("bash"):
    r = tares(["hook", "guard", "--plugin-root", str(fake), "a", "b"], stdin="payload")
    ck("guard runs through bash with stdin and arguments, and its exit code is ours",
       r.returncode == 3 and r.stdout.splitlines() == ["payload", "args=a b"],
       (r.returncode, r.stdout, r.stderr))
    # the real guard: a builder merging is allowed, a reviewer merging is refused, through the entry point
    for role, want in (("builder", "allow"), ("reviewer", "deny")):
        if not shutil.which("jq"):
            print("  skip the real guard: jq is not installed")
            break
        msg = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 12 --squash"}})
        r = tares(["hook", "guard", "--plugin-root", str(ROOT / "claude-plugin")], stdin=msg,
                  extra={"FACTORY_ROLE": role, "FACTORY_STATION": "test", "FACTORY_HOME": "/nonexistent"})
        got = "deny" if '"deny"' in r.stdout else "allow"
        ck(f"the real guard through `tares hook guard`: a {role} merging is {"allowed" if want == "allow" else "refused"}", got == want and r.returncode == 0, (r.returncode, r.stdout, r.stderr[-200:]))
else:
    print("  skip: bash is not on PATH")

print("compat")
r = tares(["hook", "compat"], extra={"CLAUDE_PLUGIN_ROOT": str(T / "nowhere")})
ck("`tares hook compat` still says nothing and exits 0 when there is nothing to say", r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout))

shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
