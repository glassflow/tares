"""`tares login --json`, `tares login --show [--json]` and `tares logout --json` (TR-1282), and
`tares login --cell <url> --key-stdin [--json]` (TR-1285), against a fake control plane and workspace on
localhost: the documents a program reads, the error codes, and that the key never appears in any output. The text output is covered by tests/test_login.py (unchanged).
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
HOME = tempfile.mkdtemp()
for k in [k for k in os.environ if k.startswith(("TARES_", "FACTORY_"))] + ["TARESD_URL"]:
    os.environ.pop(k, None)
os.environ["TARES_HOME"] = HOME
os.environ["TARES_KEYRING"] = "off"
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
os.environ["FACTORY_CREW_HOME"] = os.path.join(HOME, "no-crew")

REAL_OPEN = webbrowser.open       # before any test replaces it

from tares import cli as C  # noqa: E402
from tares import login as L  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


WORLD = {"version": "1.44.0", "exchange_status": 200, "logout_status": 200, "delete_status": 200,
         "good_keys": set(), "no_admin_keys": set(), "project_keys": set(), "calls": [], "health_status": 200,
         "whoami_status": 200, "n": 0, "challenge": "", "settings_force": 0, "cell_url": ""}
OUTPUTS = []        # everything any call printed, to look for a key in


class Fake(BaseHTTPRequestHandler):
    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            if WORLD["health_status"] == 299:       # answers 200 with something that is not a Tares
                return self._json(200, {"hello": "world"})
            if WORLD["health_status"] != 200:
                return self._json(WORLD["health_status"], {})
            return self._json(200, {"status": "ok", "version": WORLD["version"]})
        if self.path == "/api/whoami":
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            if WORLD["whoami_status"] != 200:
                return self._json(WORLD["whoami_status"], {})
            if bearer in WORLD["project_keys"]:
                return self._json(200, {"id": "k9", "kind": "key", "project": "p1", "scopes": ["admin", "read"]})
            if bearer in WORLD["no_admin_keys"]:
                return self._json(200, {"id": "k8", "kind": "key", "scopes": ["ingest", "read"]})
            return self._json(200 if bearer in WORLD["good_keys"] else 401,
                              {"id": "k1", "kind": "key", "scopes": ["admin", "ingest", "read"]})
        if self.path == "/api/crew/settings":
            if WORLD["settings_force"]:
                return self._json(WORLD["settings_force"], {})
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            ok = bearer in WORLD["good_keys"] or bearer in WORLD["no_admin_keys"] or bearer in WORLD["project_keys"]
            return self._json(200 if ok else 401, {"settings": {}})
        self._json(404, {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/exchange":
            if WORLD["exchange_status"] != 200:
                return self._json(WORLD["exchange_status"], {"detail": "not today"})
            WORLD["n"] += 1
            kid = f"k{WORLD['n']}"
            WORLD["good_keys"].add(f"nvf_{kid}_topsecret")
            return self._json(200, {"token": f"nvf_{kid}_topsecret", "key_id": kid, "cell": "factory",
                                    "cell_url": WORLD["cell_url"] or BASE})
        if self.path == "/api/cli/logout":
            WORLD["calls"].append(("logout", (self.headers.get("Authorization") or "").removeprefix("Bearer ")))
            return self._json(WORLD["logout_status"], {"revoked": True})
        self._json(404, {})

    def do_DELETE(self):  # noqa: N802
        WORLD["calls"].append(("delete", (self.headers.get("Authorization") or "").removeprefix("Bearer ")))
        self._json(WORLD["delete_status"], {})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def _opener(callback=True, state_override=None):
    def open_(url, *_a, **_k):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        WORLD["challenge"] = q.get("challenge", "")
        if not callback:
            return True

        def go():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{q['port']}/callback?code=c1&state="
                                       f"{state_override or q['state']}", timeout=5).read()
            except Exception:
                pass
        threading.Timer(0.2, go).start()
        return True
    return open_


def browser(callback=True, state_override=None):
    L.webbrowser.open = _opener(callback, state_override)


def run(fn, *a, **kw):
    """(exit code, stdout, stderr) of fn, whether it returns a code or exits."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(*a, **kw)
        except SystemExit as e:
            rc = e.code
    OUTPUTS.append(out.getvalue() + err.getvalue())
    return rc, out.getvalue(), err.getvalue()


def docs(out):
    return [json.loads(x) for x in out.splitlines()]


