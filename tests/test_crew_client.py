"""The crew engine's client for Tares (tares/crew/client.py) against a fake Tares: an in-process http.server.
Mirrors tests/factory/tares_test.sh (the token only as a file for stations, a second machine asks nothing,
a station's spawn uses the orchestrator's env, a stale inherited key is ignored, a refused key never
replaces the crew's, Tares down is said plainly) and the bash functions it ports: tares_http, uri, tares_url,
mcp_url, tares_on, require_tares, mcp_on, require_mcp, key_ok, key_name, keep_token, crew_key, tares_token,
no_key_hint, crew_projects*, project_row, project_repo_rows, all_repos, settings_*, crew_settings, setting,
releaser_on, crew_roles, grants_read, grants_md, grants_sum, desk_json.

Everything runs in temp folders with fake keys and explicit environments: the real environment's TARES_*
variables, ~/.tares and ~/factory are never read.
"""
import contextlib
import io
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = tempfile.mkdtemp()
os.environ["HOME"] = os.path.join(ROOT, "home")
os.environ["USERPROFILE"] = os.environ["HOME"]  # Windows reads this before HOME
for k in [k for k in os.environ if k.startswith(("TARES", "FACTORY"))]:
    os.environ.pop(k)
# a regression in the TARES_KEYRING gate must fail this test, never reach a person's OS keychain
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
try:
    import keyring  # noqa: E402
    import keyring.backends.fail  # noqa: E402
    keyring.set_keyring(keyring.backends.fail.Keyring())
except ImportError:
    pass

from tares.crew import client as C  # noqa: E402
from tares.crew import state  # noqa: E402
from tares.crew.client import TaresClient, TaresHTTPError  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── a fake Tares ────────────────────────────────────────────────────────────────────────────────
TOKEN = "stub-token"
SETTINGS = {"autonomy": "L4-ship", "releaser": False, "challenger": True, "builders_max": 2}
PROJECTS = [
    {"id": "p-1", "name": "Links App", "repos": [{"path": "/r/links"}, {"path": "/r/shared"}, {"path": None}, "glassflow/plain"]},
    {"id": "p-2", "name": "🔄 Emoji App", "repos": [{"path": "/r/emoji"}, {"path": "/r/shared"}]},
]
GRANTS = {"all": '- "the reviewer may comment on draft PRs" (2026-01-02)', "projects": {
    "p-1": '- "builders may bump patch versions of dependencies" (2026-01-03)\n- "second line" (2026-01-04)'}}
WORLD = {"auth": True, "log": [], "mcp": True, "settings_status": 200, "elsewhere": 0}


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _json(self, code, body):
        raw = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        u = urlparse(self.path)
        WORLD["log"].append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                             "leak": [k for k, v in self.headers.items() if k.lower() != "authorization" and TOKEN in v] or None,
                             "accept": self.headers.get("Accept"), "ctype": self.headers.get("Content-Type"), "body": body})
        if u.path == "/health":
            return self._json(200, {"status": "ok"})
        if u.path == "/elsewhere":
            WORLD["elsewhere"] += 1
            return self._json(200, {})
        if u.path == "/api/redirect-me":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if WORLD["auth"] and self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._json(401, {"detail": "unauthorized"})
        if u.path == "/mcp":
            if not WORLD["mcp"]:
                return self._json(404, {"detail": "no"})
            return self._json(200, '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"tares"}}}')
        if u.path == "/api/crew/anything":
            return self._json(200, {"ok": True})
        if u.path == "/api/crew/settings":
            return self._json(WORLD["settings_status"], {"settings": SETTINGS})
        if u.path == "/api/crew/projects":
            return self._json(200, {"projects": PROJECTS})
        if u.path == "/api/grants":
            q = parse_qs(u.query)
            if "project" in q:
                if q["project"][0] not in GRANTS["projects"]:
                    return self._json(404, {"detail": "unknown project"})
                return self._json(200, {"all": GRANTS["all"], "project": GRANTS["projects"][q["project"][0]]})
            return self._json(200, {"all": GRANTS["all"]})
        if u.path == "/api/desk":
            return self._json(200, {"desk": [{"id": "d1", "question": "ship?"}]})
        self._json(404, {})

    do_GET = do_POST = do_DELETE = _go  # noqa: N815

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


DEAD = f"http://127.0.0.1:{free_port()}"
HOMEN = [0]


