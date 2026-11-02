"""`tares login` / `tares logout` (TR-490..492) against a fake control plane and workspace on
localhost: the browser hand-over, the version check, the saved file, logout and its fallback, and
who uses the saved login (`tares status`'s resolve, `tares factory`'s environment, the plugin's
session shipper).
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOME = tempfile.mkdtemp()
os.environ["TARES_HOME"] = HOME
# a crew home that does not exist, so no login here touches the real ~/factory
os.environ["FACTORY_CREW_HOME"] = os.path.join(HOME, "no-crew")
for k in ("TARESD_URL", "TARES_FACTORY_URL", "TARES_AUTH_TOKEN", "TARES_CLOUD_URL"):
    os.environ.pop(k, None)

from tares import login as L  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── a fake control plane that is also the workspace ────────────────────────────────────────────
WORLD = {"version": "1.44.0", "health_status": 200, "logout_status": 200, "logouts": [],
         "deletes": [], "n": 0, "challenge": "", "redirected_auth": []}


def _s256(verifier: str) -> str:
    import base64
    import hashlib
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


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
            return self._json(WORLD["health_status"], {"status": "ok", "version": WORLD["version"]})
        if self.path == "/elsewhere":
            WORLD["redirected_auth"].append(self.headers.get("Authorization"))
            return self._json(200, {})
        self._json(404, {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/ingest/claude_code":
            self.send_response(302)
            self.send_header("Location", f"{BASE}/elsewhere")
            self.end_headers()
            return
        if self.path == "/exchange":
            # PKCE: the verifier the CLI sends must hash to the challenge the page carried
            if _s256(body.get("code_verifier", "")) != WORLD["challenge"]:
                return self._json(400, {"detail": "the code_verifier does not match"})
            WORLD["n"] += 1
            kid = f"k{WORLD['n']}"
            return self._json(200, {"token": f"nvf_{kid}_secret", "key_id": kid, "cell": "factory",
                                    "cell_url": BASE})
        if self.path == "/api/cli/logout":
            WORLD["logouts"].append((body.get("cell"), self.headers.get("Authorization")))
            return self._json(WORLD["logout_status"], {"revoked": True})
        self._json(404, {})

    def do_DELETE(self):  # noqa: N802
        WORLD["deletes"].append((self.path, self.headers.get("Authorization")))
        self._json(200, {"ok": True})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def browser(state_override=None):
    """Stands in for the person: opens the page address, picks a workspace, and the control plane
    sends the browser to the CLI's callback."""
    def open_(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert urlparse(url).path == "/connect/cli"
        WORLD["challenge"] = q.get("challenge", "")     # what the control plane would keep
        state = state_override or q["state"]

        def go():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{q['port']}/callback?code=c1&state={state}",
                                       timeout=5).read()
            except Exception:
                pass
        threading.Timer(0.2, go).start()
        return True
    L.webbrowser.open = open_


print("login saves the workspace and its key, readable only by me")
browser()
rc = L.login(BASE, wait=10)
s = L.saved()
ck("login succeeds", rc == 0, rc)
ck("saved the workspace and key", s and s["cell"] == "factory" and s["key"] == "nvf_k1_secret"
   and s["key_id"] == "k1" and s["cell_url"] == BASE and s["cloud"] == BASE, s)
mode = stat.S_IMODE(os.stat(L.login_file()).st_mode)
ck("login.json is 600", mode == 0o600, oct(mode))

print("resolve: a flag, then the environment, then the saved login, then local")
ck("saved login when nothing is set", L.resolve() == (BASE, "nvf_k1_secret", "saved login"), L.resolve())
os.environ["TARESD_URL"] = "http://elsewhere:8787"
ck("the environment wins, and the saved key is not sent there",
   L.resolve() == ("http://elsewhere:8787", "", "environment"), L.resolve())
ck("a flag wins over all", L.resolve("http://flag:1")[2] == "flag")
os.environ.pop("TARESD_URL")

print("tares factory gets the saved workspace and key")
import tares.cli as C  # noqa: E402
got = {}
C.os.execve = lambda path, argv, env: got.update(env) or (_ for _ in ()).throw(SystemExit(0))
try:
    C._factory(["doctor"])