def err_code(out):
    d = docs(out)
    return d[0].get("error", {}).get("code") if len(d) == 1 else None


print("a good login: an open document, then a done document")
browser()
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
d = docs(out)
ck("exit 0, two documents, schema_version first", rc == 0 and len(d) == 2
   and all(next(iter(x)) == "schema_version" and x["schema_version"] == 1 for x in d), (rc, out))
ck("open: the connect address and the wait used", d[0]["login"]["step"] == "open"
   and d[0]["login"]["url"].startswith(f"{BASE}/connect/cli?") and d[0]["login"]["expires_in"] == 10, d[0])
ck("done: cell, address, machine, key_store file", d[1]["login"] == {
    "step": "done", "cell": "factory", "cell_url": BASE, "machine": L._machine(), "key_store": "file"}, d[1])
ck("nothing on stderr", err == "", err)
ck("the key is saved", (L.saved() or {}).get("key") == "nvf_k1_topsecret")
ck("default wait is 300", L.WAIT_SECONDS == 300)

print("failures are the error document, exit 1")
browser(callback=False)
rc, out, err = run(L.login, BASE, wait=2, json_out=True)
d = docs(out)
ck("timeout: open then login_timeout", rc == 1 and len(d) == 2 and d[0]["login"]["step"] == "open"
   and d[1]["error"]["code"] == "login_timeout", (rc, out))
browser(state_override="wrong")
rc, out, err = run(L.login, BASE, wait=2, json_out=True)
ck("a callback with the wrong state is login_timeout too", rc == 1 and docs(out)[-1]["error"]["code"] == "login_timeout", out)
browser()
WORLD["exchange_status"] = 403
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
ck("refused exchange: login_refused", rc == 1 and docs(out)[-1]["error"]["code"] == "login_refused", (rc, out))
WORLD["exchange_status"] = 200
WORLD["version"] = "1.0.0"
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
ck("old cell: cell_too_old", rc == 1 and docs(out)[-1]["error"]["code"] == "cell_too_old", (rc, out))
WORLD["version"] = "1.44.0"
rc, out, err = run(L.login, "http://127.0.0.1:9", wait=10, json_out=True)
ck("cloud unreachable: error", rc == 1 and docs(out)[-1]["error"]["code"] == "error", (rc, out))
real_server = L.HTTPServer


def no_bind(*a, **k):
    raise OSError("no port")


L.HTTPServer = no_bind
rc, out, err = run(L.login, BASE, wait=2, json_out=True)
L.HTTPServer = real_server
ck("listener cannot bind: error, no traceback", rc == 1 and err_code(out) == "error" and "Traceback" not in err, (rc, out, err))

print("--show")
rc, out, err = run(L.show, json_out=True)
s = docs(out)[0]["login"]
ck("saved login, key works", rc == 0 and s["signed_in"] is True and s["key_works"] is True
   and s["source"] == "saved login" and s["cell_url"] == BASE and s["key_id"] == "k1"
   and s["key_store"] == "file" and s["cell"] == "factory" and "key" not in s, s)
rc, out, err = run(L.show)
ck("text mode names the facts", rc == 0 and "factory" in out and "works" in out, out)
good = set(WORLD["good_keys"])
WORLD["good_keys"].clear()
rc, out, err = run(L.show, json_out=True)
ck("a refused key: key_works false", docs(out)[0]["login"]["key_works"] is False, out)
WORLD["good_keys"].update(good)
raw = json.loads(L.login_file().read_text())
L.login_file().write_text(json.dumps({**raw, "cell_url": "http://127.0.0.1:9"}))
rc, out, err = run(L.show, json_out=True)
ck("an unreachable cell: key_works null, still signed in", rc == 0
   and docs(out)[0]["login"]["signed_in"] is True and docs(out)[0]["login"]["key_works"] is None, out)
L.login_file().write_text(json.dumps(raw))

print("logout --json")
rc, out, err = run(L.logout, json_out=True)
ck("signed in: removed", rc == 0 and docs(out) == [{"schema_version": 1, "logout": {
    "was_signed_in": True, "key_removed": True}}], (rc, out))
ck("the login is gone", L.saved() is None)
rc, out, err = run(L.show, json_out=True)
ck("show with nothing saved: signed_in false only", rc == 0 and docs(out) == [
    {"schema_version": 1, "login": {"signed_in": False}}], out)
rc, out, err = run(L.logout, json_out=True)
ck("not signed in: false and false, exit 0", rc == 0 and docs(out)[0]["logout"] == {
    "was_signed_in": False, "key_removed": False}, (rc, out))
