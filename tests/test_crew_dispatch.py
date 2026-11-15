"""`tares factory <cmd>`: which Python function runs a command, and what is printed when there is none (tares/crew/dispatch.py).

Checks: the routing table (the longest key wins); that there is no engine switch any more (TR-975):
TARES_FACTORY_ENGINE, TARES_FACTORY_WATCH and an "engine" in crew.json change nothing; the help, an unknown
command and a bad `crew` subcommand (their text and exit codes: 0 for help, 1 for the others); UTF-8 forced
before a command; that the dispatch path imports no duckdb, fastapi, tares.daemon or tares.store; that
`python -m tares.crew` takes the same routes; that tares.cli._factory really calls the dispatcher, end to
end through tares.cli.main with a throwaway crew; and the test hook TARES_PLUGIN_DIR (the plugin folder
named is the one used, read at call time; unset or empty changes nothing; a folder without hooks/hooks.json
is an error, not a fall back).

Run: .venv/bin/python tests/test_crew_dispatch.py
"""
import ast
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# a regression in the TARES_KEYRING gate must fail this test, never reach a person's OS keychain
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
try:
    import keyring  # noqa: E402
    import keyring.backends.fail  # noqa: E402
    keyring.set_keyring(keyring.backends.fail.Keyring())
except ImportError:
    pass

from _crew_fake import fake_crew_env  # noqa: E402
from tares.crew import dispatch, usage  # noqa: E402


P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def short(x, n=300):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "..."


T = Path(tempfile.mkdtemp(prefix="tares crew dispatch "))
# A person's real ~/factory must not change what these checks do: the checks that pass an env of {} read the
# home of this process.
(T / "home").mkdir()
os.environ["HOME"] = os.environ["USERPROFILE"] = str(T / "home")
os.environ.pop("FACTORY_CREW_HOME", None)
# A bare environment is enough for Python on Mac and Linux. On Windows the interpreter will not start
# without SYSTEMROOT ("failed to get random numbers"), so a probe's environment carries it.
# a bare `-I` probe on Windows still needs the system root and a home (Path.home() reads USERPROFILE) to start
SYS_ENV = ({k: os.environ[k] for k in ("SYSTEMROOT", "USERPROFILE", "HOME", "TEMP", "TMP") if k in os.environ}
           if os.name == "nt" else {})
ENGINE = "TARES_FACTORY_ENGINE"  # the switch the bash crew had; removed in TR-975, so setting it must change nothing
UNPORTED = "unported-probe"  # a command the table does not have
USAGE = usage.USAGE
CREW_LINE = ("tares factory: tares factory crew takes up, down, status, desk, attach, logs, watch, halt, resume, "
             "freeze, unfreeze, update or restart\n")

calls = []


def fake_a(argv, env):
    calls.append(("a", list(argv)))
    return 0


def fake_sub(argv, env):
    calls.append(("sub", list(argv)))
    return 7


def fake_none(argv, env):
    calls.append(("none", list(argv)))


@contextlib.contextmanager
def ported(table):
    """PORTED holds exactly this table for the block, whatever later tickets add to it."""
    saved = dict(dispatch.PORTED)
    dispatch.PORTED.clear()
    dispatch.PORTED.update(table)
    try:
        yield
    finally:
        dispatch.PORTED.clear()
        dispatch.PORTED.update(saved)


TABLE = {("zz",): fake_a, ("zz", "sub"): fake_sub, ("zn",): fake_none}


