"""The MCP headers helper (claude-plugin/scripts/mcp_headers.py, started by `tares hook mcp-headers`), which replaced bin/mcp-headers.

Checks: the crew's token file wins and is stripped of every space and newline; the key `tares login`
saved is sent only to the endpoint of the workspace it belongs to (the saved cell_url plus /mcp, a
trailing slash on either does not matter); a missing, empty, broken or mismatching file gives `{}`;
a key kept in the OS keychain (login.json says key_store keychain) is read through tares.login with a
stub keychain, only when TARES_KEYRING is on, and nothing the keychain says reaches stdout or stderr;
the helper answers what bin/mcp-headers answered (pinned in a table here); `tares hook
mcp-headers` runs it in process; the script stays stdlib-only and 3.9-safe. No real key, keychain or
~/.tares is touched: every file lives in a temp folder.

Run: .venv/bin/python tests/test_crew_mcp_headers.py
"""
import ast
import contextlib
import importlib.util
import io
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


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def short(x, n=300):
    return str(x)[:n]


WINDOWS = os.name == "nt"
PY = sys.executable
SCRIPT = ROOT / "claude-plugin" / "scripts" / "mcp_headers.py"
T = Path(tempfile.mkdtemp(prefix="crew_mcp_headers_"))
HOME = T / "home"
HOME.mkdir()
CELL = "https://cell.example.com"
KEY = "nvf_abc123_fakekeyfortests"      # gitleaks:allow, not a real key
CREW = "crew-token-0123456789"          # gitleaks:allow, not a real token