browser()
run(L.login, BASE, wait=10, json_out=True)
kid = L.saved()["key_id"]
WORLD["logout_status"] = 500
WORLD["delete_status"] = 500
rc, out, err = run(L.logout, json_out=True)
msg = docs(out)[0].get("error", {}).get("message", "")
ck("revoke failed: logout_failed naming the cell and key, exit 1", rc == 1 and err_code(out) == "logout_failed"
   and "factory" in msg and f"key {kid}" in msg and "not removed" in msg, (rc, out))
ck("and the local login is deleted anyway", L.saved() is None)
WORLD["logout_status"] = WORLD["delete_status"] = 200

print("a key that cannot be read")
L.login_file().write_text(json.dumps({"cloud": BASE, "cell": "factory", "cell_url": BASE, "key_id": "k9",
                                      "machine": "m", "signed_in_at": "t", "key_store": "keychain"}))
rc, out, err = run(L.logout, json_out=True)
ck("logout: keychain_unreadable, login kept", rc == 1 and err_code(out) == "keychain_unreadable"
   and L.login_file().exists(), (rc, out))
rc, out, err = run(L.login, BASE, wait=2, json_out=True)
ck("login: keychain_unreadable", rc == 1 and err_code(out) == "keychain_unreadable", (rc, out))
rc, out, err = run(L.show, json_out=True)
s = docs(out)[0]["login"]
ck("show: still signed in, key_works null, keychain", s["signed_in"] is True and s["key_works"] is None
   and s["key_store"] == "keychain", s)
L.login_file().unlink()

print("usage")
ns = argparse.Namespace(cloud=BASE, no_browser=False, show=True, json=True, cell="", key_stdin=False)
rc, out, err = run(C._login, ns)
ck("--show --cloud --json: usage error document, exit 2", rc == 2 and err_code(out) == "usage", (rc, out))
ns = argparse.Namespace(cloud="", no_browser=True, show=True, json=False, cell="", key_stdin=False)
rc, out, err = run(C._login, ns)
ck("--show --no-browser: exit 2, text on stderr", rc == 2 and out == "" and "--show" in err, (rc, out, err))
ns = argparse.Namespace(cloud="", no_browser=False, show=True, json=True, cell="", key_stdin=False)
rc, out, err = run(C._login, ns)
ck("--show --json through the CLI", rc == 0 and docs(out)[0]["login"]["signed_in"] is False, out)

print("not signed in: the crew client names it")
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError, NotSignedInError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

crew_home = Path(tempfile.mkdtemp())
login_path = L.login_file()
saved_login = login_path.read_bytes() if login_path.exists() else None
if saved_login is not None:
    login_path.unlink()
try:
    def need(env):
        try:
            TaresClient(CrewPaths.at(crew_home), env=env).require_tares()
        except CrewError as e:
            return e
        return None
    e = need({"TARES_HOME": HOME, "TARES_FACTORY_SOURCE": "local default", "TARESD_URL": "http://127.0.0.1:1"})
    ck("nothing named and no saved login: not_signed_in", isinstance(e, NotSignedInError) and e.code == "not_signed_in", e)
    ck("...with the old message text", e is not None and "no Tares answers at" in str(e), e)
    e = need({"TARES_HOME": HOME, "TARESD_URL": "http://127.0.0.1:1"})
    ck("an address named by the person stays a plain error", e is not None and not isinstance(e, NotSignedInError)
       and e.code == "error", e)
    (crew_home / "crew.json").write_text(json.dumps({"tares": {"url": "http://127.0.0.1:1"}}))
    e = need({"TARES_HOME": HOME})
    ck("an address in crew.json is a named Tares: plain error", e is not None and e.code == "error", e)
    (crew_home / "crew.json").unlink()
    L.login_file().write_text(json.dumps({"cell_url": BASE, "key": "x"}))
    e = need({"TARES_HOME": HOME, "TARES_FACTORY_SOURCE": "local default", "TARESD_URL": "http://127.0.0.1:1"})
    ck("a saved login (in the client's TARES_HOME) is not 'not signed in'", e is not None and e.code == "error", e)
    L.login_file().unlink()
finally:
    if saved_login is not None:
        login_path.write_bytes(saved_login)

