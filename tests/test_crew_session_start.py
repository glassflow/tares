"""The SessionStart hook in Python (claude-plugin/hooks/session_start.py), the twin of session-start.sh.

Checks: where bash and jq are here, the Python hook says the same as the bash script for every kind of
session (a plain one, a person's bound session, a contributor, each station role, a helper of each
parent, a halted crew) and every kind of input (each source, none, null, a number, junk, invalid
UTF-8); the compat line is bounded and tidy (TR-972 F2, F3): a `tares` on PATH that prints two lines
and noise gives one line and no noise, one that hangs costs about two seconds and drops only that line,
one that fails or is missing drops it too; run in process under `tares hook session-start` it uses the
installed tares's own check; the output is one UTF-8 JSON line; the script stays stdlib-only and 3.9-safe.

Run: .venv/bin/python tests/test_crew_session_start.py
"""
import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def short(x, n=300):
    return str(x)[:n]


WINDOWS = os.name == "nt"
PY = sys.executable
SCRIPT = ROOT / "claude-plugin" / "hooks" / "session_start.py"
BASH_HOOK = ROOT / "claude-plugin" / "hooks" / "session-start.sh"
T = Path(tempfile.mkdtemp(prefix="crew_session_start_"))

# a clean base: nothing of the machine's own factory or Tares settings leaks in
CLEAN = {k: v for k, v in os.environ.items()
         if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_")) and k not in ("PYTHONPATH", "PYTHONHOME")}

bare = T / "bare"          # a plugin root with no min_tares: the compat check has nothing to say
bare.mkdir()
far = T / "far"            # a plugin that wants a tares that does not exist yet
far.mkdir()
(far / "min_tares.txt").write_text("999.0.0\n", encoding="utf-8")
for _plugin in (bare, far):  # a plugin root `tares hook` can run the hook from
    (_plugin / "hooks").mkdir()
    shutil.copy(SCRIPT, _plugin / "hooks" / "session_start.py")


def run_py(stdin, extra=None, path=None, script=SCRIPT, python=None, flags=()):
    env = dict(CLEAN, CLAUDE_PLUGIN_ROOT=str(bare), PYTHONUTF8="1")
    if path is not None:
        env["PATH"] = path
    env.update(extra or {})
    data = stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")
    return subprocess.run([python or PY, *flags, str(script)], input=data, capture_output=True, env=env,
                          cwd=str(T), timeout=60)


def ctx_of(stdout: bytes):
    """The additionalContext of a hook's output, or None when it printed nothing or not JSON."""
    try:
        return json.loads(stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return None


# ---------- parity with session-start.sh ----------
print("parity with session-start.sh")
if WINDOWS or not shutil.which("bash") or not shutil.which("jq"):
    print("  skip  no bash or jq here: the parity checks")
else:
    nobin = T / "nobin"  # bash, jq and the system tools, and no tares
    nobin.mkdir()
    for tool in ("bash", "jq"):
        (nobin / tool).symlink_to(str(shutil.which(tool)))
    syspath = os.pathsep.join([str(nobin), "/usr/bin", "/bin"])
    halted = T / "halted"
    halted.mkdir()
    (halted / "HALT").write_text(json.dumps({"reason": "deploy freeze \u00e9\u540d", "at": "now"}), encoding="utf-8")
    junk_halt = T / "junkhalt"
    junk_halt.mkdir()
    (junk_halt / "HALT").write_text("not json at all", encoding="utf-8")
    num_halt = T / "numhalt"
    num_halt.mkdir()
    (num_halt / "HALT").write_text('{"reason": 42}', encoding="utf-8")
    null_halt = T / "nullhalt"
    null_halt.mkdir()
    (null_halt / "HALT").write_text('{"reason": null}', encoding="utf-8")
    clear_home = T / "clearhome"
    clear_home.mkdir()

    STATION = {"FACTORY_ROLE": "builder", "FACTORY_STATION": "shop-build-m1", "FACTORY_PARENT": "crew-orchestrator",
               "TARES_PROJECT": "uc_123", "TARES_PROJECT_NAME": "Shop \u00e9\u540d", "FACTORY_REPO": "/r/shop",
               "TARES_FACTORY_URL": "http://127.0.0.1:8788"}
    envs = {
        "a plain session": {},
        "a plain session with a Tares address": {"TARES_FACTORY_URL": "http://x"},
        "a bound session": {"TARES_PROJECT": "uc_1"},
        "a bound session with a name and an address": {"TARES_PROJECT": "uc_1", "TARES_PROJECT_NAME": "Shop \u00e9",
                                                       "TARES_FACTORY_URL": "http://x:1"},
        "a bound session taken over": {"TARES_PROJECT": "uc_1", "TARES_SESSION_TAKEOVER": "1"},
        "a bound session with an empty takeover": {"TARES_PROJECT": "uc_1", "TARES_SESSION_TAKEOVER": ""},
        "a contributor": {"TARES_PROJECT": "uc_1", "TARES_SESSION_KIND": "contributor", "TARES_FACTORY_URL": "http://x"},
        "a contributor with no address": {"TARES_PROJECT": "uc_1", "TARES_SESSION_KIND": "contributor"},
        "a session kind that is not contributor": {"TARES_PROJECT": "uc_1", "TARES_SESSION_KIND": "other"},
        "a builder": STATION,
        "a builder with no name or repo": {"FACTORY_ROLE": "builder", "TARES_PROJECT": "uc_9"},
        "an orchestrator": {"FACTORY_ROLE": "orchestrator", "FACTORY_STATION": "crew-orchestrator"},
        "a reviewer with no station name": {"FACTORY_ROLE": "reviewer"},
        "a station with an address": {"FACTORY_ROLE": "releaser", "FACTORY_STATION": "r", "TARES_FACTORY_URL": "http://t"},
        "a helper of the orchestrator": {"FACTORY_ROLE": "helper", "FACTORY_STATION": "h", "FACTORY_PARENT": "crew-orchestrator"},
        "a helper of a reviewer": {"FACTORY_ROLE": "helper", "FACTORY_STATION": "h", "FACTORY_PARENT": "crew-reviewer-2"},
        "a helper of a releaser in a named crew": {"FACTORY_ROLE": "helper", "FACTORY_CREW": "acme", "FACTORY_STATION": "h",
                                                   "FACTORY_PARENT": "acme-releaser"},
        "a helper of a builder": {"FACTORY_ROLE": "helper", "FACTORY_STATION": "h", "FACTORY_PARENT": "shop-build-m1"},
        "a helper of a builder in a named crew": {"FACTORY_ROLE": "helper", "FACTORY_CREW": "acme", "FACTORY_STATION": "h",
                                                  "FACTORY_PARENT": "acme-shop-build-m2"},
        "a helper whose parent is in another crew": {"FACTORY_ROLE": "helper", "FACTORY_CREW": "acme", "FACTORY_STATION": "h",
                                                     "FACTORY_PARENT": "crew-orchestrator"},
        "a helper with no parent": {"FACTORY_ROLE": "helper", "FACTORY_STATION": "h"},
        "a station in a halted crew": dict(STATION, FACTORY_HOME=str(halted)),
        # (a HALT file that is not JSON is left out: jq fails on it and the bash hook dies with exit 5)
        "a station, HALT with a number reason": dict(STATION, FACTORY_HOME=str(num_halt)),
        "a station, HALT with a null reason": dict(STATION, FACTORY_HOME=str(null_halt)),
        "a station with a crew home and no HALT": dict(STATION, FACTORY_HOME=str(clear_home)),
        "a station with a crew home that is missing": dict(STATION, FACTORY_HOME=str(T / "nowhere")),
    }
    # (no empty input: jq prints nothing for it and the bash hook then took it for a resume)
    sources = ['{"source": "startup"}', '{"source": "resume"}', '{"source": "compact"}', '{"source": "clear"}',
               "{}", '{"source": null}', '{"source": false}', '{"source": ""}', '{"source": 7}',
               "[]", "null", "junk"]
    # the bash hook wrote "compactd" and "cleard"; the Python one says "compacted" and "cleared"
    fix = (("was compactd:", "was compacted:"), ("was cleard:", "was cleared:"))
    bad = []
    n = 0
    for label, extra in envs.items():
        for src in sources:
            n += 1
            base = dict(CLEAN, PATH=syspath, CLAUDE_PLUGIN_ROOT=str(bare))
            base.update(extra)
            sh = subprocess.run(["bash", str(BASH_HOOK)], input=src.encode(), capture_output=True, env=base, cwd=str(T), timeout=60)
            py = subprocess.run([PY, str(SCRIPT)], input=src.encode(), capture_output=True, env=dict(base, PYTHONUTF8="1"),
                                cwd=str(T), timeout=60)
            a, b = ctx_of(sh.stdout), ctx_of(py.stdout)
            for old, new in fix:
                a = a.replace(old, new) if a else a
            if sh.returncode != 0 or py.returncode != 0 or a != b or (a is None) != (not py.stdout.strip()) \
                    or (a is not None and "hookEventName" not in py.stdout.decode()):
                bad.append((label, src, sh.returncode, py.returncode, a, b, py.stderr[-200:]))
    ck(f"the Python hook says what the bash hook says, {len(envs)} kinds of session x {len(sources)} inputs ({n} runs)",
       not bad, short(bad[:2], 900))
    # silence is silence: nothing at all on stdout for a plain session
    py = run_py('{"source": "startup"}')
    ck("a plain session hears nothing: no output, exit 0, no noise", py.returncode == 0 and py.stdout == b"" and py.stderr == b"",
       short((py.returncode, py.stdout, py.stderr)))

# ---------- the output ----------
print("output")
r = run_py('{"source": "resume"}', {"FACTORY_ROLE": "builder", "FACTORY_STATION": "s-\u540d", "TARES_PROJECT": "uc_1",
                                      "TARES_PROJECT_NAME": "Shop \u00e9\u540d"})
text = r.stdout.decode("utf-8", errors="replace")
obj = json.loads(text) if text.strip() else {}
ck("one JSON line, UTF-8, ending in a newline", r.returncode == 0 and text.endswith("\n") and text.count("\n") == 1, short(r.stdout))
ck("... shaped as a SessionStart hook answer",
   list(obj) == ["hookSpecificOutput"] and obj["hookSpecificOutput"]["hookEventName"] == "SessionStart", short(obj))
c = ctx_of(r.stdout) or ""
ck("... names the station, the project and the role's skill, with non-ASCII intact",
   "`s-\u540d`" in c and "`Shop \u00e9\u540d`" in c and "`station-builder`" in c, short(c))
ck("... a resumed station is told to reload its skills and re-arm its schedule",
   "This session was resumed: load both skills again" in c and "re-arm your schedule" in c, short(c))
r = run_py(b'{"source": "compact", "x": "\xff\xfe"}', {"FACTORY_ROLE": "orchestrator", "FACTORY_STATION": "crew-orchestrator"})
ck("invalid UTF-8 in the input does not fail the hook", r.returncode == 0 and "was compacted:" in (ctx_of(r.stdout) or ""),
   short((r.returncode, r.stdout, r.stderr[-200:])))
r = run_py("", {"FACTORY_ROLE": "orchestrator", "FACTORY_STATION": "crew-orchestrator"})
ck("no input at all is a startup", r.returncode == 0 and "was " not in (ctx_of(r.stdout) or "").split("Your rules")[-1],
   short((r.returncode, r.stdout, r.stderr[-200:])))
halted_dir = T / "h2"
halted_dir.mkdir()
(halted_dir / "HALT").write_text(json.dumps({"reason": "freeze"}), encoding="utf-8")
r = run_py("{}", {"FACTORY_ROLE": "builder", "FACTORY_STATION": "b", "FACTORY_HOME": str(halted_dir)})
ck("a halted crew says so, with the reason", "The crew is halted (freeze): start nothing new until it is resumed." in (ctx_of(r.stdout) or ""),
   short(r.stdout))

(T / "junkhalt").mkdir(exist_ok=True)
(T / "junkhalt" / "HALT").write_text("not json at all", encoding="utf-8")
r = run_py("{}", {"FACTORY_ROLE": "builder", "FACTORY_STATION": "b", "FACTORY_HOME": str(T / "junkhalt")})
ck("a HALT file that is not JSON still halts the station, with no reason (the bash hook died here)",
   r.returncode == 0 and "The crew is halted (): start nothing new" in (ctx_of(r.stdout) or ""), short((r.returncode, r.stdout, r.stderr[-200:])))

# ---------- the compat line ----------
print("compat line")
SRC = SCRIPT.read_text(encoding="utf-8")
spec = importlib.util.spec_from_file_location("session_start_under_test", str(SCRIPT))
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

TWO = "This Tares plugin needs tares 9.9.9 or newer, but tares 1.0.0 is installed. Upgrade it with `uv tool upgrade tares`."


def fake_tares(folder: Path, kind: str) -> Path:
    """A `tares` on PATH: 'two' prints two lines and noise on stderr, 'slow' hangs, 'fail' exits 1, 'quiet' prints nothing."""
    folder.mkdir()
    if WINDOWS:
        body = {"two": f"@echo off\r\necho {TWO}\r\necho second line\r\necho noise 1>&2\r\n",
                "slow": "@echo off\r\nping -n 40 127.0.0.1 >nul\r\necho late\r\n",
                "fail": "@echo off\r\necho junk\r\necho oops 1>&2\r\nexit /b 1\r\n",
                "quiet": "@echo off\r\n"}[kind]
        (folder / "tares.cmd").write_bytes(body.encode("ascii"))
    else:
        body = {"two": f"#!/bin/sh\necho '{TWO}'\necho 'second line'\necho noise >&2\n",
                "slow": "#!/bin/sh\nsleep 40\necho late\n",
                "fail": "#!/bin/sh\necho junk\necho oops >&2\nexit 1\n",
                "quiet": "#!/bin/sh\n"}[kind]
        f = folder / "tares"
        f.write_bytes(body.encode("ascii"))
        f.chmod(0o755)
    return folder


# The script copied out of the repo and run with -S -I: no site-packages, so tares cannot be imported and
# the hook has to ask the `tares` on PATH, the way it does on a machine where the plugin runs alone.
alone = T / "alone"
alone.mkdir()
shutil.copy(SCRIPT, alone / "session_start.py")
base_path = os.environ.get("PATH", "")
sysdirs = os.pathsep.join(["/usr/bin", "/bin"]) if not WINDOWS else os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"


def run_alone(bins, stdin='{"source": "startup"}', extra=None, root=far):
    env = {"CLAUDE_PLUGIN_ROOT": str(root), "PATH": os.pathsep.join([str(bins), sysdirs]), "PYTHONUTF8": "1"}
    if WINDOWS:
        env.update(SystemRoot=os.environ.get("SystemRoot", r"C:\Windows"), PATHEXT=os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"))
    env.update(extra or {})
    t0 = time.time()
    r = subprocess.run([PY, "-S", "-I", str(alone / "session_start.py")], input=stdin.encode(), capture_output=True,
                       env=env, cwd=str(T), timeout=60)
    return r, time.time() - t0


r, took = run_alone(fake_tares(T / "bin-two", "two"))
c = ctx_of(r.stdout)
ck("a tares that prints two lines and noise: one line is used, in a session that would hear nothing", c == TWO, short((c, r.stdout)))
ck("... and the hook's own stderr stays empty", r.stderr == b"" and r.returncode == 0, short((r.returncode, r.stderr)))
r, took = run_alone(T / "bin-two", extra={"FACTORY_ROLE": "builder", "FACTORY_STATION": "b"})
c = ctx_of(r.stdout) or ""
ck("... after what a station is told, in the same sentence block", c.startswith("You are the builder station `b`") and c.endswith(" " + TWO)
   and "second line" not in c and r.stderr == b"", short((c, r.stderr)))
r, took = run_alone(fake_tares(T / "bin-slow", "slow"), extra={"FACTORY_ROLE": "builder", "FACTORY_STATION": "b"})
c = ctx_of(r.stdout) or ""
ck("a tares that hangs costs about two seconds and drops only the compat line",
   took < 12 and c.startswith("You are the builder station `b`") and "late" not in c and "needs tares" not in c and r.returncode == 0,
   short((took, c, r.stderr[-200:])))
r, took = run_alone(T / "bin-slow")
ck("... in a session that would hear nothing: silence, exit 0", took < 12 and r.stdout == b"" and r.returncode == 0, short((took, r.stdout)))
r, took = run_alone(fake_tares(T / "bin-fail", "fail"))
ck("a tares that fails and prints junk: nothing, and the hook does not fail",
   r.returncode == 0 and r.stderr == b"" and r.stdout == b"", short((r.returncode, r.stdout, r.stderr)))
r, took = run_alone(fake_tares(T / "bin-quiet", "quiet"))
ck("a tares that prints nothing: silence", r.returncode == 0 and r.stdout == b"" and r.stderr == b"", short(r.stdout))
empty = T / "bin-none"
empty.mkdir()
r, took = run_alone(empty, extra={"FACTORY_ROLE": "builder", "FACTORY_STATION": "b"})
ck("no tares on PATH: a station is told what it is told, no compat line, the hook does not fail",
   r.returncode == 0 and (ctx_of(r.stdout) or "").startswith("You are the builder station") and "needs tares" not in (ctx_of(r.stdout) or ""),
   short((r.returncode, r.stdout, r.stderr[-200:])))
ck("the bound is two seconds", mod.COMPAT_SECONDS == 2.0, mod.COMPAT_SECONDS)

# in process: under `tares hook session-start` the installed tares's own check answers, with no subprocess
HOOK_RUN = ("import sys; from tares.crew import hooks; sys.exit(hooks.run(['session-start'] + sys.argv[1:]))")


def run_hook(root, stdin='{"source": "startup"}', extra=None, args=()):
    env = dict(CLEAN, PYTHONPATH=str(ROOT), PYTHONUTF8="1", PATH=sysdirs if not WINDOWS else os.environ.get("PATH", ""))
    if root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(root)
    env.update(extra or {})
    return subprocess.run([PY, "-c", HOOK_RUN, *args], input=stdin.encode(), capture_output=True, env=env, cwd=str(T), timeout=60)


r = run_hook(far)
ck("`tares hook session-start` with a plugin that wants tares 999.0.0: the line, from the installed check",
   r.returncode == 0 and (ctx_of(r.stdout) or "").startswith("This Tares plugin needs tares 999.0.0 or newer") and r.stderr == b"",
   short((r.returncode, r.stdout, r.stderr[-300:])))
r = run_hook(bare)
ck("... with a plugin that names no minimum: nothing", r.returncode == 0 and r.stdout == b"" and r.stderr == b"", short((r.stdout, r.stderr[-300:])))
r = run_hook(bare, extra={"FACTORY_ROLE": "reviewer", "FACTORY_STATION": "crew-reviewer"})
ck("... a station is still told who it is", "You are the reviewer station `crew-reviewer`" in (ctx_of(r.stdout) or ""), short(r.stdout))
dry = run_hook(bare, args=("--dry-run",))
ck("`tares hook session-start --dry-run` names the Python hook, and it exists",
   dry.returncode == 0 and dry.stdout.decode().strip().endswith("hooks/session_start.py"), short((dry.returncode, dry.stdout, dry.stderr[-200:])))

# ---------- the script itself ----------
print("the script")
tree = ast.parse(SRC, feature_version=(3, 9))
tops = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
names = sorted({a.name.split(".")[0] for n in tops if isinstance(n, ast.Import) for a in n.names}
               | {n.module.split(".")[0] for n in tops if isinstance(n, ast.ImportFrom) and n.module})
ck("it imports only the standard library at the top", set(names) <= {"__future__", "json", "os", "shutil", "subprocess", "sys"}, str(names))
ck("it does not import tares at the top (the import is inside compat_line, where its failure is caught)",
   "tares" not in names and "from tares.crew.version import check_plugin_compat" in SRC)
ck("it has no stray CR (a CRLF checkout is fine) and no em dash", b"\r" not in SCRIPT.read_bytes().replace(b"\r\n", b"") and "\u2014" not in SRC)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
