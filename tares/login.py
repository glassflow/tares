"""`tares login` and `tares logout` (TR-490): connect this machine to a Tares Cloud workspace.

`tares login` opens the control plane's /connect/cli page in the browser with a localhost port, a
random state and the machine's name. The person signs in and picks a workspace; the control plane
makes a key on it for this machine and sends the browser back to http://127.0.0.1:<port>/callback
with a one-time code, which this module swaps at the control plane's /exchange for the key and the
workspace's address. The result is saved in ~/.tares/login.json, readable only by its owner.

Everything that needs a Tares finds it in this order: an explicit flag, then TARESD_URL /
TARES_FACTORY_URL and TARES_AUTH_TOKEN, then this saved login, then the local default.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

DEFAULT_CLOUD = "https://console.tares-glassflow.com"
MIN_VERSION = (1, 44, 0)
MIN_VERSION_TEXT = "1.44.0"
WAIT_SECONDS = 300


def home() -> Path:
    return Path(os.getenv("TARES_HOME", str(Path.home() / ".tares"))).expanduser()


def login_file() -> Path:
    return home() / "login.json"


def saved() -> dict | None:
    """The saved login, or None. A file that cannot be read or lacks the essentials is ignored."""
    try:
        data = json.loads(login_file().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("cell_url") or not data.get("key"):
        return None
    return data


def _save(data: dict) -> None:
    path = login_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def resolve(explicit_url: str = "") -> tuple[str, str, str]:
    """(url, token, source) for a command that talks to Tares. source is one of "flag",
    "environment", "saved login", "local default". The token from the saved login is used only
    together with its own address, never sent to an address that came from somewhere else."""
    env_url = (os.getenv("TARES_FACTORY_URL") or os.getenv("TARESD_URL") or "").strip()
    env_tok = os.getenv("TARES_AUTH_TOKEN", "").strip()
    if explicit_url:
        return explicit_url.rstrip("/"), env_tok, "flag"
    if env_url:
        return env_url.rstrip("/"), env_tok, "environment"
    s = saved()
    if s:
        return s["cell_url"].rstrip("/"), env_tok or s["key"], "saved login"
    return f"http://127.0.0.1:{os.getenv('TARES_PORT', '8787')}", env_tok, "local default"


def _version(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _machine() -> str:
    name = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "-", name)[:63] or "machine"


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font:15px/1.5 system-ui,sans-serif;max-width:520px;margin:12vh auto;"
        "padding:0 16px;color:#2b211c;background:#faf6f1'>"
        f"<h1 style='font-size:22px'>{html.escape(title)}</h1><p>{html.escape(body)}</p>"
    ).encode()


class _Callback(BaseHTTPRequestHandler):
    """Answers the one redirect the control plane sends the browser to, then the CLI goes on."""
    state = ""
    result: dict = {}
    done = threading.Event()

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path != "/callback":
            self.send_response(404); self.end_headers(); return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not secrets.compare_digest(q.get("state", ""), type(self).state) or not q.get("code"):
            body = _page("Not connected", "This sign-in did not come from the tares login that "
                         "is waiting. Run tares login again.")
            self.send_response(400)
        else:
            type(self).result["code"] = q["code"]
            body = _page("Connected", "Go back to your terminal. You can close this tab.")
            self.send_response(200)
            type(self).done.set()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):   # keep the terminal quiet
        pass


def _http(method: str, url: str, body: dict | None = None, token: str = "",
          timeout: float = 15) -> tuple[int, dict]:
    import httpx
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.request(method, url, json=body, headers=headers, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


def _revoke(data: dict) -> tuple[bool, str]:
    """Delete a saved login's key: through the control plane, else on the workspace directly
    (an admin key may delete itself). (ok, how)."""
    import httpx
    cloud = (data.get("cloud") or DEFAULT_CLOUD).rstrip("/")
    try:
        code, _ = _http("POST", f"{cloud}/api/cli/logout", {"cell": data.get("cell", "")},
                        token=data["key"])
        if code == 200:
            return True, "control plane"
    except httpx.HTTPError:
        pass
    key_id = data.get("key_id") or ""
    if key_id:
        try:
            code, _ = _http("DELETE", f"{data['cell_url'].rstrip('/')}/api/keys/{key_id}",
                            token=data["key"])
            if code in (200, 204, 404):
                return True, "workspace"
        except httpx.HTTPError:
            pass
    return False, ""


def mcp_setting(cell_url: str) -> str:
    return (f"For ordinary Claude Code sessions, set the Tares plugin's MCP endpoint to "
            f"{cell_url.rstrip('/')}/mcp (run /plugin in Claude Code, pick tares, Configure). "
            f"Inside tares factory this is done for you.")


def login(cloud: str = "", open_browser: bool = True, wait: int = WAIT_SECONDS) -> int:
    import httpx
    cloud = (cloud or os.getenv("TARES_CLOUD_URL") or DEFAULT_CLOUD).rstrip("/")
    machine = _machine()
    state = secrets.token_urlsafe(24)
    # PKCE (RFC 7636): the page only ever sees the challenge; /exchange wants the verifier, which
    # never leaves this process. A code that reaches some other listener on this machine is useless.
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    class Handler(_Callback):
        pass
    Handler.state, Handler.result, Handler.done = state, {}, threading.Event()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"{cloud}/connect/cli?" + urlencode({"port": port, "state": state, "machine": machine,
                                               "challenge": challenge})
    print("tares login: pick the workspace this machine works with, in your browser:")
    print(f"  {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    got = Handler.done.wait(wait)
    time.sleep(0.2)          # let the "Connected" page finish sending
    server.shutdown()
    if not got:
        print("tares login: no workspace was picked in the browser in time, or the page that came "
              "back was not for this login. Run tares login again.")
        return 1

    try:
        code, out = _http("POST", f"{cloud}/exchange", {"code": Handler.result["code"],
                                                         "code_verifier": verifier})
    except httpx.HTTPError as e:
        print(f"tares login: could not reach {cloud} to finish signing in ({e}).")
        return 1
    if code != 200 or not out.get("token") or not out.get("cell_url"):
        print(f"tares login: the control plane did not accept the sign-in "
              f"({out.get('detail') or code}). Run tares login again.")
        return 1
    data = {"cloud": cloud, "cell": out.get("cell", ""), "cell_url": out["cell_url"].rstrip("/"),
            "key": out["token"], "key_id": out.get("key_id", ""), "machine": machine,
            "signed_in_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # The control plane checked the version when it offered the workspace; check what the
    # workspace itself says now, before saving a key for it.
    try:
        status, health = _http("GET", f"{data['cell_url']}/health", timeout=10)
        version = str(health.get("version") or "") if status == 200 else ""
    except httpx.HTTPError:
        version = ""
    if (_version(version) or (0, 0, 0)) < MIN_VERSION:
        _revoke(data)
        print(f"tares login: {data['cell']} runs Tares {version or 'of an unknown version'}; "
              f"tares login needs {MIN_VERSION_TEXT} or later. Nothing was saved.")
        return 1

    old = saved()
    _save(data)
    if old and old.get("key_id") != data["key_id"]:
        ok, _how = _revoke(old)
        if not ok:
            print(f"tares login: the previous key on {old.get('cell')} could not be removed; "
                  f"remove it under Security > API keys on {old.get('cell_url')}.")
    print(f"tares login: this machine ({machine}) is connected to {data['cell']} "
          f"({data['cell_url']}), Tares {version}.")
    print(f"  tares factory and tares status use it now. {mcp_setting(data['cell_url'])}")
    return 0


def logout() -> int:
    data = saved()
    if not data:
        if login_file().exists():
            login_file().unlink()
        print("tares logout: this machine is not signed in.")
        return 0
    ok, _how = _revoke(data)
    login_file().unlink(missing_ok=True)
    if ok:
        print(f"tares logout: this machine's key on {data.get('cell')} is removed, "
              f"and the saved login is deleted.")
        return 0
    print(f"tares logout: the saved login is deleted, but the key is still on {data.get('cell')}: "
          f"remove it under Security > API keys on {data.get('cell_url')} "
          f"(named cli: {data.get('machine')}).")
    return 1