print("more failures and facts")
browser()
WORLD["settings_force"] = 403
run(L.login, BASE, wait=10, json_out=True)
rc, out, err = run(L.show, json_out=True)
ck("403 from the cell: key_works false", docs(out)[0]["login"]["key_works"] is False, out)
for status in (302, 500):
    WORLD["settings_force"] = status
    rc, out, err = run(L.show, json_out=True)
    ck(f"{status} from the cell: key_works null", docs(out)[0]["login"]["key_works"] is None, out)
WORLD["settings_force"] = 0
os.environ["TARESD_URL"] = "http://elsewhere:8787"
rc, out, err = run(L.show, json_out=True)
ck("show: source is environment when TARESD_URL is set", docs(out)[0]["login"]["source"] == "environment", out)
os.environ.pop("TARESD_URL")
WORLD["cell_url"] = "http://127.0.0.1:9"
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
ck("cell unreachable: code error, not cell_too_old", rc == 1 and docs(out)[-1]["error"]["code"] == "error", (rc, out))
WORLD["cell_url"] = ""
import httpx  # noqa: E402
real_http = L._http


def bad_url(*a, **k):
    raise httpx.InvalidURL("Invalid URL")     # what httpx raises for a --cloud it cannot parse


L._http = bad_url
browser()
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
L._http = real_http
ck("a bad --cloud (InvalidURL): open document then an error document, no traceback", rc == 1
   and docs(out)[-1]["error"]["code"] == "error" and "Traceback" not in err, (rc, out, err))

print("the crew key file notice goes to stderr with --json")
crew = Path(HOME) / "crew-home"
(crew / ".tares-token").mkdir(parents=True)      # a directory where the file goes: the write fails
os.environ["FACTORY_CREW_HOME"] = str(crew)
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
ck("login still succeeds", rc == 0 and docs(out)[-1]["login"]["step"] == "done", (rc, out))
ck("stdout holds only the two documents", len(docs(out)) == 2, out)
ck("the notice is on stderr", "could not update the crew's key file" in err, err)
os.environ["FACTORY_CREW_HOME"] = os.path.join(HOME, "no-crew")

print("the keychain as the key store")
try:
    import keyring.backend

    class Mem(keyring.backend.KeyringBackend):
        priority = 1
        store = {}

        def get_password(self, service, username):
            return self.store.get((service, username))

        def set_password(self, service, username, password):
            self.store[(service, username)] = password

        def delete_password(self, service, username):
            self.store.pop((service, username), None)
    keyring.set_keyring(Mem())
    os.environ["TARES_KEYRING"] = "on"
    L.login_file().unlink(missing_ok=True)
    rc, out, err = run(L.login, BASE, wait=10, json_out=True)
    ck("done document says keychain", rc == 0 and docs(out)[-1]["login"]["key_store"] == "keychain", (rc, out))
    rc, out, err = run(L.show, json_out=True)
    ck("show says keychain and the key works", docs(out)[0]["login"]["key_store"] == "keychain"
       and docs(out)[0]["login"]["key_works"] is True, out)
    run(L.logout, json_out=True)
    os.environ["TARES_KEYRING"] = "off"
except ImportError:
    print("  skip keyring is not installed")

print("a browser that writes to fd 1 cannot break the stream")


class Noisy(webbrowser.BaseBrowser):
    def open(self, url, new=0, autoraise=True):
        os.write(1, b"STRAY BROWSER OUTPUT\n")
        return _opener()(url)


webbrowser.register("noisy-test-browser", None, Noisy(), preferred=True)
L.webbrowser.open = REAL_OPEN
tmp1, tmp2 = tempfile.TemporaryFile(), tempfile.TemporaryFile()
sys.stdout.flush()
sys.stderr.flush()
save1, save2 = os.dup(1), os.dup(2)
os.dup2(tmp1.fileno(), 1)
os.dup2(tmp2.fileno(), 2)
try:
    rc, out, err = run(L.login, BASE, wait=10, json_out=True)
finally:
    os.dup2(save1, 1)
    os.dup2(save2, 2)
    os.close(save1)
    os.close(save2)
tmp1.seek(0)
tmp2.seek(0)
fd1, fd2 = tmp1.read(), tmp2.read()
ck("the login worked", rc == 0 and len(docs(out)) == 2, (rc, out))
ck("the browser's output did not reach fd 1", b"STRAY" not in fd1, fd1)
ck("it went to stderr", b"STRAY" in fd2, fd2)
ck("fd 1 is back to what it was", os.write(1, b"") == 0)

print("a cell by its address and a key (TR-1285)")
import subprocess  # noqa: E402

