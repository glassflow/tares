"""`tares factory <cmd>` runs a Python command when it is ported, else bash (tares/crew/dispatch.py).

Checks: the routing table (ported or not, the longest key wins); the one engine switch
TARES_FACTORY_ENGINE (python, bash, unset, an invalid value); that the bash route is the same execve as
before and the environment reaches the script unchanged (a stub script run by a real execve in a
subprocess); the Windows branch (no exec, a clear message, exit 2); UTF-8 forced before a Python
command; that the dispatch path imports no duckdb, fastapi, tares.daemon or tares.store; that
`python -m tares.crew` takes the same routes; and that tares.cli._factory really calls the dispatcher,
end to end through tares.cli.main with a throwaway crew; and the test hook TARES_PLUGIN_DIR (the plugin
folder named is the one bash runs, read at call time; unset or empty changes nothing; a folder without
bin/factory is an error, not a fall back).

Run: .venv/bin/python tests/test_crew_dispatch.py
"""
import ast
import contextlib
import io
import json
import os
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
from tares.crew import dispatch  # noqa: E402

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
# The dispatcher reads "engine" from ~/factory/crew.json when nothing else says; a person's real one must not
# change what these checks route (TR-1069). The checks that pass an env of {} read the home of this process.
(T / "home").mkdir()
os.environ["HOME"] = os.environ["USERPROFILE"] = str(T / "home")
os.environ.pop("FACTORY_CREW_HOME", None)
HAVE_BASH = os.name != "nt" and Path("/bin/bash").exists()
# A bare environment is enough for Python on Mac and Linux. On Windows the interpreter will not start
# without SYSTEMROOT ("failed to get random numbers"), so a probe's environment carries it.
# a bare `-I` probe on Windows still needs the system root and a home (Path.home() reads USERPROFILE) to start
SYS_ENV = ({k: os.environ[k] for k in ("SYSTEMROOT", "USERPROFILE", "HOME", "TEMP", "TMP") if k in os.environ}
           if os.name == "nt" else {})
ENGINE = dispatch.ENGINE_VAR
# A command that stays on bash, for the checks that need one (roster, status and the other views are
# ported with TR-957). Whoever ports this one (settings, TR-953) changes this line to another.
UNPORTED = "unported-probe"  # a command the table does not have (each port used up the last real one: TR-1122 took settings)
BASH_RAN = "no Tares answers at http://127.0.0.1:9"  # what bash says to any command with no Tares, in the fake crew

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


def call_run(argv, env=None, script="/nonexistent/factory", windows=False):
    """Run dispatch.run in-process with execve replaced. Returns (exit code, stdout, stderr, execve calls)."""
    execs = []
    out, err = io.StringIO(), io.StringIO()
    code = None
    with mock.patch.object(os, "execve", lambda *a: execs.append(a)), \
            mock.patch.object(dispatch, "is_windows", lambda: windows), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            dispatch.run(argv, {} if env is None else env, script)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue(), execs


# ---------- the routing table ----------
print("routing")
with ported(TABLE):
    ck("a command that is in the table is found", dispatch.find(["zz"]) is fake_a)
    ck("a command that is not in the table is not found", dispatch.find([UNPORTED]) is None)
    ck("no arguments is not found (bash prints help)", dispatch.find([]) is None)
    ck("extra arguments after a key still find it", dispatch.find(["zz", "--help"]) is fake_a)
    ck("the two-word key wins over the one-word key", dispatch.find(["zz", "sub", "x"]) is fake_sub)
    ck("the one-word key serves another subcommand", dispatch.find(["zz", "other"]) is fake_a)
    ck("a two-word key does not match its first word alone", dispatch.find(["zy"]) is None)
    ck("the subcommand alone is not a key", dispatch.find(["sub"]) is None)
    ck("a key is matched from the front only", dispatch.find(["x", "zz"]) is None)
with ported({("zz", "sub"): fake_sub}):
    ck("with only the two-word key, the command word alone is not ported", dispatch.find(["zz"]) is None)
    ck("...and another subcommand is not ported", dispatch.find(["zz", "other"]) is None)