def call_run(argv, env=None):
    """Run dispatch.run in-process. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            dispatch.run(argv, {} if env is None else env)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ---------- the routing table ----------
print("routing")
with ported(TABLE):
    ck("a command that is in the table is found", dispatch.find(["zz"]) is fake_a)
    ck("a command that is not in the table is not found", dispatch.find([UNPORTED]) is None)
    ck("no arguments is not found (the help is printed)", dispatch.find([]) is None)
    ck("extra arguments after a key still find it", dispatch.find(["zz", "--help"]) is fake_a)
    ck("the two-word key wins over the one-word key", dispatch.find(["zz", "sub", "x"]) is fake_sub)
    ck("the one-word key serves another subcommand", dispatch.find(["zz", "other"]) is fake_a)
    ck("a two-word key does not match its first word alone", dispatch.find(["zy"]) is None)
    ck("the subcommand alone is not a key", dispatch.find(["sub"]) is None)
    ck("a key is matched from the front only", dispatch.find(["x", "zz"]) is None)
with ported({("zz", "sub"): fake_sub}):
    ck("with only the two-word key, the command word alone is not found", dispatch.find(["zz"]) is None)
    ck("...and another subcommand is not found", dispatch.find(["zz", "other"]) is None)
ck("PORTED maps keys to functions", all(isinstance(k, tuple) and callable(v) for k, v in dispatch.PORTED.items()))
ck("the hidden _mcp-url is in the table (TR-1155)", dispatch.find(["_mcp-url"]) is not None)
ck("every command the help names is in the table",
   all(dispatch.find(w.split()) is not None for w in (
       "crew up", "crew status", "crew attach", "crew desk", "crew logs", "crew watch", "crew update", "crew restart",
       "crew halt", "crew resume", "crew freeze", "crew unfreeze", "crew down", "spec", "session", "projects", "finish",
       "restart", "context", "plan", "doctor", "settings", "roster", "spawn", "revive", "tidy", "retire", "heavy",
       "status", "desk", "attach", "logs", "watch", "halt", "resume", "down")),
   short([k for k in dispatch.PORTED]))

# ---------- no engine switch (TR-975) ----------
print("no engine switch")
SWITCH_NAMES = ("ENGINE_VAR", "ENGINES", "WATCH_VAR", "route", "engine_setting", "bash_exe", "no_bash_reason",
                "is_windows", "command_name")
ck("dispatch has none of the engine switch's names", [n for n in SWITCH_NAMES if hasattr(dispatch, n)] == [],
   short([n for n in SWITCH_NAMES if hasattr(dispatch, n)]))
src_dispatch = (ROOT / "tares" / "crew" / "dispatch.py").read_text(encoding="utf-8")
ck("dispatch.py does not read the engine variable, the watch variable or os.execve",
   not any(w in src_dispatch for w in ("TARES_FACTORY_ENGINE", "TARES_FACTORY_WATCH", "execve")))
ck("run takes (argv, env) and no script", list(inspect.signature(dispatch.run).parameters) == ["argv", "env"],
   short(inspect.signature(dispatch.run)))
with ported(TABLE):
    for value in ("python", "bash", "perl", "", " bash"):
        calls.clear()
        code, out, err = call_run(["zz", "a"], {ENGINE: value})
        ck(f"{ENGINE}={value!r} changes nothing: a command runs in Python", calls == [("a", ["zz", "a"])] and code == 0 and err == "",
           short((calls, code, err)))
        code, out, err = call_run([UNPORTED], {ENGINE: value})
        ck(f"...and an unknown command still prints the help and exits 1", code == 1 and out == USAGE and err == "", short((code, err)))
    calls.clear()
    code, out, err = call_run(["zz"], {"TARES_FACTORY_WATCH": "bash"})
    ck("TARES_FACTORY_WATCH=bash changes nothing either", calls == [("a", ["zz"])] and code == 0)


def crew_json_env(text, name="cj"):
    """An env whose crew home holds a crew.json with this text (None: no file)."""
    home = T / name
    home.mkdir(parents=True, exist_ok=True)
    f = home / "crew.json"
    f.unlink(missing_ok=True)
    if text is not None:
        f.write_text(text, encoding="utf-8")
    return {"FACTORY_CREW_HOME": str(home)}


with ported(TABLE):
    for text in ('{"engine": "bash"}', '{"engine": "python"}', '{"engine": "perl"}', '{"engine": 3}', "not json"):
        calls.clear()
        code, out, err = call_run(["zz"], crew_json_env(text))
        ck(f"an \"engine\" in crew.json ({text}) is ignored: the command runs in Python", calls == [("a", ["zz"])] and code == 0 and err == "",
           short((calls, code, err)))
    code, out, err = call_run([UNPORTED], crew_json_env('{"engine": "bash"}'))
    ck("...and an unknown command with \"engine\": \"bash\" still prints the help, exit 1", code == 1 and out == USAGE, short((code, err)))
ck("the old fallback variable is not read", "TARES_FACTORY_BASH_FALLBACK" not in
   src_dispatch + (ROOT / "tares" / "cli.py").read_text(encoding="utf-8"))

# ---------- help, an unknown command, a bad crew subcommand ----------
print("help and unknown commands")
with ported(TABLE):
    for argv in ([], [""], ["help"], ["-h"], ["--help"], ["help", "extra"]):
        code, out, err = call_run(argv)
        ck(f"{argv}: prints the help on stdout and exits 0", code == 0 and out == USAGE and err == "", short((code, out[:60], err)))
    for argv in ([UNPORTED], ["--bogus"], ["Help"], [UNPORTED, "--json"], ["-x"]):
        code, out, err = call_run(argv)
        ck(f"{argv}: prints the help on stdout and exits 1", code == 1 and out == USAGE and err == "", short((code, out[:60], err)))
    for argv in (["crew", "bogus"], ["crew", ""], ["crew", "--help"], ["crew", "help"]):
        code, out, err = call_run(argv)
        ck(f"{argv}: the crew-takes line on stderr, nothing on stdout, exit 1", code == 1 and out == "" and err == CREW_LINE, short((code, out[:60], err)))
    ck("the crew-takes line is the dispatcher's CREW_TAKES", CREW_LINE == f"{dispatch.ME}: {dispatch.ME} crew takes {dispatch.CREW_TAKES}\n")
    calls.clear()
    with ported({("crew", "status"): fake_a}):
        code, out, err = call_run(["crew"])
    ck("a bare `crew` is `crew status`", calls == [("a", ["crew", "status"])] and code == 0, short((calls, code, err)))
ck("the help names `tares factory` on every command line", re.search(r"(?m)^\s+factory ", USAGE) is None
   and USAGE.startswith("tares factory: one crew"), short(re.findall(r"(?m)^\s+factory .*", USAGE)))
ck("the help has no em dash", "\u2014" not in USAGE)
ck("the help ends with a newline and has the crew home line", USAGE.endswith("\n") and "The crew home is ~/factory" in USAGE)
ck("the help names every ported top-level command it teaches",
   all(f"tares factory {w}" in USAGE for w in ("crew up", "crew status", "crew watch", "crew update", "crew restart", "crew halt", "crew resume",
                                              "crew freeze", "crew unfreeze", "crew down", "spec", "session", "projects", "finish", "restart",
                                              "context", "plan", "doctor", "settings", "roster", "spawn builder", "spawn helper", "revive",
                                              "tidy", "retire", "heavy")))

# ---------- what a Python command gets and returns ----------
print("python route")
with ported(TABLE):
    calls.clear()
    env = {"A": "1"}
    seen = []
    TABLE_SEEN = {("zs",): lambda argv, e: seen.append((argv, e)) or 0}
    with ported(TABLE_SEEN):
        call_run(["zs", "x", "--y"], env)
    ck("the function gets argv (the command words included) and the env dict", seen == [(["zs", "x", "--y"], env)] and seen[0][1] is env,
       short(seen))
    ck("its return value is the exit code", call_run(["zz", "sub"], {})[0] == 7)
    ck("zero is exit 0", call_run(["zz"], {})[0] == 0)
    ck("None is exit 0", call_run(["zn"], {})[0] == 0)

# ---------- UTF-8 ----------
print("utf-8")


class Stream(io.StringIO):
    def __init__(self, log, name):
        super().__init__()
        self.log, self.name = log, name

    def reconfigure(self, **kw):
        self.log.append((self.name, kw))


class NoReconfigure:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


log = []
order = []
with ported({("zu",): lambda argv, env: order.append(list(log)) or 0}):
    with mock.patch.object(sys, "stdout", Stream(log, "out")), mock.patch.object(sys, "stderr", Stream(log, "err")):
        try:
            dispatch.run(["zu"], {})
        except SystemExit:
            pass
ck("stdout and stderr are set to UTF-8 before the Python command runs",
   order and sorted(n for n, kw in order[0] if kw.get("encoding") == "utf-8") == ["err", "out"], short(order))
ck("...with errors replaced, not raised", all(kw.get("errors") == "replace" for _, kw in log), short(log))
log.clear()
with mock.patch.object(sys, "stdout", Stream(log, "out")), mock.patch.object(sys, "stderr", Stream(log, "err")):
    try:
        dispatch.run([UNPORTED], {})
    except SystemExit:
        pass
ck("the help of an unknown command is written to UTF-8 streams too", sorted(n for n, kw in log if kw.get("encoding") == "utf-8") == ["err", "out"],
   short(log))
ns = NoReconfigure()
with ported({("zu",): lambda argv, env: print("é名") or 0}), mock.patch.object(sys, "stdout", ns), mock.patch.object(sys, "stderr", NoReconfigure()):
    try:
        dispatch.run(["zu"], {})
        code = "returned"
    except SystemExit as e:
        code = e.code
ck("a stream without reconfigure is left alone, the command still runs", ns.text == "é名\n" and code == 0, short((ns.text, code)))
ns = NoReconfigure()
with mock.patch.object(sys, "stdout", ns), mock.patch.object(sys, "stderr", NoReconfigure()):
    try:
        dispatch.run(["help"], {})
        code = "returned"
    except SystemExit as e:
        code = e.code
ck("...and so is the help", ns.text == USAGE and code == 0, short((ns.text[:60], code)))

# a console in another code page: the Python command's output is still UTF-8
UTF_PROBE = (
    "import sys\nsys.path.insert(0, %r)\n"
    "sys.stdout.reconfigure(encoding='latin-1', errors='replace'); sys.stderr.reconfigure(encoding='latin-1', errors='replace')\n"
    "from tares.crew import dispatch\n"
    "dispatch.PORTED[('zu',)] = lambda argv, env: (print('é名 out'), print('é名 err', file=sys.stderr), 0)[-1]\n"
    "dispatch.run(['zu'], {})\n"
) % str(ROOT)
r = subprocess.run([sys.executable, "-I", "-c", UTF_PROBE], capture_output=True, cwd=str(T))
# bytes, so the check sees the encoding; Windows ends a printed line with CRLF, which is not what this check is about
ck("in a latin-1 console a Python command still writes UTF-8 to stdout", r.stdout.decode("utf-8", "replace").replace("\r\n", "\n") == "é名 out\n" and r.returncode == 0,
   short((r.stdout, r.stderr[-200:])))
ck("...and to stderr", r.stderr.decode("utf-8", "replace").replace("\r\n", "\n") == "é名 err\n", short(r.stderr))

# ---------- no heavy imports on the dispatch path ----------
print("light path")
HEAVY = ("duckdb", "fastapi", "tares.daemon", "tares.store")
LIGHT_PROBE = (
    "import json, os, sys\nsys.path.insert(0, %r)\n"
    "def heavy():\n"
    "    return sorted(m for m in sys.modules if m in %r or m.split('.')[0] in ('duckdb', 'fastapi'))\n"
    "def python_cmd(argv, env):\n"
    "    return 0\n"
    "from tares.crew import dispatch\n"
    "dispatch.PORTED[('zz',)] = python_cmd\n"
    "import tares.cli\n"
    "sys.argv = ['tares', 'factory'] + sys.argv[1:]\n"
    "code = None\n"
    "try:\n"
    "    tares.cli.main()\n"
    "except SystemExit as e:\n"
    "    code = e.code\n"
    "sys.stderr.write(json.dumps({'code': code, 'heavy': heavy(), 'tares': sorted(m for m in sys.modules if m.startswith('tares'))}))\n"
) % (str(ROOT), HEAVY)
env_light = {**SYS_ENV, "PATH": os.environ.get("PATH", ""), "HOME": str(T), "TARES_HOME": str(T / "th"), "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
for mode, args, want_code in (("python", ["zz"], 0), ("help", ["help"], 0), ("unknown", [UNPORTED], 1)):
    r = subprocess.run([sys.executable, "-I", "-c", LIGHT_PROBE, *args], capture_output=True, text=True, env=env_light, cwd=str(T))
    try:
        info = json.loads(r.stderr[r.stderr.rindex('{"code"'):])
    except Exception:
        info = {}
    ck(f"{mode} route through tares.cli.main runs in a bare interpreter and exits {want_code}", info.get("code") == want_code, short((r.stdout[:80], r.stderr[-300:])))
    ck(f"{mode} route: no duckdb, fastapi, tares.daemon or tares.store was imported", info.get("heavy") == [], short(info.get("heavy")))
    if mode == "python":
        ck("...and the tares modules loaded are the light ones",
           set(info.get("tares", [])) <= {"tares", "tares.cli", "tares.config", "tares.login", "tares.crew", "tares.crew.dispatch",
                                          "tares.crew.errors", "tares.crew.paths", "tares.crew.perms", "tares.crew.state", "tares.crew.locks",
                                          "tares.crew.contract"},
           short(info.get("tares")))
    else:
        ck(f"...{mode} printed the help", r.stdout == USAGE, short(r.stdout[:80]))
bad = []
for f in sorted((ROOT / "tares" / "crew").glob("*.py")):
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [("." * node.level) + (node.module or "")] + [("." * node.level) + (node.module or "") + "." + a.name for a in node.names]
        for n in names:
            base = n.lstrip(".")
            if base.split(".")[0] in ("duckdb", "fastapi") or base in ("daemon", "store") or base.startswith(("tares.daemon", "tares.store", "daemon.", "store.")):
                bad.append((f.name, n))
ck("no module of tares/crew imports duckdb, fastapi, the daemon or the store (not even lazily)", bad == [], short(bad))

# ---------- python -m tares.crew ----------
print("python -m tares.crew")
ENV_M = fake_crew_env(T / "m")
ENV_M["PYTHONPATH"] = str(ROOT)


def mod(args, extra=None, **kw):
    e = dict(ENV_M, **(extra or {}))
    return subprocess.run([sys.executable, "-m", "tares.crew", *args], capture_output=True, text=True, encoding="utf-8", env=e, cwd=str(ROOT), **kw)


r = mod(["help"])
ck("`python -m tares.crew help` prints the help and exits 0", r.returncode == 0 and r.stdout.replace("\r\n", "\n") == USAGE, short((r.returncode, r.stdout[:80], r.stderr[-200:])))
r = mod([UNPORTED])
ck("...an unknown command prints the help and exits 1", r.returncode == 1 and r.stdout.replace("\r\n", "\n") == USAGE, short((r.returncode, r.stdout[:80], r.stderr[-200:])))
r = mod([UNPORTED], {ENGINE: "python"})
ck("...TARES_FACTORY_ENGINE=python does not make it an error (no exit 2)", r.returncode == 1 and r.stdout.replace("\r\n", "\n") == USAGE, short((r.returncode, r.stderr)))
r = mod(["help"], {ENGINE: "nope"})
ck("...and an invalid value is not checked", r.returncode == 0 and r.stderr == "", short((r.returncode, r.stderr)))
r = mod(["crew", "bogus"])
ck("...a bad crew subcommand: the crew-takes line, exit 1", r.returncode == 1 and r.stdout == "" and r.stderr.replace("\r\n", "\n") == CREW_LINE, short((r.returncode, r.stderr)))
MOD_PROBE = (
    "import runpy, sys\nsys.path.insert(0, %r)\nfrom tares.crew import dispatch\n"
    "dispatch.PORTED[('zz',)] = lambda argv, env: (print('python ran', argv, env.get('KEEP')), 5)[-1]\n"
    "sys.argv = ['tares.crew'] + sys.argv[1:]\nrunpy.run_module('tares.crew', run_name='__main__')\n"
) % str(ROOT)
r = subprocess.run([sys.executable, "-I", "-c", MOD_PROBE, "zz", "x"], capture_output=True, text=True, env={**SYS_ENV, "PATH": os.environ.get("PATH", ""), "KEEP": "yes", "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}, cwd=str(T))
ck("`python -m tares.crew` calls a registered Python command with argv and os.environ, and exits with its code",
   r.returncode == 5 and r.stdout == "python ran ['zz', 'x'] yes\n", short((r.returncode, r.stdout, r.stderr[-300:])))

# ---------- tares.cli._factory calls the dispatcher ----------
print("tares.cli._factory")
import tares.cli  # noqa: E402

ENV_F = fake_crew_env(T / "f")
recorded = []
stray_execs = []
with mock.patch.dict(os.environ, ENV_F, clear=True), mock.patch.object(dispatch, "run", lambda *a: recorded.append(a)), \
        mock.patch.object(os, "execve", lambda *a: stray_execs.append(a)), mock.patch.object(sys, "argv", ["tares", "factory", UNPORTED]):
    tares.cli._factory([UNPORTED, "--x"])
ck("_factory does not exec anything itself", stray_execs == [], short(stray_execs))
ck("_factory calls dispatch.run exactly once", len(recorded) == 1, short(recorded))
if recorded:
    ck("...with two arguments: argv and env (no script)", len(recorded[0]) == 2, short(recorded[0]))
    a_argv, a_env = recorded[0][:2]
    ck("...with the argv after `factory`", a_argv == [UNPORTED, "--x"], short(a_argv))
    ck("...with the environment _factory built (the address it resolved)", a_env.get("TARESD_URL") == "http://127.0.0.1:9" and a_env.get("TARES_FACTORY_URL") == "http://127.0.0.1:9"
       and a_env.get("TARES_FACTORY_SOURCE") == "environment" and a_env.get("FACTORY_CREW_HOME") == ENV_F["FACTORY_CREW_HOME"], short(a_env.get("TARES_FACTORY_SOURCE")))
src = (ROOT / "tares" / "cli.py").read_text(encoding="utf-8")
ck("cli.py no longer execs bash itself", "os.execve" not in src, "")
ck("cli.py does not name the old script", "bin/factory" not in src and '"bin"' not in src, "")

FACTORY_PROBE = (
    "import sys\nsys.path.insert(0, %r)\nfrom tares.crew import dispatch\n"
    "dispatch.PORTED[('zz',)] = lambda argv, env: (print('python ran', argv, env['TARES_FACTORY_URL'], env.get('TARES_FACTORY_SOURCE')), 4)[-1]\n"
    "from tares.cli import main\nsys.argv = ['tares', 'factory'] + sys.argv[1:]\nmain()\n"
) % str(ROOT)


def factory(args, extra=None, probe=FACTORY_PROBE):
    e = dict(ENV_F, **(extra or {}))
    e.pop("PYTHONPATH", None)
    e.setdefault("PYTHONIOENCODING", "utf-8")   # a profile folder with a letter outside ASCII is in the messages
    return subprocess.run([sys.executable, "-I", "-c", probe, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, cwd=str(T))


r = factory(["zz", "q"])
ck("end to end: a registered Python command is reached through tares.cli.main and its code is the exit code",
   r.returncode == 4 and r.stdout == "python ran ['zz', 'q'] http://127.0.0.1:9 environment\n", short((r.returncode, r.stdout, r.stderr[-300:])))
r = factory(["zz", "q"], {ENGINE: "bash"})
ck("end to end: TARES_FACTORY_ENGINE=bash changes nothing, the Python command still runs",
   r.returncode == 4 and r.stdout == "python ran ['zz', 'q'] http://127.0.0.1:9 environment\n", short((r.returncode, r.stdout, r.stderr[-300:])))
r = factory([UNPORTED])
ck("end to end: an unknown command prints the help and exits 1", r.returncode == 1 and r.stdout.replace("\r\n", "\n") == USAGE, short((r.returncode, r.stdout[:80], r.stderr[-300:])))
r = factory(["help"])
ck("end to end: help exits 0", r.returncode == 0 and r.stdout.replace("\r\n", "\n") == USAGE, short((r.returncode, r.stderr[-300:])))
r = factory(["crew", "bogus"])
ck("end to end: a bad crew subcommand says what crew takes", r.returncode == 1 and r.stderr.replace("\r\n", "\n").endswith(CREW_LINE), short((r.returncode, r.stderr[-300:])))

# ---------- TARES_PLUGIN_DIR: the plugin under test ----------
print("TARES_PLUGIN_DIR")
HOOK = "TARES_PLUGIN_DIR"
KIT = T / "kit under test"
(KIT / "hooks").mkdir(parents=True)
(KIT / "hooks" / "hooks.json").write_text('{"hooks": {}}\n')
DEFAULT_PLUGIN = (ROOT / "claude-plugin").resolve()

with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop(HOOK, None)
    ck("without the variable plugin_dir() is the checkout's plugin, as before", tares.cli.plugin_dir().resolve() == DEFAULT_PLUGIN, short(tares.cli.plugin_dir()))
    ck("...and the checkout's plugin has the marker file hooks/hooks.json", (DEFAULT_PLUGIN / "hooks" / "hooks.json").is_file())
    ck("...and no bin/factory (the bash crew is gone)", not (DEFAULT_PLUGIN / "bin" / "factory").exists() and not (DEFAULT_PLUGIN / "bin" / "factory-heavy").exists())
    os.environ[HOOK] = ""
    ck("...an empty variable is the same as none", tares.cli.plugin_dir().resolve() == DEFAULT_PLUGIN, short(tares.cli.plugin_dir()))
    os.environ[HOOK] = str(KIT)
    ck("with it set plugin_dir() is that folder", tares.cli.plugin_dir() == KIT, short(tares.cli.plugin_dir()))
    os.environ[HOOK] = str(T / "no such kit")
    try:
        tares.cli.plugin_dir()
        msg = ""
    except SystemExit as e:
        msg = str(e)
    ck("...a folder without hooks/hooks.json stops with a message naming the variable, it does not fall back",
       HOOK in msg and "has no hooks/hooks.json" in msg, msg)
    os.environ[HOOK] = str(KIT)
    (KIT / "bin").mkdir()
    (KIT / "bin" / "factory").write_text("#!/bin/sh\n")
    (KIT / "hooks" / "hooks.json").unlink()
    try:
        tares.cli.plugin_dir()
        msg = ""
    except SystemExit as e:
        msg = str(e)
    ck("a folder with bin/factory but no hooks/hooks.json is refused", "has no hooks/hooks.json" in msg, msg)
    (KIT / "hooks" / "hooks.json").write_text('{"hooks": {}}\n')

r = factory([UNPORTED], {HOOK: str(KIT)})
ck("tares factory (through tares.cli.main) accepts the folder named and runs on", r.returncode == 1 and r.stdout.replace("\r\n", "\n") == USAGE, short((r.returncode, r.stdout[:80], r.stderr[-300:])))
r = factory([UNPORTED], {HOOK: str(T / "no such kit")})
ck("...with a folder that has no hooks/hooks.json it exits 1 and says so, before any command", r.returncode == 1 and HOOK in r.stderr
   and "has no hooks/hooks.json" in r.stderr and r.stdout == "", short((r.returncode, r.stdout[:80], r.stderr[-300:])))
r = factory(["zz"], {HOOK: str(T / "no such kit")})
ck("...even for a command that exists", r.returncode == 1 and "python ran" not in r.stdout and HOOK in r.stderr, short((r.returncode, r.stdout, r.stderr[-300:])))

# ====================================================================================
shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