KEY = "nvf_c1_cellsecret"
WORLD["good_keys"].add(KEY)
WORLD["no_admin_keys"].add("nvf_c2_readonly")
WORLD["project_keys"].add("nvf_c3_project")
WORLD["calls"].clear()
L.login_file().unlink(missing_ok=True)
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("TARES_", "FACTORY_")) and k != "TARESD_URL"}
CLEAN_ENV.update(TARES_HOME=HOME, TARES_KEYRING="off", PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                 FACTORY_CREW_HOME=os.path.join(HOME, "no-crew"), PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")


def cli(*argv, stdin="", env=None):
    """The real command in its own process: (exit code, stdout, stderr). Its output is searched for the key too."""
    r = subprocess.run([sys.executable, "-c", "import sys;from tares.cli import main;sys.exit(main())", *argv],
                       input=stdin.encode("utf-8"), capture_output=True, timeout=120,
                       env={**CLEAN_ENV, "PYTHONUTF8": "1", **(env or {})})
    out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    OUTPUTS.append(out + err)
    return r.returncode, out, err


rc, out, err = cli("login", "--cell", BASE + "/", "--key-stdin", "--json", stdin=KEY + "\n")
d = docs(out)
ck("a good key: one done document, exit 0", rc == 0 and len(d) == 1 and d[0]["login"]["step"] == "done"
   and d[0]["login"]["cell_url"] == BASE and d[0]["login"]["cell"] == "127.0.0.1"
   and d[0]["login"]["key_store"] == "file", (rc, out, err))
saved = json.loads(L.login_file().read_text())
ck("saved: cloud null, host name as cell, no key id, the key in the file",
   saved["cloud"] is None and saved["cell"] == "127.0.0.1" and saved["cell_url"] == BASE
   and saved["key_id"] is None and saved["key"] == KEY and saved["machine"] and saved["signed_in_at"], saved)
rc, out, err = cli("factory", "doctor", "--json")
checks = {c["id"]: c["ok"] for c in json.loads(out)["checks"]}
ck("doctor: tares and tares_access are ok against the fake cell", checks.get("tares") is True
   and checks.get("tares_access") is True, checks)
rc, out, err = cli("login", "--show", "--json")
ck("--show --json: source saved login, cloud null, key works", rc == 0 and docs(out)[0]["login"]["source"] == "saved login"
   and docs(out)[0]["login"]["cloud"] is None and docs(out)[0]["login"]["key_works"] is True, out)
rc, out, err = cli("logout", "--json")
ck("logout --json: signed in, key not removed, exit 0", rc == 0 and docs(out)[0]["logout"] ==
   {"was_signed_in": True, "key_removed": False}, (rc, out, err))
ck("the saved login is gone and the cell was not asked to revoke or delete the key",
   not L.login_file().exists() and WORLD["calls"] == [], WORLD["calls"])

rc, out, err = cli("login", "--cell", BASE, "--key-stdin", stdin=KEY + "\n")
ck("text: one line", rc == 0 and out.strip() == f"tares login: this machine is connected to {BASE}", (rc, out, err))
rc, out, err = cli("logout")
ck("text logout of such a login: exit 0, says the key stays on the cell", rc == 0 and "still valid" in out, (rc, out))

for label, key, code in (("a refused key", "nvf_zz_wrong", "key_refused"),
                         ("a read and ingest key (real cells answer 200 to the settings call)", "nvf_c2_readonly",
                          "key_lacks_admin"),
                         ("a project key", "nvf_c3_project", "key_lacks_admin")):
    rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=key + "\n")
    ck(f"{label}: {code}, exit 1, nothing saved", rc == 1 and err_code(out) == code and not L.login_file().exists(),
       (rc, out, err))
WORLD["version"] = "1.43.9"
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=KEY + "\n")
ck("a cell that is too old: cell_too_old, exit 1", rc == 1 and err_code(out) == "cell_too_old"
   and not L.login_file().exists(), (rc, out))
WORLD["version"] = "1.44.0"
rc, out, err = cli("login", "--cell", "http://127.0.0.1:9", "--key-stdin", "--json", stdin=KEY + "\n")
ck("an address that does not answer: cell_unreachable, exit 1", rc == 1 and err_code(out) == "cell_unreachable", (rc, out))
rc, out, err = cli("login", "--cell", "https://127.0.0.1:" + BASE.rsplit(":", 1)[1], "--key-stdin", "--json", stdin=KEY + "\n")
ck("a certificate or TLS failure is cell_unreachable and names the error", rc == 1
   and err_code(out) == "cell_unreachable" and len(docs(out)[0]["error"]["message"]) > 40, (rc, out))