ck("PORTED maps keys to functions", all(isinstance(k, tuple) and callable(v) for k, v in dispatch.PORTED.items()))
ck("the hidden _mcp-url is ported (TR-1155): the python engine answers it, no bash", dispatch.route(["_mcp-url"], {"TARES_FACTORY_ENGINE": "python"})[0] == "python")
ck("the command name in messages has the subcommand", dispatch.command_name(["crew", "up", "-y"]) == "crew up")
ck("...but not a flag", dispatch.command_name([UNPORTED, "--json"]) == UNPORTED)
ck("...and no command is help", dispatch.command_name([]) == "help")

# ---------- the engine switch ----------
print("engine switch")
with ported(TABLE):
    cases = [
        # engine, argv, kind
        ("", ["zz"], "python"), ("", [UNPORTED], "bash"), ("", [], "bash"), ("", ["zz", "sub"], "python"),
        ("python", ["zz"], "python"), ("python", [UNPORTED], "error"), ("python", [], "error"),
        ("bash", ["zz"], "bash"), ("bash", [UNPORTED], "bash"),
        ("Python", ["zz"], "error"), ("BASH", [UNPORTED], "error"), ("auto", ["zz"], "error"),
        (" bash", [UNPORTED], "error"), ("1", [UNPORTED], "error"),
    ]
    for engine, argv, want in cases:
        got = dispatch.route(argv, {ENGINE: engine} if engine else {})[0]
        ck(f"engine {engine!r}, {argv}: {want}", got == want, got)
    ck("an unset variable is the same as an empty one",
       dispatch.route([UNPORTED], {})[0] == dispatch.route([UNPORTED], {ENGINE: ""})[0] == "bash")

    calls.clear()
    code, out, err, execs = call_run(["zz", "a"], {ENGINE: "bash"}, "/x/factory")
    ck("engine=bash: a ported command goes to bash, not Python", len(execs) == 1 and not calls, short((execs, calls)))
    calls.clear()
    code, out, err, execs = call_run(["zz", "a"], {ENGINE: "python"})
    ck("engine=python: a ported command runs in Python", calls == [("a", ["zz", "a"])] and not execs and code == 0,
       short((calls, execs, code)))
    calls.clear()
    code, out, err, execs = call_run([UNPORTED], {ENGINE: "python"})
    ck("engine=python: an unported command exits 2", code == 2, short(code))
    ck("...with a clear message on stderr", err == (f'tares factory: "{UNPORTED}" is not yet ported to the Python engine '
                                                    f"({ENGINE}=python); unset {ENGINE} to let bash run it\n"), short(err))
    ck("...never falling to bash", not execs and not calls and out == "", short((execs, calls, out)))
    code, out, err, execs = call_run(["crew", "up", "-y"], {ENGINE: "python"})
    ck("...and it names the subcommand", '"crew up" is not yet ported' in err, short(err))
    calls.clear()
    code, out, err, execs = call_run(["zz"], {})
    ck("unset: a ported command runs in Python", calls == [("a", ["zz"])] and not execs, short((calls, execs)))
    calls.clear()
    code, out, err, execs = call_run([UNPORTED], {})
    ck("unset: an unported command goes to bash", len(execs) == 1 and not calls and code is None, short((execs, calls, code)))
    code, out, err, execs = call_run(["zz"], {ENGINE: "perl"})
    ck("an invalid value exits 2", code == 2, short(code))
    ck("...naming the variable and the value", err == f'tares factory: {ENGINE} must be "python" or "bash" (or unset), not "perl"\n',
       short(err))
    ck("...and runs nothing", not execs and calls == [], short((execs, calls)))
    ck("an invalid value fails even for a command that would go to bash", call_run([UNPORTED], {ENGINE: "x"})[0] == 2)