except SystemExit:
    pass
ck("TARESD_URL and TARES_FACTORY_URL are the workspace",
   got.get("TARESD_URL") == BASE and got.get("TARES_FACTORY_URL") == BASE, got.get("TARESD_URL"))
ck("the key is the token", got.get("TARES_AUTH_TOKEN") == "nvf_k1_secret")
ck("doctor is told where it came from", got.get("TARES_FACTORY_SOURCE") == "saved login")
ck("and where the key came from", got.get("TARES_FACTORY_KEY_SOURCE") == "saved login")
os.environ["TARES_AUTH_TOKEN"] = "mine"
os.environ["TARESD_URL"] = "http://elsewhere:8787"
got.clear()
try:
    C._factory(["doctor"])
except SystemExit:
    pass
ck("set variables win over the saved login",
   got.get("TARESD_URL") == "http://elsewhere:8787" and got.get("TARES_AUTH_TOKEN") == "mine"
   and got.get("TARES_FACTORY_SOURCE") == "environment", got.get("TARES_FACTORY_SOURCE"))
os.environ.pop("TARESD_URL")
got.clear()
try:
    C._factory(["doctor"])
except SystemExit:
    pass
ck("a set TARES_AUTH_TOKEN beside the saved workspace is named as the key's source (TR-560)",
   got.get("TARES_FACTORY_SOURCE") == "saved login" and got.get("TARES_AUTH_TOKEN") == "mine"
   and got.get("TARES_FACTORY_KEY_SOURCE") == "environment", got.get("TARES_FACTORY_KEY_SOURCE"))
os.environ["TARESD_URL"] = "http://elsewhere:8787"
os.environ.pop("TARES_AUTH_TOKEN")
os.environ.pop("TARESD_URL")
os.environ["TARES_FACTORY_MCP_URL"] = "https://someone-else.example.com/mcp"
got.clear()
try:
    C._factory(["doctor"])
except SystemExit:
    pass
ck("an MCP endpoint set elsewhere never gets the saved key", "TARES_AUTH_TOKEN" not in got,
   got.get("TARES_AUTH_TOKEN"))
os.environ["TARES_FACTORY_MCP_URL"] = BASE + "/mcp"
got.clear()
try:
    C._factory(["doctor"])
except SystemExit:
    pass
ck("the workspace's own MCP endpoint does", got.get("TARES_AUTH_TOKEN") == "nvf_k1_secret")
os.environ.pop("TARES_FACTORY_MCP_URL")

print("the plugin's session shipper uses the saved login when its options are untouched")
spec = importlib.util.spec_from_file_location(
    "ship", Path(__file__).resolve().parents[1] / "claude-plugin" / "scripts" / "ship.py")
ship = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship)
os.environ["CLAUDE_PLUGIN_DATA"] = tempfile.mkdtemp()
cfg = ship.config()
ck("ships to the workspace with its key",
   cfg["base"] == BASE and cfg["headers"].get("Authorization") == "Bearer nvf_k1_secret", cfg)
os.environ["CLAUDE_PLUGIN_OPTION_ACCESS_TOKEN"] = "by-hand"
cfg = ship.config()
ck("an access_token set by hand wins", cfg["headers"].get("Authorization") == "Bearer by-hand"
   and cfg["base"] == "http://127.0.0.1:8787", cfg)
os.environ.pop("CLAUDE_PLUGIN_OPTION_ACCESS_TOKEN")
os.environ["CLAUDE_PLUGIN_OPTION_TARES_URL"] = "https://other.example.com"
cfg = ship.config()
ck("a tares_url set by hand gets no saved key", cfg["base"] == "https://other.example.com"
   and "Authorization" not in cfg["headers"], cfg)
os.environ.pop("CLAUDE_PLUGIN_OPTION_TARES_URL")
os.environ["TARESD_URL"] = "http://elsewhere:8787"
cfg = ship.config()
ck("TARESD_URL wins over the saved login there too, without the key",
   "Authorization" not in cfg["headers"] and cfg["base"] == "http://elsewhere:8787", cfg)