rc, out, err = cli("login", "--cell", BASE, "--json", stdin=KEY + "\n")
ck("--cell without --key-stdin: usage error document, exit 2", rc == 2 and err_code(out) == "usage", (rc, out, err))
rc, out, err = cli("login", "--cell", BASE, stdin=KEY + "\n")
ck("...text on stderr, nothing on stdout", rc == 2 and out == "" and "--key-stdin" in err, (rc, out, err))
for bad in ("http://example.com", "ftp://x.test", "not-an-address", "https://user:pw@h.test", "https://h.test/?a=1"):
    rc, out, err = cli("login", "--cell", bad, "--key-stdin", "--json", stdin=KEY + "\n")
    ck(f"address {bad}: usage error, exit 2", rc == 2 and err_code(out) == "usage", (rc, out))
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin="\n")
ck("an empty key line: usage error, exit 2", rc == 2 and err_code(out) == "usage", (rc, out))
ck("a localhost address is allowed over http, and a trailing slash is dropped", L.cell_address("http://localhost:8787/") ==
   ("http://localhost:8787", "") and L.cell_address("https://cell.example.com/")[0] == "https://cell.example.com", "")
ck("nothing was saved by any failure", not L.login_file().exists(), "")

print("more of the cell login (TR-1285 review)")
WORLD["settings_force"] = 500
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=KEY + "\n")
ck("a 5xx on the key check is not 'connected'", rc == 1 and err_code(out) == "cell_unreachable"
   and not L.login_file().exists(), (rc, out))
WORLD["settings_force"] = 0
for status, what in ((500, "a /health that fails"), (299, "a /health with no version")):
    WORLD["health_status"] = status
    rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=KEY + "\n")
    ck(f"{what}: cell_unreachable, nothing saved", rc == 1 and err_code(out) == "cell_unreachable"
       and not L.login_file().exists(), (rc, out))
WORLD["health_status"] = 200
WORLD["whoami_status"] = 404
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=KEY + "\n")
ck("a cell with no /api/whoami: the key that worked is accepted", rc == 0, (rc, out))
WORLD["whoami_status"] = 200
L.login_file().unlink(missing_ok=True)
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--show", "--json", stdin=KEY + "\n")
ck("--cell with --show: usage error, exit 2", rc == 2 and err_code(out) == "usage", (rc, out))
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin="\ufeff" + KEY + "\r\n")
ck("a BOM and a CRLF in front of and after the key are dropped", rc == 0, (rc, out, err))

crew2 = Path(HOME) / "crew-for-cell"
crew2.mkdir()
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=KEY + "\n",
                   env={"FACTORY_CREW_HOME": str(crew2)})
ck("a crew home follows the cell login: its key file holds the key", rc == 0
   and (crew2 / ".tares-token").read_text().strip() == KEY, (rc, out, err))
rc, out, err = cli("logout", "--json", env={"FACTORY_CREW_HOME": str(crew2)})
ck("...and logout empties it again", rc == 0 and (crew2 / ".tares-token").read_text().strip() == "", (rc, out))

print("a Cloud login and a cell login replace each other")
WORLD["calls"].clear()
L.login_file().unlink(missing_ok=True)
browser()
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
cloud_key = json.loads(L.login_file().read_text())["key"]
ck("a Cloud login first", rc == 0 and cloud_key.startswith("nvf_k"), (rc, out))
rc, out, err = cli("login", "--cell", BASE, "--key-stdin", "--json", stdin=KEY + "\n")
ck("a cell login after it revokes the Cloud key", rc == 0 and ("logout", cloud_key) in WORLD["calls"],
   (rc, out, WORLD["calls"]))
WORLD["calls"].clear()
L.DEFAULT_CLOUD = BASE      # where a wrongly revoked cell key would go
browser()
rc, out, err = run(L.login, BASE, wait=10, json_out=True)
ck("a Cloud login after a cell login does not send the cell key to anyone", rc == 0
   and not [c for c in WORLD["calls"] if c[1] == KEY], WORLD["calls"])
L.login_file().unlink(missing_ok=True)

print("the key is in no output")
ck("no stdout or stderr held a key", all("topsecret" not in o and "nvf_" not in o for o in OUTPUTS)
   and len(OUTPUTS) > 10)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