# ---------- "engine" in crew.json (TR-1069) ----------
print("engine in crew.json")


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
    ck("no crew.json: the default", dispatch.engine_setting(crew_json_env(None)) == ("", ""))
    ck("an empty crew.json object: the default", dispatch.engine_setting(crew_json_env("{}")) == ("", ""))
    ck('"engine": "bash" is bash, from crew.json', dispatch.engine_setting(crew_json_env('{"engine": "bash"}')) == ("bash", "crew.json"))
    ck('"engine": "python" in crew.json is the default, not Python alone', dispatch.engine_setting(crew_json_env('{"engine": "python"}')) == ("", "crew.json"))
    ck("the variable wins over crew.json", dispatch.engine_setting({**crew_json_env('{"engine": "bash"}'), ENGINE: "python"}) == ("python", ENGINE))
    for text in ("not json", "[]", '{"engine": 3}', '{"engine": null}', '{"engine": ""}', ""):
        ck(f"a crew.json of {text!r} is ignored", dispatch.engine_setting(crew_json_env(text)) == ("", ""))
    e = crew_json_env('{"engine": "bash"}')
    ck("crew.json bash: a ported command goes to bash", dispatch.route(["zz"], e)[0] == "bash")
    ck("...and an unported one", dispatch.route([UNPORTED], e)[0] == "bash")
    ck("...and the watch", dispatch.route(["crew", "watch"], e)[0] == "bash" or dispatch.is_windows())
    ck("TARES_FACTORY_ENGINE=python beats crew.json bash", dispatch.route(["zz"], {**e, ENGINE: "python"})[0] == "python")
    ck("...and TARES_FACTORY_ENGINE=bash beats crew.json python",
       dispatch.route(["zz"], {**crew_json_env('{"engine": "python"}'), ENGINE: "bash"})[0] == "bash")
    ck("crew.json python: a ported command runs in Python", dispatch.route(["zz"], crew_json_env('{"engine": "python"}'))[0] == "python")
    ck("...an unported one still goes to bash until it is ported", dispatch.route([UNPORTED], crew_json_env('{"engine": "python"}'))[0] == "bash")
    code, out, err, execs = call_run(["zz"], crew_json_env('{"engine": "perl"}'))
    ck("an unknown crew.json engine exits 2, naming crew.json", code == 2 and 'engine" in crew.json must be' in err and not execs, short((code, err)))
    code, out, err, execs = call_run(["zz"], crew_json_env('{"engine": "bash"}'), windows=True)
    ck("crew.json bash on Windows exits 2: no bash there", code == 2 and "needs bash, which is not available on Windows" in err and not execs, short((code, err)))
    calls.clear()
    code, out, err, execs = call_run(["zz"], crew_json_env('{"engine": "bash"}'), "/x/factory")
    ck("crew.json bash through run: one execve of bash, no Python command", len(execs) == 1 and not calls, short((execs, calls)))
    ck("the crew home comes from HOME when FACTORY_CREW_HOME is not set",
       dispatch.engine_setting({"HOME": str(T / "cjhome"), "USERPROFILE": str(T / "cjhome")}) == ("", ""))
    (T / "cjhome" / "factory").mkdir(parents=True, exist_ok=True)
    (T / "cjhome" / "factory" / "crew.json").write_text('{"engine": "bash"}', encoding="utf-8")
    ck("...and then ~/factory/crew.json is read",
       dispatch.engine_setting({"HOME": str(T / "cjhome"), "USERPROFILE": str(T / "cjhome")}) == ("bash", "crew.json"))
ck("the old fallback variable is not read", "TARES_FACTORY_BASH_FALLBACK" not in
   (ROOT / "tares" / "crew" / "dispatch.py").read_text(encoding="utf-8") + (ROOT / "tares" / "cli.py").read_text(encoding="utf-8"))

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
            dispatch.run(["zu"], {}, "/x")
        except SystemExit:
            pass
ck("stdout and stderr are set to UTF-8 before the Python command runs",
   order and sorted(n for n, kw in order[0] if kw.get("encoding") == "utf-8") == ["err", "out"], short(order))
ck("...with errors replaced, not raised", all(kw.get("errors") == "replace" for _, kw in log), short(log))
log.clear()
with mock.patch.object(sys, "stdout", Stream(log, "out")), mock.patch.object(sys, "stderr", Stream(log, "err")), \
        mock.patch.object(os, "execve", lambda *a: None):
    try:
        dispatch.run([UNPORTED], {}, "/x")
    except SystemExit:  # Windows has no bash route: an unported command exits 2 there
        pass
ck("the bash route leaves the streams alone", log == [], short(log))
ns = NoReconfigure()
with ported({("zu",): lambda argv, env: print("é名") or 0}), mock.patch.object(sys, "stdout", ns), mock.patch.object(sys, "stderr", NoReconfigure()):
    try:
        dispatch.run(["zu"], {}, "/x")
        code = "returned"
    except SystemExit as e:
        code = e.code
ck("a stream without reconfigure is left alone, the command still runs", ns.text == "é名\n" and code == 0, short((ns.text, code)))

