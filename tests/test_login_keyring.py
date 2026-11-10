"""Where `tares login` keeps the key (TR-949): the OS keychain through keyring, a plain file with owner-only
permissions where there is none, and the move of an older login's key from login.json into the keychain.
Only a stub in-memory keyring backend and fake keys are used here; this test never touches a real keychain,
the real ~/.tares or the real ~/factory (everything lives in temp folders).
"""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = tempfile.mkdtemp()
os.environ["HOME"] = os.path.join(ROOT, "home")
os.environ["TARES_HOME"] = os.path.join(ROOT, "tares")
os.environ["FACTORY_CREW_HOME"] = os.path.join(ROOT, "no-crew")
for k in ("TARESD_URL", "TARES_FACTORY_URL", "TARES_AUTH_TOKEN", "TARES_CLOUD_URL", "TARES_KEYRING",
          "TARES_FACTORY_TOKEN_FILE", "FACTORY_STATION"):
    os.environ.pop(k, None)
# If the stub below ever fails to install, keyring must fall back to a backend that refuses everything,
# never to the machine's real keychain.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
os.environ["TARES_KEYRING"] = "on"      # the keychain is opt-in; the stub backend below stands in for it

import keyring  # noqa: E402
import keyring.backend  # noqa: E402
import keyring.backends.fail  # noqa: E402
import keyring.errors  # noqa: E402


