"""`tares login` and `tares logout` (TR-490): connect this machine to a Tares Cloud workspace.

`tares login` opens the control plane's /connect/cli page in the browser with a localhost port, a
random state and the machine's name. The person signs in and picks a workspace; the control plane
makes a key on it for this machine and sends the browser back to http://127.0.0.1:<port>/callback
with a one-time code, which this module swaps at the control plane's /exchange for the key and the
workspace's address. The workspace's address and the rest of the login are saved in
~/.tares/login.json, readable only by its owner; the key itself goes to the OS keychain (keyring: the
macOS Keychain, Windows Credential Manager, the Linux Secret Service) under the service "tares" and
the workspace's address as the account. Where no keychain works (headless Linux, CI) the key stays in
login.json instead (mode 0600, or an access list for the current user alone on Windows) and the
command says so. A key an older login left in login.json moves to the keychain on first use.
The keychain is opt-in for now (TARES_KEYRING=on): the plugin's mcp-headers and ship.py and the bash
doctor still read the key from login.json, so nothing moves it out of the file by default. Unset, or
off, keeps the key in the file and never touches a keychain.

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
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .crew import contract
from .crew.errors import CrewError

KEYRING_SERVICE = "tares"
DEFAULT_CLOUD = "https://console.tares-glassflow.com"
MIN_VERSION = (1, 44, 0)
MIN_VERSION_TEXT = "1.44.0"
WAIT_SECONDS = 300


def home() -> Path:
    return Path(os.getenv("TARES_HOME", str(Path.home() / ".tares"))).expanduser()


def login_file() -> Path:
    return home() / "login.json"


_told: set = set()


def _tell_once(key: str, text: str, stream=None) -> None:
    """Say a line once per process (stderr unless told otherwise, so --json output stays clean)."""
    if key not in _told:
        _told.add(key)
        print(text, file=stream or sys.stderr, flush=True)


def _gate_on() -> bool:
    """Is the keychain switched on (TARES_KEYRING=on, or keychain, 1, true, yes)? Off by default."""
    return os.getenv("TARES_KEYRING", "").strip().lower() in ("on", "keychain", "1", "true", "yes")


def _keyring():
    """The keyring module when the keychain is switched on (TARES_KEYRING) and this machine has a keychain
    that works, else None; `_gate_on()` tells the two apart. Off by default, so no key leaves login.json
    until the readers that still look there (mcp-headers, ship.py, the bash doctor) can read the keychain.
    keyring's fail and null backends have priority 0 or less."""
    if not _gate_on():
        return None
    try:
        import keyring
        backend = keyring.get_keyring()
        return keyring if float(getattr(backend, "priority", 0)) > 0 else None
    except Exception:  # noqa: BLE001 - a missing module or a broken backend both mean no keychain
        return None


_BACKEND_NAMES = {
    "keyring.backends.macOS": "the macOS Keychain",
    "keyring.backends.Windows": "Windows Credential Manager",
    "keyring.backends.SecretService": "the Secret Service keychain",
    "keyring.backends.kwallet": "KWallet",
}


def keychain_name(kr=None) -> str:
    kr = kr or _keyring()
    if kr is None:
        return ""
    backend = kr.get_keyring()
    return _BACKEND_NAMES.get(type(backend).__module__) or f"the keychain ({type(backend).__name__})"


def _kc_set(kr, account: str, key: str) -> bool:
    """Put the key in the keychain and read it back; False on any failure."""
    try:
        kr.set_password(KEYRING_SERVICE, account, key)
        return kr.get_password(KEYRING_SERVICE, account) == key
    except Exception:  # noqa: BLE001
        return False


def _kc_get(account: str) -> tuple[str, str]:
    """(key, problem). problem is "" when the keychain answered: an empty key then means there is no
    entry. Else why it could not be asked: "off" (TARES_KEYRING is not on), "unusable" (on, but no
    keychain works here) or "error" (the keychain refused: locked, access denied, timed out)."""
    if not _gate_on():
        return "", "off"
    kr = _keyring()
    if kr is None:
        return "", "unusable"
    try:
        return kr.get_password(KEYRING_SERVICE, account) or "", ""
    except Exception:  # noqa: BLE001
        return "", "error"


def _kc_delete(account: str) -> None:
    kr = _keyring()
    if kr is None or not account:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, account)
    except Exception:  # noqa: BLE001 - nothing there is as good as deleted
        pass