# a console in another code page: the Python command's output is still UTF-8
UTF_PROBE = (
    "import sys\nsys.path.insert(0, %r)\n"
    "sys.stdout.reconfigure(encoding='latin-1', errors='replace'); sys.stderr.reconfigure(encoding='latin-1', errors='replace')\n"
    "from tares.crew import dispatch\n"
    "dispatch.PORTED[('zu',)] = lambda argv, env: (print('é名 out'), print('é名 err', file=sys.stderr), 0)[-1]\n"
    "dispatch.run(['zu'], {}, '/x')\n"
) % str(ROOT)
r = subprocess.run([sys.executable, "-I", "-c", UTF_PROBE], capture_output=True, cwd=str(T))
# bytes, so the check sees the encoding; Windows ends a printed line with CRLF, which is not what this check is about
ck("in a latin-1 console a Python command still writes UTF-8 to stdout", r.stdout.decode("utf-8", "replace").replace("\r\n", "\n") == "é名 out\n" and r.returncode == 0,
   short((r.stdout, r.stderr[-200:])))
ck("...and to stderr", r.stderr.decode("utf-8", "replace").replace("\r\n", "\n") == "é名 err\n", short(r.stderr))

# ---------- the Windows branch ----------
print("windows")
with ported(TABLE):
    code, out, err, execs = call_run([UNPORTED], {}, windows=True)
    ck("an unported command on Windows exits 2", code == 2, short(code))
    ck("...with the message", err == f'tares factory: "{UNPORTED}" is not yet ported to the Python engine, and bash is not available on Windows\n',
       short(err))
    ck("...and execs nothing", execs == [] and out == "", short((execs, out)))
    code, out, err, execs = call_run(["crew", "up"], {}, windows=True)
    ck("...naming the subcommand", '"crew up" is not yet ported' in err and code == 2, short(err))
    code, out, err, execs = call_run([], {}, windows=True)
    ck("...and help when there is no command", '"help" is not yet ported' in err and code == 2, short(err))
    calls.clear()
    code, out, err, execs = call_run(["zz"], {}, windows=True)
    ck("a ported command still runs on Windows", calls == [("a", ["zz"])] and code == 0 and not execs and err == "", short((calls, code, err)))
    code, out, err, execs = call_run(["zz"], {ENGINE: "python"}, windows=True)
    ck("engine=python on Windows runs a ported command", code == 0 and not execs)
    code, out, err, execs = call_run([UNPORTED], {ENGINE: "python"}, windows=True)
    ck("engine=python on Windows: an unported command fails by the engine's own message", code == 2 and "(TARES_FACTORY_ENGINE=python)" in err and not execs,
       short(err))
    calls.clear()
    code, out, err, execs = call_run(["zz"], {ENGINE: "bash"}, windows=True)
    ck("engine=bash on Windows exits 2 with a message of its own", code == 2 and "needs bash, which is not available on Windows" in err, short((code, err)))
    ck("...and execs nothing, runs nothing", not execs and not calls, short((execs, calls)))
    code, out, err, execs = call_run([UNPORTED], {ENGINE: "weird"}, windows=True)
    ck("an invalid value is still an invalid value on Windows", code == 2 and "must be" in err and not execs, short(err))
ck("is_windows is true only for win32", dispatch.is_windows() == (sys.platform == "win32"))
with mock.patch.object(sys, "platform", "win32"):
    ck("...patching sys.platform flips it", dispatch.is_windows() is True)
with mock.patch.object(sys, "platform", "linux"):
    ck("...and back", dispatch.is_windows() is False)

# ---------- the execve is the one bash has always had, and the env reaches the script ----------
print("bash route")
code, out, err, execs = call_run([UNPORTED, "-x", "a b"], {"K": "v", ENGINE: ""}, "/some dir/factory")
ck("the bash route calls os.execve('/bin/bash', ['bash', script, *argv], env)",
   execs == [("/bin/bash", ["bash", "/some dir/factory", UNPORTED, "-x", "a b"], {"K": "v", ENGINE: ""})], short(execs))
env_obj = {"K": "v"}
code, out, err, execs = call_run([UNPORTED], env_obj, Path("/p/factory"))
ck("...with the same env object and a script given as a Path turned into text", execs and execs[0][2] is env_obj and execs[0][1][1] == str(Path("/p/factory")), short(execs))

