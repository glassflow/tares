"""The crew engine's client for Tares: the same requests, the same addresses and the same error texts as
the bash `claude-plugin/bin/factory` (tares_http, tares_url, mcp_url, tares_on, key_ok, crew_projects,
crew_settings, grants_read, desk_json and the rest). Where the two differ, bash is right.

Standard library plus httpx (sync) only. The key goes to Tares in an Authorization header held in memory:
never on a command line, never in a log line or an error text. The key comes from the same places as in
bash: TARES_AUTH_TOKEN for the person's own terminal, the crew's key file for a station (never the OS
keychain: stations read a file).

Time limits: bash's curl `--max-time N` caps the whole request. httpx has no total limit, only one per step
(connect, write, each read), so a request here is also stopped at N seconds in total while its body comes
in. The one step that can still run past N is waiting for the first byte of the answer: it can take N
seconds to connect, N to write and N to read the headers (3N at worst), where curl gives up at N.

A `TaresClient` is one command's view of Tares: like bash, it asks once whether Tares answers, and keeps
the project list and the settings it read, so later calls reuse them.
"""
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from . import state
from .errors import CrewError, NotSignedInError
from .paths import ME, CrewPaths

# Settings the crew asks about, in the order bash asks them (SETTING_KEYS).
SETTING_KEYS = "autonomy releaser release_profile prod_pattern challenger builders_max"

MCP_INITIALIZE = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26",'
                  '"capabilities":{},"clientInfo":{"name":"tares-factory","version":"0"}}}')


class _Answer:
    """What a request got: the status and the whole body (all a caller reads of it)."""

    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class TaresHTTPError(CrewError):
    """A call to Tares that did not give a 2xx/3xx answer. The text is what curl prints with `-sS -f`
    ("curl: (22) The requested URL returned error: 404"), because bash callers read it: grants_read looks
    for `error: 404`, and crew_projects shows the last line. `status` is the HTTP status, None when no
    answer came at all."""

    code = "tares_error"

    def __init__(self, text: str, status=None):
        super().__init__(text)
        self.status = status


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr)


# ---------- pure helpers ----------


def uri(text: str) -> str:
    """`jq @uri`: every byte but A-Z a-z 0-9 - _ . ~ as %XX (UTF-8, upper-case hex)."""
    return quote(text, safe="")


def key_name(key: str) -> str:
    """A key's id (nvf_<id>_<secret>), safe to print; never the key."""
    if key == "":
        return "no key"
    if key.startswith("nvf_") and "_" in key[4:]:
        return f"key {key[4:].split('_', 1)[0]}"
    return "a token with no key id"


def _crc_table():
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
            c &= 0xFFFFFFFF
        table.append(c)
    return table


_CRC = _crc_table()


def cksum(data: bytes) -> int:
    """POSIX `cksum`'s checksum (the first number it prints): CRC-32 with polynomial 0x04C11DB7, most
    significant bit first, the length folded in, then complemented. Not zlib.crc32."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    n = len(data)
    while n:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ (n & 0xFF)) & 0xFF]
        n >>= 8
    return ~crc & 0xFFFFFFFF


def grants_sum(text) -> int:
    """`grants_sum`: the fingerprint of a grants text (`cksum | cut -d' ' -f1`)."""
    return cksum(text if isinstance(text, bytes) else text.encode("utf-8"))


def settings_defaults() -> dict:
    """The crew settings Tares has not been given yet, in bash's order."""
    return {"autonomy": "L4-ship", "releaser": False, "release_profile": "kind",
            "prod_pattern": "(^|[-_.])(prod|production)([-_.]|$)", "challenger": False, "builders_max": 2,
            "kit": {"frozen": None}, "idle_wake": False, "pace": False, "claude_update": True}


def settings_missing(raw: dict) -> list:
    """The setting keys still to ask: unset, or an autonomy that is neither L3-review nor L4-ship. The
    release ones only matter with a releaser; the prod pattern comes with the release profile, unasked."""
    out = []
    for k in SETTING_KEYS.split():
        v = raw.get(k)
        if not (v is None or (k == "autonomy" and v not in ("L3-review", "L4-ship"))):
            continue
        if k in ("release_profile", "prod_pattern") and raw.get("releaser") is not True:
            continue
        if k == "prod_pattern":
            continue
        out.append(k)
    return out