def machine(env=None, **extra):
    """A client for a fresh machine: its own crew home, an explicit environment, Tares at BASE."""
    HOMEN[0] += 1
    home = Path(ROOT) / f"crew{HOMEN[0]}"
    home.mkdir()
    e = {"TARES_FACTORY_URL": BASE, "TARES_AUTH_TOKEN": TOKEN}
    e.update(env or {})
    e = {k: v for k, v in e.items() if v is not None}
    c = TaresClient(CrewPaths.at(home), env=e, **extra)
    return c, home


def seen(n=None):
    return WORLD["log"][-n:] if n else WORLD["log"]


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def err_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except CrewError as e:
        return e
    return None


# ── no process, no log line may carry the key ────────────────────────────────────────────────────
class Grab(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + " ".join(str(a) for a in (record.args or ())
                                                                if not isinstance(record.args, dict)))


grab = Grab()
logging.getLogger().addHandler(grab)
logging.getLogger().setLevel(logging.DEBUG)
spawned = []
real_popen = subprocess.Popen


# On Windows the client restricts the key file with icacls, after asking whoami for the user's SID
# (tares/crew/perms.py): those two are the only processes it may start, and nothing else.
WINDOWS_OK = ("whoami", "icacls")


def no_popen(*a, **kw):
    argv = a[0] if a else kw.get("args")
    if os.name == "nt" and isinstance(argv, (list, tuple)) and argv \
            and Path(str(argv[0])).stem.lower() in WINDOWS_OK:
        return real_popen(*a, **kw)
    spawned.append(a)
    raise AssertionError("the client must not start a process")


subprocess.Popen = no_popen

print("addresses: tares_url, mcp_url")
c, home = machine({"TARES_FACTORY_URL": "http://a:1/", "TARESD_URL": "http://b:2", "TARES_URL": "http://c:3"})
ck("TARES_FACTORY_URL first, one trailing slash dropped", c.tares_url() == "http://a:1")
c, _ = machine({"TARES_FACTORY_URL": "", "TARESD_URL": "http://b:2", "TARES_URL": "http://c:3"})
ck("an empty variable counts as unset: TARESD_URL next", c.tares_url() == "http://b:2")
c, _ = machine({"TARES_FACTORY_URL": None, "TARESD_URL": None, "TARES_URL": "http://c:3//"})
ck("then TARES_URL (only one slash dropped, as ${u%/} does)", c.tares_url() == "http://c:3/")
c, home = machine({"TARES_FACTORY_URL": None})
ck("then the local default", c.tares_url() == "http://127.0.0.1:8787")
(home / "crew.json").write_text(json.dumps({"tares": {"url": "https://cell.example/"}}))
ck("crew.json's tares.url comes before the default", c.tares_url() == "https://cell.example")
c, _ = machine({"TARES_FACTORY_URL": "http://127.0.0.1:9999"})
ck("local Tares: its MCP server on its own port", c.mcp_url() == "http://127.0.0.1:8788/mcp")
c, _ = machine({"TARES_FACTORY_URL": "http://localhost:9999/x", "TARES_MCP_HOST": "0.0.0.0", "TARES_MCP_PORT": "9000"})
ck("localhost with the MCP host and port set", c.mcp_url() == "http://0.0.0.0:9000/mcp")
c, _ = machine({"TARES_FACTORY_URL": "https://cell.example.com:8443/"})
ck("a remote Tares serves /mcp on its own host", c.mcp_url() == "https://cell.example.com:8443/mcp")
c, _ = machine({"TARES_FACTORY_URL": "cell.example.com"})
ck("an address without a scheme is read the way bash reads it", c.mcp_url() == "cell.example.com/mcp")
c, _ = machine({"TARES_FACTORY_MCP_URL": "http://m:1/mcp/"})
ck("TARES_FACTORY_MCP_URL overrides both", c.mcp_url() == "http://m:1/mcp")