STUB = T / "stub factory"
STUB.write_text('echo "ARGC=$#"\n'
                'for a in "$@"; do printf "ARG<%s>\\n" "$a"; done\n'
                'for k in $CHECK_VARS; do printf "ENV %s=<%s>\\n" "$k" "${!k-UNSET}"; done\n'
                '[ "${1:-}" = fail ] && exit 3\nexit 0\n')
EXEC_PROBE = (
    "import json, os, sys\nsys.path.insert(0, %r)\nfrom tares.crew import dispatch\n"
    "a = json.loads(sys.argv[1])\ndispatch.run(a['argv'], a['env'], a['script'])\n"
) % str(ROOT)


def run_stub(argv, env):
    payload = json.dumps({"argv": argv, "env": env, "script": str(STUB)})
    # the probe's own environment has a variable the env dict lacks: it must not reach the script
    penv = {**SYS_ENV, "PATH": os.environ.get("PATH", ""), "PARENT_ONLY": "leaks", "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    r = subprocess.run([sys.executable, "-I", "-c", EXEC_PROBE, payload], capture_output=True, text=True, encoding="utf-8", env=penv, cwd=str(T))
    return r


if HAVE_BASH:
    env = {"PATH": os.environ.get("PATH", ""), "CHECK_VARS": "TARESD_URL TARES_AUTH_TOKEN TARES_FACTORY_URL EMPTY SPACED UNI PARENT_ONLY TARES_FACTORY_ENGINE",
           "TARESD_URL": "http://127.0.0.1:1", "TARES_AUTH_TOKEN": "tok en", "TARES_FACTORY_URL": "http://x:2",
           "EMPTY": "", "SPACED": "a  b\tc", "UNI": "é名", ENGINE: ""}
    r = run_stub([UNPORTED, "builder", "a b", "é名", ""], env)
    ck("a real exec runs the script under bash", r.returncode == 0, short((r.returncode, r.stderr[-300:])))
    ck("...with the arguments one by one, spaces, unicode and empty ones intact",
       r.stdout.splitlines()[:6] == ["ARGC=5", f"ARG<{UNPORTED}>", "ARG<builder>", "ARG<a b>", "ARG<é名>", "ARG<>"], short(r.stdout))
    ck("...with the env variables unchanged",
       all(f"ENV {k}=<{v}>" in r.stdout.splitlines() for k, v in env.items() if k in ("TARESD_URL", "TARES_AUTH_TOKEN", "TARES_FACTORY_URL", "EMPTY", "SPACED", "UNI")),
       short(r.stdout))
    ck("...the engine variable included", "ENV TARES_FACTORY_ENGINE=<>" in r.stdout, short(r.stdout))
    ck("...and a variable only the parent process had does not leak in", "ENV PARENT_ONLY=<UNSET>" in r.stdout, short(r.stdout))
    r = run_stub(["fail"], env)
    ck("the script's exit code is the command's", r.returncode == 3, short(r.returncode))
    r = run_stub([UNPORTED], dict(env, **{ENGINE: "bash"}))
    ck("engine=bash reaches the script as set", "ENV TARES_FACTORY_ENGINE=<bash>" in r.stdout and r.returncode == 0, short(r.stdout))
    r = run_stub([], env)
    ck("no arguments reach the script as none", r.returncode == 0 and "ARGC=0" in r.stdout and "ARG<" not in r.stdout, short(r.stdout))
    r = run_stub([UNPORTED], dict(env, **{ENGINE: "python"}))
    ck("engine=python with an unported command runs nothing and exits 2", r.returncode == 2 and r.stdout == "" and "not yet ported" in r.stderr,
       short((r.returncode, r.stdout, r.stderr)))
else:
    print("  skip  no /bin/bash here: the real-exec checks")

# ---------- no heavy imports on the dispatch path ----------
print("light path")
HEAVY = ("duckdb", "fastapi", "tares.daemon", "tares.store")
LIGHT_PROBE = (
    "import json, os, sys\nsys.path.insert(0, %r)\n"
    "mode = sys.argv[1]\n"
    "def heavy():\n"
    "    return sorted(m for m in sys.modules if m in %r or m.split('.')[0] in ('duckdb', 'fastapi'))\n"
    "def python_cmd(argv, env):\n"
    "    print(json.dumps({'mode': 'python', 'heavy': heavy(), 'tares': sorted(m for m in sys.modules if m.startswith('tares'))}))\n"
    "    return 0\n"
    "def execve(path, args, env):\n"
    "    print(json.dumps({'mode': 'bash', 'heavy': heavy(), 'args': args[2:], 'tares': sorted(m for m in sys.modules if m.startswith('tares'))}))\n"
    "    sys.exit(0)\n"
    "os.execve = execve\n"
    "from tares.crew import dispatch\n"
    "dispatch.PORTED[('zz',)] = python_cmd\n"
    "import tares.cli\n"
    "sys.argv = ['tares', 'factory'] + sys.argv[2:]\n"
    "tares.cli.main()\n"
) % (str(ROOT), HEAVY)
env_light = {**SYS_ENV, "PATH": os.environ.get("PATH", ""), "HOME": str(T), "TARES_HOME": str(T / "th"), "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
for mode, args in (("python", ["zz"]), ("bash", [UNPORTED])):
    if mode == "bash" and os.name == "nt":
        print("  skip  the bash route in a bare interpreter: Windows has no bash route (an unported command exits 2)")
        continue
    r = subprocess.run([sys.executable, "-I", "-c", LIGHT_PROBE, mode, *args], capture_output=True, text=True, env=env_light, cwd=str(T))
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        info = {}
    ck(f"{mode} route through tares.cli.main runs in a bare interpreter", info.get("mode") == mode, short((r.stdout, r.stderr[-300:])))
    ck(f"{mode} route: no duckdb, fastapi, tares.daemon or tares.store was imported", info.get("heavy") == [], short(info.get("heavy")))
    if mode == "python":
        ck("...and the tares modules loaded are the light ones",
           set(info.get("tares", [])) <= {"tares", "tares.cli", "tares.config", "tares.login", "tares.crew", "tares.crew.dispatch",
                                          "tares.crew.errors", "tares.crew.paths", "tares.crew.perms", "tares.crew.state", "tares.crew.locks"},
           short(info.get("tares")))
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


if HAVE_BASH and shutil.which("jq"):
    r = mod([UNPORTED])
    ck(f"`python -m tares.crew {UNPORTED}` runs the plugin's bash script", r.returncode == 1 and BASH_RAN in r.stderr,
       short((r.returncode, r.stdout, r.stderr[-300:])))
    r = mod([UNPORTED], {ENGINE: "python"})
    ck("...engine=python refuses it, exit 2", r.returncode == 2 and "not yet ported" in r.stderr and r.stdout == "", short((r.returncode, r.stdout, r.stderr)))
    r = mod([UNPORTED], {ENGINE: "nope"})
    ck("...an invalid engine exits 2", r.returncode == 2 and "must be" in r.stderr, short(r.stderr))
    r = mod([UNPORTED], {ENGINE: "bash"})
    ck("...engine=bash runs bash", r.returncode == 1 and BASH_RAN in r.stderr, short((r.returncode, r.stdout)))
else:
    print("  skip  no bash or jq: the roster checks")
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
ck("_factory does not exec bash itself", stray_execs == [], short(stray_execs))
ck("_factory calls dispatch.run exactly once", len(recorded) == 1, short(recorded))
if recorded:
    a_argv, a_env, a_script = recorded[0]
    ck("...with the argv after `factory`", a_argv == [UNPORTED, "--x"], short(a_argv))
    ck("...with the environment _factory built (the address it resolved)", a_env.get("TARESD_URL") == "http://127.0.0.1:9" and a_env.get("TARES_FACTORY_URL") == "http://127.0.0.1:9"
       and a_env.get("TARES_FACTORY_SOURCE") == "environment" and a_env.get("FACTORY_CREW_HOME") == ENV_F["FACTORY_CREW_HOME"], short(a_env.get("TARES_FACTORY_SOURCE")))
    ck("...and the plugin's factory script", Path(a_script) == tares.cli.plugin_dir() / "bin" / "factory" and Path(a_script).is_file(), short(a_script))
src = (ROOT / "tares" / "cli.py").read_text(encoding="utf-8")
ck("cli.py no longer execs bash itself", "os.execve" not in src, "")

FACTORY_PROBE = (
    "import sys\nsys.path.insert(0, %r)\nfrom tares.crew import dispatch\n"
    "dispatch.PORTED[('zz',)] = lambda argv, env: (print('python ran', argv, env['TARES_FACTORY_URL'], env.get('TARES_FACTORY_SOURCE')), 4)[-1]\n"
    "from tares.cli import main\nsys.argv = ['tares', 'factory'] + sys.argv[1:]\nmain()\n"
) % str(ROOT)


def factory(args, extra=None, probe=FACTORY_PROBE):
    e = dict(ENV_F, **(extra or {}))
    e.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-I", "-c", probe, *args], capture_output=True, text=True, encoding="utf-8", env=e, cwd=str(T))


r = factory(["zz", "q"])
ck("end to end: a registered Python command is reached through tares.cli.main and its code is the exit code",
   r.returncode == 4 and r.stdout == "python ran ['zz', 'q'] http://127.0.0.1:9 environment\n", short((r.returncode, r.stdout, r.stderr[-300:])))
r = factory(["zz", "q"], {ENGINE: "bash"})
ck("end to end: engine=bash sends even that command to the bash script (which does not know it)",
   r.returncode != 4 and "python ran" not in r.stdout, short((r.returncode, r.stdout)))
if HAVE_BASH and shutil.which("jq"):
    r = factory([UNPORTED])
    ck(f"end to end: {UNPORTED} goes to bash and gets bash's answer", r.returncode == 1 and BASH_RAN in r.stderr,
       short((r.returncode, r.stdout, r.stderr[-300:])))
    r = factory([UNPORTED], {ENGINE: "python"})
    ck("end to end: engine=python refuses an unported command with exit 2", r.returncode == 2 and "not yet ported" in r.stderr and r.stdout == "", short((r.returncode, r.stdout, r.stderr)))
    r = factory([UNPORTED], {ENGINE: "nope"})
    ck("end to end: an invalid engine exits 2", r.returncode == 2 and "must be" in r.stderr, short(r.stderr))
else:
    print("  skip  no bash or jq: the roster checks")

# ---------- TARES_PLUGIN_DIR: the plugin under test ----------
print("TARES_PLUGIN_DIR")
HOOK = "TARES_PLUGIN_DIR"
KIT = T / "kit under test"
(KIT / "bin").mkdir(parents=True)
(KIT / "bin" / "factory").write_text('#!/usr/bin/env bash\n'
                                     'echo "KIT $(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd) $(cat "$(dirname "${BASH_SOURCE[0]}")/../marker") $*"\n')
(KIT / "bin" / "factory").chmod(0o755)
(KIT / "marker").write_text("v1\n")
KIT_REAL = os.path.realpath(KIT)
DEFAULT_PLUGIN = (ROOT / "claude-plugin").resolve()

with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop(HOOK, None)
    ck("without the variable plugin_dir() is the checkout's plugin, as before", tares.cli.plugin_dir().resolve() == DEFAULT_PLUGIN, short(tares.cli.plugin_dir()))
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
    ck("...a folder without bin/factory stops with a message naming the variable, it does not fall back", HOOK in msg and "bin/factory" in msg, msg)

if HAVE_BASH:
    r = mod([UNPORTED, "a b"], {HOOK: str(KIT)})
    ck("python -m tares.crew hands bash the script of the folder named", r.returncode == 0 and r.stdout == f"KIT {KIT_REAL} v1 {UNPORTED} a b\n", short((r.returncode, r.stdout, r.stderr[-300:])))
    (KIT / "marker").write_text("v2\n")
    r = mod([UNPORTED], {HOOK: str(KIT)})
    ck("...and reads the folder at call time: an edit between two calls shows", r.stdout == f"KIT {KIT_REAL} v2 {UNPORTED}\n", short(r.stdout))
    r = factory([UNPORTED], {HOOK: str(KIT)})
    ck("tares factory (through tares.cli.main) uses the folder named too", r.returncode == 0 and r.stdout == f"KIT {KIT_REAL} v2 {UNPORTED}\n", short((r.returncode, r.stdout, r.stderr[-300:])))
    r = mod([UNPORTED], {HOOK: str(T / "no such kit")})
    ck("python -m tares.crew with a folder that has no script exits 1 and says so", r.returncode == 1 and HOOK in r.stderr and r.stdout == "", short((r.returncode, r.stdout, r.stderr[-300:])))
    r = mod([UNPORTED], {HOOK: str(KIT), ENGINE: "python"})
    ck("...the engine switch still rules: engine=python refuses an unported command", r.returncode == 2 and "not yet ported" in r.stderr, short((r.returncode, r.stderr)))
else:
    print("  skip  no /bin/bash here: the TARES_PLUGIN_DIR runs")

# ====================================================================================
shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