def _read_file() -> dict | None:
    try:
        data = json.loads(login_file().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("cell_url"):
        return None
    return data


def _write_file(data: dict) -> None:
    """login.json, replaced whole, readable by its owner only."""
    from .crew import state
    path = login_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    state.atomic_write_text(path, json.dumps(data, indent=2), mode=0o600, private=True)


def _account(data: dict) -> str:
    return str(data["cell_url"]).rstrip("/")


def _load() -> tuple[dict | None, str, str]:
    """(login.json's content, the key, why the key could not be read). The key is the one in the file, else
    the one in the keychain. When the file says the key is in the keychain and the keychain could not be
    asked, `why` is "off", "unusable" or "error" (see _kc_get); with no key and no `why` there is simply
    no key there. A key still in the file (an older login, or a machine with no keychain) is used as it
    is, and moved to the keychain first when there is one."""
    data = _read_file()
    if data is None:
        return None, "", ""
    key = data.get("key") or ""
    if key:
        if _migrate(data):
            data = _read_file() or data
        return data, key, ""
    key, problem = _kc_get(_account(data))
    if key or data.get("key_store") != "keychain":
        return data, key, ""
    return data, "", problem


def saved() -> dict | None:
    """The saved login, or None. A file that cannot be read or lacks the essentials is ignored, and so is
    one whose key cannot be read (said once, on stderr)."""
    data, key, why = _load()
    if key:
        return {**data, "key": key}
    if why in ("off", "unusable"):
        _tell_once("no-keychain", "tares: the saved key is in the OS keychain, which cannot be used here "
                   "(TARES_KEYRING is not on, or no keychain works on this machine). Run tares login again.")
    elif why == "error":
        _tell_once("keychain-error", "tares: the saved key is in the OS keychain, which would not give it "
                   "(locked, or access was denied). Unlock it and try again.")
    return None


def _unreadable(why: str, data: dict, what: str) -> str:
    """Plain words for a saved key that is in the keychain and cannot be read now, and what to do."""
    if why == "off":
        cause = "the OS keychain is off here (TARES_KEYRING is not on)"
        fix = "Run this again with TARES_KEYRING=on."
    elif why == "unusable":
        cause = "no OS keychain works on this machine now"
        fix = "Run this again on the machine that saved it, or with its keychain working."
    else:
        cause = "the OS keychain would not give it (locked, or access was denied)"
        fix = "Unlock the keychain, allow the access, and run this again."
    key_id = data.get("key_id") or ""
    named = f" (key {key_id} on {data.get('cell') or data.get('cell_url')})" if key_id else ""
    return (f"{what}: the saved key{named} is in the OS keychain, but {cause}, so it cannot be read "
            f"and taken off the workspace. {login_file()} and the keychain entry were left as they are. {fix}")


def _migrate(data: dict) -> bool:
    """Move the key from login.json into the keychain, once: written and read back first, then removed
    from the file. True when the file no longer holds the key."""
    kr = _keyring()
    if kr is None or not _kc_set(kr, _account(data), data["key"]):
        return False
    rest = {k: v for k, v in data.items() if k != "key"}
    rest["key_store"] = "keychain"
    try:
        _write_file(rest)
    except (OSError, CrewError):     # a file that cannot be made private is left as it was
        return False
    _tell_once("migrated", f"tares: moved your saved key from {login_file()} into {keychain_name(kr)}.")
    return True


def _save(data: dict) -> str:
    """Save a login: the key in the keychain and the rest in login.json; where no keychain works, all of
    it in login.json (0600). Returns where the key went: the keychain's name, or the file's path. When
    the file cannot be written, the keychain entry goes back to what it held and the error is raised."""
    kr = _keyring()
    meta = {k: v for k, v in data.items() if k not in ("key", "key_store")}
    if kr is not None:
        account = _account(data)
        try:
            before: str | None = kr.get_password(KEYRING_SERVICE, account) or ""
        except Exception:  # noqa: BLE001
            before = None      # unknown: what the entry held cannot be put back, and must not be deleted
        if _kc_set(kr, account, data["key"]):
            try:
                _write_file({**meta, "key_store": "keychain"})
            except (OSError, CrewError):
                if before:
                    _kc_set(kr, account, before)
                elif before is not None:
                    _kc_delete(account)
                raise
            return keychain_name(kr)
    _write_file({**meta, "key": data["key"], "key_store": "file"})
    return str(login_file())


def _forget() -> None:
    """Delete the saved login: its keychain entry and the file."""
    raw = _read_file()
    if raw:
        _kc_delete(_account(raw))
    login_file().unlink(missing_ok=True)


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
    except (httpx.HTTPError, httpx.InvalidURL):
        pass
    key_id = data.get("key_id") or ""
    if key_id:
        try:
            code, _ = _http("DELETE", f"{data['cell_url'].rstrip('/')}/api/keys/{key_id}",
                            token=data["key"])
            if code in (200, 204, 404):
                return True, "workspace"
        except (httpx.HTTPError, httpx.InvalidURL):
            pass
    return False, ""


def _left_behind(data: dict) -> str:
    """Plain words for a key just made that could not be removed again, and where to remove it."""
    key_id = data.get("key_id") or ""
    named = f" (key {key_id})" if key_id else ""
    return (f"The key just made for this machine{named} could not be removed; remove it under "
            f"Security > API keys on {data.get('cell') or data.get('cell_url')}.")


def crew_key_file() -> Path:
    """The tares factory crew's key file: its stations read only this, never login.json."""
    home_ = os.getenv("FACTORY_CREW_HOME") or str(Path.home() / "factory")
    return Path(home_).expanduser() / ".tares-token"


def _crew_follows(new_key: str, old_key: str = "", json_out: bool = False) -> bool:
    """The crew follows the person's login (TR-560): a login gives a crew on this machine the new
    key, a logout takes away the key it ends. Nothing happens without a crew, and a logout leaves
    a file holding some other key alone (a crew on a Tares of your own). True when it wrote."""
    path = crew_key_file()
    if not path.parent.is_dir():
        return False
    if not new_key:
        try:
            if path.read_text().strip() != old_key:
                return False
        except OSError:
            return False
    from .crew import state
    from .crew.errors import CrewError
    try:
        state.atomic_write_text(path, new_key + "\n" if new_key else "", mode=0o600, private=True)
    except (OSError, CrewError) as e:
        print(f"tares login: could not update the crew's key file {path} ({e}).",
              file=sys.stderr if json_out else sys.stdout)
        return False
    return True


def mcp_setting(cell_url: str) -> str:
    return (f"For ordinary Claude Code sessions, set the Tares plugin's MCP endpoint to "
            f"{cell_url.rstrip('/')}/mcp (run /plugin in Claude Code, pick tares, Configure). "
            f"Inside tares factory this is done for you.")


def _err(json_out: bool, code: str, text: str) -> int:
    """A failure of login/logout: the text line for a person, or with --json the error document (exit 1 is
    the caller's return). The text never holds the key."""
    if json_out:
        contract._write_quiet(contract.line(contract.error_document(code, text)) + "\n")
    else:
        print(text)
    return 1


def _open_browser(url: str, json_out: bool) -> None:
    """Open the sign-in page. With --json, stdout holds the documents only, and a browser started in the
    background (xdg-open and the like) inherits file descriptor 1: point it at stderr while it starts."""
    saved = None
    if json_out:
        try:
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(2, 1)
        except OSError:
            saved = None
    try:
        webbrowser.open(url)
    finally:
        if saved is not None:
            os.dup2(saved, 1)
            os.close(saved)


def _key_store(data: dict) -> str:
    """Where the key is kept: "keychain" or "file". An old login.json with a key and no key_store is a file."""
    return "keychain" if data.get("key_store") == "keychain" else "file"


def login(cloud: str = "", open_browser: bool = True, wait: int = WAIT_SECONDS, json_out: bool = False) -> int:
    """Sign in. With json_out, stdout holds JSON documents only (one per line, see schemas/login.json)."""
    try:
        return _login(cloud, open_browser, wait, json_out)
    except OSError as e:
        if not json_out:
            raise
        return _err(True, "error", f"tares login: {type(e).__name__}: {e}")


def _login(cloud: str, open_browser: bool, wait: int, json_out: bool) -> int:
    import httpx
    cloud = (cloud or os.getenv("TARES_CLOUD_URL") or DEFAULT_CLOUD).rstrip("/")
    # A saved key that cannot be read cannot be taken off the workspace either: stop before a new one is
    # made, so the last record of a live key is never overwritten.
    old_data, old_key, why = _load()
    if old_data is not None and not old_key and why:
        return _err(json_out, "keychain_unreadable",
                    _unreadable(why, old_data, "tares login") + f" Or, to start over, delete {login_file()} "
                    "and remove that key yourself under Security > API keys.")
    old = {**old_data, "key": old_key} if old_data is not None and old_key else None
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
    if json_out:
        contract.emit({"login": {"step": "open", "url": url, "expires_in": wait}}, "login")
    else:
        print("tares login: pick the workspace this machine works with, in your browser:")
        print(f"  {url}", flush=True)
    if open_browser:
        _open_browser(url, json_out)
    got = Handler.done.wait(wait)
    time.sleep(0.2)          # let the "Connected" page finish sending
    server.shutdown()
    if not got:
        return _err(json_out, "login_timeout",
                    "tares login: no workspace was picked in the browser in time, or the page that came "
                    "back was not for this login. Run tares login again.")

    try:
        code, out = _http("POST", f"{cloud}/exchange", {"code": Handler.result["code"],
                                                         "code_verifier": verifier})
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        return _err(json_out, "error", f"tares login: could not reach {cloud} to finish signing in ({e}).")
    if code != 200 or not out.get("token") or not out.get("cell_url"):
        return _err(json_out, "login_refused", f"tares login: the control plane did not accept the sign-in "
                    f"({out.get('detail') or code}). Run tares login again.")
    data = {"cloud": cloud, "cell": out.get("cell", ""), "cell_url": out["cell_url"].rstrip("/"),
            "key": out["token"], "key_id": out.get("key_id", ""), "machine": machine,
            "signed_in_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # The control plane checked the version when it offered the workspace; check what the
    # workspace itself says now, before saving a key for it.
    unreachable = False
    try:
        status, health = _http("GET", f"{data['cell_url']}/health", timeout=10)
        version = str(health.get("version") or "") if status == 200 else ""
    except (httpx.HTTPError, httpx.InvalidURL):
        version = ""
        unreachable = True
    if (_version(version) or (0, 0, 0)) < MIN_VERSION:
        removed, _how = _revoke(data)
        return _err(json_out, "error" if unreachable else "cell_too_old",
                    f"tares login: {data['cell']} runs Tares {version or 'of an unknown version'}; "
                    f"tares login needs {MIN_VERSION_TEXT} or later. Nothing was saved."
                    + ("" if removed else f" {_left_behind(data)}"))

    try:
        where = _save(data)
    except (OSError, CrewError) as e:
        removed, _how = _revoke(data)
        return _err(json_out, "error", f"tares login: could not save the login ({e}). "
                    + ("The key just made for this machine was removed, and the saved login (if any) is as it was."
                       if removed else f"The saved login (if any) is as it was. {_left_behind(data)}"))
    _crew_follows(data["key"], json_out=json_out)
    if old and old.get("key_id") != data["key_id"]:
        ok, _how = _revoke(old)
        if not ok:
            print(f"tares login: the previous key on {old.get('cell')} could not be removed; "
                  f"remove it under Security > API keys on {old.get('cell_url')}.",
                  file=sys.stderr if json_out else sys.stdout)
    if old and _account(old) != _account(data):
        _kc_delete(_account(old))        # the previous workspace's key leaves the keychain, after it is revoked
    if json_out:
        contract.emit({"login": {"step": "done", "cell": data["cell"], "cell_url": data["cell_url"],
                                 "machine": machine,
                                 "key_store": "file" if where == str(login_file()) else "keychain"}}, "login")
        return 0
    print(f"tares login: this machine ({machine}) is connected to {data['cell']} "
          f"({data['cell_url']}), Tares {version}.")
    if where == str(login_file()):
        if not _gate_on():
            reason = "the OS keychain is off (set TARES_KEYRING=on to use it)"
        elif _keyring() is None:
            reason = "no OS keychain works on this machine"
        else:
            reason = "the OS keychain would not take it"
        print(f"  The key is saved in {where}, readable only by you, because {reason}.")
    else:
        print(f"  The key is saved in {where}.")
    print(f"  tares factory and tares status use it now. {mcp_setting(data['cell_url'])}")
    return 0


def cell_address(address: str) -> tuple[str, str]:
    """(url, problem) for a cell address typed after --cell: https://, or http:// only for localhost and
    127.0.0.1; a trailing slash is dropped. problem is plain words, empty when the address is fine."""
    from urllib.parse import urlsplit
    text = (address or "").strip()
    try:
        u = urlsplit(text)
        host, _port = u.hostname, u.port
    except ValueError:
        return "", f"{text!r} is not an address"
    if u.scheme not in ("http", "https") or not host:
        return "", f"{text!r} is not an address: it must start with https://"
    if u.scheme == "http" and host not in ("localhost", "127.0.0.1"):
        return "", f"{text!r} is not safe: http:// is only for localhost and 127.0.0.1, use https://"
    if u.username is not None or u.password is not None or u.query or u.fragment:
        return "", f"{text!r} must be a plain address, without a user, a query or a fragment"
    return text.rstrip("/"), ""


def connect_cell(address: str, key: str, json_out: bool = False) -> int:
    """`tares login --cell <address> --key-stdin`: save a sign-in to a cell that is not Tares Cloud, with a
    key the person made (the Security page shows it once). address comes from cell_address()."""
    try:
        return _connect_cell(address, key, json_out)
    except OSError as e:
        if not json_out:
            raise
        return _err(True, "error", f"tares login: {type(e).__name__}: {e}")


def _connect_cell(address: str, key: str, json_out: bool) -> int:
    import httpx
    old_data, old_key, why = _load()
    if old_data is not None and not old_key and why:
        return _err(json_out, "keychain_unreadable",
                    _unreadable(why, old_data, "tares login") + f" Or, to start over, delete {login_file()} "
                    "and remove that key yourself under Security > API keys.")
    old = {**old_data, "key": old_key} if old_data is not None and old_key else None
    try:
        status, health = _http("GET", f"{address}/health", timeout=10)
        status2, detail = (_http("GET", f"{address}/api/crew/settings", token=key, timeout=10)
                           if status == 200 else (0, {}))
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        return _err(json_out, "cell_unreachable",
                    f"tares login: could not reach {address} ({e}). Nothing was saved.")
    if status != 200:
        return _err(json_out, "cell_unreachable",
                    f"tares login: {address} answered {status} at /health, so it does not look like a Tares. "
                    "Nothing was saved.")
    version = str(health.get("version") or "")
    if (_version(version) or (0, 0, 0)) < MIN_VERSION:
        return _err(json_out, "cell_too_old",
                    f"tares login: {address} runs Tares {version or 'of an unknown version'}; "
                    f"tares login needs {MIN_VERSION_TEXT} or later. Nothing was saved.")
    if status2 == 403 and "admin" in str(detail.get("detail") or ""):
        return _err(json_out, "key_lacks_admin",
                    f"tares login: {address} accepted the key but it lacks the admin scope, which "
                    "tares factory needs. Make a key with the admin scope under Security > API keys. "
                    "Nothing was saved.")
    if status2 in (401, 403):
        return _err(json_out, "key_refused",
                    f"tares login: {address} refused the key ({status2}). Check it, or make a new one under "
                    "Security > API keys. Nothing was saved.")
    if not 200 <= status2 < 300:
        return _err(json_out, "cell_unreachable",
                    f"tares login: {address} answered {status2} when the key was checked. Nothing was saved.")
    from urllib.parse import urlsplit
    machine = _machine()
    data = {"cloud": None, "cell": urlsplit(address).hostname, "cell_url": address, "key": key, "key_id": None,
            "machine": machine, "signed_in_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        where = _save(data)
    except (OSError, CrewError) as e:
        return _err(json_out, "error", f"tares login: could not save the login ({e}). "
                    "The saved login (if any) is as it was.")
    _crew_follows(key, json_out=json_out)
    if old and old.get("cloud"):
        ok, _how = _revoke(old)
        if not ok:
            print(f"tares login: the previous key on {old.get('cell')} could not be removed; "
                  f"remove it under Security > API keys on {old.get('cell_url')}.",
                  file=sys.stderr if json_out else sys.stdout)
    if old and _account(old) != _account(data):
        _kc_delete(_account(old))
    store = "file" if where == str(login_file()) else "keychain"
    if json_out:
        contract.emit({"login": {"step": "done", "cell": data["cell"], "cell_url": address, "machine": machine,
                                 "key_store": store}}, "login")
        return 0
    print(f"tares login: this machine is connected to {address}")
    return 0


def show(json_out: bool = False) -> int:
    """`tares login --show`: what is saved, and whether its key still works. Never prints the key."""
    import httpx
    data, key, why = _load()
    if data is None:
        if json_out:
            contract.emit({"login": {"signed_in": False}}, "login")
        else:
            print("tares login: this machine is not signed in. Run tares login.")
        return 0
    _url, _tok, source = resolve()
    works = None
    if key:
        try:
            status, _ = _http("GET", f"{_account(data)}/api/crew/settings", token=key, timeout=10)
            works = True if 200 <= status < 300 else False if status in (401, 403) else None
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            works = None
    info = {"signed_in": True, "source": source, "cloud": data.get("cloud") or None, "cell": data.get("cell") or "",
            "cell_url": _account(data), "machine": data.get("machine") or "",
            "signed_in_at": data.get("signed_in_at") or "", "key_id": data.get("key_id") or "",
            "key_store": _key_store(data), "key_works": works}
    if json_out:
        contract.emit({"login": info}, "login")
        return 0
    print(f"tares login: signed in to {info['cell'] or info['cell_url']} ({info['cell_url']}) "
          f"as machine {info['machine']}.")
    print(f"  Signed in at {info['signed_in_at']}; key {info['key_id'] or '(unnamed)'} kept in "
          f"{'the OS keychain' if info['key_store'] == 'keychain' else login_file()}.")
    print(f"  Tares commands find it as: {source}.")
    print("  The key " + {True: "works on the workspace.", False: "was refused by the workspace: run tares login again.",
                          None: "could not be checked" + (" (it cannot be read here)." if not key
                                                          else " (the workspace did not answer clearly).")}[works])
    return 0


def logout(json_out: bool = False) -> int:
    data, key, why = _load()
    if data is not None and not key and why:
        # The login is there but its key cannot be read: deleting it would lose the last record of a live key.
        return _err(json_out, "keychain_unreadable",
                    _unreadable(why, data, "tares logout") + f" Or, to start over, delete {login_file()} "
                    "and remove that key yourself under Security > API keys.")
    try:
        return _logout(data, key, json_out)
    except OSError as e:
        if not json_out:
            raise
        return _err(True, "error", f"tares logout: {type(e).__name__}: {e}")


def _logout(data, key: str, json_out: bool) -> int:
    if not key:
        _forget()
        if json_out:
            contract.emit({"logout": {"was_signed_in": False, "key_removed": False}}, "logout")
        else:
            print("tares logout: this machine is not signed in.")
        return 0
    data = {**data, "key": key}
    if not data.get("cloud"):
        # connected by address and key: the person made that key, so it stays on the cell
        _forget()
        crew = _crew_follows("", data["key"], json_out=json_out)
        if json_out:
            contract.emit({"logout": {"was_signed_in": True, "key_removed": False}}, "logout")
            return 0
        print(f"tares logout: the saved login is deleted. The key was not made by this machine, so it is still "
              f"valid on {data.get('cell')}: remove it under Security > API keys on {data.get('cell_url')} "
              f"if you no longer need it.")
        if crew:
            print("  The factory crew on this machine stops reaching Tares until you sign in again "
                  "with tares login.")
        return 0
    ok, _how = _revoke(data)
    _forget()
    crew = _crew_follows("", data["key"], json_out=json_out)
    if json_out:
        if ok:
            contract.emit({"logout": {"was_signed_in": True, "key_removed": True}}, "logout")
            return 0
        return _err(True, "logout_failed",
                    f"tares logout: the saved login is deleted, but the key was not removed on "
                    f"{data.get('cell')} (key {data.get('key_id') or 'unknown'}): remove it under Security > "
                    f"API keys on {data.get('cell_url')} (named cli: {data.get('machine')}).")
    if ok:
        print(f"tares logout: this machine's key on {data.get('cell')} is removed, "
              f"and the saved login is deleted.")
    else:
        print(f"tares logout: the saved login is deleted, but the key is still on {data.get('cell')}: "
              f"remove it under Security > API keys on {data.get('cell_url')} "
              f"(named cli: {data.get('machine')}).")
    if crew:
        print("  The factory crew on this machine stops reaching Tares until you sign in again "
              "with tares login.")
    return 0 if ok else 1