print("small helpers: uri, key_name, cksum")
ck("uri keeps unreserved characters", C.uri("Aa0-_.~") == "Aa0-_.~")
ck("uri encodes spaces and emoji as UTF-8 bytes in upper-case hex", C.uri("🔄 Emoji App") == "%F0%9F%94%84%20Emoji%20App", C.uri("🔄 Emoji App"))
ck("uri encodes slash, question mark and ampersand", C.uri("a/b?c&d=e") == "a%2Fb%3Fc%26d%3De")
ck("key_name: no key", C.key_name("") == "no key")
ck("key_name: a key's id, never the key", C.key_name("nvf_dead0000_revoked") == "key dead0000")
ck("key_name: a token with no id", C.key_name("stub-token") == "a token with no key id" and C.key_name("nvf_abc") == "a token with no key id")
ck("cksum matches POSIX cksum (vectors from /usr/bin/cksum)",
   [C.cksum(b"abc"), C.cksum(b""), C.cksum(b"hello\n"), C.cksum("café 🔄\nline2\n".encode()), C.cksum(bytes(300))]
   == [1219131554, 4294967295, 3015617425, 673707734, 351385237])
if shutil.which("cksum"):
    data = os.urandom(5000)
    subprocess.Popen = real_popen  # the test itself may start the system cksum; the client may not
    try:
        sys_sum = int(subprocess.run(["cksum"], input=data, capture_output=True).stdout.split()[0])
    finally:
        subprocess.Popen = no_popen
    ck("and a random 5000 bytes", C.cksum(data) == sys_sum, (C.cksum(data), sys_sum))
ck("grants_sum takes text", C.grants_sum("abc") == 1219131554)

print("the token: a person's terminal, a station, the crew file")
c, home = machine({"TARES_AUTH_TOKEN": "person-key"})
ck("the person's terminal uses TARES_AUTH_TOKEN", not c.in_crew_session() and c.tares_token() == "person-key")
(home / ".tares-token").write_text("  crew key\n")
ck("crew_key strips all whitespace", c.crew_key() == "crewkey")
c2, home2 = machine({"TARES_AUTH_TOKEN": "stale-inherited", "FACTORY_STATION": "crew-reviewer"})
(home2 / ".tares-token").write_text("crew-key\n")
ck("a station ignores a stale inherited key and reads the crew file", c2.in_crew_session() and c2.tares_token() == "crew-key")
tf = Path(ROOT) / "elsewhere-token"
tf.write_text("file-key\n")
c3, _ = machine({"TARES_AUTH_TOKEN": "stale", "TARES_FACTORY_TOKEN_FILE": str(tf)})
ck("TARES_FACTORY_TOKEN_FILE makes a crew session and names the file", c3.in_crew_session() and c3.tares_token() == "file-key")
c4, _ = machine({"TARES_AUTH_TOKEN": None})
ck("no key anywhere: empty", c4.tares_token() == "")
ck("no_key_hint with a key: nothing", c.no_key_hint() == "")
ck("no_key_hint for the person's terminal", c4.no_key_hint() == "; if it has auth on, run tares login (or set TARES_AUTH_TOKEN for a Tares of your own)")
c5, home5 = machine({"TARES_AUTH_TOKEN": None, "FACTORY_STATION": "crew-reviewer"})
home5.joinpath(".tares-token").write_text("")
ck("no_key_hint for a station whose person signed out",
   c5.no_key_hint() == "; the crew has no key because the person signed out of Tares: they run tares login, and the crew picks it up")

print("is Tares there: tares_on, require_tares, mcp_on, require_mcp")
WORLD["log"].clear()
c, _ = machine()
ck("tares_on is true when /health answers", c.tares_on() is True and c.tares == BASE)
ck("/health is asked without the key", seen()[0]["path"] == "/health" and seen()[0]["auth"] is None, seen())
c.tares_on()
ck("and asked once", len([r for r in seen() if r["path"] == "/health"]) == 1)
c.require_tares()
down, _ = machine({"TARES_FACTORY_URL": DEAD})
e = err_of(down.require_tares)
ck("require_tares says plainly how to start one",
   e and str(e) == f"no Tares answers at {DEAD}. Start one with `tares up` in another terminal, connect to your Tares Cloud "
   "workspace with `tares login` (or set TARESD_URL to where yours runs), then try again.", e)
ck("and the line is prefixed like bash's die", e and e.line.startswith("tares factory: no Tares answers at "))
WORLD["log"].clear()
c, _ = machine({"TARES_FACTORY_MCP_URL": f"{BASE}/mcp"})
ck("mcp_on is true when an MCP server answers initialize", c.mcp_on() is True)
r = seen()[0]
ck("the MCP probe carries the token (auth on)", r["auth"] == f"Bearer {TOKEN}" and r["method"] == "POST", r)
ck("with the initialize call, as JSON, accepting event streams",
   r["accept"] == "application/json, text/event-stream" and r["ctype"] == "application/json"
   and json.loads(r["body"])["method"] == "initialize" and json.loads(r["body"])["params"]["clientInfo"]["name"] == "tares-factory", r)