def _tostring(value) -> str:
    """jq's tostring: a string as is, anything else as compact JSON."""
    return value if isinstance(value, str) else state.dumps_compact(value)


def _curl_text(exc: Exception, url: str, seconds: float) -> str:
    """What curl would say when no answer came (best effort: curl's wording varies by version)."""
    u = httpx.URL(url)
    host = u.host
    port = u.port or (443 if u.scheme == "https" else 80)
    if isinstance(exc, httpx.TimeoutException):
        return f"curl: (28) Operation timed out after {int(seconds * 1000)} milliseconds with 0 bytes received"
    if isinstance(exc, httpx.ConnectError):
        if isinstance(exc.__cause__, socket.gaierror):
            return f"curl: (6) Could not resolve host: {host}"
        return f"curl: (7) Failed to connect to {host} port {port}: Couldn't connect to server"
    return f"curl: (56) Failure when receiving data from the peer ({type(exc).__name__})"


# ---------- the client ----------


class TaresClient:
    def __init__(self, paths: CrewPaths | None = None, env=None, say=None):
        self.paths = paths or CrewPaths.from_env()
        self.env = os.environ if env is None else env
        self.say = say or _say
        self.tares = ""            # TARES: set by tares_on (or on first use)
        self.tares_state = ""      # TARES_STATE: "on", "off" or "" (not asked yet)
        self.mcp_state = ""        # MCP_STATE
        self._caps = None          # CAPS: what /health lists, read once per command
        self._projects = None      # CREW_PROJECTS
        self.projects_err = ""     # CREW_PROJECTS_ERR
        self._settings = None      # SETTINGS
        self._http = None

    # -- plumbing --

    def _e(self, name: str) -> str:
        """An environment variable the way `${VAR:-}` reads it: unset and empty are the same."""
        return self.env.get(name) or ""

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _client(self) -> httpx.Client:
        if self._http is None:
            # No redirects (curl has no -L), so a key is never carried to another address.
            self._http = httpx.Client(follow_redirects=False)
        return self._http

    def _log_url(self, url: str) -> None:
        """Test hook TARES_URL_LOG: a file each request's URL is appended to, one per line, as the suites' curl shim
        does for bash (a Python request never goes through curl). Off when unset; a write that fails is ignored."""
        path = self._e("TARES_URL_LOG")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(url + "\n")
        except OSError:
            pass

    def _request(self, method: str, url: str, headers: dict, content, seconds: float):
        """One request: the response, or TaresHTTPError with curl's text for a transport failure. A status
        of 400 or more is left to the caller (curl's -f). `seconds` limits each step and, while the body
        comes in, the whole request (see the module docstring)."""
        deadline = time.monotonic() + seconds
        self._log_url(url)
        try:
            with self._client().stream(method, url, headers=headers, content=content,
                                       timeout=httpx.Timeout(seconds)) as r:
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if time.monotonic() > deadline:
                        raise TaresHTTPError(f"curl: (28) Operation timed out after {int(seconds * 1000)} "
                                             f"milliseconds with {len(body)} bytes received")
                return _Answer(r.status_code, bytes(body))
        except httpx.HTTPError as e:
            raise TaresHTTPError(_curl_text(e, url, seconds)) from None
        except httpx.InvalidURL:
            raise TaresHTTPError("curl: (3) URL rejected: Malformed input to a URL function") from None

    # -- the session and the key --

    def in_crew_session(self) -> bool:
        """A session the factory started (a station, or the person's spec or build session) reads only the
        crew file; the person's own terminal uses the key `tares factory` gave it."""
        return (self._e("FACTORY_STATION") + self._e("TARES_FACTORY_TOKEN_FILE")) != ""

    def crew_key(self) -> str:
        return state.crew_key(self.paths, self.env)

    def tares_token(self) -> str:
        if not self.in_crew_session() and self._e("TARES_AUTH_TOKEN"):
            return self._e("TARES_AUTH_TOKEN")
        return self.crew_key()

    def no_key_hint(self) -> str:
        """What to add to "Tares refused" when no key was sent."""
        if self.tares_token():
            return ""
        if self.in_crew_session():
            return "; the crew has no key because the person signed out of Tares: they run tares login, and the crew picks it up"
        return "; if it has auth on, run tares login (or set TARES_AUTH_TOKEN for a Tares of your own)"

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"} if token else {}

    # -- addresses --

    def tares_url(self) -> str:
        """TARES_FACTORY_URL (what stations are given), else TARESD_URL, else TARES_URL, else crew.json's
        tares.url, else the local default; one trailing slash dropped."""
        u = self._e("TARES_FACTORY_URL") or self._e("TARESD_URL") or self._e("TARES_URL")
        if not u:
            u = state.ccfg("tares", "url", paths=self.paths)
        u = u or "http://127.0.0.1:8787"
        return u[:-1] if u.endswith("/") else u

    def mcp_url(self) -> str:
        """Where stations reach Tares's MCP server. Locally it is its own process on its own port; Tares
        Cloud serves /mcp on the cell's own host. TARES_FACTORY_MCP_URL overrides both."""
        override = self._e("TARES_FACTORY_MCP_URL")
        if override:
            return override[:-1] if override.endswith("/") else override
        u = self.tares_url()
        rest = u.split("://", 1)[1] if "://" in u else u
        host = re.split(r"[:/]", rest, maxsplit=1)[0]
        if host in ("127.0.0.1", "localhost"):
            return f"http://{self._e('TARES_MCP_HOST') or '127.0.0.1'}:{self._e('TARES_MCP_PORT') or '8788'}/mcp"
        return f"{u}/mcp"

    # -- is Tares there --

    def tares_on(self) -> bool:
        """Does Tares answer /health (asked once, no key sent)?"""
        if not self.tares_state:
            self.tares = self.tares_url()
            self.tares_state = "off"
            try:
                if self._request("GET", f"{self.tares}/health", {}, None, 5).status_code < 400:
                    self.tares_state = "on"
            except TaresHTTPError:
                pass
        return self.tares_state == "on"

    def require_tares(self) -> None:
        if not self.tares_on():
            cls = NotSignedInError if self._not_configured() else CrewError
            raise cls(f"no Tares answers at {self.tares}. Start one with `tares up` in another terminal, "
                      "connect to your Tares Cloud workspace with `tares login` (or set TARESD_URL to where "
                      "yours runs), then try again.")

    def _not_configured(self) -> bool:
        """True when no Tares was ever named: no saved login, no address in the environment or crew.json.
        Inside `tares factory` the address is always set, so its source says "local default" then."""
        if state.ccfg("tares", "url", paths=self.paths):
            return False
        named = self._e("TARES_FACTORY_URL") or self._e("TARESD_URL") or self._e("TARES_URL")
        if named and self._e("TARES_FACTORY_SOURCE") != "local default":
            return False
        home = Path(self._e("TARES_HOME") or Path.home() / ".tares")
        try:
            data = json.loads((home / "login.json").read_text())
        except (OSError, ValueError):
            return True
        return not (isinstance(data, dict) and data.get("cell_url"))

    def need_capability(self, name: str) -> None:
        """Stop with an upgrade message when the cell's /health does not list the capability. /health is
        read once per command and kept. Only commands that start or change something call it: `crew status`
        and `crew down` never do, and no later ticket may add it to them, because a version mismatch must
        never stop someone seeing or stopping their crew."""
        self.require_tares()
        if self._caps is None:
            self._caps = []
            try:
                r = self._request("GET", f"{self.tares}/health", {}, None, 5)
                if r.status_code < 400:
                    caps = json.loads(r.text).get("capabilities") or []
                    self._caps = [c for c in caps if isinstance(c, str)]
            except (TaresHTTPError, ValueError, AttributeError, TypeError):
                pass
        if name not in self._caps:
            raise CrewError(f"This needs a newer Tares on the cell (missing {name}). Upgrade Tares, then run this again.")

    def mcp_on(self) -> bool:
        """Does an MCP server answer `initialize` at mcp_url (asked once)? Only an MCP server's answer
        counts: taresd's 404 or a web page does not."""
        if not self.mcp_state:
            self.mcp_state = "off"
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       **self._auth(self.tares_token())}
            try:
                r = self._request("POST", self.mcp_url(), headers, MCP_INITIALIZE.encode(), 5)
                if r.status_code < 400 and '"serverInfo"' in r.text:
                    self.mcp_state = "on"
            except TaresHTTPError:
                pass
        return self.mcp_state == "on"

    def require_mcp(self) -> None:
        if self.mcp_on():
            return
        how = "tares mcp --transport streamable-http"
        if self.tares_token():
            how = f"TARES_AUTH_TOKEN=<your token> {how}"
        raise CrewError(f"no MCP server answers at {self.mcp_url()}. Stations reach Tares through it. "
                        f"Start one in another terminal: {how} (or set TARES_FACTORY_MCP_URL to where yours runs).")

    # -- calls --

    def tares_http(self, method: str, path: str, body=None, headers: dict | None = None) -> str:
        """A call to Tares; the answer's body. `body` is sent as is when it is text or bytes, else as
        compact JSON. The key goes in the Authorization header, from memory. A status of 400 or more, or no
        answer, raises TaresHTTPError with curl's text. 15 seconds at most."""
        if not self.tares:
            self.tares = self.tares_url()
        headers = {"Accept": "application/json", **self._auth(self.tares_token()), **(headers or {})}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = body if isinstance(body, bytes) else (
                body if isinstance(body, str) else state.dumps_compact(body)).encode("utf-8")
        r = self._request(method, f"{self.tares}{path}", headers, content, 15)
        if r.status_code >= 400:
            raise TaresHTTPError(f"curl: (22) The requested URL returned error: {r.status_code}", r.status_code)
        return r.content.decode("utf-8", "replace")

    def key_ok(self, key: str) -> bool:
        """Does this Tares let a key in? (no key: does it let anyone in)"""
        if not self.tares:
            self.tares = self.tares_url()
        try:
            return self._request("GET", f"{self.tares}/api/crew/settings", self._auth(key), None, 10).status_code < 400
        except TaresHTTPError:
            return False

    def keep_token(self) -> None:
        """The person's key, copied for the stations into a file only they can read. Never a session the
        factory started, and only with a key this Tares accepts, so a stale key can't replace a good one."""
        key = self._e("TARES_AUTH_TOKEN")
        if self.in_crew_session() or not key or not self.paths.home.is_dir():
            return
        try:
            if state.read_text(self.paths.token_file).rstrip("\n") == key:
                return
        except OSError:
            pass
        if not self.tares_on():
            return
        if not self.key_ok(key):
            self.say(f"warning: Tares at {self.tares} refuses {key_name(key)} from the "
                     f"{self._e('TARES_FACTORY_KEY_SOURCE') or 'environment'}; the crew keeps the key it has")
            return
        state.write_token(key, self.paths)

    # -- the crew's projects --

    def crew_projects(self) -> list:
        """The projects handed to the crew, as Tares lists them (cached), each repo with the folder this
        crew handed over (`crew`). When Tares can't say, an empty list, and `projects_err` holds why."""
        if self._projects is None:
            try:
                out = self.tares_http("GET", f"/api/crew/projects?crew={quote(state.crew_name(self.paths), safe='')}")
            except TaresHTTPError as e:
                self._projects, self.projects_err = [], (str(e).splitlines() or [""])[-1]
                return self._projects
            try:
                doc = state.loads_json(out)
                if not isinstance(doc, dict):
                    raise ValueError
            except ValueError:
                self._projects, self.projects_err = [], (out.rstrip("\n").splitlines() or [""])[-1]
                return self._projects
            found = doc.get("projects")
            self._projects = [] if found is None or found is False else found
            self.projects_err = ""
        return self._projects

    def crew_projects_or_die(self) -> list:
        """The same, but a CrewError when Tares could not give the list, so a refusal is never read as
        "no such project"."""
        projects = self.crew_projects()
        if self.projects_err:
            raise CrewError(f"Tares at {self.tares} did not give the crew's projects ({self.projects_err}){self.no_key_hint()}")
        return projects

    def project_row(self, name: str):
        """The first project whose name or id is `name`, or whose name has the same slug; None if none."""
        slug = state.slugify(name)
        for p in self.crew_projects():
            pname = p.get("name")
            if pname == name or p.get("id") == name or (isinstance(pname, str) and state.slugify(pname) == slug):
                return p
        return None

    def project_repo_rows(self) -> list:
        """(project name, repo path) for every repo that has a path (a repo that is not an object has none)."""
        return [(p.get("name"), r["path"]) for p in self.crew_projects()
                for r in (p.get("repos") or []) if isinstance(r, dict) and r.get("path") is not None]

    def all_repos(self) -> list:
        """Every repo path of every project, sorted, once each."""
        return sorted({r["path"] for p in self.crew_projects()
                       for r in (p.get("repos") or []) if isinstance(r, dict) and r.get("path") is not None})

    # -- crew settings and grants --

    def settings_raw(self) -> dict:
        try:
            doc = state.loads_json(self.tares_http("GET", "/api/crew/settings"))
        except (TaresHTTPError, ValueError):
            raise CrewError(f"could not read the crew settings from Tares at {self.tares}{self.no_key_hint()}") from None
        if not isinstance(doc, dict):
            raise CrewError(f"could not read the crew settings from Tares at {self.tares}{self.no_key_hint()}")
        found = doc.get("settings")
        return doc if found is None or found is False else found

    def crew_settings(self) -> dict:
        """The settings with defaults for anything unset (cached). Raises the CrewError of settings_raw
        when they can't be read; bash prints that line and fails without dying a second time."""
        if self._settings is None:
            raw = self.settings_raw()
            self._settings = {k: (v if raw.get(k) is None else raw[k]) for k, v in settings_defaults().items()}
        return self._settings

    def setting(self, key: str) -> str:
        """One setting as bash prints it (`jq -r '.[$k] | tostring'`): true, 2, L4-ship, null."""
        try:
            s = self.crew_settings()
        except CrewError as e:
            self.say(e.message)
            raise CrewError("no crew settings to work from") from None
        return _tostring(s.get(key))

    def releaser_on(self) -> bool:
        return self.setting("releaser") == "true"

    def crew_roles(self) -> list:
        return ["orchestrator", "reviewer"] + (["releaser"] if self.releaser_on() else [])

    def grants_read(self, project: str = "") -> str:
        """Markdown of the grants for all projects, then the project's lines each led by `@project `, as
        bash prints them (every line ends with a newline). Raises TaresHTTPError when Tares can't give
        them. A project Tares doesn't know answers 404; the grants for all projects still apply."""
        q = f"?project={uri(project)}" if project else ""
        try:
            g = self.tares_http("GET", f"/api/grants{q}")
        except TaresHTTPError as e:
            if not (q and "error: 404" in str(e)):
                raise
            g = self.tares_http("GET", "/api/grants")
        try:
            doc = state.loads_json(g)
            if not isinstance(doc, dict):
                raise ValueError
        except ValueError:
            raise TaresHTTPError("jq: error: the grants are not JSON") from None

        def field(k):
            v = doc.get(k)
            return "" if v is None or v is False else state.jq_raw(v)
        out = field("all") + "\n"
        if project:
            out += "".join(f"@project {line}\n" for line in (field("project") + "\n").split("\n")[:-1])
        return out

    def grants_md(self, project: str = "") -> str:
        """The same, or nothing (said) when Tares can't give them."""
        try:
            return self.grants_read(project)
        except TaresHTTPError:
            self.say("could not read grants from Tares")
            return ""

    # -- the desk --

    def desk_json(self) -> list:
        """The open desk items from Tares; an empty list when Tares can't say."""
        try:
            doc = state.loads_json(self.tares_http("GET", "/api/desk?state=open"))
        except (TaresHTTPError, ValueError):
            return []
        found = doc.get("desk") if isinstance(doc, dict) else None
        return [] if found is None or found is False else found

    # -- once per command --

    def prime(self) -> None:
        """Decide once where Tares is and what the crew settings are, so later calls reuse the answer."""
        state.merge_crew_defaults(self.paths)
        self.require_tares()
        self.keep_token()
        if self.paths.crew_cfg.is_file():
            try:
                self.crew_settings()
            except CrewError as e:
                self.say(e.message)


def cmd_mcp_url(argv: list, env: dict) -> int:
    """The hidden `_mcp-url`: print where stations reach Tares's MCP server (bash's `mcp_url`, which
    tests and scripts call). Like bash it checks no crew name, merges the crew.json defaults and prints one
    line. Bash also asks whether Tares answers, and reads the key; neither changes the line, so
    this does not: it works with Tares down and sends no request."""
    from .paths import default_home
    paths = CrewPaths(env.get("FACTORY_CREW_HOME") or default_home(env))
    state.merge_crew_defaults(paths)
    print(TaresClient(paths, env).mcp_url())
    return 0
