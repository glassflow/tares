"""`tares login --json`, `tares login --show [--json]` and `tares logout --json` (TR-1282) against a fake
control plane and workspace on localhost: the documents a program reads, the error codes, and that the
key never appears in any output. The text output is covered by tests/test_login.py (unchanged).
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

from tares import cli as C  # noqa: E402
from tares import login as L  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


WORLD = {"version": "1.44.0", "exchange_status": 200, "logout_status": 200, "delete_status": 200,
         "good_keys": set(), "n": 0, "challenge": ""}
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
            return self._json(200, {"status": "ok", "version": WORLD["version"]})
        if self.path == "/api/crew/settings":
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            return self._json(200 if bearer in WORLD["good_keys"] else 401, {"settings": {}})
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
                                    "cell_url": BASE})
        if self.path == "/api/cli/logout":
            return self._json(WORLD["logout_status"], {"revoked": True})
        self._json(404, {})

    def do_DELETE(self):  # noqa: N802
        self._json(WORLD["delete_status"], {})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def browser(callback=True, state_override=None):
    def open_(url):
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
    L.webbrowser.open = open_


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
ck("revoke failed: error document naming the cell and key, exit 1", rc == 1 and err_code(out) == "error"
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
ns = argparse.Namespace(cloud=BASE, no_browser=False, show=True, json=True)
rc, out, err = run(C._login, ns)
ck("--show --cloud --json: usage error document, exit 2", rc == 2 and err_code(out) == "usage", (rc, out))
ns = argparse.Namespace(cloud="", no_browser=True, show=True, json=False)
rc, out, err = run(C._login, ns)
ck("--show --no-browser: exit 2, text on stderr", rc == 2 and out == "" and "--show" in err, (rc, out, err))
ns = argparse.Namespace(cloud="", no_browser=False, show=True, json=True)
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
    e = need({"TARES_FACTORY_SOURCE": "local default", "TARESD_URL": "http://127.0.0.1:1"})
    ck("nothing named and no saved login: not_signed_in", isinstance(e, NotSignedInError) and e.code == "not_signed_in", e)
    ck("...with the old message text", e is not None and "no Tares answers at" in str(e), e)
    e = need({"TARESD_URL": "http://127.0.0.1:1"})
    ck("an address named by the person stays a plain error", e is not None and not isinstance(e, NotSignedInError)
       and e.code == "error", e)
finally:
    if saved_login is not None:
        login_path.write_bytes(saved_login)

print("the key is in no output")
ck("no stdout or stderr held a key", all("topsecret" not in o and "nvf_" not in o for o in OUTPUTS)
   and len(OUTPUTS) > 10)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