WORLD["mcp"] = False
c, _ = machine({"TARES_FACTORY_MCP_URL": f"{BASE}/mcp"})
ck("a 404 from taresd is not an MCP server", c.mcp_on() is False)
e = err_of(c.require_mcp)
ck("no MCP server with auth on: the command carries a token placeholder, never the token",
   e and str(e) == f"no MCP server answers at {BASE}/mcp. Stations reach Tares through it. Start one in another terminal: "
   "TARES_AUTH_TOKEN=<your token> tares mcp --transport streamable-http (or set TARES_FACTORY_MCP_URL to where yours runs)."
   and TOKEN not in str(e), e)
c, _ = machine({"TARES_FACTORY_MCP_URL": f"{BASE}/mcp", "TARES_AUTH_TOKEN": None})
e = err_of(c.require_mcp)
ck("without a key the placeholder is left out", e and "Start one in another terminal: tares mcp --transport streamable-http (or" in str(e), e)
WORLD["mcp"] = True
c, _ = machine({"TARES_FACTORY_MCP_URL": f"{BASE}/health"})
ck("a web page that answers 200 without serverInfo does not count", c.mcp_on() is False)
c, _ = machine({"TARES_FACTORY_MCP_URL": f"{DEAD}/mcp"})
ck("nothing listening is off, not an error", c.mcp_on() is False)

print("tares_http: the request, the key in a header, curl's error texts")
WORLD["log"].clear()
c, _ = machine()
body = c.tares_http("GET", "/api/crew/projects")
r = seen()[-1]
ck("a GET carries Accept and the key as a bearer header", r["method"] == "GET" and r["accept"] == "application/json"
   and r["auth"] == f"Bearer {TOKEN}" and r["ctype"] is None, r)
ck("and gives the body", json.loads(body)["projects"][0]["id"] == "p-1")
c.tares_http("POST", "/api/crew/anything", '{"a": 1}')
r = seen()[-1]
ck("a body is sent as given, as JSON", r["method"] == "POST" and r["body"] == '{"a": 1}' and r["ctype"] == "application/json", r)
c.tares_http("POST", "/api/crew/anything", {"a": [1, "é"]})
ck("a dict is sent as compact JSON", seen()[-1]["body"] == '{"a":[1,"é"]}', seen()[-1])
c0, _ = machine({"TARES_AUTH_TOKEN": None})
e = err_of(c0.tares_http, "GET", "/api/crew/settings")
ck("a refusal is curl's text (-sS -f): error: 401", isinstance(e, TaresHTTPError) and e.status == 401
   and str(e) == "curl: (22) The requested URL returned error: 401", e)
ck("and no Authorization header was sent without a key", seen()[-1]["auth"] is None)
e = err_of(c.tares_http, "GET", "/api/nothing")
ck("404 is error: 404", e and str(e).endswith("error: 404") and e.status == 404, e)
down, _ = machine({"TARES_FACTORY_URL": DEAD})
e = err_of(down.tares_http, "GET", "/api/x")
ck("no answer: curl's connect failure", e and str(e).startswith(f"curl: (7) Failed to connect to 127.0.0.1 port {DEAD.rsplit(':', 1)[1]}") and e.status is None, e)
import httpx  # noqa: E402
ck("a timeout reads as curl's 28", C._curl_text(httpx.ReadTimeout("t"), BASE, 15).startswith("curl: (28) Operation timed out after 15000 milliseconds"))
n_before = WORLD["elsewhere"]
body = c.tares_http("GET", "/api/redirect-me")
ck("a redirect is not followed, so the key is never carried to another address", WORLD["elsewhere"] == n_before and body == "", WORLD["elsewhere"])