class Stub(keyring.backend.KeyringBackend):
    """An in-memory keychain. `calls` counts what asked it, `refuse` makes it refuse to store."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}
        self.calls = []
        self.refuse = False
        self.forget = False     # says yes to a write and keeps nothing
        self.broken_get = False  # a locked keychain: every read raises
        self.fail_first_get = False  # one read raises, then the keychain answers again

    def set_password(self, service, username, password):
        self.calls.append(("set", service, username))
        if self.refuse:
            raise keyring.errors.PasswordSetError("refused")
        if self.forget:
            return
        self.items[(service, username)] = password

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        if self.fail_first_get:
            self.fail_first_get = False
            raise keyring.errors.KeyringLocked("locked once")
        if self.broken_get:
            raise keyring.errors.KeyringLocked("locked")
        return self.items.get((service, username))

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("not there")
        del self.items[(service, username)]


STUB = Stub()
keyring.set_keyring(STUB)      # before tares.login looks: the real keychain is never asked
assert keyring.get_keyring() is STUB, "the stub keychain did not install; refusing to go on"

from tares import login as L  # noqa: E402
from tares.crew import perms, state  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def reset(backend=None):
    """A clean ~/.tares and a fresh backend (the stub by default)."""
    STUB.items.clear()
    STUB.calls.clear()
    STUB.refuse = False
    STUB.forget = False
    STUB.broken_get = False
    STUB.fail_first_get = False
    WORLD["revoke_fails"] = False
    WORLD["version"] = "1.44.0"
    WORLD["seen"].clear()
    keyring.set_keyring(backend or STUB)
    assert keyring.get_keyring() is (backend or STUB), "the test keychain did not install"
    L._told.clear()
    L.login_file().unlink(missing_ok=True)


def captured(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rv = fn(*a, **kw)
    return rv, out.getvalue(), err.getvalue()


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ── a fake control plane that is also the workspace ────────────────────────────────────────────
WORLD = {"n": 0, "challenge": "", "logouts": [], "revoke_fails": False, "version": "1.44.0", "seen": []}


def _s256(v):
    import base64
    import hashlib
    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


class Fake(BaseHTTPRequestHandler):
    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._json(200, {"status": "ok", "version": WORLD["version"]} if self.path == "/health" else {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/exchange":
            if _s256(body.get("code_verifier", "")) != WORLD["challenge"]:
                return self._json(400, {"detail": "bad verifier"})
            WORLD["n"] += 1
            return self._json(200, {"token": f"nvf_kc{WORLD['n']}_fakesecret", "key_id": f"kc{WORLD['n']}",
                                    "cell": "factory", "cell_url": BASE})
        if self.path == "/api/cli/logout":
            WORLD["logouts"].append(self.headers.get("Authorization"))
            WORLD["seen"].append((self.headers.get("Authorization"), dict(STUB.items)))   # what the keychain held at that moment
            if WORLD["revoke_fails"]:
                return self._json(500, {"detail": "try later"})
            return self._json(200, {"revoked": True})
        self._json(404, {})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def browser():
    def open_(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        WORLD["challenge"] = q.get("challenge", "")

        def go():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{q['port']}/callback?code=c1&state={q['state']}",
                                       timeout=5).read()
            except Exception:
                pass
        threading.Timer(0.2, go).start()
        return True
    L.webbrowser.open = open_


def read_file():
    return json.loads(L.login_file().read_text())


def key_now():
    """The key the fake control plane handed out last."""
    return f"nvf_kc{WORLD['n']}_fakesecret"



print("login with a keychain: the key goes there, the file keeps only what is not secret")
reset()
browser()
rc, out, err = captured(L.login, BASE, wait=10)
f = read_file()
ck("login succeeds", rc == 0, (rc, out, err))
ck("the key is in the keychain under service tares and the workspace address",
   STUB.items.get(("tares", BASE)) == key_now(), STUB.items)
ck("login.json holds no key", "key" not in f and key_now() not in L.login_file().read_text(), f)
ck("login.json keeps the workspace, key id and machine",
   f["cell_url"] == BASE and f["cell"] == "factory" and f["key_id"] == "kc1" and f["cloud"] == BASE
   and f["machine"] and f["signed_in_at"] and f["key_store"] == "keychain", f)
ck("login.json is 600", mode(L.login_file()) == 0o600, oct(mode(L.login_file())))
ck("the command says once where the key went", out.count("The key is saved in the keychain (Stub)") == 1
   and out.count("The key is saved in") == 1, out)
ck("the key is in no output", key_now() not in out + err)
ck("saved() reads the key back from the keychain", (L.saved() or {}).get("key") == key_now(), L.saved())
ck("resolve() gives that key with its workspace", L.resolve() == (BASE, key_now(), "saved login"), L.resolve())

print("an older login.json with the key in it moves to the keychain once")
reset()
legacy = {"cloud": "https://cp.example", "cell": "acme", "cell_url": BASE + "/", "key": "nvf_old1_legacykey",   # gitleaks:allow, a fake key for a test
          "key_id": "old1", "machine": "m1", "signed_in_at": "2026-01-01T00:00:00Z"}
L.login_file().parent.mkdir(parents=True, exist_ok=True)
L.login_file().write_text(json.dumps(legacy))
s, out, err = captured(L.saved)
f = read_file()
ck("the first read returns the key", (s or {}).get("key") == "nvf_old1_legacykey", s)
ck("the key is in the keychain (account is the workspace address)",
   STUB.items.get(("tares", BASE)) == "nvf_old1_legacykey", STUB.items)
ck("and gone from the file", "key" not in f and "nvf_old1_legacykey" not in L.login_file().read_text(), f)
ck("the other fields are kept", all(f[k] == legacy[k] for k in ("cloud", "cell", "cell_url", "key_id", "machine",
                                                                   "signed_in_at")), f)
ck("it says so once, on stderr, without the key", err.count("moved your saved key") == 1 and out == ""
   and "nvf_old1_legacykey" not in err, (out, err))
before = L.login_file().read_bytes()
STUB.calls.clear()
s2, out2, err2 = captured(L.saved)
ck("the second read says nothing and writes nothing", err2 == "" and out2 == "" and L.login_file().read_bytes() == before)
ck("and still returns the key", (s2 or {}).get("key") == "nvf_old1_legacykey")
ck("login.json is 600", mode(L.login_file()) == 0o600)

print("a keychain that refuses leaves the older login as it was")
reset()
STUB.refuse = True
L.login_file().write_text(json.dumps(legacy))
s, out, err = captured(L.saved)
ck("the key is still returned", (s or {}).get("key") == "nvf_old1_legacykey", s)
ck("and still in the file, nothing lost", read_file().get("key") == "nvf_old1_legacykey" and not STUB.items)
ck("with nothing said", err == "" and out == "", (out, err))
STUB.refuse = False
s, out, err = captured(L.saved)
ck("once the keychain works it moves", "key" not in read_file() and STUB.items.get(("tares", BASE)))

print("no keychain on this machine: the key stays in login.json, 0600, and the command says so")
for label, setup in (("keyring has no backend", lambda: keyring.set_keyring(keyring.backends.fail.Keyring())),
                     ("keyring is not installed", None)):
    reset()
    if setup:
        setup()
    saved_module = sys.modules.get("keyring")
    if setup is None:
        sys.modules["keyring"] = None       # `import keyring` raises ImportError
    try:
        browser()
        rc, out, err = captured(L.login, BASE, wait=10)
        f = read_file()
        ck(f"{label}: login succeeds", rc == 0, (rc, out, err))
        ck(f"{label}: the key is in the file", f.get("key") == key_now() and f.get("key_store") == "file", f)
        ck(f"{label}: the file is 600", mode(L.login_file()) == 0o600, oct(mode(L.login_file())))
        ck(f"{label}: the command names the file once and why",
           out.count(f"The key is saved in {L.login_file()}, readable only by you, because no OS keychain works") == 1, out)
        ck(f"{label}: saved() works", (L.saved() or {}).get("key") == key_now())
        ck(f"{label}: the key is in no output", key_now() not in out + err)
    finally:
        if setup is None:
            sys.modules["keyring"] = saved_module

print("a keychain that is there but refuses the key: the file, and the command says that")
reset()
STUB.refuse = True
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("login succeeds", rc == 0)
ck("the key is in the file", read_file().get("key") == key_now() and not STUB.items)
ck("it says the keychain would not take it", f"{L.login_file()}, readable only by you, because the OS keychain would not take it" in out, out)
STUB.refuse = False

print("a keychain that says yes and keeps nothing: read back, so the key stays in the file")
reset()
STUB.forget = True
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("login succeeds", rc == 0)
ck("the key is in the file, since the keychain did not hold it", read_file().get("key") == key_now() and read_file().get("key_store") == "file", read_file())
STUB.forget = False

print("unless TARES_KEYRING is on, no keychain is asked")
reset()
os.environ.pop("TARES_KEYRING")
STUB.calls.clear()
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("by default the key stays in the file and the stub is not asked", rc == 0 and read_file().get("key") == key_now() and STUB.calls == [], STUB.calls)
os.environ["TARES_KEYRING"] = "off"
browser()
STUB.calls.clear()
rc, out, err = captured(L.login, BASE, wait=10)
ck("the key is in the file", rc == 0 and read_file().get("key") == key_now())
ck("the stub was not asked", STUB.calls == [], STUB.calls)
L.login_file().write_text(json.dumps({"cell_url": BASE, "cell": "factory", "key_store": "keychain"}))
L._told.clear()
s, out, err = captured(L.saved)
s_again, out2, err2 = captured(L.saved)
ck("saved() is None and says why once", s is None and s_again is None and err.count("OS keychain, which cannot be used here") == 1
   and err2 == "", (err, err2))
os.environ["TARES_KEYRING"] = "on"

print("logging in to another workspace removes the old workspace's key from the keychain")
reset()
browser()
captured(L.login, BASE, wait=10)
STUB.items[("tares", "http://other.example")] = "nvf_x_other"      # stands for the previous workspace
L.login_file().write_text(json.dumps({**read_file(), "cell_url": "http://other.example"}))
STUB.items.pop(("tares", BASE))
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("the new workspace has its key", STUB.items.get(("tares", BASE)) == key_now(), STUB.items)
ck("the old one's entry is gone", ("tares", "http://other.example") not in STUB.items, STUB.items)
old_seen = [held for who, held in WORLD["seen"] if who == "Bearer nvf_x_other"]
ck("the old key was revoked before its keychain entry was removed (F11)",
   len(old_seen) == 1 and ("tares", "http://other.example") in old_seen[0], WORLD["seen"])

print("logout removes the keychain entry and the file")
reset()
browser()
captured(L.login, BASE, wait=10)
rc, out, err = captured(L.logout)
ck("logout succeeds", rc == 0, (rc, out, err))
ck("the keychain is empty", STUB.items == {}, STUB.items)
ck("the file is gone", not L.login_file().exists())
ck("the control plane was handed the key as a header", WORLD["logouts"][-1] == f"Bearer {key_now()}", WORLD["logouts"])
ck("no key in the output", key_now() not in out + err)
reset()
L.login_file().write_text(json.dumps({"cell_url": BASE, "cloud": BASE, "cell": "factory", "key_store": "keychain"}))
STUB.items[("tares", BASE)] = "nvf_lost_1"
rc, out, err = captured(L.logout)
ck("logout clears both the file and the keychain entry, though the control plane was asked with a key from the keychain", rc == 0 and STUB.items == {} and not L.login_file().exists(), (rc, STUB.items))

print("the line after login says why the key is in the file: the keychain is off by default")
for label, setting in (("TARES_KEYRING unset", None), ("TARES_KEYRING=off", "off")):
    reset()
    os.environ.pop("TARES_KEYRING", None)
    if setting:
        os.environ["TARES_KEYRING"] = setting
    browser()
    rc, out, err = captured(L.login, BASE, wait=10)
    ck(f"{label}: the line says the keychain is off and how to turn it on",
       f"The key is saved in {L.login_file()}, readable only by you, because the OS keychain is off "
       f"(set TARES_KEYRING=on to use it)." in out and "no OS keychain works" not in out, out)
os.environ["TARES_KEYRING"] = "on"
reset(keyring.backends.fail.Keyring())
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("TARES_KEYRING=on but no keychain works: it says that instead",
   "because no OS keychain works on this machine." in out and "is off" not in out, out)

print("logout while the saved key cannot be read: nothing is deleted, the key is not forgotten")


def keychain_login():
    reset()
    browser()
    captured(L.login, BASE, wait=10)
    return key_now(), L.login_file().read_bytes()


def refused(label, rc, out, err, key, before, words):
    ck(f"{label}: exit 1 and the reason in plain words", rc == 1 and words in out and "not signed in" not in out, (rc, out, err))
    ck(f"{label}: login.json is untouched", L.login_file().read_bytes() == before)
    ck(f"{label}: the keychain entry is kept", STUB.items.get(("tares", BASE)) == key, STUB.items)
    ck(f"{label}: the key was not sent anywhere and is in no output", len(WORLD["logouts"]) == n_before and key not in out + err,
       WORLD["logouts"])


key, before = keychain_login()
os.environ["TARES_KEYRING"] = "off"                       # (b) the keychain is off after a keychain login
n_before = len(WORLD["logouts"])
rc, out, err = captured(L.logout)
refused("keychain off", rc, out, err, key, before, "the OS keychain is off here (TARES_KEYRING is not on)")
ck("keychain off: it says what to do", "TARES_KEYRING=on" in out, out)
ck("keychain off: it also says how to give up on the key (F12)",
   f"Or, to start over, delete {L.login_file()} and remove that key yourself under Security > API keys." in out, out)
os.environ["TARES_KEYRING"] = "on"
STUB.broken_get = True                                    # (a) a locked or denied keychain
rc, out, err = captured(L.logout)
refused("keychain locked", rc, out, err, key, before, "the OS keychain would not give it (locked, or access was denied)")
STUB.broken_get = False
keyring.set_keyring(keyring.backends.fail.Keyring())      # on, but no keychain works now
rc, out, err = captured(L.logout)
refused("no keychain works now", rc, out, err, key, before, "no OS keychain works on this machine now")
keyring.set_keyring(STUB)
rc, out, err = captured(L.logout)
ck("with the keychain back, logout takes the key off the workspace and clears everything",
   rc == 0 and WORLD["logouts"][-1] == f"Bearer {key}" and STUB.items == {} and not L.login_file().exists(), (rc, out, WORLD["logouts"]))
reset()
L.login_file().write_text(json.dumps({"cell_url": BASE, "cloud": BASE, "cell": "factory", "key_store": "keychain"}))
rc, out, err = captured(L.logout)
ck("a keychain that answers with no entry is not signed in: logout clears the file and exits 0",
   rc == 0 and "not signed in" in out and not L.login_file().exists(), (rc, out))

print("login while the saved key cannot be read: it stops before making a new key")
key, before = keychain_login()
for label, setup, words in (
        ("keychain off", lambda: os.environ.__setitem__("TARES_KEYRING", "off"), "the OS keychain is off here"),
        ("keychain locked", lambda: setattr(STUB, "broken_get", True), "would not give it (locked, or access was denied)")):
    setup()
    n_before, n_made = len(WORLD["logouts"]), WORLD["n"]
    browser()
    rc, out, err = captured(L.login, BASE, wait=3)
    ck(f"{label}: login stops with exit 1 and says why", rc == 1 and words in out and "tares login:" in out, (rc, out, err))
    ck(f"{label}: no new key was made and nothing was removed", WORLD["n"] == n_made and len(WORLD["logouts"]) == n_before, WORLD["n"])
    ck(f"{label}: login.json and the keychain entry are as they were",
       L.login_file().read_bytes() == before and STUB.items.get(("tares", BASE)) == key, STUB.items)
    ck(f"{label}: it names the old key's id and how to start over", "key kc" in out and f"delete {L.login_file()}" in out, out)
    os.environ["TARES_KEYRING"] = "on"
    STUB.broken_get = False
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("with the keychain readable, login replaces the key and revokes the old one",
   rc == 0 and f"Bearer {key}" in WORLD["logouts"] and STUB.items.get(("tares", BASE)) == key_now() != key, (rc, out, WORLD["logouts"]))

print("a login that cannot be saved safely: no traceback, the new key is revoked, the old login is kept")
real_owner_only = perms.owner_only
reset()
legacy2 = dict(legacy, key="nvf_old2_legacykey")      # gitleaks:allow, a fake key for a test
L.login_file().parent.mkdir(parents=True, exist_ok=True)
L.login_file().write_text(json.dumps(legacy2))
perms.owner_only = lambda *a, **k: False              # icacls failing, or a chmod that cannot be done
try:
    before = L.login_file().read_bytes()
    s, out, err = captured(L.saved)
    ck("saved() with a file that cannot be made private: no traceback, the key is still returned",
       (s or {}).get("key") == "nvf_old2_legacykey", s)
    ck("...and the file is left as it was", L.login_file().read_bytes() == before)
    ck("...with nothing said about a move", "moved your saved key" not in err and out == "", (out, err))
    reset()
    STUB.items[("tares", BASE)] = "nvf_prev_inkeychain"
    L.login_file().write_text(json.dumps({"cell_url": BASE, "cloud": BASE, "cell": "factory", "key_store": "keychain",
                                          "key_id": "prev", "machine": "m1"}))
    before = L.login_file().read_bytes()
    n_before = len(WORLD["logouts"])
    browser()
    rc, out, err = captured(L.login, BASE, wait=10)
    ck("login: exit 1 with a message, not a traceback", rc == 1 and "could not save the login" in out and "readable by you only" in out, (rc, out, err))
    ck("login: the key it had just made was revoked", WORLD["logouts"][n_before:] == [f"Bearer {key_now()}"], WORLD["logouts"][n_before:])
    ck("login: the old login is as it was, file and keychain entry",
       L.login_file().read_bytes() == before and STUB.items == {("tares", BASE): "nvf_prev_inkeychain"}, (STUB.items,))
    ck("login: the new key is in no output", key_now() not in out + err)
    reset()
    keyring.set_keyring(keyring.backends.fail.Keyring())       # no keychain: the file path fails the same way
    browser()
    n_before = len(WORLD["logouts"])
    rc, out, err = captured(L.login, BASE, wait=10)
    ck("login with no keychain: the same, and nothing is saved",
       rc == 1 and "could not save the login" in out and not L.login_file().exists()
       and WORLD["logouts"][n_before:] == [f"Bearer {key_now()}"], (rc, out, WORLD["logouts"][n_before:]))
finally:
    perms.owner_only = real_owner_only
reset()

print("a login that cannot be saved, and a new key that cannot be removed again: it says where to remove it (F9)")
real_owner_only = perms.owner_only
reset()
perms.owner_only = lambda *a, **k: False
try:
    WORLD["revoke_fails"] = True
    browser()
    rc, out, err = captured(L.login, BASE, wait=10)
    ck("save failure and failed revoke: exit 1", rc == 1 and "could not save the login" in out, (rc, out, err))
    ck("it does not claim the key was removed", "was removed" not in out, out)
    ck("it names the key and where to remove it",
       f"(key kc{WORLD['n']})" in out and f"Security > API keys on {BASE}" not in out and "Security > API keys on factory" in out, out)
    ck("the key is in no output", key_now() not in out + err)
    reset()
    browser()
    rc, out, err = captured(L.login, BASE, wait=10)
    ck("save failure and revoke fine: it still says the key was removed", rc == 1 and "was removed" in out and "Security > API keys" not in out, out)
finally:
    perms.owner_only = real_owner_only
reset()
print("a workspace too old for tares login, and a new key that cannot be removed again: it says where to remove it (F9)")
WORLD["version"] = "1.0.0"
WORLD["revoke_fails"] = True
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("version gate and failed revoke: Nothing was saved, and where to remove the key",
   rc == 1 and "Nothing was saved." in out and "could not be removed" in out and f"(key kc{WORLD['n']})" in out, (rc, out))
reset()
WORLD["version"] = "1.0.0"
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("version gate and revoke fine: the old words, nothing about removing", rc == 1 and "Nothing was saved." in out and "could not be removed" not in out, out)
reset()

print("a keychain read that failed once is not an empty entry: a failed file write does not delete it (F10)")
perms.owner_only = lambda *a, **k: False
try:
    STUB.items[("tares", BASE)] = "nvf_prev_inkeychain"
    STUB.fail_first_get = True
    STUB.calls.clear()
    try:
        L._save({"cell_url": BASE, "cloud": BASE, "cell": "factory", "key": "nvf_new_1", "key_id": "new1"})
        raised = False
    except CrewError:
        raised = True
    except OSError:
        raised = True
    ck("the failed write was raised", raised)
    ck("the keychain entry was not deleted", not [c for c in STUB.calls if c[0] == "delete"], STUB.calls)
    reset()
    # a read that works and finds nothing: the rollback still removes the entry it made
    try:
        L._save({"cell_url": BASE, "cloud": BASE, "cell": "factory", "key": "nvf_new_2", "key_id": "new2"})
    except (CrewError, OSError):
        pass
    ck("an empty entry that was written then rolled back is removed again", STUB.items == {}, STUB.items)
finally:
    perms.owner_only = real_owner_only
reset()

print("the crew's key file: a failed write says so and changes nothing")
crew2 = Path(ROOT) / "crew2"
crew2.mkdir()
os.environ["FACTORY_CREW_HOME"] = str(crew2)
perms.owner_only = lambda *a, **k: False
try:
    rc, out, err = captured(L._crew_follows, "nvf_for_the_crew")
    ck("_crew_follows: False and a plain message naming the file", rc is False and "could not update the crew's key file" in out
       and str(crew2 / ".tares-token") in out, (rc, out))
    ck("...no key file and no temp file left, no key in the output", not (crew2 / ".tares-token").exists()
       and not list(crew2.glob("*.tmp")) and "nvf_for_the_crew" not in out + err, list(crew2.iterdir()))
    (crew2 / ".tares-token").write_text("nvf_theirs\n")
    rc, out, err = captured(L._crew_follows, "", "nvf_theirs")
    ck("a logout that cannot empty the file reports it and leaves the file alone",
       rc is False and "could not update the crew's key file" in out
       and (crew2 / ".tares-token").read_text() == "nvf_theirs\n" and not list(crew2.glob("*.tmp")), (rc, out))
finally:
    perms.owner_only = real_owner_only
os.environ["FACTORY_CREW_HOME"] = os.path.join(ROOT, "no-crew")

print("owner_only on Mac and Linux: False when the chmod fails")
orig_windows_fn, orig_chmod = perms._windows, os.chmod
perms._windows = lambda: False
target2 = Path(ROOT) / "chmod-target"
target2.write_text("x")


def bad_chmod(*a, **k):
    raise PermissionError("no")


try:
    ck("a chmod that works gives True and mode 600", perms.owner_only(target2) is True and mode(target2) == 0o600, oct(mode(target2)))
    os.chmod = bad_chmod
    ck("a chmod that fails gives False, not a traceback", perms.owner_only(target2) is False)
    refused_write = ""
    try:
        state.atomic_write_text(Path(ROOT) / "private.txt", "secret-fake", mode=0o600, private=True)
    except CrewError as e:
        refused_write = str(e)
    ck("and a private write is refused: nothing written, no temp file",
       "readable by you only" in refused_write and not (Path(ROOT) / "private.txt").exists()
       and not list(Path(ROOT).glob("private.txt.*")), refused_write)
finally:
    os.chmod, perms._windows = orig_chmod, orig_windows_fn

print("no real keychain backend was ever loaded")
real = [m for m in sys.modules if m.startswith("keyring.backends.")
        and m.rsplit(".", 1)[-1] in ("macOS", "Windows", "SecretService", "kwallet", "libsecret", "chainer")]
ck("only the stub and the refusing backend were used", real == [], real)

print("stations read the crew key from its file, never from the keychain")
reset()
STUB.items[("tares", BASE)] = "nvf_person_key"
L.login_file().write_text(json.dumps({"cell_url": BASE, "cell": "factory", "key_store": "keychain"}))
crew = Path(ROOT) / "crew"
crew.mkdir()
paths = CrewPaths.at(crew)
STUB.calls.clear()
station = TaresClient(paths, env={"FACTORY_STATION": "crew-reviewer", "TARESD_URL": BASE})
ck("a station with no crew file has no key, whatever the keychain holds", station.tares_token() == "", station.tares_token())
(crew / ".tares-token").write_text("nvf_crew_filekey\n")
ck("a station uses the crew file", station.tares_token() == "nvf_crew_filekey")
tf = Path(ROOT) / "elsewhere-token"
tf.write_text("nvf_other_filekey\n")
ck("TARES_FACTORY_TOKEN_FILE names the file", TaresClient(paths, env={"TARES_FACTORY_TOKEN_FILE": str(tf)}).tares_token() == "nvf_other_filekey")
person = TaresClient(paths, env={"TARES_AUTH_TOKEN": "nvf_env_key"})
ck("the person's terminal uses TARES_AUTH_TOKEN", person.tares_token() == "nvf_env_key")
ck("none of that asked the keychain", STUB.calls == [], STUB.calls)

print("the crew's key file: owner only")
state.write_token("nvf_crew_filekey2", paths)
ck("the crew file holds the key and a newline", (crew / ".tares-token").read_text() == "nvf_crew_filekey2\n")
ck("and is 600", mode(crew / ".tares-token") == 0o600, oct(mode(crew / ".tares-token")))
os.environ["FACTORY_CREW_HOME"] = str(crew)
browser()
rc, out, err = captured(L.login, BASE, wait=10)
ck("a login gives the crew home's file the key, 600",
   (crew / ".tares-token").read_text().strip() == "nvf_kc%d_fakesecret" % WORLD["n"] and mode(crew / ".tares-token") == 0o600,
   (crew / ".tares-token").read_text())
os.environ["FACTORY_CREW_HOME"] = os.path.join(ROOT, "no-crew")

print("Windows: an access list for the current user alone, set before the key is written")
calls = []
orig_windows, orig_run = perms._windows, subprocess.run


WHOAMI = b'"azuread\\kirank","S-1-12-1-1111-2222-3333-4444"\r\n'    # an Azure AD login: its name may not resolve in icacls


def fake_run(cmd, **kw):
    calls.append(cmd)
    if cmd[0] == "whoami":
        return subprocess.CompletedProcess(cmd, fake_run.whoami_rc, stdout=fake_run.whoami_out, stderr=b"")
    return subprocess.CompletedProcess(cmd, fake_run.rc)


fake_run.rc = 0
fake_run.whoami_rc, fake_run.whoami_out = 0, WHOAMI
perms._windows = lambda: True
os.environ["USERNAME"], os.environ["USERDOMAIN"] = "kiran", "WORKGROUP"
try:
    target = Path(ROOT) / "win-token"
    ck("owner_only runs icacls for the user alone, named by security identifier",
       perms.owner_only(target, run=fake_run) is True
       and calls[-2] == ["whoami", "/user", "/fo", "csv", "/nh"]
       and calls[-1] == ["icacls", str(target), "/inheritance:r", "/grant:r", "*S-1-12-1-1111-2222-3333-4444:F"], calls)
    ck("icacls_command is pure: it grants whoever it is given, else DOMAIN\\name from the environment",
       perms.icacls_command(target, "*S-1-5-21-7-8-9-1001") == ["icacls", str(target), "/inheritance:r", "/grant:r", "*S-1-5-21-7-8-9-1001:F"]
       and perms.icacls_command(target) == ["icacls", str(target), "/inheritance:r", "/grant:r", "WORKGROUP\\kiran:F"])
    for label, rc_, out_ in (("whoami fails", 1, WHOAMI), ("whoami prints something else", 0, b"not csv at all\r\n"),
                             ("whoami prints a name where the SID goes", 0, b'"a\\b","kiran"\r\n"'), ("whoami prints nothing", 0, b"")):
        fake_run.whoami_rc, fake_run.whoami_out = rc_, out_
        calls.clear()
        ck(f"{label}: icacls gets DOMAIN\\name instead",
           perms.owner_only(target, run=fake_run) is True and calls[-1] == ["icacls", str(target), "/inheritance:r", "/grant:r", "WORKGROUP\\kiran:F"], calls)
    fake_run.whoami_rc, fake_run.whoami_out = 0, WHOAMI
    ck("whoami missing: the same fallback", perms.windows_grantee(run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())) == "WORKGROUP\\kiran")
    fake_run.rc = 5
    ck("and says false when icacls fails", perms.owner_only(target, run=fake_run) is False)
    ck("and when icacls is missing", perms.owner_only(target, run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())) is False)
    fake_run.rc = 0
    calls.clear()
    subprocess.run = fake_run
    seen = {}
    real_owner = perms.owner_only

    def spy(path, **kw):
        # at this moment the key must not be in the file yet
        seen["content_before"] = Path(path).read_bytes()
        return real_owner(path, **kw)
    perms.owner_only = spy
    state.write_token("nvf_win_key_1", paths)
    ck("write_token restricts the temp file before the key is in it", seen.get("content_before") == b"", seen)
    icacls_calls = [c for c in calls if c[0] == "icacls"]
    ck("the icacls call carries the path and the user, not the key", len(icacls_calls) == 1 and "nvf_win_key_1" not in " ".join(" ".join(c) for c in calls), calls)
    ck("the crew file holds the key", (crew / ".tares-token").read_text() == "nvf_win_key_1\n")
    perms.owner_only = real_owner
    fake_run.rc = 1
    before = (crew / ".tares-token").read_text()
    try:
        state.write_token("nvf_win_key_2", paths)
        refused = ""
    except CrewError as e:
        refused = str(e)
    ck("a file that cannot be restricted is refused, not written", "readable by you only" in refused
       and (crew / ".tares-token").read_text() == before and not list(crew.glob("*.tmp")), refused)
    fake_run.rc = 0
    calls.clear()
    reset()
    L._save({"cloud": BASE, "cell": "factory", "cell_url": BASE, "key": "nvf_win_key_3", "key_id": "w"})
    ck("login.json goes through the same restriction", any(c[0] == "icacls" and c[1].startswith(str(L.login_file().parent)) for c in calls), calls)
finally:
    perms._windows, subprocess.run = orig_windows, orig_run
    perms.owner_only = real_owner

srv.shutdown()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