CLEAN = {k: v for k, v in os.environ.items()
         if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_", "PYTHON_KEYRING")) and k not in ("PYTHONPATH", "PYTHONHOME")}
CLEAN["HOME"] = str(HOME)
CLEAN["USERPROFILE"] = str(HOME)


def write(name, text):
    path = T / name
    path.write_text(text, encoding="utf-8")
    return path


def login_dir(name, data, raw=None):
    """A TARES_HOME with a login.json of `data` (or the raw text)."""
    d = T / name
    d.mkdir(exist_ok=True)
    (d / "login.json").write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
    return d


def run_py(extra=None, python=None):
    env = dict(CLEAN, PYTHONUTF8="1")
    env.update(extra or {})
    return subprocess.run([python or PY, str(SCRIPT)], capture_output=True, env=env, cwd=str(T), timeout=60)


def answer(r):
    """The parsed JSON of a run, or None."""
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except Exception:
        return None


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


# ---------- the crew's token file ----------
print("the crew's token file")
cred_plain = cred = write("cred.txt", CREW + "\n")
r = run_py({"TARES_FACTORY_CRED_FILE": str(cred_plain)})
ck("the token in the file", r.returncode == 0 and answer(r) == bearer(CREW) and r.stderr == b"", short((r.returncode, r.stdout, r.stderr)))
ck("one line of compact JSON, as the bash helper prints it", r.stdout == b'{"Authorization":"Bearer ' + CREW.encode() + b'"}\n', r.stdout)
cred_spaces = write("cred-spaces.txt", "  crew-\ttoken-\r\n01 23\n\n")
r = run_py({"TARES_FACTORY_CRED_FILE": str(cred_spaces)})
ck("every space, tab and newline inside the file is dropped", answer(r) == bearer("crew-token-0123"), short(r.stdout))
cred_empty = write("cred-empty.txt", " \n\t\n")
r = run_py({"TARES_FACTORY_CRED_FILE": str(cred_empty)})
ck("a file with only whitespace: {}", answer(r) == {}, short(r.stdout))
r = run_py({"TARES_FACTORY_CRED_FILE": str(T / "no-such-file")})
ck("a file that is not there: {}", r.returncode == 0 and answer(r) == {}, short((r.returncode, r.stdout)))
r = run_py({"TARES_FACTORY_CRED_FILE": str(T)})
ck("a folder where the file should be: {}", r.returncode == 0 and answer(r) == {}, short((r.returncode, r.stdout)))
bad = T / "cred-bytes.txt"
bad.write_bytes(b"tok\xff\xfe\n")
r = run_py({"TARES_FACTORY_CRED_FILE": str(bad)})
ck("a file that is not UTF-8: {}, no failure", r.returncode == 0 and answer(r) == {}, short((r.returncode, r.stdout, r.stderr)))
r = run_py({})
ck("nothing set: {}", r.returncode == 0 and answer(r) == {} and r.stdout == b"{}\n" and r.stderr == b"", short((r.returncode, r.stdout, r.stderr)))
quoted = write("cred-quote.txt", 'a"b\\c\n')
r = run_py({"TARES_FACTORY_CRED_FILE": str(quoted)})
ck("a quote or a backslash in the token stays valid JSON", answer(r) == bearer('a"b\\c'), short(r.stdout))

# ---------- the key `tares login` saved ----------
print("the key tares login saved")
th = login_dir("h-ok", {"cell_url": CELL, "cell": "c", "key": KEY})
url = CELL + "/mcp"
r = run_py({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url})
ck("the saved key goes to that workspace's MCP endpoint", answer(r) == bearer(KEY) and r.stderr == b"", short((r.stdout, r.stderr)))
r = run_py({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url + "/"})
ck("a trailing slash on the endpoint does not matter", answer(r) == bearer(KEY), short(r.stdout))
th2 = login_dir("h-slash", {"cell_url": CELL + "/", "key": KEY})
r = run_py({"TARES_HOME": str(th2), "CLAUDE_CODE_MCP_SERVER_URL": url})
ck("...nor one on the saved cell_url", answer(r) == bearer(KEY), short(r.stdout))
for label, other in (("another host", "https://other.example.com/mcp"), ("the same host, another path", CELL + "/other"),
                     ("the cell with no /mcp", CELL), ("the cell with a longer path", CELL + "/mcp/x"),
                     ("http for https", "http://cell.example.com/mcp")):
    r = run_py({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": other})
    ck(f"{label}: the key is not sent", answer(r) == {} and KEY.encode() not in r.stdout, short(r.stdout))
r = run_py({"TARES_HOME": str(th)})
ck("Claude Code names no endpoint: {}", answer(r) == {}, short(r.stdout))
r = run_py({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url, "TARES_FACTORY_CRED_FILE": str(write("c2.txt", CREW))})
ck("the crew's token beats the saved key", answer(r) == bearer(CREW), short(r.stdout))
r = run_py({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url, "TARES_FACTORY_CRED_FILE": str(cred_empty)})
ck("an empty crew file falls through to the saved key", answer(r) == bearer(KEY), short(r.stdout))
for label, data, raw in (
        ("no key", {"cell_url": CELL}, None),
        ("an empty key", {"cell_url": CELL, "key": ""}, None),
        ("no cell_url", {"key": KEY}, None),
        ("an empty cell_url", {"cell_url": "", "key": KEY}, None),
        ("a list instead of an object", None, json.dumps([CELL, KEY])),
        ("not JSON", None, "{ nope"),
        ("an empty file", None, "")):
    h = login_dir("h-" + label.replace(" ", "-"), data, raw)
    r = run_py({"TARES_HOME": str(h), "CLAUDE_CODE_MCP_SERVER_URL": url})
    ck(f"login.json with {label}: {{}} and no failure", r.returncode == 0 and answer(r) == {} and r.stderr == b"",
       short((r.returncode, r.stdout, r.stderr)))
r = run_py({"TARES_HOME": str(T / "no-home"), "CLAUDE_CODE_MCP_SERVER_URL": url})
ck("no login.json at all: {}", answer(r) == {} and r.returncode == 0, short(r.stdout))
(HOME / ".tares").mkdir()
(HOME / ".tares" / "login.json").write_text(json.dumps({"cell_url": CELL, "key": KEY}), encoding="utf-8")
r = run_py({"CLAUDE_CODE_MCP_SERVER_URL": url})
ck("with TARES_HOME unset it looks in ~/.tares", answer(r) == bearer(KEY), short(r.stdout))
shutil.rmtree(HOME / ".tares")

# ---------- a key in the OS keychain ----------
print("a key kept in the OS keychain")
try:
    import keyring  # noqa: F401
    import keyring.backend
    import keyring.backends.fail
    HAVE_KEYRING = True
except ImportError:
    HAVE_KEYRING = False

if not HAVE_KEYRING:
    print("  skip keyring is not installed here (CI installs it); the keychain checks did not run")
else:
    class Stub(keyring.backend.KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.items = {}
            self.locked = False

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def get_password(self, service, username):
            if self.locked:
                raise keyring.errors.KeyringLocked("locked")
            return self.items.get((service, username))

        def delete_password(self, service, username):
            self.items.pop((service, username), None)

    import keyring.errors  # noqa: E402

    # never the machine's real keychain: the stub, or a backend that refuses everything
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    stub = Stub()
    keyring.set_keyring(stub)
    assert keyring.get_keyring() is stub, "the stub keychain did not install"
    stub.items[("tares", CELL)] = KEY
    kc_home = login_dir("h-kc", {"cell_url": CELL, "cell": "c", "key_store": "keychain", "key_id": "k1"})

    spec = importlib.util.spec_from_file_location("mcp_headers_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    saved_env = {k: os.environ.get(k) for k in ("TARES_HOME", "TARES_KEYRING", "CLAUDE_CODE_MCP_SERVER_URL",
                                                "TARES_FACTORY_CRED_FILE", "HOME", "USERPROFILE")}

    def in_process(extra):
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(HOME)
        os.environ.update(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main()
        return rc, out.getvalue(), err.getvalue()

    try:
        env_kc = {"TARES_HOME": str(kc_home), "CLAUDE_CODE_MCP_SERVER_URL": url, "TARES_KEYRING": "on"}
        rc, out, err = in_process(env_kc)
        ck("TARES_KEYRING=on: the key from the keychain", rc == 0 and json.loads(out) == bearer(KEY) and err == "", (rc, out, err))
        rc, out, err = in_process({**env_kc, "CLAUDE_CODE_MCP_SERVER_URL": "https://other.example.com/mcp"})
        ck("...and only to that workspace's endpoint", json.loads(out) == {} and KEY not in out + err, (out, err))
        rc, out, err = in_process({k: v for k, v in env_kc.items() if k != "TARES_KEYRING"})
        ck("TARES_KEYRING not on: {}, and nothing on stderr", json.loads(out) == {} and err == "", (out, err))
        stub.locked = True
        rc, out, err = in_process(env_kc)
        ck("a locked keychain: {}, no failure, nothing on stderr", rc == 0 and json.loads(out) == {} and err == "", (rc, out, err))
        stub.locked = False
        keyring.set_keyring(keyring.backends.fail.Keyring())
        rc, out, err = in_process(env_kc)
        ck("no keychain works: {}, nothing on stderr", rc == 0 and json.loads(out) == {} and err == "", (rc, out, err))
        keyring.set_keyring(stub)
        stub.items.clear()
        rc, out, err = in_process(env_kc)
        ck("a keychain with no entry: {}", json.loads(out) == {}, (out, err))
        stub.items[("tares", CELL)] = KEY
        rc, out, err = in_process({**env_kc, "TARES_FACTORY_CRED_FILE": str(cred_plain)})
        ck("the crew's token still comes first", json.loads(out) == bearer(CREW), out)
        # login.json that holds the key wins over a keychain entry: no keychain call at all
        stub.items[("tares", CELL)] = "nvf_other_inkeychain"
        rc, out, err = in_process({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url, "TARES_KEYRING": "on"})
        ck("a key in login.json is used as it is", json.loads(out) == bearer(KEY), out)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

# ---------- what bin/mcp-headers answered ----------
print("the helper answers what bin/mcp-headers answered")
# What bin/mcp-headers answered in each case, taken from it before it was removed (TR-1103).
EXPECT = {
    'nothing': {},
    'crew file': bearer(CREW),
    'crew file with spaces': {'Authorization': 'Bearer crew-token-0123'},
    'empty crew file': {},
    'missing crew file': {},
    "login ok 'https://cell.example.com/mcp'": bearer(KEY),
    "login ok 'https://cell.example.com/mcp/'": bearer(KEY),
    "login ok 'https://other.example.com/mcp'": {},
    "login ok 'https://cell.example.com'": {},
    "login ok ''": {},
    "login slash 'https://cell.example.com/mcp'": bearer(KEY),
    "login slash 'https://cell.example.com/mcp/'": bearer(KEY),
    "login slash 'https://other.example.com/mcp'": {},
    "login slash 'https://cell.example.com'": {},
    "login slash ''": {},
    'login h-a-list-instead-of-an-object': {},
    'login h-an-empty-cell_url': {},
    'login h-an-empty-file': {},
    'login h-an-empty-key': {},
    'login h-no-cell_url': {},
    'login h-no-key': {},
    'login h-not-JSON': {},
    'both': bearer(CREW),
}
cases = []
cases.append(("nothing", {}))
cases.append(("crew file", {"TARES_FACTORY_CRED_FILE": str(cred_plain)}))
cases.append(("crew file with spaces", {"TARES_FACTORY_CRED_FILE": str(T / "cred-spaces.txt")}))
cases.append(("empty crew file", {"TARES_FACTORY_CRED_FILE": str(cred_empty)}))
cases.append(("missing crew file", {"TARES_FACTORY_CRED_FILE": str(T / "no-such-file")}))
for name, h in (("ok", th), ("slash", th2)):
    for u in (url, url + "/", "https://other.example.com/mcp", CELL, ""):
        cases.append((f"login {name} {u!r}", {"TARES_HOME": str(h), "CLAUDE_CODE_MCP_SERVER_URL": u}))
for d in sorted(T.glob("h-*")):
    if d.name not in ("h-ok", "h-slash", "h-kc"):
        cases.append((f"login {d.name}", {"TARES_HOME": str(d), "CLAUDE_CODE_MCP_SERVER_URL": url}))
cases.append(("both", {"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url, "TARES_FACTORY_CRED_FILE": str(cred_plain)}))
bad_cases = []
for label, extra in cases:
    env = dict(CLEAN, PYTHONUTF8="1", **extra)
    env["PATH"] = os.environ.get("PATH", "")
    p = subprocess.run([PY, str(SCRIPT)], capture_output=True, env=env, cwd=str(T), timeout=60)
    try:
        same = p.returncode == 0 and json.loads(p.stdout) == EXPECT[label]
    except ValueError:
        same = False
    if not same:
        bad_cases.append((label, EXPECT[label], p.stdout))
ck("the table covers every case", sorted(EXPECT) == sorted(label for label, _ in cases), sorted(set(EXPECT) ^ {label for label, _ in cases}))
ck(f"the same JSON as bin/mcp-headers gave in {len(cases)} cases (crew file, saved key, mismatches, broken files)",
   not bad_cases, short(bad_cases, 600))

# ---------- tares hook mcp-headers ----------
print("tares hook mcp-headers")
HOOK_RUN = "import sys; from tares.crew import hooks; sys.exit(hooks.run(['mcp-headers'] + sys.argv[1:]))"
plugin = T / "plugin"
(plugin / "scripts").mkdir(parents=True)
shutil.copy(SCRIPT, plugin / "scripts" / "mcp_headers.py")


def run_hook(extra):
    env = dict(CLEAN, PYTHONPATH=str(ROOT), PYTHONUTF8="1", CLAUDE_PLUGIN_ROOT=str(plugin))
    env.update(extra)
    return subprocess.run([PY, "-c", HOOK_RUN], capture_output=True, env=env, cwd=str(T), timeout=60)


r = run_hook({"TARES_FACTORY_CRED_FILE": str(cred_plain)})
ck("the dispatcher runs it in process: the crew token", r.returncode == 0 and answer(r) == bearer(CREW) and r.stderr == b"",
   short((r.returncode, r.stdout, r.stderr[-300:])))
r = run_hook({"TARES_HOME": str(th), "CLAUDE_CODE_MCP_SERVER_URL": url})
ck("...and the saved key", r.returncode == 0 and answer(r) == bearer(KEY), short((r.stdout, r.stderr[-300:])))
r = run_hook({})
ck("...and {} when there is nothing", r.returncode == 0 and r.stdout == b"{}\n", short((r.returncode, r.stdout, r.stderr[-300:])))
sys.path.insert(0, str(ROOT))
from tares.crew import hooks as H  # noqa: E402
ck("mcp-headers is a registered hook", H.HOOKS.get("mcp-headers") == ("py", "scripts/mcp_headers.py"), H.HOOKS.get("mcp-headers"))

# ---------- the script itself ----------
print("the script")
src = SCRIPT.read_text(encoding="utf-8")
tree = ast.parse(src, feature_version=(3, 9))
ok_top = {"__future__", "contextlib", "io", "json", "os", "sys"}
tops = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        tops.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        tops.add((node.module or "").split(".")[0])
ck("it parses as Python 3.9", True)
ck("it imports only the standard library, and tares.login for the keychain", tops <= ok_top | {"tares"}, tops)
ck("tares is imported inside a function, not at the top", all(
    not (isinstance(n, (ast.Import, ast.ImportFrom)) and (getattr(n, "module", "") or "").startswith("tares"))
    for n in tree.body))
ck("no token or key goes through argv or an env lookup of its own", "sys.argv" not in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