print("key_ok, keep_token (the first crew up keeps the key in a file only the person reads)")
c, home = machine()
ck("key_ok: Tares accepts the key", c.tares_on() and c.key_ok(TOKEN))
ck("key_ok: Tares refuses another", c.key_ok("nvf_dead0000_revoked") is False)
ck("key_ok with no key asks without a header", c.key_ok("") is False and seen()[-1]["auth"] is None)
c.keep_token()
tok = home / ".tares-token"
if os.name == "nt":
    # No mode bits. icacls prints one entry per line with ":(". The engine drops every inherited entry and grants the
    # person full control; SYSTEM, Administrators and OWNER RIGHTS keep their own entries on a file made by an elevated
    # account (the runner is one) and always could read it, so those are expected. What must be absent is anything
    # that opens the file to other people: Everyone, Users, Authenticated Users.
    acl = subprocess.run(["icacls", str(tok)], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    entries = [ln for ln in acl.splitlines() if ":(" in ln]
    low = " ".join(entries).lower()
    me = (os.environ.get("USERNAME") or "").lower()
    open_to_others = [w for w in ("everyone", "builtin\\users", "authenticated users", "nt authority\\interactive") if w in low]
    ck("the token is kept in a file only the person reads",
       tok.read_text() == "stub-token\n" and me != "" and any(me in e.lower() and "(f)" in e.lower() for e in entries)
       and not open_to_others and not any("(i)" in e.lower() for e in entries),
       "text=%r entries=%r open_to_others=%r" % (tok.read_text(), entries, open_to_others))
else:
    ck("the token is kept in a file only the person reads", tok.read_text() == "stub-token\n" and mode(tok) == 0o600, oct(mode(tok)) if tok.exists() else "missing")
before = tok.stat().st_mtime_ns
n = len(seen())
c.keep_token()
ck("a file already holding the key is left alone, with no call to Tares", tok.stat().st_mtime_ns == before and len(seen()) == n)
buf = io.StringIO()
bad, home_b = machine({"TARES_AUTH_TOKEN": "nvf_dead0000_revoked", "TARES_FACTORY_KEY_SOURCE": "saved login"}, say=lambda m: buf.write(m + "\n"))
(home_b / ".tares-token").write_text("stub-token\n")
bad.keep_token()
ck("a key Tares refuses never replaces the crew's", (home_b / ".tares-token").read_text() == "stub-token\n")
ck("and the warning names the key id and where it came from, not the key",
   buf.getvalue() == f"warning: Tares at {BASE} refuses key dead0000 from the saved login; the crew keeps the key it has\n", buf.getvalue())
err = io.StringIO()
with contextlib.redirect_stderr(err):
    envdefault, home_e = machine({"TARES_AUTH_TOKEN": "nvf_dead0000_revoked"})
    (home_e / ".tares-token").write_text("stub-token\n")
    envdefault.keep_token()
ck("the default say writes `tares factory: ...` to stderr, source defaults to the environment",
   err.getvalue() == f"tares factory: warning: Tares at {BASE} refuses key dead0000 from the environment; the crew keeps the key it has\n", err.getvalue())
st, home_s = machine({"TARES_AUTH_TOKEN": "nvf_stale_1", "FACTORY_STATION": "crew-builder"})
(home_s / ".tares-token").write_text("stub-token\n")
st.keep_token()
ck("a station never writes the crew file", (home_s / ".tares-token").read_text() == "stub-token\n")
nohome, _ = machine()
shutil.rmtree(nohome.paths.home)
nohome.keep_token()
ck("without a crew home nothing is written", not nohome.paths.home.exists())
offline, home_o = machine({"TARES_FACTORY_URL": DEAD})
offline.keep_token()
ck("with Tares down nothing is written", not (home_o / ".tares-token").exists())

print("a second machine asks nothing and gets the same settings; a station's spawn uses the orchestrator's env")
m1, home1 = machine()
m2, home2 = machine()
s1, s2 = m1.crew_settings(), m2.crew_settings()
ck("both machines read the same settings from Tares", s1 == s2 and list(s1) == list(C.settings_defaults()), (s1, s2))
ck("saved values win, unset ones take the default",
   s1["autonomy"] == "L4-ship" and s1["releaser"] is False and s1["challenger"] is True and s1["builders_max"] == 2
   and s1["release_profile"] == "kind" and s1["kit"] == {"frozen": None} and s1["idle_wake"] is False and s1["claude_update"] is True, s1)
ck("settings Tares has no key for are not added", "extra" not in s1)
orch_home = Path(ROOT) / "orch"
orch_home.mkdir()
(orch_home / ".tares-token").write_text(TOKEN + "\n")
orch = TaresClient(CrewPaths.at(orch_home), env={"TARES_FACTORY_URL": BASE, "TARES_FACTORY_TOKEN_FILE": str(orch_home / ".tares-token"),
                                                    "FACTORY_STATION": "crew-orchestrator", "HOME": ROOT})
WORLD["log"].clear()
ck("a spawn run with the settings env reaches Tares with the token file", orch.crew_settings()["autonomy"] == "L4-ship" and seen()[-1]["auth"] == f"Bearer {TOKEN}", seen())
(orch_home / ".tares-token").rename(orch_home / "token.bak")
orch2 = TaresClient(CrewPaths.at(orch_home), env=dict(orch.env))
e = err_of(orch2.crew_settings)
ck("unreadable crew settings stop a spawn plainly", e and str(e) == f"could not read the crew settings from Tares at {BASE}"
   "; the crew has no key because the person signed out of Tares: they run tares login, and the crew picks it up", e)
(orch_home / "token.bak").rename(orch_home / ".tares-token")
(orch_home / ".tares-token").write_text("")
orch3 = TaresClient(CrewPaths.at(orch_home), env=dict(orch.env))
ck("an empty crew file (the person signed out) says so", "the person signed out of Tares" in str(err_of(orch3.crew_settings)))
stale = TaresClient(CrewPaths.at(orch_home), env={**orch.env, "TARES_AUTH_TOKEN": "nvf_dead0000_revoked"})
(orch_home / ".tares-token").write_text(TOKEN + "\n")
WORLD["log"].clear()
ck("a station with a stale inherited key reaches Tares through the crew file", stale.crew_settings()["autonomy"] == "L4-ship"
   and all(r["auth"] == f"Bearer {TOKEN}" for r in seen()), seen())

print("crew settings: errors, helpers, cache")
down, _ = machine({"TARES_FACTORY_URL": DEAD})
buf = io.StringIO()
down.say = lambda m: buf.write(m + "\n")
e = err_of(down.crew_settings)
ck("Tares down: could not read the crew settings (no hint with a key)", e and str(e) == f"could not read the crew settings from Tares at {DEAD}", e)
e = err_of(down.setting, "autonomy")
ck("setting says both lines, as bash does", e and str(e) == "no crew settings to work from"
   and buf.getvalue() == f"could not read the crew settings from Tares at {DEAD}\n", (e, buf.getvalue()))
wrong, _ = machine({"TARES_AUTH_TOKEN": "wrong"})
e = err_of(wrong.settings_raw)
ck("a wrong token stops plainly", e and str(e) == f"could not read the crew settings from Tares at {BASE}", e)
c, _ = machine()
ck("setting prints values the way jq -r tostring does", [c.setting("autonomy"), c.setting("builders_max"), c.setting("releaser"),
                                                         c.setting("challenger"), c.setting("kit"), c.setting("nope")]
   == ["L4-ship", "2", "false", "true", '{"frozen":null}', "null"], [c.setting("autonomy"), c.setting("kit")])
ck("releaser_on is false, so two roles", c.releaser_on() is False and c.crew_roles() == ["orchestrator", "reviewer"])
SETTINGS["releaser"] = True
c2, _ = machine()
ck("releaser_on true gives three roles", c2.releaser_on() is True and c2.crew_roles() == ["orchestrator", "reviewer", "releaser"])
SETTINGS["releaser"] = False
n = len(seen())
c.crew_settings()
c.setting("autonomy")
ck("the settings are read once per client", len(seen()) == n)
raw = {"autonomy": "L2", "releaser": True}
ck("settings_missing: a bad autonomy and the unset ones; release ones with a releaser; never the prod pattern",
   C.settings_missing(raw) == ["autonomy", "release_profile", "challenger", "builders_max"], C.settings_missing(raw))
ck("without a releaser the release ones are skipped",
   C.settings_missing({"autonomy": "L3-review", "releaser": False}) == ["challenger", "builders_max"], C.settings_missing({"autonomy": "L3-review", "releaser": False}))
ck("nothing missing when all is set", C.settings_missing({"autonomy": "L4-ship", "releaser": False, "release_profile": "kind",
                                                          "prod_pattern": "x", "challenger": False, "builders_max": 1}) == [])
ck("a false value is a set value", "challenger" not in C.settings_missing({"challenger": False}))
ck("settings_raw unwraps .settings, else takes the document",
   c.settings_raw() == SETTINGS)

print("the crew's projects")
c, _ = machine()
ps = c.crew_projects()
ck("crew_projects lists them", [p["id"] for p in ps] == ["p-1", "p-2"] and c.projects_err == "")
n = len(seen())
c.crew_projects()
ck("and asks once", len(seen()) == n)
ck("project_row by name, id and slug",
   c.project_row("Links App")["id"] == "p-1" and c.project_row("p-2")["name"] == "🔄 Emoji App"
   and c.project_row("links-app")["id"] == "p-1" and c.project_row("LINKS   app!")["id"] == "p-1", c.project_row("links-app"))
ck("project_row of an unknown project is None", c.project_row("nope") is None)
ck("project_repo_rows skips repos with no path, and repos that are plain strings (TR-1130)", c.project_repo_rows() == [("Links App", "/r/links"), ("Links App", "/r/shared"),
                                                                        ("🔄 Emoji App", "/r/emoji"), ("🔄 Emoji App", "/r/shared")], c.project_repo_rows())
ck("all_repos is sorted and unique", c.all_repos() == ["/r/emoji", "/r/links", "/r/shared"], c.all_repos())
ck("crew_projects_or_die passes when Tares answered", len(c.crew_projects_or_die()) == 2)
wrong, _ = machine({"TARES_AUTH_TOKEN": None})
ck("a refusal gives an empty list, not an error", wrong.crew_projects() == [] and wrong.projects_err == "curl: (22) The requested URL returned error: 401", wrong.projects_err)
wrong.tares_on()
e = err_of(wrong.crew_projects_or_die)
ck("crew_projects_or_die says Tares refused, with the hint", e and str(e) == f"Tares at {BASE} did not give the crew's projects "
   "(curl: (22) The requested URL returned error: 401); if it has auth on, run tares login (or set TARES_AUTH_TOKEN for a Tares of your own)", e)

print("grants: the builder gets its project's, quoted; an unknown project falls back to all")
c, _ = machine()
for fn, args in ((c.grants_read, ("p-1",)),):
    out = fn(*args)
    ck("grants_read: the grants for all, then the project's, each line led by @project",
       out == GRANTS["all"] + "\n" + "".join(f"@project {l}\n" for l in GRANTS["projects"]["p-1"].split("\n")), out)
ck("the project is percent-encoded in the query", c.grants_read("🔄 Emoji App") is not None and
   any(r["path"] == "/api/grants?project=%F0%9F%94%84%20Emoji%20App" for r in seen()))
ck("without a project: just the grants for all", c.grants_read() == GRANTS["all"] + "\n")
ck("a project Tares doesn't know: the grants for all still apply (and an empty @project line, as bash prints)",
   c.grants_read("nope") == GRANTS["all"] + "\n@project \n", c.grants_read("nope"))
bad, _ = machine({"TARES_AUTH_TOKEN": None})
ck("any other failure is no answer at all, never no grants", isinstance(err_of(bad.grants_read, "p-1"), TaresHTTPError))
buf = io.StringIO()
bad.say = lambda m: buf.write(m + "\n")
ck("grants_md says it and gives nothing", bad.grants_md("p-1") == "" and buf.getvalue() == "could not read grants from Tares\n", buf.getvalue())
ck("grants_md gives the text when Tares can", c.grants_md("p-1").startswith("- \"the reviewer may comment"))
ck("the fingerprint changes with the text", C.grants_sum(c.grants_read("p-1")) != C.grants_sum(c.grants_read()) and
   C.grants_sum(c.grants_read("p-1")) == C.grants_sum(c.grants_read("p-1")))

print("the desk")
c, _ = machine()
ck("desk_json: open items", c.desk_json() == [{"id": "d1", "question": "ship?"}] and seen()[-1]["path"] == "/api/desk?state=open")
ck("and [] when Tares can't say", bad.desk_json() == [] and down.desk_json() == [])

print("prime: Tares, key, settings, once")
pc, phome = machine()
(phome / "crew.json").write_text("{}")
pc.prime()
ck("prime asks Tares, keeps the key and reads the settings", pc.tares_state == "on" and (phome / ".tares-token").read_text() == "stub-token\n"
   and pc._settings is not None)
pd, _ = machine({"TARES_FACTORY_URL": DEAD})
ck("prime stops plainly when no Tares answers", "no Tares answers at" in str(err_of(pd.prime)))

print("time limits: the numbers bash's curl --max-time gave, and a whole-request cap on the body")
import httpx  # noqa: E402


class _Resp:
    status_code = 200

    def iter_bytes(self):
        yield b'{"status":"ok"}'


class _Stream:
    def __enter__(self):
        return _Resp()

    def __exit__(self, *_exc):
        return False


class RecordingHttp:
    """Stands in for httpx.Client: records the Timeout each request is made with, answers 200, no network."""

    def __init__(self):
        self.timeouts = []

    def stream(self, method, url, headers=None, content=None, timeout=None):
        self.timeouts.append(timeout)
        return _Stream()

    def close(self):
        pass


tc, _ = machine()
rec = tc._http = RecordingHttp()
tc.tares_http("GET", "/api/crew/anything")
tc.key_ok(TOKEN)
tc.tares_on()
tc.mcp_on()
ck("tares_http waits 15 seconds, key_ok 10, /health 5 and the MCP initialize 5 (curl --max-time)",
   rec.timeouts == [httpx.Timeout(15), httpx.Timeout(10), httpx.Timeout(5), httpx.Timeout(5)]
   and all(t.connect == t.read == t.write == t.pool for t in rec.timeouts) and rec.timeouts[0].read == 15,
   [(t.connect, t.read) for t in rec.timeouts])
tc._http = None


class Drip(BaseHTTPRequestHandler):
    """Answers 200 at once, then one byte every quarter second for ten seconds: every single read is
    quick, so only a limit on the whole request stops it."""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        try:
            for _ in range(40):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.25)
        except OSError:
            pass

    def log_message(self, *a):
        pass