os.environ.pop("TARESD_URL")
try:
    ship._post(BASE + "/ingest/claude_code", b"{}", {"Authorization": "Bearer nvf_k1_secret"})
    redirected = "followed"
except Exception as e:  # noqa: BLE001
    redirected = type(e).__name__
ck("the shipper refuses a redirect instead of carrying the key along",
   redirected == "HTTPError" and WORLD["redirected_auth"] == [], (redirected, WORLD["redirected_auth"]))

print("logging in again replaces the key and revokes the old one")
browser()
rc = L.login(BASE, wait=10)
ck("second login succeeds", rc == 0 and L.saved()["key_id"] == "k2", L.saved())
ck("the old key was revoked through the control plane",
   ("factory", "Bearer nvf_k1_secret") in WORLD["logouts"], WORLD["logouts"])

print("a callback with the wrong state is refused, and nothing changes")
browser(state_override="x" * 32)
rc = L.login(BASE, wait=2)
ck("login fails", rc == 1)
ck("the saved login is untouched", L.saved()["key_id"] == "k2")

print("a workspace whose /health fails is refused, whatever version it names")
WORLD["health_status"] = 503
browser()
rc = L.login(BASE, wait=10)
ck("login fails", rc == 1)
ck("the saved login is untouched", L.saved()["key_id"] == "k2")
WORLD["health_status"] = 200

print("a code without the right verifier is refused (PKCE)")
browser()
orig_http = L._http
L._http = lambda m, u, body=None, **kw: orig_http(
    m, u, ({**body, "code_verifier": "stolen"} if u.endswith("/exchange") else body), **kw)
rc = L.login(BASE, wait=10)
L._http = orig_http
ck("login fails", rc == 1)
ck("the saved login is untouched", L.saved()["key_id"] == "k2")

print("a workspace below 1.44.0 is refused and its new key removed")
WORLD["version"] = "1.43.0"
browser()
rc = L.login(BASE, wait=10)
ck("login fails", rc == 1)
ck("the saved login is untouched", L.saved()["key_id"] == "k2")
ck("the key it was handed was revoked", any(a == "Bearer nvf_k3_secret" for _c, a in WORLD["logouts"]),
   WORLD["logouts"])
WORLD["version"] = "1.44.0"

print("logout: through the control plane, else on the workspace")
WORLD["logout_status"] = 500
rc = L.logout()
ck("logout succeeds through the fallback", rc == 0)
ck("the workspace deleted the key itself", ("/api/keys/k2", "Bearer nvf_k2_secret") in WORLD["deletes"],
   WORLD["deletes"])
ck("the file is gone", not L.login_file().exists())
ck("a second logout says so and succeeds", L.logout() == 0)

print("the factory crew follows the login (TR-560)")
WORLD["logout_status"] = 200
crew = Path(tempfile.mkdtemp()) / "factory"
os.environ["FACTORY_CREW_HOME"] = str(crew)
browser()
ck("no crew: login succeeds and makes none", L.login(BASE, wait=10) == 0 and not crew.exists())
crew.mkdir()
(crew / ".tares-token").write_text("nvf_stale_revoked\n")
browser()
rc = L.login(BASE, wait=10)
key = L.saved()["key"]
ck("login gives the crew the new key", rc == 0 and (crew / ".tares-token").read_text().strip() == key,
   (crew / ".tares-token").read_text())
mode = stat.S_IMODE(os.stat(crew / ".tares-token").st_mode)
ck("the crew file is 600", mode == 0o600, oct(mode))
rc = L.logout()
ck("logout empties the crew file", rc == 0 and (crew / ".tares-token").read_text().strip() == "",
   (crew / ".tares-token").read_text())
browser()
L.login(BASE, wait=10)
(crew / ".tares-token").write_text("my-own-tares-root\n")
L.logout()
ck("logout leaves a key it did not end alone",
   (crew / ".tares-token").read_text().strip() == "my-own-tares-root")
os.environ["FACTORY_CREW_HOME"] = os.path.join(HOME, "no-crew")

srv.shutdown()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