drip = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
threading.Thread(target=drip.serve_forever, daemon=True).start()
dc, _ = machine()
t0 = time.monotonic()
e = err_of(dc._request, "GET", f"http://127.0.0.1:{drip.server_address[1]}/slow", {}, None, 1)
took = time.monotonic() - t0
ck("a body that keeps trickling in is cut at the limit in total, with curl's text",
   isinstance(e, TaresHTTPError) and str(e).startswith("curl: (28) Operation timed out after 1000 milliseconds with ")
   and e.status is None, (e, took))
ck("...after about a second, not when the ten-second trickle ends", 0.9 <= took < 4, took)
drip.shutdown()
dc.close()

print("test hook TARES_URL_LOG: each request's URL is noted, nothing else")
ulog = Path(ROOT) / "urls.log"
cu, _ = machine({"TARES_URL_LOG": str(ulog)})
cu.tares_http("GET", "/api/crew/projects")
cu.tares_http("GET", "/api/crew/settings")
ck("one line per request, the full address", ulog.read_text(encoding="utf-8").splitlines() ==
   [f"{BASE}/api/crew/projects", f"{BASE}/api/crew/settings"], ulog.read_text(encoding="utf-8"))
ck("no key in the file", TOKEN not in ulog.read_text(encoding="utf-8"))
cn, _ = machine()
cn.tares_http("GET", "/api/crew/projects")
ck("off when unset: the file is not touched", len(ulog.read_text(encoding="utf-8").splitlines()) == 2)
cb, _ = machine({"TARES_URL_LOG": str(Path(ROOT) / "no-such-dir" / "urls.log")})
ck("a file that cannot be written does not fail the request", cb.tares_http("GET", "/api/crew/projects") is not None)

print("the key is in no log line and no process was started")
ck("no header but Authorization (and no URL or body) ever carried the key",
   all(r["leak"] is None and TOKEN not in r["path"] and TOKEN not in (r["body"] or "") for r in WORLD["log"]))
ck("no process", spawned == [], spawned)
logged = "\n".join(grab.lines)
ck("the logs saw requests", "HTTP Request" in logged or len(grab.lines) > 0, len(grab.lines))
ck("but never the key", TOKEN not in logged and "nvf_dead0000_revoked" not in logged and "person-key" not in logged, [l for l in grab.lines if TOKEN in l][:2])
all_errors = [str(err_of(c0.tares_http, "GET", "/api/crew/settings")), str(err_of(wrong.crew_projects_or_die) or "")]
ck("and in no error text", not any(TOKEN in t for t in all_errors))
subprocess.Popen = real_popen

srv.shutdown()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
